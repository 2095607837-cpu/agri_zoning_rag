#!/usr/bin/env python3
"""Oracle Rank 可分性实验：180 Query × 791 Chunk 全量余弦诊断。

回答：BGE 表示空间本身能不能把 Query 和 Gold Chunk 排在一起？

指标：
  - gold_cos         原始 query 与 gold chunk 余弦（多 gold 取最大）
  - oracle_rank      gold 在 791 chunk 全量余弦排序下的排名（1-based，多 gold 取最小）
  - percentile       oracle_rank / N
  - sibling_margin   gold_cos − 同 section 兄弟 chunk 最强余弦（排除全部 gold）
  - global_margin    gold_cos − 全部非 gold chunk 最强余弦
  - dense_rank       gold 在线上 Dense top-30 的排名（Chroma cosine 距离序）
  - bm25_rank        gold 在线上 BM25 top-20 的排名
  - rrf_rank         gold 在线上 RRF 融合池的排名
  - rw_oracle_rank   改写 query（rewrite_cache.json）的最优 oracle rank

不改任何线上代码；只读生产索引 vectordb/agri_zoning。

用法: python3 eval_oracle_rank.py
"""
import json
import time
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
GOLDEN = DATA / "golden_set_v2.json"
CHUNKS_SPLIT = DATA / "chunks_split.json"
REWRITE_CACHE = DATA / "rewrite_cache.json"
CK_AB_RESULTS = BASE / "eval_ck_ab_results.json"
OUT = DATA / "eval_oracle_rank_results.json"

EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
DENSE_K = 30
BM25_K = 20


def load_golden() -> list[dict]:
    gs = json.load(open(GOLDEN, encoding="utf-8"))
    if isinstance(gs, dict):
        gs = gs.get("questions", gs)
    in_domain = [q for q in gs if q.get("capability") != "ood_detection"]
    return in_domain


def load_index():
    """从生产 Chroma 索引读取全部向量（保证与线上检索同源）。"""
    import chromadb
    client = chromadb.PersistentClient(path=str(BASE / "vectordb"))
    col = client.get_collection("agri_zoning")
    d = col.get(include=["embeddings", "metadatas"])
    chunk_ids, vecs = [], []
    for meta, emb in zip(d["metadatas"], d["embeddings"]):
        chunk_ids.append(meta.get("chunk_id"))
        vecs.append(np.asarray(emb, dtype=np.float32))
    M = np.stack(vecs)
    M /= np.linalg.norm(M, axis=1, keepdims=True) + 1e-12
    print(f"[index] 生产索引向量: {M.shape[0]} × {M.shape[1]}")
    return chunk_ids, M


def load_section_map():
    chunks = json.load(open(CHUNKS_SPLIT, encoding="utf-8"))
    return {c["id"]: c["metadata"].get("section_id", "") for c in chunks}


def load_still_fail():
    if not CK_AB_RESULTS.exists():
        return set()
    d = json.load(open(CK_AB_RESULTS, encoding="utf-8"))
    return set((d.get("summary", {}) or {}).get("rescue", {}).get("still_fail", []))


def get_rewrite_texts(raw_query: str, cache: dict) -> list[str]:
    for suffix in ("|ck", "|base"):
        entry = cache.get(raw_query + suffix)
        if entry:
            texts = list(entry.get("rewrite_queries") or []) + \
                    list(entry.get("sub_queries") or [])
            return [t for t in texts if t and t.strip()]
    return []


