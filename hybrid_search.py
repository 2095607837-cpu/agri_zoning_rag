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
from langchain.retrievers import EnsembleRetriever
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
        pairs = [(query, c.page_content) for c in candidates]
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
        self._vectorstore: Chroma | None = None
        self._bm25_retriever: BM25Retriever | None = None
        self._ensemble: EnsembleRetriever | None = None
        self._reranker: Reranker | None = None
        self._init()

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

        # BM25 — 从原始 chunks 构建（保留完整语义，而非切分后碎片）
        with open(CHUNKS_PATH, "r", encoding="utf-8") as f:
            raw_chunks = json.load(f)
        bm25_docs = [
            Document(page_content=c["content"], metadata={**c["metadata"], "chunk_id": c["id"]})
            for c in raw_chunks
        ]
        self._bm25_retriever = BM25Retriever.from_documents(bm25_docs)
        self._bm25_retriever.k = 10

        # Chroma retriever
        vector_retriever = self._vectorstore.as_retriever(
            search_type="similarity",
            search_kwargs={"k": 10},
        )

        # Ensemble
        self._ensemble = EnsembleRetriever(
            retrievers=[self._bm25_retriever, vector_retriever],
            weights=[0.3, 0.7],  # 语义为主，关键词为辅
        )

        # Reranker
        if self.enable_reranker:
            self._reranker = Reranker()

        print(f"[HybridSearcher] 就绪 (reranker={'on' if self.enable_reranker else 'off'})")

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """混合检索 + 可选精排。"""
        # Ensemble 检索
        docs = self._ensemble.invoke(query)

        # Reranker 精排
        if self._reranker:
            return self._reranker.rerank(query, docs[:top_k * 3], top_k=top_k)

        # 否则直接取 top_k，附上 similarity（BM25 无 score，统一用 rank）
        results = []
        for i, doc in enumerate(docs[:top_k]):
            results.append({
                "content": doc.page_content,
                "metadata": doc.metadata,
                "similarity": round(1.0 / (i + 1), 4),  # rank-based 近似
            })
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
