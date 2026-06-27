# 农业气候区划 RAG — Chunk Pipeline 技术文档

> 完整记录从原始文档到检索结果的全链路 chunk 处理逻辑
>
> **最后更新**: 2026-06-26 (v1.8 compact header 修复)

---

## 总览

```
源文件 (.docx/.pdf/.xlsx/.csv)
    │
    ▼ step1_parse.py   — 三层切分 (样式标题 → 正则降级 → 固定长度兜底)
chunks.json            — 每个 chunk 含 heading_path / section_id
    │
    ▼ step2_embed.py   — H3回退切分 → RecursiveCharacterTextSplitter(800/150)
    │                    page_content = heading_path + 正文
    │                    按 section_id 分配 chunk_index / chunk_count
    │
    ├─ BM25 索引 ───── 从 vectordb 构建（与 Dense 同一套子块）
    │
    ▼
vectordb/              — 1053 条向量，含 heading_path 语义信号
    │
    ▼ hybrid_search.py — RRF 融合 → [Reranker] → 同 section 上下文扩展(±1)
    │
    ▼
top_k 结果 → Judge → Generate
```

---

## 核心改动总览

| 维度 | 旧方案 | 新方案 |
|------|--------|--------|
| **DOCX 切分** | 遇 Heading 就切，H1/H2/H3 平级 | H1 文档标题，H2 章节边界，H3+ 内联正文 |
| **层级信息** | 仅 section_title，父子关系丢失 | heading_path 完整谱系：`["黑龙江大豆区划", "区划指标", "冷害指数"]` |
| **page_content** | `[省份 作物 区划]` header + 正文 | `[省份 作物 区划]` compact header + `heading_path` + 正文（v1.8 修复） |
| **大章节处理** | 无中间层，直接切 1000 字子块 | >3000 字先按 H3 回退切分，保留 path，再走 800/150 |
| **切分参数** | chunk_size=1000, overlap=200 | chunk_size=800, overlap=150 |
| **parent-child** | parent_content = 完整章节全文 | 按 section_id + chunk_index 同 section ±1 窗口扩展 |
| **上下文扩展** | 命中子块 → 展开全部父文档（可能万字） | 命中子块 → 同 section 内前1+当前+后1 chunks |
| **BM25 数据源** | 从 raw chunks.json 构建（step1 原始大小） | 从 vectordb 构建（与 Dense 共享同一套子块） |
| **降级策略** | 无标题 → 每 3 段一个 chunk | 无 style 标题 → 正则识别 → 仍失败 → 固定长度 ~800 字/句边界 |
| **metadata 字段** | province, crop, zoning, section_title | + doc_id, section_id, heading_path, heading_level, chunk_index, chunk_count |

---

## 第1层：step1_parse.py — 三层切分策略

### 旧方案

```python
# 所有级别标题一视同仁
if "Heading" in style or "heading" in style or "标题" in style:
    sections.append(current_section)  # H1/H2/H3 全部平级切
    current_section = {"title": text, "paragraphs": [], "tables": []}
```

```
Heading 1  黑龙江省大豆冷害区划    → Chunk A (空壳，丢弃)
Heading 2  一、数据来源            → Chunk B (几乎空的)
Heading 3  1.1 气象数据 + 正文     → Chunk C (仅有正文，丢失父级语境)
Heading 3  1.2 产量数据 + 正文     → Chunk D (同上)
```

问题：H3 成为独立 chunk 后，不知道父级是"一、数据来源"，更不知道自己在"黑龙江大豆冷害区划"这个大主题下。

### 新方案：三层递进

```
Tier 1: 样式标题切分
  H1 → 记录文档标题，不切 chunk
  H2 → 章节边界，触发新 section
  H3+ → 内联为 ### / #### markdown，保留在所属 H2 章节内

Tier 2: 正则标题降级（< 2 sections 时触发）
  匹配模式：一、二、三、  |  第一章  |  （一）  |  1. 2.
  所有匹配项视为 H2 级别

Tier 3: 固定长度兜底（Tier 2 也失败时）
  max_chars = 800
  按句边界（。！？\n）切分，不硬截断
  heading_path = [doc_stem]
```

