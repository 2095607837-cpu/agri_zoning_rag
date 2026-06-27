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
    """BGE CrossEncoder 精排器（模块级单例，避免重复加载模型）。"""

    _instance: "Reranker | None" = None
    _lock = None

    def __new__(cls, model_name: str = "BAAI/bge-reranker-v2-m3"):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._model_name = model_name
            cls._instance._model = None
            import threading
            cls._instance._load_lock = threading.Lock()
        return cls._instance

    def _load(self):
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            from sentence_transformers import CrossEncoder
            print(f"[Reranker] 加载 {self._model_name}...")
            self._model = CrossEncoder(self._model_name, device="cpu")

    def rerank(self, query: str, candidates: list[dict], top_k: int = 5) -> list[dict]:
        """重排序。candidates 中已有 dense_similarity（余弦相似度），reranker 只决定顺序。"""
        self._load()
        pairs = [(query, c["content"][:500]) for c in candidates]
        scores = self._model.predict(pairs, show_progress_bar=False)

        # 附加 rerank_score，保留原始字段
        ranked = []
        for i, c in enumerate(candidates):
            ranked.append({**c, "rerank_score": round(float(scores[i]), 4)})

        ranked.sort(key=lambda x: x["rerank_score"], reverse=True)

        results = []
        for r in ranked[:top_k]:
            results.append({
                "content": r["content"],
                "metadata": r["metadata"],
                "similarity": r["rerank_score"],          # 精排分用于排序展示
                "dense_similarity": r.get("dense_similarity", r.get("similarity", 0)),  # 余弦相似度，供 Judge 判定
            })
        return results


