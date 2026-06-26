# 农业气候区划 RAG — Chunk Pipeline 技术文档

> 完整记录从原始文档到检索结果的全链路 chunk 处理逻辑

---

## 总览

```
源文件 (.docx/.pdf/.xlsx/.csv)
    │
    ▼ step1_parse.py
chunks.json (1177条, 按章节/页切分)
    │
    ├──────────────────┐
    ▼                  ▼
step2_embed.py    hybrid_search.py
    │             (BM25从原始chunks构建)
    │                  │
    ├─ metadata注入     │
    ├─ ≤800字免切       │
    ├─ >800字滑动窗口   │
    ├─ parent_content   │
    └─ BGE embed        │
    │                  │
    ▼                  ▼
vectordb/          BM25 倒排索引
(1088条向量)       (1060条原文)
    │                  │
    └──────┬───────────┘
           ▼
      RRF 融合 (Dense 0.7 + BM25 0.3)
           │
           ▼
      [Reranker 精排]
           │
           ▼
      [expand_parent 子→父展开]
           │
           ▼
      top_k 结果 → Judge → Generate
```

---

## 第1层：原始文档解析 (step1_parse.py)

### 输入

```
农业区划算法/
├── 黑龙江/大豆/冷害区划.docx
├── 黑龙江/大豆/干旱区划.docx
├── 河南/冬小麦/品质区划.pdf
├── 江西/柑橘/冻害区划.xlsx
├── 新疆/冬小麦/台站数据.csv
└── ...
```

### 解析逻辑

| 格式 | 方法 | 切分单元 | 特殊处理 |
|------|------|---------|---------|
| DOCX | `python-docx` | 按章节标题（Heading style）切 | 表格转 Markdown；无标题时降级为每3段落一个 chunk |
| PDF | `pymupdf` | 按页切 | 单页 >1200字时均分为 600 字片段 |
| XLSX | `openpyxl` | 每个 sheet 一个 chunk | 行列转文本描述 |
| CSV | `csv` 模块 | 整个文件一个 chunk | 自动检测编码（utf-8/gbk/gb2312/gb18030/latin-1） |

### Metadata 提取

每条 chunk 通过文件路径推断元信息：

```python
# 示例：黑龙江/大豆/冷害风险区划技术规范.docx
{
    "id": "黑龙江大豆冷害风险区划技术规范_s0",
    "content": "## 冷害指标\n\n≥10℃积温距平...",
    "metadata": {
        "source_type": "technical_spec",
        "source_file": "黑龙江大豆冷害风险区划技术规范.docx",
        "province": "黑龙江",
        "crop": "大豆",
        "zoning_type": "冷害风险区划",
        "section_title": "冷害指标"
    }
}
```

### 后处理

| 步骤 | 方法 | 数量变化 |
|------|------|---------|
| ID 去重 | 相同 chunk_id 只保留一个 | 1177 → ~1177 |
| 内容去重 | 前200字 MD5 哈希 | ~1177 → ~1060 |
| 文档级去重 | 同一(省份,作物,类型)的 DOCX/PDF 对，保留内容量多的 | 标记 excluded |
| 质量标记 | 目录/参考文献/极短(<50字) → `quality=low` | — |

### 产出

`data/chunks.json` — **1177 条**（65 excluded + 52 low quality + 1060 有效）

---

## 第2层：切分 + 向量化 (step2_embed.py)

### 过滤

跳过 `excluded=True` 和 `quality=low` 的 chunk，保留 **1060 条**。

### Metadata 注入 Content 头

切分前在 content 头部注入领域信号：

```python
header = f"[{province} {crop} {zoning_type}] "
doc = Document(page_content=header + original_content)
# "[黑龙江 大豆 冷害风险区划] ≥10℃积温距平..."
```

让 BGE embedding 直接感知省份/作物/区划类型，跨省对比检索更精准。

### 切分决策

```
1060 条有效 chunk
    │
    ├── ≤800字 或 含 Markdown 表格 → 免切分 (988条, 93.2%)
    │     直接入库，无 parent_content
    │
    └── >800字 → RecursiveCharacterTextSplitter (72条, 6.8%)
```

### 滑动窗口参数

```python
RecursiveCharacterTextSplitter(
    separators=["\n\n", "\n", "。", "；", "，", " ", ""],
    chunk_size=1000,      # 每个窗口 1000 字符
    chunk_overlap=200,    # 相邻窗口重叠 200 字符
)
```

分隔符优先级：先尝试段落边界 `\n\n`，再尝试句子边界 `。`，最后才按字符切，保证语义完整性。

### Parent-Child 机制

以一条 2500 字的文档为例：

```
原文 (parent): [========== 2500 字 ==========]
                  ↓ 切分前存入 metadata

child[0]: [==== 0~1000 ====]
child[1]:        [==== 800~1800 ====]   ← 与 child[0] 重叠 200 字
child[2]:               [==== 1600~2500 ====]

每个 child 的 metadata:
{
    "province": "黑龙江",
    "crop": "大豆",
    "zoning_type": "冷害风险区划",
    "parent_content": "完整的2500字原文..."  ← 检索命中后展开
}
```

### 设计意图