```
Tier 3 示例（某无标题 DOCX 全文 2400 字）:

原始文本:
  "黑龙江省农业气候资源普查空间范围覆盖省、市、县（区）三级行政区。
   时间范围包含年度农业气候资源和不同作物全生育期的农业气候资源。
   空间范围：全省、各地市及下辖县。时间范围：1961-1990年、1991-2020年两个标准气候期。
   普查内容主要包括以下几个方面：（1）光照资源...（2）热量资源...（3）水分资源...
   ..."  (2400 字)

↓ _split_by_sentence(text, max_chars=800)

Chunk 0 (780字): "黑龙江省农业气候资源普查空间范围覆盖省..."
Chunk 1 (810字): "（2）热量资源。包括年平均气温、各月平均气温..."
Chunk 2 (790字): "（5）基础地理信息。① 新疆、伊犁州..."

每个 chunk 的 metadata:
{
    "doc_id": "新疆农业气候资源普查清单",
    "section_id": "新疆农业气候资源普查清单_sec_0",
    "heading_path": ["新疆农业气候资源普查清单"],
    "heading_level": 1,
}
```

### 三层触发条件与阈值

| 层级 | 触发条件 | 切分方式 | 阈值 |
|------|---------|---------|------|
| Tier 1 | 文档含 Heading style | H2 为章节边界，H3+ 内联 | — |
| Tier 2 | Tier 1 产出 < 2 sections | 正则匹配标题行视为 H2 | — |
| Tier 3 | Tier 2 产出 < 2 sections | 按句边界固定长度切分 | **800 字** |

三层实际触发情况（本次 898 chunk 测试）：

| 文档类别 | 数量 | 触发层级 |
|---------|------|---------|
| 有 style 标题的 DOCX | ~10 个 | Tier 1 |
| 无 style 但有编号标题 | 0 个（本次数据未出现） | Tier 2 |
| 完全无标题文档 | 0 个（本次数据未出现） | Tier 3 |

```
以二级标题作为章节边界：
  H2 "一、数据来源与处理" ──→ Section A
    H3 "1.1 气象数据" ──→ 内联为 ### 1.1 气象数据
    H3 "1.2 产量数据" ──→ 内联为 ### 1.2 产量数据
    正文段落 ──→ 全部归属 Section A

  H2 "二、区划指标与方法" ──→ Section B
    H3 "2.1 冷害指数" ──→ 内联为 ### 2.1 冷害指数
    H3 "2.2 阈值确定" ──→ 内联为 ### 2.2 阈值确定
    正文段落 ──→ 全部归属 Section B
```

### Heading Path 设计

每个 chunk 携带完整层级谱系：

```python
# chunk from Section A
metadata = {
    "doc_id": "黑龙江大豆冷害区划技术规范",
    "section_id": "黑龙江大豆冷害区划技术规范_sec_1",
    "heading_path": ["黑龙江省大豆冷害气候风险区划", "一、数据来源与处理"],
    "heading_level": 2,
}

# chunk from Section B
metadata = {
    "section_id": "黑龙江大豆冷害区划技术规范_sec_2",
    "heading_path": ["黑龙江省大豆冷害气候风险区划", "二、区划指标与方法"],
    "heading_level": 2,
}
```

### 无标题文档处理对比

| | 旧方案 | 新方案 |
|---|---|---|
| 无 heading style | 每 3 段打包一个 chunk | 先尝试正则识别标题 (Tier 2) |
| 正则也匹配不到 | — | 固定长度 800 字，句边界对齐 (Tier 3) |
| heading_path | 无 | `[doc_stem]`，不丢文档归属 |
| 切分函数 | — | `_split_by_sentence(text, max_chars=800)` |

### 产出

`data/chunks.json`

| 指标 | 旧值 | 新值 |
|------|------|------|
| 总 chunk 数 | 1177 | 898 |
| 有效 chunk (排除 excluded + low) | 1060 | 839 |
| 平均 chunk 长度 | ~490 字符 | 623 字符 |
| heading_level 1 / 2 | 无此字段 | 721 / 177 |

---

## 第2层：step2_embed.py — 切分 + 向量化

### page_content 组装

**旧方案**：文件名推断的三元组 header
```python
header = f"[{province} {crop} {zoning_type}] "
page_content = header + c["content"]
# "[黑龙江 大豆 冷害风险区划] ## 一、数据来源\n\n正文..."
```

**v1.7 方案**：heading_path 替代 header（**已弃用 — 见 v1.8**）
```python
heading_path = m.get("heading_path", [])
path_str = " > ".join(heading_path) if heading_path else ""
page_content = path_str + "\n\n" + c["content"]
# "黑龙江省大豆冷害气候风险区划 > 一、数据来源与处理\n\n正文..."
```

