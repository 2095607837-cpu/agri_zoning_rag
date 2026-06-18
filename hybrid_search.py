"""
混合检索引擎（LangChain 实现）

组合 BM25 关键词检索 + Chroma 语义检索，通过 EnsembleRetriever 融合。
同时支持 CrossEncoder Reranker 精排。

用法:
  searcher = HybridSearcher()
  results = searcher.search("大豆冷害区划指标", top_k=5)
"""

import os
import json
from pathlib import Path

from langchain.schema import Document
from langchain_community.retrievers import BM25Retriever
from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
CHUNKS_PATH = BASE_DIR / "data" / "chunks.json"
PERSIST_DIR = str(BASE_DIR / "vectordb")
COLLECTION_NAME = "agri_zoning"
EMBED_MODEL = "BAAI/bge-small-zh-v1.5"


class Reranker:
    """BGE CrossEncoder 精排器（LangChain 暂未内置，保留自定义实现）。"""

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3"):
        self._model_name = model_name
        self._model = None

    def _load(self):
        if self._model is not None:
            return
        from sentence_transformers import CrossEncoder
        print(f"[Reranker] 加载 {self._model_name}...")
        self._model = CrossEncoder(self._model_name)

    def rerank(self, query: str, candidates: list[Document], top_k: int = 5) -> list[dict]:
        self._load()
        # 截断到 500 字符，避免长文档拖垮 CrossEncoder（模型 max 512 tokens）
        pairs = [(query, c.page_content[:500]) for c in candidates]
        scores = self._model.predict(pairs, show_progress_bar=False)
        ranked = np.argsort(scores)[::-1]
        results = []
        for i in ranked[:top_k]:
            results.append({
                "content": candidates[i].page_content,
                "metadata": candidates[i].metadata,
                "similarity": round(float(scores[i]), 4),
            })
        return results


