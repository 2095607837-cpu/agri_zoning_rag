# Knowledge Bridge 方案：语义桥接增强检索

## 问题诊断回顾

22 个 query_rewrite 的 query 中，21 个的 rewrite keywords 能**精确命中** gold chunk 文本，但 embedding 检索仍然无法召回。根因不是 rewrite 不够好，而是 **embedding 模型无法关联"问句"和"技术定义/数值表"两种语言分布**。

## 核心设计理念

- **原始 chunk 检索是主通道**，权重最高，保证存量效果不回退
- **Knowledge 作为独立的并行检索通道**，有自己的索引空间和评分体系
- **在候选池层面融合**，knowledge 通道可以：① 引入 primary 漏掉的 chunk；② 提升 primary 已找到但排名低的 chunk
- **不改 primary 的 embedding 索引**（790 个原始 chunk 的 Chroma collection 保持不动）

## 架构

```
                         User Query
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         Rewriter      Primary通道     Knowledge通道
         (不变)         (不变)         (新增)
              │              │              │
    ┌─────────┼──────┐       │       ┌──────┼──────┐
    ▼         ▼      ▼       │       ▼      ▼      ▼
 keywords rewrite sub_q      │    UE索引  DQ索引  Term索引
    │         │      │       │    (4.7K)  (2.2K)  (2.3K)
    │         │      │       │       │      │      │
    └─────────┼──────┘       │       └──────┼──────┘
              │              │              │
              ▼              ▼              ▼
         Keyword BM25   Dense+BM25    UE embed搜
                        → RRF融合     DQ embed搜
                        候选池         Term精确匹配
              │              │              │
              └──────────────┼──────────────┘
                             ▼
                    候选池合并 (chunk_id去重)
                    多通道 Evidence Voting
                    Dynamic Coverage 配额分配
                             │
                             ▼
                      CE Rerank → Top-K
```

## 三层 Knowledge 索引

### 索引 1：User Expression 向量库（最关键）

**原理**：user_expression 和真实 query 都是"用户问句"，在同一嵌入子空间。query 先 match 到 user_expression，再确定性路由到 chunk。

**构建**：
- 从 chunk_knowledge.json 提取所有 `user_expressions`（~4,721 条）
- 每个 expression 作为独立 document 写入 Chroma collection `agri_zoning_ue`
- metadata 记录 `source_chunk_id`、`intent_type`
- 同 chunk 的多个 expression 都指向同一个 chunk，多路命中天然形成 evidence

**检索**：
```
query → embed → Chroma(4,721 expressions) → top-30 expressions
→ 按 source_chunk_id 聚合 → 同一 chunk 被多个 expression 命中则加分
→ 产出 candidate list (chunk_id + ue_score)
```

### 索引 2：Dense Query 向量库

**原理**：`_retrieval.dense_queries` 是 LLM 生成的检索友好短句，介于 query 语言和 chunk 语言之间。

**构建**：
- 提取所有 `dense_queries`（~2,182 条）+ `semantic_summary`（~757 条）
- 写入 Chroma collection `agri_zoning_dq`
- metadata 记录 `source_chunk_id`

**检索**：
```
query → embed → Chroma(2,939 documents) → top-20
→ 按 source_chunk_id 聚合
→ 产出 candidate list (chunk_id + dq_score)
```

### 索引 3：Term 倒排索引

**原理**：`technical_terms` 和 `bm25_terms` 是 chunk 中实际出现的术语，精确匹配不受 embedding 分布影响。

**构建**：
- `{term: [chunk_id, ...]}` 内存 dict
- 2,295 unique technical_terms + 3,705 bm25_terms

**检索**：
```
rewrite keywords → 逐词查倒排索引 → 命中的 chunk 直接入选
→ 产出 candidate list (chunk_id + 1.0 精确分)
```

## 候选池合并策略

```
候选池 P = P_primary ∪ P_ue ∪ P_dq ∪ P_term

对每个候选 chunk：
  base_score = 0.5 × primary_rrf_score    # 主通道权重
             + 0.25 × ue_score            # UE 权重（query语言匹配）
             + 0.15 × dq_score            # DQ 权重（桥接语言匹配）
             + 0.10 × term_match_score    # Term 权重（精确术语匹配）

  evidence = count(通道命中数)  # 多通道命中 = 高置信度
  final = 0.7 × base_score_norm + 0.3 × evidence_norm
```

## 实现计划

### 新增文件：`knowledge_bridge.py`

**类 `KnowledgeBridge`**：
- `__init__()`: 加载 chunk_knowledge.json → 构建 3 个索引
- `build_ue_index()` → Chroma collection（首次运行 embed 4,721 条，后续持久化）
- `build_dq_index()` → Chroma collection（首次运行 embed 2,939 条）
- `build_term_index()` → Python dict
- `search(query, keywords, top_k)` → 三通道检索 → 合并 → 返回 `[{chunk_id, score, channel, ...}]`

### 修改文件：`hybrid_search.py`

改动集中在 `search_multi_query` 方法：

1. **模块级初始化** `KnowledgeBridge` 单例（与 Reranker 同样的单例模式）
2. **Phase 0（新增）**：调用 `bridge.search(query, kw_list)` → 获取 knowledge candidates
3. **注入候选池**：将 knowledge candidates 以独立 qid 注入 `candidates` dict
4. **Dynamic Coverage**：给 Knowledge 通道分配配额（如 10/50 = 20%）

### 修改文件：`eval_v2_full.py`（评测适配）

- 传入 `keyword_queries` 参数以触发 knowledge bridge

## 预期效果

| 指标 | 当前 | 目标 | 原理 |
|------|------|------|------|
| query_rewrite cap MRR | 0.362 | > 0.50 | UE 索引直接匹配 question→question |
| query_rewrite cap R10 | 56% | > 75% | Term 索引精确命中 + UE 语义桥接 |
| query_rewrite zero@10 | 11/25 | < 5/25 | 被 primary 漏掉的 chunk 通过 knowledge 通道补回 |
| 整体 MRR | 0.570 | > 0.60 | 不影响存量，纯增量提升 |
| 整体 zero@10 | 39 | < 25 | knowledge 补回 primary 漏掉的 |

## 不做什么

- 不重新 embed 原始 791 个 chunk（primary 索引不动）
- 不修改 Rewriter
- 不修改 CE Reranker
- 不修改 OOD Judge
- Knowledge 通道的 embedding 模型复用现有的 `BAAI/bge-small-zh-v1.5`

## 风险与回滚

- **风险**：UE 索引把不相关的 chunk 拉进来（假阳性）。**缓解**：UE 只提供候选，最终由 CE Reranker 把关
- **回滚**：Knowledge Bridge 是可插拔模块，一个 flag 即可关闭