**v1.8 方案（当前）**：compact header + heading_path 共存
```python
compact = f"[{province} {crop} {zoning_type}] "  # 三元组关键词锚点
path_str = " > ".join(heading_path) if heading_path else ""
page_content = compact + path_str + "\n\n" + c["content"]
# "[黑龙江 大豆 冷害风险区划] 黑龙江省大豆冷害气候风险区划 > 一、数据来源与处理\n\n正文..."
```

v1.7 用 heading_path 替代三元组 header 后 MRR -11.2%（0.4958→0.4402）。根因：BGE embedding 对所有 token 平均池化，heading_path 的冗余词（"技术规范""初稿""3.3区化方法"）稀释了核心关键词的信号权重。PDF 文档 heading_path 仅为文件名（如 `D_P_R_150000_001-内蒙古区划报告`），信号更弱。

v1.8 修复：compact header 提供高密度关键词锚点（15 字符，100% 信号密度），heading_path 保留在 page_content 中提供层级语义，同时也在 metadata 中驱动 section 级上下文扩展。MRR 恢复到 0.8333。

### H3 回退切分（新增）

章节 >3000 字的，在进入 Recursive splitter 之前先按 H3 子标题切开：

```
原始 Section: heading_path = ["黑龙江", "区划指标"]
  content = 5000字，内含 H3 "2.1 冷害指数" / "2.2 阈值" / "2.3 分级标准"

↓ _split_by_h3()

Sub 1: heading_path = ["黑龙江", "区划指标", "2.1 冷害指数"]  (~1500字)
Sub 2: heading_path = ["黑龙江", "区划指标", "2.2 阈值确定"]  (~1800字)
Sub 3: heading_path = ["黑龙江", "区划指标", "2.3 分级标准"]  (~1700字)
```

每个子章节保持完整 heading_path 谱系，H3 标题不丢失。

### RecursiveCharacterTextSplitter 参数

| 参数 | 旧值 | 新值 | 说明 |
|------|------|------|------|
| chunk_size | 1000 | 800 | 中文技术文档更适合 ~800 字检索窗口 |
| chunk_overlap | 200 | 150 | 减少冗余，检索精度更高 |
| separators | 不变 | `["\n\n", "\n", "。", "；", "，", " ", ""]` | 句边界优先 |

### 切分决策

```
839 条 step1 chunk
    │
    ├── Phase 1: >3000 字 → H3 回退切分 (+62 子章节)
    │
    ├── Phase 2: RecursiveCharacterTextSplitter
    │     ├── ≤800字 或 含表格 → 免切分 (818 条)
    │     └── >800字 → 切分为 800 字子块 (83 条 → 235 条)
    │
    └── Phase 3: 按 section_id 分组，分配 chunk_index / chunk_count
```

### Parent-Child → Section 上下文

**旧方案**：parent_content 存完整父文档

```python
d.metadata["parent_content"] = d.page_content  # 可能是 5000~10000 字
# 检索命中 → expand_parent → 返回完整万字章节（大量冗余）
```

**新方案**：chunk_index + chunk_count 替代 parent_content

```python
d.metadata["chunk_index"] = 0   # 第 0 / 3 块
d.metadata["chunk_count"] = 3
# 检索命中 → expand_context → 取同 section 的前后 ±1 chunks（固定窗口）
```

### 产出

ChromaDB `vectordb/`

| 指标 | 旧值 | 新值 |
|------|------|------|
| 向量总数 | 1088 | **1053** |
| 免切分 | 988 | 818 |
| 被切分源 | 72 | 83 |
| 切分后子块 | 100 | 235 |
| H3 回退切分 | 无 | +62 子章节 |
| 平均 chunk 长度 | ~490 字符 | **530** 字符 |

---

## 第3层：BM25 索引 + Section 索引 (hybrid_search.py)

### 数据源变更

**旧方案**：从 raw chunks.json 构建（与 Dense 不同源）

```python
raw_chunks = json.load("chunks.json")
bm25_docs = [Document(page_content=c["content"], metadata=c["metadata"])
             for c in raw_chunks if not c.get("excluded")]
```

**新方案**：从 Chroma vectordb 构建（与 Dense 同一套子块）

```python
all_data = self._vectorstore.get(include=["documents", "metadatas"])
bm25_docs = [Document(page_content=content, metadata=meta)
             for content, meta in zip(all_data["documents"], all_data["metadatas"])]
```

### 两个索引对比

| | Chroma (Dense) | BM25 |
|---|---|---|
| **数据源** | step2 切分后的 1053 条 | 同一套 1053 条 |
| **metadata** | heading_path, section_id, chunk_index, chunk_count | 相同 |
| **匹配方式** | 语义相似（cosine） | 关键词精确匹配（TF-IDF） |

