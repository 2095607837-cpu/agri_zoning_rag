"""
混合检索引擎（LangChain 实现）

Dense + BM25 → RRF 融合 → CrossEncoder 精排 → top-k。

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


def bm25_tokenize(text: str) -> list[str]:
    """中文 BM25 分词：单字 + 双字 bigram，无外部依赖。

    langchain BM25Retriever 默认 `text.split()` 对中文失效（整段成一个 token，
    查询永远匹配不上）。改用字 + bigram：查询"大豆冷害"与文档都切成
    ['大','豆','冷','害','大豆','豆冷','冷害']，含"大豆"的文档即可在 bigram 上命中。
    """
    text = text.replace("\n", " ").strip()
    tokens = [ch for ch in text if not ch.isspace()]
    for i in range(len(text) - 1):
        bigram = text[i:i + 2]
        if not bigram[0].isspace() and not bigram[1].isspace():
            tokens.append(bigram)
    return tokens


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
            cls._instance._infer_lock = threading.Lock()
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
        # 序列化 CrossEncoder 推理，避免多线程 CPU 争抢反而变慢
        with self._infer_lock:
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
      - Chroma 语义检索
      - BM25Retriever 关键词检索
      - RRF 加权融合
      - CrossEncoder 精排
      - 同 section 上下文扩展（命中 chunk ±1）

    BM25 和 Chroma 使用同一套 document（step2 切分后的子块），
    metadata 中含 section_id / chunk_index / chunk_count，支持按 section 扩展上下文。
    """

    def __init__(self, enable_reranker: bool = True):
        self.enable_reranker = enable_reranker
        self._embeddings = None
        self._vectorstore = None
        self._bm25_retriever = None
        self._reranker = None  # type: Reranker | None
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
        self._chunk_id_map: dict[str, str] = {}  # content[:80] → chunk_id
        for c in chunks:
            content = c["content"]
            meta = dict(c["metadata"])
            cid = c["id"]
            meta["chunk_id"] = cid
            # 若 content[:80] 碰撞，后出现的覆盖（罕见，且不劣于直接用 content[:80] 去重）
            self._chunk_id_map[content[:80]] = cid

            sid = meta.get("section_id", "")
            if sid:
                idx = len(self._section_index.get(sid, []))
                meta["chunk_index"] = idx
                self._section_index.setdefault(sid, []).append({
                    "content": content,
                    "metadata": meta,
                    "chunk_index": idx,
                })

            bm25_docs.append(Document(page_content=content, metadata=meta))

        for sid in self._section_index:
            self._section_index[sid].sort(key=lambda x: x["chunk_index"])

        self._bm25_retriever = BM25Retriever.from_documents(
            bm25_docs, preprocess_func=bm25_tokenize
        )
        self._bm25_retriever.k = 20

        if self.enable_reranker:
            self._reranker = Reranker()

        print(f"[HybridSearcher] 就绪 (docs={len(bm25_docs)}, sections={len(self._section_index)}, "
              f"reranker={'on' if self.enable_reranker else 'off'})")

    def _chunk_key(self, doc) -> str:
        """解析 Document 或 result dict → chunk_id（fallback: content[:80]）。"""
        if isinstance(doc, dict):
            return doc.get("metadata", {}).get("chunk_id") or \
                   self._chunk_id_map.get(doc["content"][:80], doc["content"][:80])
        return doc.metadata.get("chunk_id") or \
               self._chunk_id_map.get(doc.page_content[:80], doc.page_content[:80])

    def _expand_results(self, results: list[dict]) -> list[dict]:
        """同 section 上下文扩展：命中 chunk ±1 窗口。"""
        expanded = []
        seen = set()
        for r in results:
            sid = r["metadata"].get("section_id", "")
            chunk_idx = r["metadata"].get("chunk_index", 0)
            if sid and sid in self._section_index:
                sc = self._section_index[sid]
                start, end = max(0, chunk_idx - 1), min(len(sc) - 1, chunk_idx + 1)
                wk = f"{sid}:{start}:{end}"
                if wk in seen:
                    continue
                seen.add(wk)
                parts = []
                hp = r["metadata"].get("heading_path", [])
                if hp:
                    parts.append(" > ".join(hp))
                for i in range(start, end + 1):
                    if i < len(sc):
                        parts.append(sc[i]["content"])
                r["content"] = "\n\n---\n\n".join(parts)
                r["context_range"] = [start, end]
            expanded.append(r)
        return expanded

    def _rrf_ce_fusion(self, query: str, rrf_scores: dict, doc_store: dict,
                        candidate_keys: list[str], top_k: int, alpha: float) -> list[dict]:
        """CE 精排 + RRF-CE alpha 融合，返回 top_k 结果。"""
        candidates = []
        for key in candidate_keys:
            content, metadata, cosine_sim = doc_store[key]
            candidates.append({
                "content": content, "metadata": metadata,
                "dense_similarity": cosine_sim or 0.0,
            })

        if not self._reranker:
            results = []
            for c in candidates[:top_k]:
                results.append({
                    "content": c["content"], "metadata": c["metadata"],
                    "similarity": c["dense_similarity"],
                    "dense_similarity": c["dense_similarity"],
                })
            return results

        self._reranker._load()
        ce_pairs = [(query, c["content"][:500]) for c in candidates]
        with self._reranker._infer_lock:
            ce_scores = [float(x) for x in self._reranker._model.predict(
                ce_pairs, show_progress_bar=False)]

        if alpha > 0.0:
            rrf_vals = [rrf_scores[k] for k in candidate_keys]
            rrf_min, rrf_max = min(rrf_vals), max(rrf_vals)
            ce_min, ce_max = min(ce_scores), max(ce_scores)
            rrf_range = rrf_max - rrf_min or 1e-8
            ce_range = ce_max - ce_min or 1e-8
            final_scores = []
            for i, key in enumerate(candidate_keys):
                rrf_norm = (rrf_scores[key] - rrf_min) / rrf_range
                ce_norm = (ce_scores[i] - ce_min) / ce_range
                final = alpha * rrf_norm + (1 - alpha) * ce_norm
                final_scores.append((final, i))
            final_scores.sort(key=lambda x: -x[0])
            results = []
            for final, idx in final_scores[:top_k]:
                c = candidates[idx]
                results.append({
                    "content": c["content"], "metadata": c["metadata"],
                    "similarity": round(final, 4),
                    "dense_similarity": c["dense_similarity"],
                })
        else:
            ranked = sorted(zip(ce_scores, candidates), key=lambda x: -x[0])
            results = []
            for score, c in ranked[:top_k]:
                results.append({
                    "content": c["content"], "metadata": c["metadata"],
                    "similarity": round(float(score), 4),
                    "dense_similarity": c["dense_similarity"],
                })
        return results

    def search_multi_query(
        self, query: str, top_k: int = 5, expand_context: bool = True,
        extra_queries=None, w_dense: float = 0.7, w_bm25: float = 0.3,
        alpha: float = 0.2,
    ):
        """Append 架构：Original 独占 CE 管线，Rewrite 以 RRF-only 补充。

        Original → RRF+CE → 排在前面（高质量 CE 信号）
        Rewrite → RRF-only → 去重追加到后面（不干扰 original 排序）

        子查询跳过 CE 避免了噪声干扰，同时保留改写召回的高价值 chunk。
        """
        if not extra_queries:
            results = self.search(query, top_k=top_k, expand_context=expand_context)
            return results, results

        # Original query → 完整 RRF+CE 管线
        results = self.search(
            query, top_k=top_k, expand_context=False,
            w_dense=w_dense, w_bm25=w_bm25, alpha=alpha,
        )
        seen = {self._chunk_key(r) for r in results}

        # Rewrite queries → RRF-only (skip CE)，去重追加
        for rq in extra_queries:
            if len(results) >= top_k * 3:
                break
            rq_results = self.search(
                rq, top_k=top_k, expand_context=False,
                skip_reranker=True, w_dense=w_dense, w_bm25=w_bm25,
            )
            for r in rq_results:
                key = self._chunk_key(r)
                if key not in seen:
                    seen.add(key)
                    results.append(r)

        results = results[:top_k]

        if expand_context:
            results = self._expand_results(results)

        return results, results

    def search(self, query: str, top_k: int = 5, expand_context: bool = False,
               skip_reranker: bool = False, w_dense: float = 0.7, w_bm25: float = 0.3,
               alpha: float = 0.2) -> list[dict]:
        """单 query 混合检索（RRF 融合 + CE 精排 + 可选上下文扩展）。"""
        pool_size = max(top_k * 4, 20)
        RRF_K = 60
        rrf_scores: dict[str, float] = {}
        doc_store: dict[str, tuple] = {}  # key → (content, metadata, cosine_sim)

        chroma_raw = self._vectorstore.similarity_search_with_score(query, k=pool_size)
        bm25_docs = self._bm25_retriever.invoke(query)[:pool_size]

        for rank, (doc, l2_dist) in enumerate(chroma_raw):
            key = self._chunk_key(doc)
            rrf_scores[key] = rrf_scores.get(key, 0) + w_dense / (RRF_K + rank)
            if key not in doc_store:
                doc_store[key] = (doc.page_content, doc.metadata, round(1.0 - l2_dist, 4))

        for rank, doc in enumerate(bm25_docs):
            key = self._chunk_key(doc)
            rrf_scores[key] = rrf_scores.get(key, 0) + w_bm25 / (RRF_K + rank)
            if key not in doc_store:
                doc_store[key] = (doc.page_content, doc.metadata, None)

        # BM25-only 补算余弦相似度
        bm25_only = [k for k in doc_store if doc_store[k][2] is None]
        if bm25_only:
            query_emb = np.array(self._embeddings.embed_query(query))
            for key in bm25_only:
                content, metadata, _ = doc_store[key]
                doc_emb = np.array(self._embeddings.embed_documents([content])[0])
                sim = float(np.dot(query_emb, doc_emb)
                            / (np.linalg.norm(query_emb) * np.linalg.norm(doc_emb) + 1e-8))
                doc_store[key] = (content, metadata, round(sim, 4))

        rerank_input = min(top_k * 3, 40)
        sorted_keys = sorted(rrf_scores, key=lambda k: -rrf_scores[k])[:rerank_input]

        if self._reranker and not skip_reranker:
            results = self._rrf_ce_fusion(query, rrf_scores, doc_store,
                                           sorted_keys, top_k, alpha)
        else:
            results = []
            for key in sorted_keys[:top_k]:
                content, metadata, sim = doc_store[key]
                results.append({
                    "content": content, "metadata": metadata,
                    "similarity": sim or 0.0, "dense_similarity": sim or 0.0,
                })

        if expand_context:
            results = self._expand_results(results)

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
