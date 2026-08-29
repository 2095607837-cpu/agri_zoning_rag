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
import threading
from pathlib import Path
from typing import Optional

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


class ThreadSafeEmbeddings:
    """MPS 线程安全包装。MPS 不支持并发推理，所有 embedding 调用串行化。"""

    def __init__(self, embeddings):
        self._embeddings = embeddings
        self._lock = threading.Lock()

    def embed_query(self, text: str):
        with self._lock:
            return self._embeddings.embed_query(text)

    def embed_documents(self, texts: list[str]):
        with self._lock:
            return self._embeddings.embed_documents(texts)

    def __getattr__(self, name):
        return getattr(self._embeddings, name)


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
            print(f"[Reranker] 加载 {self._model_name} (mps)...")
            self._model = CrossEncoder(self._model_name, device="mps")

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
                model_kwargs={"device": "mps"},
                encode_kwargs={"normalize_embeddings": True, "batch_size": 64},
            )

            chunks_future = pool.submit(self._load_chunks)

            raw_embeddings = emb_future.result()
            print(f"[HybridSearcher] Embedding 模型就绪 (mps)")
            self._embeddings = ThreadSafeEmbeddings(raw_embeddings)
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
                        lambda_length: float = 0.1,
                        prior_is_normalized: bool = False) -> list[dict]:
        """CE 精排 + Prior-CE alpha 融合，返回 top_k 结果。

        lambda_length: 长度归一化系数。CE 原始分减去 λ·log(len(content))，
                       抵消 CrossEncoder 天然偏长文本的 bias（长 chunk 有更多关键词）。
        prior_is_normalized: True 时 rrf_scores 已在 [0,1] 范围，跳过 min-max 归一化。
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
            ce_min, ce_max = min(ce_scores), max(ce_scores)
            ce_range = ce_max - ce_min or 1e-8
            final_scores = []
            if prior_is_normalized:
                for i, key in enumerate(candidate_keys):
                    ce_norm = (ce_scores[i] - ce_min) / ce_range
                    final = alpha * rrf_scores[key] + (1 - alpha) * ce_norm
                    final_scores.append((final, i))
            else:
                rrf_vals = [rrf_scores[k] for k in candidate_keys]
                rrf_min, rrf_max = min(rrf_vals), max(rrf_vals)
                rrf_range = rrf_max - rrf_min or 1e-8
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

    def _rrf_retrieve(
        self, query: str, dense_k: int, bm25_k: int,
        w_dense: float = 0.7, w_bm25: float = 0.3,
        keywords: Optional[list[str]] = None,
    ) -> tuple[dict, dict, list]:
        """单查询 Dense+BM25→RRF 融合（不含 CE）。

        keywords: kw-only 场景的关键词，拼入 BM25 query 增强术语召回
        （Dense 与 CE 仍只用原 query）。

        Returns:
            (rrf_scores, doc_store, chroma_raw)
            rrf_scores:  key → rrf_score (已含 single-channel boost)
            doc_store:   key → (content, metadata, cosine_sim)
            chroma_raw:  [(doc, l2_dist), ...] 供 Dense Protect 使用
        """
        RRF_K = 60
        rrf_scores: dict[str, float] = {}
        doc_store: dict[str, tuple] = {}
        dense_keys: set[str] = set()
        bm25_keys: set[str] = set()

        chroma_raw = self._vectorstore.similarity_search_with_score(query, k=dense_k)
        bm25_query = query
        if keywords:
            bm25_query = query + " " + " ".join(kw for kw in keywords if kw)
        bm25_docs = self._bm25_retriever.invoke(bm25_query)[:bm25_k]

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

        # Single-channel boost
        for key in rrf_scores:
            in_dense = key in dense_keys
            in_bm25 = key in bm25_keys
            if in_dense and not in_bm25:
                rrf_scores[key] /= w_dense
            elif in_bm25 and not in_dense:
                rrf_scores[key] /= w_bm25

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

        return rrf_scores, doc_store, chroma_raw

    def search_multi_query(
        self, query: str, top_k: int = 5, expand_context: bool = True,
        rewrite_queries=None, sub_queries=None, keyword_queries=None,
        extra_queries=None,
        alpha: float = 0.3, lambda_length: float = 0.1,
        orig_dense_k: int = 30, orig_bm25_k: int = 20,
        subq_dense_k: int = 20, subq_bm25_k: int = 10,
        max_pool: int = 50,
    ):
        """Union + RRF + Evidence Voting → CE Rerank 架构。

        ① Original: Dense30 + BM25(keyword enhanced)20 → dict自然去重
        ② Rewrite: 每个 Dense20 + BM2510 → dict自然去重
        ③ SubQuery: 每个 Dense20 + BM2510 → dict自然去重
        → Global Merge(chunk_id去重, 无截断)
        → Retrieval Prior(0.7×RRF_norm + 0.3×Evidence_norm) 排序
        → Dynamic Coverage(方案C): Orig=20, Rewrite=10, SubQ预算30
          （每 SubQ 至少 5, 剩余按 retrieval_prior 全局分配）
        → Global Fill 补齐池上限（多 SubQuery 60, 其余 50）
        → CE Rerank（query 用 rw[0], 无 rw 用原 query）→ Top K

        Keywords：rw/sq 存在时作为独立 BM25 通道检索（术语精确召回）；
        kw-only（无 rw/sq）走 plain 路径，kw 拼入 Original BM25 query 增强召回，
        不触发 multi-query 机制（路径切换会改变 RRF 权重组合，搅黄简单事实题）。

        Returns:
            (judge_results, merged): judge_results 为原始查询独立 CE 结果（供 OOD Judge），
            merged 为合并结果（供生成/评估）。
        """
        judge_results, cand_list, rw_list, sq_list = self._collect_candidates(
            query, top_k, expand_context, rewrite_queries, sub_queries,
            keyword_queries, extra_queries, lambda_length,
            orig_dense_k, orig_bm25_k, subq_dense_k, subq_bm25_k)
        if cand_list is None:
            merged = judge_results
            if keyword_queries:
                merged = self.search(query, top_k=top_k, expand_context=expand_context,
                                     alpha=alpha, lambda_length=lambda_length,
                                     keywords=list(keyword_queries))
            return judge_results, merged
        # CE 精排 query 用 rw[0]（标准化改写句，压制碎片 chunk 虚高分，v2.9）；
        # 无 rw 时用原 query
        ce_query = rw_list[0] if rw_list else query
        results = self._coverage_reserve_and_rerank(
            ce_query, cand_list, rw_list, sq_list, top_k, expand_context,
            alpha, lambda_length, max_pool)
        return judge_results, results

    def _collect_candidates(
        self, query: str, top_k: int, expand_context: bool,
        rewrite_queries, sub_queries, keyword_queries, extra_queries,
        lambda_length: float,
        orig_dense_k: int, orig_bm25_k: int,
        subq_dense_k: int, subq_bm25_k: int,
        orig_kw_k: int = None, force_multi: bool = False,
    ):
        """Phase 1+2：多路召回 + Evidence + Retrieval Prior 排序。

        orig_kw_k: BM25-kw 通道 topk（默认=orig_bm25_k，实验用拆分）;
        force_multi: rw/sq 全空也走 multi 路径（实验用，生产 False）。

        Returns: (judge_results, cand_list, rw_list, sq_list)。
        cand_list 已按 retrieval_prior 降序排序（Phase 3 输入）；
        无任何改写输入时返回 (judge_results, None, None, None)。
        """
        # 原始查询独立 CE 结果（供 OOD Judge 判定检索质量）
        judge_results = self.search(query, top_k=top_k, expand_context=expand_context,
                                    lambda_length=lambda_length)

        rw_list = list(rewrite_queries or [])
        sq_list = list(sub_queries or [])
        kw_list = list(keyword_queries or [])
        # backward compat: old callers pass merged list as extra_queries → treat as SubQ
        if not rw_list and not sq_list and extra_queries:
            sq_list = list(extra_queries)
        kw_query = " ".join(kw_list) if kw_list else ""
        if not force_multi and not rw_list and not sq_list:
            # kw-only（无 rw/sq）不触发 multi-query 机制——kw 只做 BM25 增强，
            # 由 search_multi_query 走 plain 路径拼入 BM25（见 search(keywords=...)）
            return judge_results, None, None, None

        # ── Phase 1: 每路 Dense+BM25→RRF → dict-based dedup ──
        RRF_K = 60
        W_DENSE = 0.7
        W_BM25_ORIG = 0.15    # 原始 BM25：弱辅助，保用户真实表达
        W_BM25_KW = 0.3       # Keyword BM25：术语精准召回
        candidates: dict[str, dict] = {}

        def _collect(q: str, dense_k: int, bm25_k: int, qid: int, qtype: str,
                     bm25_keyword_q: str = "", kw_k: int = None):
            """qtype: 'Original' | 'Rewrite' | 'SubQ'
            bm25_keyword_q: 仅 Original 使用，独立 BM25 通道做术语召回"""
            local_rrf: dict[str, float] = {}
            dense_keys: set[str] = set()
            bm25_orig_keys: set[str] = set()
            bm25_kw_keys: set[str] = set()

            # Dense
            chroma_raw = self._vectorstore.similarity_search_with_score(q, k=dense_k)
            for rank, (doc, l2_dist) in enumerate(chroma_raw):
                cid = self._chunk_key(doc)
                dense_keys.add(cid)
                rrf = W_DENSE / (RRF_K + rank)
                local_rrf[cid] = local_rrf.get(cid, 0) + rrf
                sim = round(1.0 - l2_dist, 4)

                if cid not in candidates:
                    candidates[cid] = {
                        "chunk_id": cid, "text": doc.page_content,
                        "metadata": doc.metadata,
                        "sources": [], "query_hits": set(),
                        "dense_rank": rank, "bm25_rank": 999, "bm25_kw_rank": 999,
                        "best_channel": "Dense", "best_rank": rank,
                        "rrf_prior": 0.0, "cosine_sim": sim,
                    }
                else:
                    if rank < candidates[cid]["dense_rank"]:
                        candidates[cid]["dense_rank"] = rank
                    candidates[cid]["cosine_sim"] = max(candidates[cid]["cosine_sim"], sim)
                candidates[cid]["sources"].append(f"{qtype}-Dense")
                candidates[cid]["query_hits"].add(qid)

            # BM25 Route 1: Original query — 保留用户真实表达，弱辅助
            bm25_orig_docs = self._bm25_retriever.invoke(q)[:bm25_k]
            for rank, doc in enumerate(bm25_orig_docs):
                cid = self._chunk_key(doc)
                bm25_orig_keys.add(cid)
                rrf = W_BM25_ORIG / (RRF_K + rank)
                local_rrf[cid] = local_rrf.get(cid, 0) + rrf

                if cid not in candidates:
                    candidates[cid] = {
                        "chunk_id": cid, "text": doc.page_content,
                        "metadata": doc.metadata,
                        "sources": [], "query_hits": set(),
                        "dense_rank": 999, "bm25_rank": rank, "bm25_kw_rank": 999,
                        "best_channel": "BM25", "best_rank": rank,
                        "rrf_prior": 0.0, "cosine_sim": 0.0,
                    }
                else:
                    if rank < candidates[cid]["bm25_rank"]:
                        candidates[cid]["bm25_rank"] = rank
                candidates[cid]["sources"].append(f"{qtype}-BM25o")
                candidates[cid]["query_hits"].add(qid)

            # BM25 Route 2: Keyword query — 术语精准召回，仅 Original 使用
            if bm25_keyword_q:
                bm25_kw_docs = self._bm25_retriever.invoke(bm25_keyword_q)[
                    :(kw_k if kw_k is not None else bm25_k)]
                for rank, doc in enumerate(bm25_kw_docs):
                    cid = self._chunk_key(doc)
                    bm25_kw_keys.add(cid)
                    rrf = W_BM25_KW / (RRF_K + rank)
                    local_rrf[cid] = local_rrf.get(cid, 0) + rrf

                    if cid not in candidates:
                        candidates[cid] = {
                            "chunk_id": cid, "text": doc.page_content,
                            "metadata": doc.metadata,
                            "sources": [], "query_hits": set(),
                            "dense_rank": 999, "bm25_rank": 999, "bm25_kw_rank": rank,
                            "best_channel": "BM25-KW", "best_rank": rank,
                            "rrf_prior": 0.0, "cosine_sim": 0.0,
                        }
                    else:
                        if rank < candidates[cid].get("bm25_kw_rank", 999):
                            candidates[cid]["bm25_kw_rank"] = rank
                    candidates[cid]["sources"].append(f"{qtype}-BM25kw")
                    candidates[cid]["query_hits"].add(qid)

            # Single-channel boost: 多通道归一化，避免多通道命中天然占优
            for cid in local_rrf:
                total_w = 0.0
                if cid in dense_keys:   total_w += W_DENSE
                if cid in bm25_orig_keys: total_w += W_BM25_ORIG
                if cid in bm25_kw_keys:   total_w += W_BM25_KW
                if total_w > 0:
                    local_rrf[cid] /= total_w

            # 更新 rrf_prior（取多路中最大值供检索排序用）
            for cid, score in local_rrf.items():
                if score > candidates[cid]["rrf_prior"]:
                    candidates[cid]["rrf_prior"] = round(score, 6)

        # 收集各路查询
        # ① Original: Dense30 + BM25(original)20 + BM25(keyword)20
        _collect(query, orig_dense_k, orig_bm25_k, 0, "Original",
                 bm25_keyword_q=kw_query, kw_k=orig_kw_k)
        qid = 1
        # ② Rewrite: Dense20 + BM2510 每个
        for rq in rw_list:
            _collect(rq, subq_dense_k, subq_bm25_k, qid, "Rewrite")
            qid += 1
        # ③ SubQuery: Dense20 + BM2510 每个
        for sq in sq_list:
            _collect(sq, subq_dense_k, subq_bm25_k, qid, "SubQ")
            qid += 1

        # 补算 best_channel / best_rank（跨多路后取最优，KW > Dense > BM25）
        for c in candidates.values():
            kw_r = c.get("bm25_kw_rank", 999)
            if kw_r < c["dense_rank"] and kw_r < c["bm25_rank"]:
                c["best_channel"] = "BM25-KW"
                c["best_rank"] = kw_r
            elif c["dense_rank"] < c["bm25_rank"]:
                c["best_channel"] = "Dense"
                c["best_rank"] = c["dense_rank"]
            else:
                c["best_channel"] = "BM25"
                c["best_rank"] = c["bm25_rank"]

        # 补算 BM25-only 候选的余弦相似度
        bm25_only = [(cid, c) for cid, c in candidates.items() if c["cosine_sim"] == 0.0
                      and c["dense_rank"] == 999]
        if bm25_only:
            query_emb = np.array(self._embeddings.embed_query(query))
            for cid, c in bm25_only:
                doc_emb = np.array(self._embeddings.embed_documents([c["text"]])[0])
                sim = float(np.dot(query_emb, doc_emb)
                            / (np.linalg.norm(query_emb) * np.linalg.norm(doc_emb) + 1e-8))
                candidates[cid]["cosine_sim"] = round(sim, 4)

        # ── Phase 2: Evidence Score + Retrieval Prior ──
        delta = 0.002  # evidence 基础单位
        cand_list = list(candidates.values())
        for c in cand_list:
            n = len(c["query_hits"])
            if n >= 3:      c["evidence_score"] = 2 * delta   # 0.004
            elif n >= 2:    c["evidence_score"] = delta       # 0.002
            else:           c["evidence_score"] = 0.0

        # Retrieval Prior = 0.7×rrf_prior_norm + 0.3×evidence_norm
        if len(cand_list) > 1:
            rrf_vals = [c["rrf_prior"] for c in cand_list]
            ev_vals = [c["evidence_score"] for c in cand_list]
            rrf_min, rrf_max = min(rrf_vals), max(rrf_vals)
            ev_min, ev_max = min(ev_vals), max(ev_vals)
            rrf_range = rrf_max - rrf_min or 1e-8
            ev_range = ev_max - ev_min or 1e-8
            for c in cand_list:
                c["retrieval_prior"] = round(
                    0.7 * (c["rrf_prior"] - rrf_min) / rrf_range
                    + 0.3 * (c["evidence_score"] - ev_min) / ev_range, 6)
        else:
            cand_list[0]["retrieval_prior"] = 0.0

        cand_list.sort(key=lambda c: -c["retrieval_prior"])

        return judge_results, cand_list, rw_list, sq_list

    def _coverage_reserve_and_rerank(
        self, query: str, cand_list: list, rw_list: list, sq_list: list,
        top_k: int, expand_context: bool, alpha: float, lambda_length: float,
        max_pool: int = 50,
    ):
        """Phase 3+4：动态配额（方案C）+ Global Fill 补齐池上限 + CE 精排融合。

        query 为 CE 精排所用 query（生产传 rw[0]，无 rw 时传原 query，见 search_multi_query）。
        """

        # ── Phase 3: Dynamic Coverage Reservation (方案C: 动态预算 + 最低保护) ──
        # 多个 SubQuery(≥2) 池上限扩到 60, 其余保持 max_pool(50)
        cap = max_pool + 10 if len(sq_list) >= 2 else max_pool
        if len(cand_list) > cap:
            n_rw = len(rw_list)
            n_sq = len(sq_list)
            rw_qids = set(range(1, 1 + n_rw))
            sq_start = 1 + n_rw     # first SubQ qid

            quota_orig = 20
            quota_rw = 10 if n_rw > 0 else 0
            sub_budget = 30 if n_sq >= 2 else (20 if n_sq == 1 else 0)

            # 每 SubQ 最低保护 5（多 SubQuery 时 SubQ 总预算 30）
            sq_quotas: dict[int, int] = {}
            if n_sq > 0:
                min_q = min(5, sub_budget)
                for i in range(n_sq):
                    sq_quotas[sq_start + i] = min_q

            reserved: list[dict] = []
            rest: list[dict] = []
            taken: dict[int, int] = {}
            rw_taken = 0

            for c in cand_list:
                placed = False
                # 1) Original: 20
                if 0 in c["query_hits"] and taken.get(0, 0) < quota_orig:
                    reserved.append(c)
                    taken[0] = taken.get(0, 0) + 1
                    placed = True
                # 2) 各 SubQ 最低保护
                elif not placed:
                    for sq_qid, sq_quota in sq_quotas.items():
                        if sq_qid in c["query_hits"] and taken.get(sq_qid, 0) < sq_quota:
                            reserved.append(c)
                            taken[sq_qid] = taken.get(sq_qid, 0) + 1
                            placed = True
                            break
                # 3) Rewrite 组: 10 共享
                if not placed and rw_qids and rw_taken < quota_rw and (rw_qids & c["query_hits"]):
                    reserved.append(c)
                    rw_taken += 1
                    placed = True

                if not placed:
                    rest.append(c)

            # SubQ 剩余额度按 retrieval_prior 全局分配（含未填满的最低保护）
            if n_sq > 0:
                sq_need = sub_budget - sum(taken.get(q, 0) for q in sq_quotas)
                if sq_need > 0:
                    new_rest = []
                    for c in rest:
                        if sq_need > 0 and any(q in c["query_hits"] for q in sq_quotas):
                            reserved.append(c)
                            sq_need -= 1
                        else:
                            new_rest.append(c)
                    rest = new_rest

            # Global Fill: 补齐到 cap
            cand_list = reserved + rest
            cand_list = cand_list[:cap]

        # ── Phase 4: CE Rerank + Retrieval Prior 融合 ──
        # Final = 0.7×CE_norm + 0.3×retrieval_prior_norm
        candidate_keys = [c["chunk_id"] for c in cand_list]
        doc_store = {c["chunk_id"]: (c["text"], c["metadata"], c["cosine_sim"])
                     for c in cand_list}
        prior_scores = {c["chunk_id"]: c["retrieval_prior"] for c in cand_list}

        if self._reranker:
            results = self._rrf_ce_fusion(query, prior_scores, doc_store,
                                           candidate_keys, top_k, alpha, lambda_length,
                                           prior_is_normalized=True)
        else:
            results = []
            scored = []
            for c in cand_list:
                s = 0.7 * c["cosine_sim"] + 0.3 * c["retrieval_prior"]
                scored.append((s, c["text"], c["metadata"], c["cosine_sim"]))
            scored.sort(key=lambda x: -x[0])
            for s, text, meta, sim in scored[:top_k]:
                results.append({
                    "content": text, "metadata": meta,
                    "similarity": round(s, 4), "dense_similarity": sim,
                })

        if expand_context:
            results = self._expand_results(results)

        return results

    def search(self, query: str, top_k: int = 5, expand_context: bool = False,
               skip_reranker: bool = False, w_dense: float = 0.7, w_bm25: float = 0.3,
               alpha: float = 0.3, lambda_length: float = 0.1,
               ce_protect_keys: Optional[list[str]] = None,
               keywords: Optional[list[str]] = None) -> list[dict]:
        """单 query 混合检索（RRF 融合 + CE 精排 + 可选上下文扩展）。

        keywords: kw-only 场景的关键词，拼入 BM25 query 增强术语召回。
        """
        pool_size = max(top_k * 4, 20)

        rrf_scores, doc_store, chroma_raw = self._rrf_retrieve(
            query, dense_k=pool_size, bm25_k=pool_size, keywords=keywords)

        # CE 候选池：RRF top-30 ∪ Dense top-5（去重）
        rrf_top30 = sorted(rrf_scores, key=lambda k: -rrf_scores[k])[:30]
        rrf_top30_set = set(rrf_top30)
        dense_top5_keys = []
        for doc, _ in chroma_raw[:5]:
            key = self._chunk_key(doc)
            if key not in rrf_top30_set and key not in dense_top5_keys:
                dense_top5_keys.append(key)
        sorted_keys = rrf_top30 + dense_top5_keys

        if ce_protect_keys:
            existing = set(sorted_keys)
            for key in ce_protect_keys:
                if key in doc_store and key not in existing:
                    sorted_keys.append(key)
                    existing.add(key)

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
