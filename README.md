# 农业气候区划 RAG 系统

基于 LangChain + LangGraph 的农业气候区划知识检索增强生成系统。

## 技术栈

| 层 | 技术 | 说明 |
|---|------|------|
| 文档加载 | `python-docx` / `pymupdf` / `openpyxl` | 解析 DOCX、PDF、XLSX、CSV |
| 文本切分 | `RecursiveCharacterTextSplitter` (LangChain) | 500字符/chunk，50字符重叠 |
| 向量嵌入 | `BAAI/bge-small-zh-v1.5` via `HuggingFaceEmbeddings` | 512维 L2归一化向量 |
| 向量存储 | `Chroma` (LangChain) | 持久化，余弦距离，HNSW索引 |
| 关键词检索 | `BM25Retriever` (LangChain) | 字符+bigram双粒度 |
| 混合检索 | `EnsembleRetriever` (LangChain) | BM25 + Dense 加权融合 (3:7) |
| 精排 | `BAAI/bge-reranker-v2-m3` (CrossEncoder) | 可选启用 |
| 管道编排 | `LangGraph StateGraph` | retrieve → judge → generate 有状态图 |
| LLM | DeepSeek API (OpenAI 兼容) | Judge + Generator + Rewriter |

## 架构

```
                      ┌──────────────────────┐
                      │        Query          │
                      └──────────┬───────────┘
                                 │
                    ┌────────────▼────────────┐
                    │   Query Rewriter (可选)   │
                    │   HyDE + 关键词 + 子查询   │
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │    Hybrid Search         │
                    │  BM25 + Dense → Ensemble │
                    │    → Reranker (可选)      │
                    └────────────┬────────────┘
                                 │
              ┌──────────────────▼──────────────────┐
              │          Judge (三层判定)              │
              │  Layer 1: 信号层 (source_type 预过滤)   │
              │  Layer 2: 分数层 (similarity < 0.46)  │
              │  Layer 3: LLM 细判 (YES/NO/PARTIAL)   │
              └──────┬──────────┬──────────┬─────────┘
                     │          │          │
                 answer    fallback    reject
                     │          │          │
              ┌──────▼──────────▼──────────▼─────────┐
              │        Generator (LLM)                │
              │  基于 context 生成专业回答 / 拒答话术    │
              └────────────────┬─────────────────────┘
                               │
                    ┌──────────▼───────────┐
                    │      Answer           │
                    └──────────────────────┘
```

## LangGraph 图结构

```
START → retrieve → judge → generate → END
```

- **retrieve**: 查询改写 + EnsembleRetriever 多路检索 + content去重
- **judge**: 三层判定 → answer / fallback / reject
- **generate**: 基于判定结果生成答案或拒答话术

## 数据集

| 类型 | 数量 | 来源 |
|------|------|------|
| 技术规范 (DOCX) | 22份 | 黑龙江、河南、陕西、新疆、辽宁、全国 |
| 区划报告 (PDF) | 6份 | 内蒙古、黑龙江、河南、江西、陕西、新疆 |
| 指标数据 (XLSX/CSV) | 5份 | 河南、新疆 |
| 省份 | 7省 + 全国 | — |
| 主要作物 | 大豆、冬小麦、柑橘、苹果 | — |

## 数据处理指标

| 指标 | 数值 |
|------|------|
| 原始文件数 | 34 份 |
| 解析后 chunks | 1,177 条 |
| 去重 (ID重复) | 3 条 |
| 去重 (内容重复) | 14 条 |
| 最终 chunks | 1,177 条 (唯一) |
| 平均 chunk 长度 | 473 字符 |
| 切分后文档数 | 1,881 条 (RecursiveCharacterTextSplitter) |
| 向量索引文档数 | 3,058 条 |
| 平均切分 chunk 长度 | 305 字符 |
| 向量维度 | 512 维 |

## 检索指标 (无 LLM，纯分数层)

| 指标 | 数值 |
|------|------|
| Judge 最优阈值 (Layer 2) | 0.46 |
| 检索延迟 | ~0.3s/query (hybrid) |

## 目录结构

```
agri_zoning_rag/
├── step1_parse.py       # 数据解析（DOCX/PDF/XLSX/CSV → chunks.json）
├── step2_embed.py       # 切分 + 嵌入 + ChromaDB 索引（LangChain）
├── hybrid_search.py     # 混合检索（LangChain EnsembleRetriever）
├── judge.py             # 三层 OOD 判定（LangChain ChatPromptTemplate）
├── generator.py         # LLM 答案生成（LangChain ChatPromptTemplate）
├── query_rewriter.py    # 查询改写（LRU 缓存 + LangChain）
├── rag_pipeline.py      # LangGraph 管道编排
├── llm_client.py        # LLM API 客户端（OpenAI 兼容）
├── data/
│   └── chunks.json      # 解析后的统一 chunks
├── vectordb/            # ChromaDB 持久化 (gitignore)
├── README.md            # 本文档
└── requirements.txt     # Python 依赖
```

## 快速开始

### 1. 安装依赖

```bash
pip install langchain langgraph langchain-community langchain-chroma \
            langchain-huggingface python-docx pymupdf openpyxl \
            rank_bm25 sentence-transformers chromadb requests
```

### 2. 设置 API Key

```bash
export LLM_API_KEY="your-deepseek-key"
# 或
export DEEPSEEK_API_KEY="your-deepseek-key"
```

### 3. 构建索引

```bash
python3 step1_parse.py        # 解析数据 → data/chunks.json
python3 step2_embed.py --rebuild  # 构建 ChromaDB 索引
```

### 4. 查询

```python
from rag_pipeline import RAGPipeline

rag = RAGPipeline(enable_reranker=False, enable_rewrite=True)
result = rag.query("黑龙江大豆冷害区划选用了哪些气象指标？")
print(result.answer)
print(result.sources)
```

### 5. 仅检索（不需要 LLM）

```python
from hybrid_search import HybridSearcher

searcher = HybridSearcher()
results = searcher.search("大豆冷害区划指标", top_k=5)
```
