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

        self._bm25_retriever = BM25Retriever.from_documents(
            bm25_docs, preprocess_func=bm25_tokenize
        )
        self._bm25_retriever.k = 20

        if self.enable_reranker:
            self._reranker = Reranker()

        print(f"[HybridSearcher] 就绪 (docs={len(bm25_docs)}, sections={len(self._section_index)}, "
              f"reranker={'on' if self.enable_reranker else 'off'})")

    def search_multi_query(
        self, query: str, top_k: int = 5, expand_context: bool = True,
        extra_queries=None, max_workers: int = 6,
    ):
        """原始检索 + 可选多路子查询并发检索 + 按 content 前 80 字符去重。

        Args:
            query: 原始查询（用于 Judge OOD 判定）
            top_k: 返回数量
            expand_context: 是否展开同 section 上下文
            extra_queries: 改写后的额外查询列表，None 表示不启用多路检索
            max_workers: 子查询并发上限

        Returns:
            (judge_results, merged_results)
            - judge_results: 原始 query 的 top_k 结果
            - merged_results: 多路合并去重后的 top_k 结果
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        judge_results = self.search(query, top_k=top_k, expand_context=expand_context)

        if not extra_queries:
            return judge_results[:top_k], judge_results[:top_k]

        # 并发检索各路改写查询
        rw_pool = list(judge_results)
        workers = min(len(extra_queries), max_workers)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(self.search, sq, top_k, expand_context, True): sq
                for sq in extra_queries
            }
            for f in as_completed(futures):
                rw_pool.extend(f.result())

        # 按 content 前 80 字去重；原查询结果排前，改写仅补充中尾部 gap
        seen_keys = set()
        merged = []
        for r in list(judge_results) + rw_pool:
            key = r["content"][:80]
            if key not in seen_keys:
                seen_keys.add(key)
                merged.append(r)

        return judge_results[:top_k], merged[:top_k]

    def search(self, query: str, top_k: int = 5, expand_context: bool = False,
               skip_reranker: bool = False, w_dense: float = 0.7, w_bm25: float = 0.3,
               alpha: float = 0.2) -> list[dict]:
        """混合检索（RRF 融合）+ CrossEncoder 精排 + 可选同 section 上下文扩展。

        expand_context=True 时：对每个命中 chunk，取同 section 内前1后1相邻 chunk，
        拼接为检索结果（固定窗口，而非展开完整父文档）。
        """
        pool_size = max(top_k * 4, 20)  # 召回池，top_k=10 时 pool=40

        # 1. Chroma 语义检索（带真实余弦距离）
        chroma_raw = self._vectorstore.similarity_search_with_score(query, k=pool_size)

        # 2. BM25 关键词检索
        bm25_docs = self._bm25_retriever.invoke(query)[:pool_size]

        # 3. RRF 加权融合
        RRF_K = 60
        rrf_scores: dict[str, float] = {}
        doc_store: dict[str, tuple] = {}  # key → (content, metadata, cosine_sim)

        for rank, (doc, l2_dist) in enumerate(chroma_raw):
            key = doc.page_content[:80]
            rrf_scores[key] = rrf_scores.get(key, 0) + w_dense / (RRF_K + rank)
            sim = round(1.0 - l2_dist, 4)
            if key not in doc_store:
                doc_store[key] = (doc.page_content, doc.metadata, sim)

        for rank, doc in enumerate(bm25_docs):
            key = doc.page_content[:80]
            rrf_scores[key] = rrf_scores.get(key, 0) + w_bm25 / (RRF_K + rank)
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
        if self._reranker and not skip_reranker:
            rerank_input = min(top_k * 3, 40)  # 精排候选数，top_k=10 时 CE=30
            candidates = []
            candidate_keys = []
            for key in sorted(rrf_scores, key=lambda k: -rrf_scores[k])[:rerank_input]:
                content, metadata, cosine_sim = doc_store[key]
                candidates.append({
                    "content": content,
                    "metadata": metadata,
                    "dense_similarity": cosine_sim or 0.0,
                })
                candidate_keys.append(key)

            # CE 精排 + 可选 RRF 分数融合
            self._reranker._load()
            ce_pairs = [(query, c["content"][:500]) for c in candidates]
            with self._reranker._infer_lock:
                ce_scores = [float(x) for x in self._reranker._model.predict(
                    ce_pairs, show_progress_bar=False)]

            if alpha > 0.0:
                # Min-max normalize both to [0,1]
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
                for _, idx in final_scores[:top_k]:
                    c = candidates[idx]
                    results.append({
                        "content": c["content"],
                        "metadata": c["metadata"],
                        "similarity": round(final_scores[idx][0], 4) if idx < len(final_scores) else ce_scores[idx],
                        "dense_similarity": c["dense_similarity"],
                    })
            else:
                # Pure CE (alpha=0): delegate to Reranker.rerank
                ranked = []
                for i, c in enumerate(candidates):
                    ranked.append((ce_scores[i], c))
                ranked.sort(key=lambda x: -x[0])
                results = []
                for score, c in ranked[:top_k]:
                    results.append({
                        "content": c["content"],
                        "metadata": c["metadata"],
                        "similarity": round(float(score), 4),
                        "dense_similarity": c["dense_similarity"],
                    })
        else:
            # RRF only
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