class HybridSearcher:
    """
    LangChain 混合检索器。

    内部组合:
      - BM25Retriever (关键词匹配)
      - Chroma.as_retriever (语义检索)
      - EnsembleRetriever (RRF-like 加权融合，weights=[0.3, 0.7])
      - 可选 Reranker 精排
    """

    def __init__(self, enable_reranker: bool = False):
        self.enable_reranker = enable_reranker
        self._embeddings = None
        self._vectorstore = None
        self._bm25_retriever = None
        self._reranker: Reranker | None = None
        self._init()

    @property
    def vectorstore(self):
        """Chroma 向量存储实例（供评测直接查询真实 L2 距离）。"""
        return self._vectorstore

    @property
    def embeddings(self):
        """HuggingFaceEmbeddings 实例（供评测复用，避免重复加载模型）。"""
        return self._embeddings

    def _init(self):
        print("[HybridSearcher] 初始化...")
        # 加载 embeddings
        self._embeddings = HuggingFaceEmbeddings(
            model_name=EMBED_MODEL,
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )

        # 加载 Chroma
        self._vectorstore = Chroma(
            collection_name=COLLECTION_NAME,
            embedding_function=self._embeddings,
            persist_directory=PERSIST_DIR,
        )

        # BM25 — 从原始 chunks 构建（跳过被文档去重排除的 chunk）
        with open(CHUNKS_PATH, "r", encoding="utf-8") as f:
            raw_chunks = json.load(f)
        bm25_docs = [
            Document(page_content=c["content"], metadata={**c["metadata"], "chunk_id": c["id"]})
            for c in raw_chunks
            if not c.get("excluded")  # 文档级去重排除的跳过
        ]
        self._bm25_retriever = BM25Retriever.from_documents(bm25_docs)
        self._bm25_retriever.k = 20  # 候选池，供 RRF 融合

        # Reranker
        if self.enable_reranker:
            self._reranker = Reranker()

        print(f"[HybridSearcher] 就绪 (reranker={'on' if self.enable_reranker else 'off'})")

    def search(self, query: str, top_k: int = 5, expand_parent: bool = False) -> list[dict]:
        """混合检索（手动 RRF 融合）+ 可选精排 + 可选父文档展开。

        使用 Chroma 真实 L2 距离作为相似度分数，不再用 rank-based 近似。

        Args:
            expand_parent: 若为 True，将子 chunk 内容替换为完整父文档，并去重。
        """
        pool_size = top_k * 3

        # 1. Chroma 语义检索（带真实 L2 距离）
        chroma_raw = self._vectorstore.similarity_search_with_score(query, k=pool_size)

        # 2. BM25 关键词检索
        bm25_docs = self._bm25_retriever.invoke(query)[:pool_size]

        # 3. 可选 Reranker 精排（各取 pool_size/2，去重后精排）
        if self._reranker:
            half = max(pool_size // 2, top_k)
            candidates = []
            seen = set()
            for doc, _ in chroma_raw[:half]:
                key = doc.page_content[:80]
                if key not in seen:
                    seen.add(key)
                    candidates.append(doc)
            for doc in bm25_docs[:half]:
                key = doc.page_content[:80]
                if key not in seen:
                    seen.add(key)
                    candidates.append(doc)
            results = self._reranker.rerank(query, candidates, top_k=top_k)
        else:
            # 4. 手动 RRF 融合（权重: 语义 0.7, 关键词 0.3）
            RRF_K = 60
            rrf_scores: dict[str, float] = {}
            doc_store: dict[str, tuple] = {}  # key → (content, metadata, sim)

            for rank, (doc, l2_dist) in enumerate(chroma_raw):
                key = doc.page_content[:80]
                rrf_scores[key] = rrf_scores.get(key, 0) + 0.7 / (RRF_K + rank)
                sim = round(1.0 / (1.0 + l2_dist), 4)  # L2距离→相似度 [0.33, 1.0]
                if key not in doc_store:
                    doc_store[key] = (doc.page_content, doc.metadata, sim)

            for rank, doc in enumerate(bm25_docs):
                key = doc.page_content[:80]
                rrf_scores[key] = rrf_scores.get(key, 0) + 0.3 / (RRF_K + rank)
                if key not in doc_store:
                    doc_store[key] = (doc.page_content, doc.metadata, None)

            # 按 RRF 得分排序，取 top_k
            sorted_keys = sorted(rrf_scores, key=lambda k: -rrf_scores[k])[:top_k]

            results = []
            for rank, key in enumerate(sorted_keys):
                content, metadata, real_sim = doc_store[key]
                if real_sim is None:
                    # BM25-only 结果：用 RRF 分数近似（罕见）
                    real_sim = round(rrf_scores[key] / (0.7 / RRF_K + 0.3 / RRF_K), 4)
                results.append({
                    "content": content,
                    "metadata": metadata,
                    "similarity": real_sim,
                })

        # 父文档展开：子 chunk 替换为完整父文档内容，同一父文档的多段去重
        if expand_parent:
            seen_parents = set()
            expanded = []
            for r in results:
                parent = r["metadata"].get("parent_content")
                if parent:
                    parent_key = parent[:120]  # 用父文档前120字做去重 key
                    if parent_key in seen_parents:
                        continue
                    seen_parents.add(parent_key)
                    r["content"] = parent
                expanded.append(r)
                if len(expanded) >= top_k:
                    break
            results = expanded

        return results


if __name__ == "__main__":
    searcher = HybridSearcher()
    for q in ["大豆冷害区划的指标", "冬小麦品质归一化方法", "柑橘种植气候适宜性"]:
        print(f"\n{'=' * 60}")
        print(f"Query: {q}")
        results = searcher.search(q, top_k=3)
        for i, r in enumerate(results, 1):
            src = r["metadata"].get("source_file", "?")
            print(f"  [{i}] sim={r['similarity']:.4f} | {src[:45]}")
            print(f"      {r['content'][:120]}...")