def main():
    t0 = time.time()
    questions = load_golden()
    print(f"[golden] in-domain 题数: {len(questions)}")

    chunk_ids, M = load_index()
    sec_map = load_section_map()
    still_fail = load_still_fail()

    # 同 section 分组（仅含索引内 chunk）
    siblings_by_section: dict[str, set[str]] = {}
    for cid in chunk_ids:
        siblings_by_section.setdefault(sec_map.get(cid, ""), set()).add(cid)

    id2pos = {cid: i for i, cid in enumerate(chunk_ids)}
    N = len(chunk_ids)

    # 索引与 gold 标注一致性检查
    missing_gold = 0
    for q in questions:
        golds = [g for g in (q.get("gold_chunks") or []) if g in id2pos]
        if not golds:
            missing_gold += 1
    print(f"[check] gold 完全不在索引内的题数: {missing_gold} / {len(questions)}")

    # 加载改写缓存
    rw_cache = json.load(open(REWRITE_CACHE, encoding="utf-8")) \
        if REWRITE_CACHE.exists() else {}

    # 查询 embedding（原始 query）
    from langchain_huggingface import HuggingFaceEmbeddings
    emb = HuggingFaceEmbeddings(
        model_name=EMBED_MODEL,
        model_kwargs={"device": "mps"},
        encode_kwargs={"normalize_embeddings": True, "batch_size": 64},
    )
    raw_queries = [q["question"] for q in questions]
    print(f"[embed] 嵌入 {len(raw_queries)} 个原始 query ...")
    Q = np.stack([np.asarray(v, dtype=np.float32) for v in emb.embed_documents(raw_queries)])
    Q /= np.linalg.norm(Q, axis=1, keepdims=True) + 1e-12
    COS = Q @ M.T  # (180, 791)

    # 改写 query embedding（批量）
    rw_texts_per_q = [get_rewrite_texts(q["question"], rw_cache) for q in questions]
    all_rw_texts = [t for texts in rw_texts_per_q for t in texts]
    RW_COS = None
    if all_rw_texts:
        print(f"[embed] 嵌入 {len(all_rw_texts)} 个改写 query ...")
        RW = np.stack([np.asarray(v, dtype=np.float32) for v in emb.embed_documents(all_rw_texts)])
        RW /= np.linalg.norm(RW, axis=1, keepdims=True) + 1e-12
        RW_COS = RW @ M.T

    # 线上管线（Dense/BM25/RRF 排名）
    print("[pipeline] 初始化 HybridSearcher (reranker off) ...")
    from hybrid_search import HybridSearcher
    searcher = HybridSearcher(enable_reranker=False)

    records = []
    for qi, q in enumerate(questions):
        qid = q.get("id", f"Q{qi}")
        raw = q["question"]
        golds = [g for g in (q.get("gold_chunks") or []) if g in id2pos]

        rec = {
            "qid": qid, "query": raw, "capability": q.get("capability"),
            "difficulty": q.get("difficulty"), "gold_chunks": golds,
            "is_still_fail": qid in still_fail,
        }
        if not golds:
            rec["error"] = "gold_not_in_index"
            records.append(rec)
            continue

        cos_row = COS[qi]
        gold_positions = [id2pos[g] for g in golds]
        gold_cos = float(max(cos_row[p] for p in gold_positions))
        best_gold = golds[int(np.argmax([cos_row[p] for p in gold_positions]))]

        # oracle rank / percentile
        order = np.argsort(-cos_row)
        rank_of = {cid: int(np.where(order == id2pos[cid])[0][0]) + 1 for cid in [g for g in golds]}
        oracle_rank = min(rank_of.values())
        percentile = oracle_rank / N

        # hard negatives
        gold_set = set(golds)
        sibling_cands = siblings_by_section.get(sec_map.get(best_gold, ""), set()) - gold_set
        sibling_cos = [cos_row[id2pos[c]] for c in sibling_cands if c in id2pos]
        non_gold_mask = np.ones(N, dtype=bool)
        for p in gold_positions:
            non_gold_mask[p] = False
        global_neg_cos = float(cos_row[non_gold_mask].max()) if non_gold_mask.any() else None
        sibling_neg_cos = float(max(sibling_cos)) if sibling_cos else None
        sibling_margin = gold_cos - sibling_neg_cos if sibling_neg_cos is not None else None
        global_margin = gold_cos - global_neg_cos if global_neg_cos is not None else None

        rec.update({
            "gold_cos": round(gold_cos, 4), "best_gold": best_gold,
            "oracle_rank": oracle_rank, "percentile": round(percentile, 4),
            "sibling_margin": round(sibling_margin, 4) if sibling_margin is not None else None,
            "global_margin": round(global_margin, 4) if global_margin is not None else None,
        })

        # 改写 query 最优 oracle rank
        texts = rw_texts_per_q[qi]
        if texts and RW_COS is not None:
            start = sum(len(t) for t in rw_texts_per_q[:qi])
            best_rw_rank, best_rw_text = None, None
            for j, t in enumerate(texts):
                rw_cos = RW_COS[start + j]
                rw_order = np.argsort(-rw_cos)
                rw_rank = min(int(np.where(rw_order == p)[0][0]) + 1 for p in gold_positions)
                if best_rw_rank is None or rw_rank < best_rw_rank:
                    best_rw_rank, best_rw_text = rw_rank, t
            rec["rw_oracle_rank"] = best_rw_rank
            rec["rw_best_text"] = best_rw_text
        else:
            rec["rw_oracle_rank"] = None

        # 线上管线排名
        try:
            rrf_scores, doc_store, chroma_raw = searcher._rrf_retrieve(raw, DENSE_K, BM25_K)
            dense_order = [d.metadata.get("chunk_id") for d, _ in chroma_raw]
            bm25_docs = searcher._bm25_retriever.invoke(raw)[:BM25_K]
            bm25_order = [d.metadata.get("chunk_id") for d in bm25_docs]
            rrf_order = [k for k, _ in sorted(rrf_scores.items(), key=lambda x: -x[1])]
        except Exception as e:
            print(f"[warn] {qid} 管线检索失败: {e}", flush=True)
            dense_order, bm25_order, rrf_order = [], [], []

        def rank_in(order_list):
            for i, cid in enumerate(order_list):
                if cid in gold_set:
                    return i + 1
            return None

        rec.update({
            "dense_rank": rank_in(dense_order),
            "bm25_rank": rank_in(bm25_order),
            "rrf_rank": rank_in(rrf_order),
        })
        records.append(rec)

        if (qi + 1) % 30 == 0:
            print(f"[progress] {qi + 1}/{len(questions)} ({time.time() - t0:.0f}s)", flush=True)

    # ---------- 汇总统计 ----------
    ok = [r for r in records if "oracle_rank" in r]
    ranks = np.array([r["oracle_rank"] for r in ok])
    gold_cos = np.array([r["gold_cos"] for r in ok])
    sib_m = np.array([r["sibling_margin"] for r in ok if r["sibling_margin"] is not None])
    glob_m = np.array([r["global_margin"] for r in ok if r["global_margin"] is not None])

    def pct(arr, p):
        return float(np.percentile(arr, p))

    summary = {
        "n_questions": len(questions),
        "n_gold_in_index": len(ok),
        "n_missing_gold": missing_gold,
        "oracle_rank": {
            "median": pct(ranks, 50), "mean": float(ranks.mean()),
            "p75": pct(ranks, 75), "p90": pct(ranks, 90), "max": int(ranks.max()),
            "band_le10": int((ranks <= 10).sum()), "band_11_30": int(((ranks > 10) & (ranks <= 30)).sum()),
            "band_31_50": int(((ranks > 30) & (ranks <= 50)).sum()),
            "band_51_100": int(((ranks > 50) & (ranks <= 100)).sum()),
            "band_101_300": int(((ranks > 100) & (ranks <= 300)).sum()),
            "band_gt300": int((ranks > 300).sum()),
        },
        "gold_cos": {"median": pct(gold_cos, 50), "mean": float(gold_cos.mean()),
                     "min": float(gold_cos.min()), "max": float(gold_cos.max())},
        "sibling_margin": {"median": pct(sib_m, 50), "mean": float(sib_m.mean()),
                           "lt0.02": int((sib_m < 0.02).sum()), "lt0.05": int((sib_m < 0.05).sum()),
                           "n": len(sib_m)},
        "global_margin": {"median": pct(glob_m, 50), "mean": float(glob_m.mean()),
                          "lt0.02": int((glob_m < 0.02).sum()), "lt0.05": int((glob_m < 0.05).sum()),
                          "n": len(glob_m)},
        "stage": {
            "dense_hit_top30": int(sum(1 for r in ok if (r.get("dense_rank") or 999) <= 30)),
            "bm25_hit_top20": int(sum(1 for r in ok if (r.get("bm25_rank") or 999) <= 20)),
            "rrf_hit_pool": int(sum(1 for r in ok if r.get("rrf_rank") is not None)),
        },
        "attribution": {
            "oracle_le30_dense_miss": [r["qid"] for r in ok if r["oracle_rank"] <= 30 and (r.get("dense_rank") or 999) > 30],
            "dense_hit_rrf_miss": [r["qid"] for r in ok if (r.get("dense_rank") or 999) <= 30 and r.get("rrf_rank") is None],
            "oracle_le30_rrf_miss": [r["qid"] for r in ok if r["oracle_rank"] <= 30 and r.get("rrf_rank") is None],
            "oracle_gt100": [r["qid"] for r in ok if r["oracle_rank"] > 100],
            "oracle_gt300": [r["qid"] for r in ok if r["oracle_rank"] > 300],
        },
        "rewrite": {
            "n_with_rw": int(sum(1 for r in ok if r.get("rw_oracle_rank") is not None)),
            "rw_better_than_raw": int(sum(1 for r in ok if r.get("rw_oracle_rank") is not None and r["rw_oracle_rank"] < r["oracle_rank"])),
            "rw_worse_than_raw": int(sum(1 for r in ok if r.get("rw_oracle_rank") is not None and r["rw_oracle_rank"] > r["oracle_rank"])),
            "raw_le30_rw_le30": int(sum(1 for r in ok if r["oracle_rank"] <= 30 and (r.get("rw_oracle_rank") or 999) <= 30)),
            "raw_gt30_rw_le30": int(sum(1 for r in ok if r["oracle_rank"] > 30 and (r.get("rw_oracle_rank") or 999) <= 30)),
        },
    }

    out = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
           "summary": summary, "per_question": records}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # ---------- 控制台报告 ----------
    print("\n" + "=" * 72)
    print("Oracle Rank 可分性实验结果")
    print("=" * 72)
    s = summary
    print(f"\n[1] 题目覆盖: {s['n_gold_in_index']}/{s['n_questions']} (gold 不在索引: {s['n_missing_gold']})")
    o = s["oracle_rank"]
    print(f"\n[2] Oracle Rank 分布 (N={N}):")
    print(f"    median={o['median']:.1f}  mean={o['mean']:.1f}  P75={o['p75']:.1f}  P90={o['p90']:.1f}  max={o['max']}")
    print(f"    ≤10: {o['band_le10']} | 11-30: {o['band_11_30']} | 31-50: {o['band_31_50']} | "
          f"51-100: {o['band_51_100']} | 101-300: {o['band_101_300']} | >300: {o['band_gt300']}")
    g = s["gold_cos"]
    print(f"\n[3] Gold Cosine: median={g['median']:.4f}  mean={g['mean']:.4f}  min={g['min']:.4f}  max={g['max']:.4f}")
    sm = s["sibling_margin"]
    print(f"[4] 同 Section 兄弟 Margin (n={sm['n']}): median={sm['median']:.4f}  mean={sm['mean']:.4f}  "
          f"<0.02: {sm['lt0.02']}  <0.05: {sm['lt0.05']}")
    gm = s["global_margin"]
    print(f"[5] 全局最强负样本 Margin (n={gm['n']}): median={gm['median']:.4f}  mean={gm['mean']:.4f}  "
          f"<0.02: {gm['lt0.02']}  <0.05: {gm['lt0.05']}")
    st = s["stage"]
    print(f"\n[6] 线上阶段命中（与 CK A/B stage_recall 81.7/74.4/79.4 对照）:")
    print(f"    Dense top-30: {st['dense_hit_top30']}/{s['n_gold_in_index']} = {st['dense_hit_top30']/s['n_gold_in_index']:.3f}")
    print(f"    BM25  top-20: {st['bm25_hit_top20']}/{s['n_gold_in_index']} = {st['bm25_hit_top20']/s['n_gold_in_index']:.3f}")
    print(f"    RRF  pool:    {st['rrf_hit_pool']}/{s['n_gold_in_index']} = {st['rrf_hit_pool']/s['n_gold_in_index']:.3f}")
    a = s["attribution"]
    print(f"\n[7] 瓶颈归因:")
    print(f"    oracle≤30 但线上 Dense 未命中（索引不一致）: {len(a['oracle_le30_dense_miss'])} {a['oracle_le30_dense_miss']}")
    print(f"    Dense 命中但 RRF 丢失（融合稀释）: {len(a['dense_hit_rrf_miss'])} {a['dense_hit_rrf_miss']}")
    print(f"    oracle≤30 但 RRF 丢失: {len(a['oracle_le30_rrf_miss'])} {a['oracle_le30_rrf_miss']}")
    print(f"    oracle>100（表示空间不可分）: {len(a['oracle_gt100'])} {a['oracle_gt100']}")
    print(f"    oracle>300: {len(a['oracle_gt300'])} {a['oracle_gt300']}")
    rw = s["rewrite"]
    if rw["n_with_rw"]:
        print(f"\n[8] 改写提升 (n={rw['n_with_rw']}):")
        print(f"    改写 oracle 优于原始: {rw['rw_better_than_raw']} | 劣于: {rw['rw_worse_than_raw']}")
        print(f"    原始>30 但改写≤30（改写救回表示）: {rw['raw_gt30_rw_le30']}")
    print(f"\n结果已保存: {OUT}")

    # ---------- 关键题明细 ----------
    print("\n[9] oracle>100 的题明细:")
    for r in ok:
        if r["oracle_rank"] > 100:
            print(f"    {r['qid']:<8} rank={r['oracle_rank']:>4} cos={r['gold_cos']:.3f} "
                  f"glob_m={r['global_margin']:.3f} sib_m={r['sibling_margin'] if r['sibling_margin'] is not None else '-'} "
                  f"| {r['query'][:40]}")
    print(f"\n总耗时: {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
