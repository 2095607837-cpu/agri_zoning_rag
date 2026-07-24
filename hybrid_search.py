"""
混合检索引擎（LangChain 实现）

Dense + BM25 → RRF 融合 → CrossEncoder 精排 → top-k。

用法:
  searcher = HybridSearcher()
  results = searcher.search("大豆冷害区划指标", top_k=5)
"""

import math
import os
import json
import re
from pathlib import Path

from langchain.schema import Document
from langchain_community.retrievers import BM25Retriever
from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
CHUNKS_PATH = BASE_DIR / "data" / "chunks_split.json"
CHUNKS_RAW_PATH = BASE_DIR / "data" / "chunks.json"  # fallback: step2 未运行时的原始数据
PERSIST_DIR = str(BASE_DIR / "vectordb")
COLLECTION_NAME = "agri_zoning"
EMBED_MODEL = "BAAI/bge-small-zh-v1.5"

# 专业词正则：保留缩写、符号+数值、单位、希腊字母作为完整 BM25 token
_TECH_TOKEN_RE = re.compile(
    r'[A-Z]{2,}\d*'                              # FCV, GIS, AHP, ET0, DEM
    r'|[A-Z][a-z]+\d*'                            # Py, Rn, Kc
    r'|[Δ∇∂αβγ][A-Z]?\d*(?:[-–]\d*)?'       # ΔT5-9, ΔT
    r'|[≥≤<>＜＞][-]?\d+(?:\.\d+)?[℃°C%天月年hd]?'  # ≥10℃, ≤5500
    r'|\d+(?:\.\d+)?[~～\-]\d+(?:\.\d+)?[℃°C%天月年hd]?'  # 5-9月, 8.5~12.5
    r'|\d+(?:\.\d+)?[℃°C%hm²kmhd]{1,3}'          # 10℃, 30%, hm², 20h
)