class HybridSearcher:
    """
    LangChain 混合检索器。

    内部组合:
      - BM25Retriever (关键词匹配)
      - Chroma 语义检索
      - RRF 加权融合
      - 可选 Reranker 精排
      - 同 section 上下文扩展（命中 chunk ±1）

    BM25 和 Chroma 使用同一套 document（step2 切分后的子块），
    metadata 中含 section_id / chunk_index / chunk_count，支持按 section 扩展上下文。
    """

    def __init__(self, enable_reranker: bool = False):
        self.enable_reranker = enable_reranker
        self._embeddings = None
        self._vectorstore = None
        self._bm25_retriever = None
        self._reranker: Reranker | None = None
        self._section_index: dict[str, list[dict]] = {}
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
        self._embeddings = HuggingFaceEmbeddings(
            model_name=EMBED_MODEL,
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )

        self._vectorstore = Chroma(
            collection_name=COLLECTION_NAME,
            embedding_function=self._embeddings,
            persist_directory=PERSIST_DIR,
        )

        # BM25 + Section Index — 直接从 chunks.json 构建，不依赖 Chroma
        with open(CHUNKS_PATH, encoding="utf-8") as f:
            chunks = json.load(f)

        bm25_docs = []
        for c in chunks:
            content = c["content"]
            meta = c["metadata"]
            bm25_docs.append(Document(page_content=content, metadata=meta))

            sid = meta.get("section_id", "")
            if sid:
                idx = len(self._section_index.get(sid, []))
                self._section_index.setdefault(sid, []).append({
                    "content": content,
                    "metadata": meta,
                    "chunk_index": idx,
                })

        for sid in self._section_index:
            self._section_index[sid].sort(key=lambda x: x["chunk_index"])

        self._bm25_retriever = BM25Retriever.from_documents(bm25_docs)
        self._bm25_retriever.k = 20

        if self.enable_reranker:
            self._reranker = Reranker()

        print(f"[HybridSearcher] 就绪 (docs={len(bm25_docs)}, sections={len(self._section_index)}, "
              f"reranker={'on' if self.enable_reranker else 'off'})")

    def search(self, query: str, top_k: int = 5, expand_context: bool = False) -> list[dict]:
        """混合检索（RRF 融合）+ 可选 CrossEncoder 重排序 + 可选同 section 上下文扩展。

        无论是否启用 reranker，返回结果中的 similarity 字段始终是余弦相似度，
        确保下游 Judge 的阈值判定一致。

        expand_context=True 时：对每个命中 chunk，取同 section 内前1后1相邻 chunk，
        拼接为检索结果（固定窗口，而非展开完整父文档）。
        """
        pool_size = max(top_k * 4, 20)  # 召回量，保证足够候选

        # 1. Chroma 语义检索（带真实余弦距离）
        chroma_raw = self._vectorstore.similarity_search_with_score(query, k=pool_size)

        # 2. BM25 关键词检索
        bm25_docs = self._bm25_retriever.invoke(query)[:pool_size]

        # 3. 构建统一候选池，记录真实余弦相似度
        RRF_K = 60
        rrf_scores: dict[str, float] = {}
        doc_store: dict[str, tuple] = {}  # key → (content, metadata, cosine_sim)

        for rank, (doc, l2_dist) in enumerate(chroma_raw):
            key = doc.page_content[:80]
            rrf_scores[key] = rrf_scores.get(key, 0) + 0.7 / (RRF_K + rank)
            sim = round(1.0 - l2_dist, 4)
            if key not in doc_store:
                doc_store[key] = (doc.page_content, doc.metadata, sim)

        for rank, doc in enumerate(bm25_docs):
            key = doc.page_content[:80]
            rrf_scores[key] = rrf_scores.get(key, 0) + 0.3 / (RRF_K + rank)
            if key not in doc_store:
                doc_store[key] = (doc.page_content, doc.metadata, None)

        # BM25-only 结果补算余弦相似度
        bm25_only_keys = [k for k in doc_store if doc_store[k][2] is None]
        if bm25_only_keys:
            query_emb = np.array(self._embeddings.embed_query(query))
            for key in bm25_only_keys:
                content, metadata, _ = doc_store[key]
                doc_emb = np.array(self._embeddings.embed_documents([content])[0])
                sim = float(np.dot(query_emb, doc_emb)
                            / (np.linalg.norm(query_emb) * np.linalg.norm(doc_emb) + 1e-8))
                doc_store[key] = (content, metadata, round(sim, 4))

        # 4. 排序
        if self._reranker:
            # Reranker 重排序：RRF 粗排截断后送 CrossEncoder 精排
            rerank_input = min(top_k * 3, 40)  # 精排输入上限，防 CrossEncoder 过载
            candidates = []
            for key in sorted(rrf_scores, key=lambda k: -rrf_scores[k])[:rerank_input]:
                content, metadata, cosine_sim = doc_store[key]
                candidates.append({
                    "content": content,
                    "metadata": metadata,
                    "dense_similarity": cosine_sim or 0.0,
                })
            results = self._reranker.rerank(query, candidates, top_k=top_k)
        else:
            # RRF 融合排序
            sorted_keys = sorted(rrf_scores, key=lambda k: -rrf_scores[k])[:top_k]
            results = []
            for key in sorted_keys:
                content, metadata, real_sim = doc_store[key]
                results.append({
                    "content": content,
                    "metadata": metadata,
                    "similarity": real_sim or 0.0,
                    "dense_similarity": real_sim or 0.0,
                })

        # 5. 同 section 上下文扩展（命中 chunk ±1）
        if expand_context:
            expanded = []
            seen_windows = set()
            for r in results:
                sid = r["metadata"].get("section_id", "")
                chunk_idx = r["metadata"].get("chunk_index", 0)

                if sid and sid in self._section_index:
                    section_chunks = self._section_index[sid]
                    chunk_count = len(section_chunks)
                    start = max(0, chunk_idx - 1)
                    end = min(chunk_count - 1, chunk_idx + 1)

                    # 同一 section 内去重：相同窗口只保留一次
                    window_key = f"{sid}:{start}:{end}"
                    if window_key in seen_windows:
                        continue
                    seen_windows.add(window_key)

                    parts = []
                    heading_path = r["metadata"].get("heading_path", [])
                    if heading_path:
                        parts.append(" > ".join(heading_path))

                    for i in range(start, end + 1):
                        if i < len(section_chunks):
                            parts.append(section_chunks[i]["content"])

                    r["content"] = "\n\n---\n\n".join(parts)
                    r["context_range"] = [start, end]

                expanded.append(r)
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