| 对比 | 无 parent-child | 有 parent-child |
|------|----------------|-----------------|
| 长文档 embedding | 整个文档求平均向量，信息稀释 | 子片段聚焦，embedding 更精准 |
| 检索命中 | 向量相似但内容碎片化 | 子 chunk 命中 → 展开为完整父文档 |
| Judge 判定 | 碎片信息可能导致误拒 | 看到完整上下文，误拒从 33.7%→0% |

### Embedding 参数

```python
HuggingFaceEmbeddings(
    model_name="BAAI/bge-small-zh-v1.5",  # 512维, ~100MB
    model_kwargs={"device": "cpu"},
    encode_kwargs={"normalize_embeddings": True},  # L2归一化
)
```

### 产出

ChromaDB `vectordb/` — **1088 条向量**（988 完整 + 100 子 chunk）

| 指标 | 数值 |
|------|------|
| 子 chunk 占比 | 100/1088 = 9.2% |
| 去重后 unique parent | 72 |
| 切分膨胀比 | 100/72 = 1.4x |
| 平均 chunk 长度 | ~490 字符 |
| BGE token 上限 | 512（中文 ~1.04 tokens/字，490字≈510 tokens，刚好边界） |

---

## 第3层：BM25 索引 (hybrid_search.py)

### 数据源

```python
# 从原始 chunks.json 构建，而非 vectordb
raw_chunks = json.load("chunks.json")
bm25_docs = [
    Document(page_content=c["content"], metadata=c["metadata"])
    for c in raw_chunks
    if not c.get("excluded")  # 跳过文档去重排除的
]
BM25Retriever.from_documents(bm25_docs, k=20)
```

### 两个索引的差异

| | Chroma (Dense) | BM25 |
|---|---|---|
| **数据源** | step2 切分后的 1088 条 child/intact | step1 原始 1060 条 parent |
| **存储内容** | 512维向量 + content + metadata | 原始 content + 倒排索引 |
| **是否含 parent_content** | 是（child chunk 有） | 否（原始完整文档） |
| **匹配方式** | 语义相似（cosine） | 关键词精确匹配（TF-IDF） |
| **优势** | 近义词、概括性问题 | 专业术语、数值、精确字段 |

### 为什么 BM25 用原始 chunks

关键词检索不需要切分——越完整的文本，术语覆盖率越好。且 BM25 用 TF-IDF 稀疏向量，不受 BGE 512 token 限制。

---

## 第4层：检索时融合 (hybrid_search.py)

### 完整流程

```
用户 query: "黑龙江大豆冷害区划选用了哪些指标？"
    │
    ├── Step 1: Chroma Dense 检索 (k=20)
    │     query → BGE encode → L2距离 → cosine相似度
    │     命中: child chunk 或 intact chunk
    │
    ├── Step 2: BM25 关键词检索 (k=20)
    │     分词 → 倒排索引匹配 → BM25 得分
    │     命中: 原始完整 chunk
    │
    ├── Step 3: RRF 融合
    │     RRF_K = 60
    │     weights: Dense=0.7, BM25=0.3
    │     去重 key: content[:80]
    │
    ├── Step 4: [可选] Reranker 精排
    │     粗排截断: top_k * 3 (最多40)
    │     CrossEncoder: bge-reranker-v2-m3
    │     精排后保留 top_k
    │
    ├── Step 5: [可选] expand_parent 子→父展开
    │     命中 child chunk → content = parent_content
    │     按 parent[:120] 去重
    │
    └── Step 6: 返回 top_k 结果
         每条带 dense_similarity(余弦) + similarity(排序分)
```

### RRF 融合公式

```python
RRF_score(doc) = Σ  weight_i / (K + rank_i)

# Dense 权重 0.7, BM25 权重 0.3, K=60
# rank 越靠前（rank_i 越小），贡献越大
```

### Similarity 字段说明

| 字段 | 含义 | 用途 |
|------|------|------|
| `similarity` | 排序分（有 reranker 时为 rerank_score，无时为 RRF score） | 返回排序 |
| `dense_similarity` | Chroma 真实余弦相似度 | Judge OOD 判定（阈值 0.46） |

两者分离确保 Judge 判定稳定：Reranker 打分会放大所有结果的分值（OOD 也可得 0.5+），但真实余弦相似度不变。

### expand_parent 实际触发率

| 指标 | 数值 |
|------|------|
| 200 条查询命中子 chunk | 58 条 (29%) |
| 命中排名分布 | rank1=14, rank2=17, rank3=9, rank4=9, rank5=9 |
| 子 chunk corpus 占比 | 9.2% |
| 实际 hit rate | 29%（子 chunk 语义聚焦，更易被命中） |

---

## 数值总结

| 阶段 | 产物 | 数量 |
|------|------|------|
| step1 解析 | chunks.json | 1177 (有效1060) |
| step2 免切分 | vectordb intact | 988 |
| step2 切分 parent | vectordb 源 | 72 |
| step2 切分 child | vectordb 子chunk | 100 |
| step2 总计 | vectordb 向量 | **1088** |
| BM25 索引 | 倒排索引 | 1060 |
| expand_parent 命中率 | 200题 top-5 | 29% |

---

> **生成日期**: 2026-06-23
> **相关文件**: step1_parse.py, step2_embed.py, hybrid_search.py, rag_pipeline.py