def bm25_tokenize(text: str) -> list[str]:
    """中文 BM25 分词：单字 + 双字 bigram + 专业词正则保留。

    langchain BM25Retriever 默认 `text.split()` 对中文失效。单字+bigram 解决
    中文切分问题；正则保留 FCV/ΔT5-9/≥10℃ 等专业词，避免被拆成无意义单字。
    """
    text = text.replace("\n", " ").strip()

    # 提取专业 token
    tech_tokens = [t for t in _TECH_TOKEN_RE.findall(text) if len(t) > 1]

    # 单字 + bigram
    tokens = [ch for ch in text if not ch.isspace()]
    for i in range(len(text) - 1):
        bigram = text[i:i + 2]
        if not bigram[0].isspace() and not bigram[1].isspace():
            tokens.append(bigram)

    # 追加专业 token（去重）
    for t in tech_tokens:
        if t not in tokens:
            tokens.append(t)

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
        from concurrent.futures import ThreadPoolExecutor

        print(f"[HybridSearcher] 初始化 (model={EMBED_MODEL})...")

        # 并行：加载 embedding 模型 + 读取 chunks 构建 Section 索引
        with ThreadPoolExecutor(max_workers=2) as pool:
            emb_future = pool.submit(
                HuggingFaceEmbeddings,
                model_name=EMBED_MODEL,
                model_kwargs={"device": "cpu"},
                encode_kwargs={"normalize_embeddings": True},
            )

            chunks_future = pool.submit(self._load_chunks)

            self._embeddings = emb_future.result()
            print(f"[HybridSearcher] Embedding 模型就绪")
            self._section_index, bm25_docs, self._chunk_id_map = chunks_future.result()
            print(f"[HybridSearcher] BM25 数据就绪 ({len(bm25_docs)} docs)")

        self._vectorstore = Chroma(
            collection_name=COLLECTION_NAME,
            embedding_function=self._embeddings,
            persist_directory=PERSIST_DIR,
        )

        self._bm25_retriever = BM25Retriever.from_documents(
            bm25_docs, preprocess_func=bm25_tokenize
        )
        self._bm25_retriever.k = 20

        if self.enable_reranker:
            self._reranker = Reranker()

        print(f"[HybridSearcher] 就绪 (docs={len(bm25_docs)}, sections={len(self._section_index)}, "
              f"reranker={'on' if self.enable_reranker else 'off'})")

    @staticmethod
    def _load_chunks() -> tuple[dict, list, dict]:
        """加载 chunks 并构建 section_index + chunk_id_map（与 embedding 模型加载并行）。"""
        actual_path = CHUNKS_PATH if CHUNKS_PATH.exists() else CHUNKS_RAW_PATH
        with open(actual_path, encoding="utf-8") as f:
            chunks = json.load(f)

        from collections import defaultdict
        section_index: dict[str, list[dict]] = defaultdict(list)
        chunk_id_map: dict[str, str] = {}
        bm25_docs = []

        for c in chunks:
            content = c["content"]
            meta = dict(c["metadata"])
            cid = c["id"]
            meta["chunk_id"] = cid
            chunk_id_map[content[:80]] = cid

            sid = meta.get("section_id", "")
            if sid:
                idx = len(section_index[sid])
                meta["chunk_index"] = idx
                section_index[sid].append({
                    "content": content,
                    "metadata": meta,
                    "chunk_index": idx,
                })

            bm25_docs.append(Document(page_content=content, metadata=meta))

        for sid in section_index:
            section_index[sid].sort(key=lambda x: x["chunk_index"])

        return dict(section_index), bm25_docs, chunk_id_map

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
                        candidate_keys: list[str], top_k: int, alpha: float,
                        lambda_length: float = 0.1) -> list[dict]:
        """CE 精排 + RRF-CE alpha 融合，返回 top_k 结果。

        lambda_length: 长度归一化系数。CE 原始分减去 λ·log(len(content))，
                       抵消 CrossEncoder 天然偏长文本的 bias（长 chunk 有更多关键词）。
        """
        candidates = []
        for key in candidate_keys:
            content, metadata, cosine_sim = doc_store[key]
            candidates.append({
                "content": content, "metadata": metadata,
                "dense_similarity": cosine_sim or 0.0,
                "_len": len(content),
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
            raw_ce = [float(x) for x in self._reranker._model.predict(
                ce_pairs, show_progress_bar=False)]

        # 长度归一化：抵消 CE 长文本偏好 bias
        ce_scores = [raw - lambda_length * math.log(c["_len"])
                      for raw, c in zip(raw_ce, candidates)]

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
        alpha: float = 0.2, lambda_length: float = 0.1,
        dense_protect_k: int = 5,
    ):
        """Dense Protected Merge + Append 架构。

        Dense top-K → 直接保序（避免 BM25 干扰 Dense 高置信结果）
        RRF+CE    → 去重追加（高质量 CE 信号，排 Dense 保护之后）
        Rewrite   → RRF-only 去重追加（不干扰前面排序）

        子查询跳过 CE 避免了噪声干扰，同时保留改写召回的高价值 chunk。
        """
        if not extra_queries:
            results = self.search(query, top_k=top_k, expand_context=expand_context,
                                  lambda_length=lambda_length)
            return results, results

        # Phase 1: Dense Protected — 直接从 Chroma 取 top-K，保序
        dense_raw = self._vectorstore.similarity_search_with_score(query, k=dense_protect_k)
        protected = []
        for doc, l2_dist in dense_raw:
            protected.append({
                "content": doc.page_content,
                "metadata": doc.metadata,
                "similarity": round(1.0 - l2_dist, 4),
                "dense_similarity": round(1.0 - l2_dist, 4),
            })
        seen = {self._chunk_key(r) for r in protected}
        final = list(protected)

        # Phase 2: Original query → 完整 RRF+CE 管线，去重追加
        results = self.search(
            query, top_k=top_k, expand_context=False,
            w_dense=w_dense, w_bm25=w_bm25, alpha=alpha, lambda_length=lambda_length,
        )
        for r in results:
            key = self._chunk_key(r)
            if key not in seen:
                seen.add(key)
                final.append(r)

        # Phase 3: Rewrite queries → RRF-only (skip CE)，去重追加
        for rq in extra_queries:
            if len(final) >= top_k * 3:
                break
            rq_results = self.search(
                rq, top_k=top_k, expand_context=False,
                skip_reranker=True, w_dense=w_dense, w_bm25=w_bm25,
            )
            for r in rq_results:
                key = self._chunk_key(r)
                if key not in seen:
                    seen.add(key)
                    final.append(r)

        final = final[:top_k]

        if expand_context:
            final = self._expand_results(final)

        return final, final

    def search(self, query: str, top_k: int = 5, expand_context: bool = False,
               skip_reranker: bool = False, w_dense: float = 0.7, w_bm25: float = 0.3,
               alpha: float = 0.2, lambda_length: float = 0.1) -> list[dict]:
        """单 query 混合检索（RRF 融合 + CE 精排 + 可选上下文扩展）。"""
        pool_size = max(top_k * 4, 20)
        RRF_K = 60
        rrf_scores: dict[str, float] = {}
        doc_store: dict[str, tuple] = {}  # key → (content, metadata, cosine_sim)
        dense_keys: set[str] = set()
        bm25_keys: set[str] = set()

        chroma_raw = self._vectorstore.similarity_search_with_score(query, k=pool_size)
        bm25_docs = self._bm25_retriever.invoke(query)[:pool_size]

        for rank, (doc, l2_dist) in enumerate(chroma_raw):
            key = self._chunk_key(doc)
            dense_keys.add(key)
            rrf_scores[key] = rrf_scores.get(key, 0) + w_dense / (RRF_K + rank)
            if key not in doc_store:
                doc_store[key] = (doc.page_content, doc.metadata, round(1.0 - l2_dist, 4))

        for rank, doc in enumerate(bm25_docs):
            key = self._chunk_key(doc)
            bm25_keys.add(key)
            rrf_scores[key] = rrf_scores.get(key, 0) + w_bm25 / (RRF_K + rank)
            if key not in doc_store:
                doc_store[key] = (doc.page_content, doc.metadata, None)

        # Single-channel boost: compensate missing channel weight
        for key in rrf_scores:
            in_dense = key in dense_keys
            in_bm25 = key in bm25_keys
            if in_dense and not in_bm25:
                rrf_scores[key] = rrf_scores[key] / w_dense
            elif in_bm25 and not in_dense:
                rrf_scores[key] = rrf_scores[key] / w_bm25

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

        # CE 候选池：RRF top-30 ∪ Dense top-5（去重）
        # Dense top-5 直接保送，不受 BM25 噪声稀释影响
        rrf_top30 = sorted(rrf_scores, key=lambda k: -rrf_scores[k])[:30]
        rrf_top30_set = set(rrf_top30)
        dense_top5_keys = []
        for doc, _ in chroma_raw[:5]:
            key = self._chunk_key(doc)
            if key not in rrf_top30_set and key not in dense_top5_keys:
                dense_top5_keys.append(key)
        sorted_keys = rrf_top30 + dense_top5_keys

        if self._reranker and not skip_reranker:
            results = self._rrf_ce_fusion(query, rrf_scores, doc_store,
                                           sorted_keys, top_k, alpha, lambda_length)
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