### Section 索引（新增）

检索初始化时同时构建 section 索引，供上下文扩展使用：

```python
self._section_index = {
    "doc1_sec_0": [
        {"content": "...", "chunk_index": 0},
        {"content": "...", "chunk_index": 1},
        {"content": "...", "chunk_index": 2},
    ],
    "doc1_sec_1": [
        {"content": "...", "chunk_index": 0},
        {"content": "...", "chunk_index": 1},
    ],
}
```

---

## 第4层：检索时融合 + 上下文扩展

### 完整流程

```
用户 query: "黑龙江大豆冷害区划选用了哪些指标？"
    │
    ├── Step 1: Chroma Dense 检索 (k=pool_size)
    │
    ├── Step 2: BM25 关键词检索 (k=pool_size)
    │
    ├── Step 3: RRF 融合
    │     weights: Dense=0.7, BM25=0.3, K=60
    │
    ├── Step 4: [可选] Reranker 精排 (bge-reranker-v2-m3)
    │
    ├── Step 5: [可选] expand_context — 同 section 上下文扩展
    │     命中 chunk (section_id=sec_A, chunk_index=2, chunk_count=4)
    │     ↓
    │     start = max(0, 2-1) = 1
    │     end   = min(3, 2+1)   = 3
    │     ↓
    │     返回: chunk_1 + chunk_2 + chunk_3 (同一 section 内)
    │     ↓
    │     最终: heading_path + "\n\n---\n\n" + chunk1 + chunk2 + chunk3
    │
    └── Step 6: 返回 top_k 结果
```

### 上下文扩展对比

**旧方案 expand_parent**：

```
命中子块 → r["content"] = parent_content (完整章节，可能万字)
多个结果命中同一 parent → 保留一个（按 parent[:120] 去重）
```

问题：子块只相关 200 字，但返回了完整 5000 字章节，大部分内容对当前 query 无帮助。

**新方案 expand_context**：

```
命中子块 → 取同 section 内 [chunk_index-1, chunk_index, chunk_index+1]
         → heading_path 前置一次
         → 永不越界（start ≥ 0, end < chunk_count）
         → 同窗口去重（section_id:start:end）
```

示例：

```
# 命中 section A 的 chunk_2 (chunk_count=4)
start = 1, end = 3

结果:
heading_path: 黑龙江大豆区划 > 二、区划指标与方法 > 冷害指数

--- (chunk_1 内容)
--- (chunk_2 内容，命中块)
--- (chunk_3 内容)
```

| 场景 | expand_parent (旧) | expand_context (新) |
|------|-------------------|---------------------|
| 命中短章节(800字) | 返回完整 800 字 | 返回完整 800 字 (1 chunk = 整个 section) |
| 命中长章节(5000字) | 返回完整 5000 字 | 返回 ~2400 字 (3 × 800) |
| 命中 section 第一个 chunk | 返回完整 5000 字 | 返回 ~1600 字 (chunk_0 + chunk_1) |
| 命中 section 最后一个 chunk | 返回完整 5000 字 | 返回 ~1600 字 (chunk_{n-1} + chunk_n) |

### Similarity 字段说明

| 字段 | 含义 | 用途 |
|------|------|------|
| `similarity` | 排序分（有 reranker 时为 rerank_score，无时为 RRF score 或余弦相似度） | 返回排序 |
| `dense_similarity` | Chroma 真实余弦相似度 | Judge OOD 判定（阈值 0.46） |

---

## 数值总结

| 阶段 | 产物 | 旧值 | 新值 |
|------|------|------|------|
| step1 解析 | chunks.json 总条数 | 1177 | **898** |
| step1 有效 | 排除 excluded + low | 1060 | **839** |
| step2 H3 回退 | 大章节切分 | 无 | **+62 子章节** |
| step2 免切分 | ≤800字/含表格 | 988 | **818** |
| step2 切分 | >800字 → 子块 | 72→100 | **83→235** |
| step2 总计 | vectordb 向量 | **1088** | **1061** (v1.8 重建) |
| 平均 chunk 长度 | — | ~490 字符 | **537** 字符 |
| section 数 | — | 无此概念 | **902** |
| heading_level 分布 | — | 无此字段 | L1=721, L2=177 |

---

> **关联文件**: step1_parse.py, step2_embed.py, hybrid_search.py, evaluate.py, rag_pipeline.py
