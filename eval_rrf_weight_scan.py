#!/usr/bin/env python3
"""RRF 权重分层扫描（二十四节）：按 capability 验证 0.7/0.3 是否仍最优、是否需要动态权重。

口径：180 in-domain 原始 query；候选池 = 生产配置（Dense top-30 + BM25 top-20，
RRF_K=60，single-channel boost 语义与 hybrid_search._rrf_retrieve 一致）。
采集一次候选池后解析式重算 0.1~0.95 权重网格下的 rrf@10，不改线上代码。

输出两部分：
  1) 0.7/0.3 下按 capability 的 d@10/d@30/b@10/b@30/rrf@10 与 A/B/C 分布
  2) 权重网格 × capability 的 rrf@10 曲面（* = 该类最优权重）

结果写 data/rrf_weight_scan_report.json。

用法: python3 eval_rrf_weight_scan.py
"""
import json
import time
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "data" / "rrf_weight_scan_report.json"

WEIGHTS = [0.95, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1]
RRF_K = 60
DENSE_K, BM25_K = 30, 20


def rrf_scores(dense_ids, bm25_ids, w_dense):
    """复刻 _rrf_retrieve 融合语义（含 single-channel boost）。"""
    w_bm25 = 1.0 - w_dense
    score, in_d, in_b = {}, set(dense_ids), set(bm25_ids)
    for r, k in enumerate(dense_ids):
        score[k] = score.get(k, 0.0) + w_dense / (RRF_K + r)
    for r, k in enumerate(bm25_ids):
        score[k] = score.get(k, 0.0) + w_bm25 / (RRF_K + r)
    for k in score:
        d, b = k in in_d, k in in_b
        if d and not b:
            score[k] /= w_dense
        elif b and not d:
            score[k] /= w_bm25
    return score


def main():
    t0 = time.time()
    gs = [q for q in json.load(open(BASE / "data" / "golden_set_v2.json", encoding="utf-8"))
          if q["capability"] != "ood_detection"]
    print(f"[scan] {len(gs)} 题, 采集 dense_k={DENSE_K}+bm25 候选池 (生产配置)...", flush=True)

    from hybrid_search import HybridSearcher
    searcher = HybridSearcher(enable_reranker=False)
    searcher._bm25_retriever.k = 30

    pools = {}  # qid -> (cap, gold, dense_ids, bm25_ids, bm25_full)
    for i, q in enumerate(gs):
        query = q["question"]
        gold = set(q["gold_chunks"])
        dense = [searcher._chunk_key(d) for d, _ in
                 searcher._vectorstore.similarity_search_with_score(query, k=DENSE_K)]
        bm25_full = [d.metadata.get("chunk_id") for d in
                     searcher._bm25_retriever.invoke(query)[:30]]
        pools[q["id"]] = (q["capability"], gold, dense, bm25_full[:BM25_K], bm25_full)
        if (i + 1) % 45 == 0:
            print(f"  {i + 1}/{len(gs)} ({time.time() - t0:.0f}s)", flush=True)

    per_cap = defaultdict(lambda: {w: [] for w in WEIGHTS})
    for cap, gold, dense, bm25, _ in pools.values():
        for wd in WEIGHTS:
            sc = rrf_scores(dense, bm25, wd)
            top10 = sorted(sc.items(), key=lambda x: -x[1])[:10]
            per_cap[cap][wd].append(1 if any(k in gold for k, _ in top10) else 0)

    # ── 0.7/0.3 下按 capability 分布 + A/B/C ──
    dist = {}
    for cap, gold, dense, bm25, bm25_full in pools.values():
        d10 = any(k in gold for k in dense[:10])
        d30 = any(k in gold for k in dense[:30])
        b10 = any(k in gold for k in bm25_full[:10])
        b30 = any(k in gold for k in bm25_full[:30])
        sc = rrf_scores(dense, bm25, 0.7)
        top10 = sorted(sc.items(), key=lambda x: -x[1])[:10]
        r10 = any(k in gold for k, _ in top10)
        row = dist.setdefault(cap, {"n": 0, "d10": 0, "d30": 0, "b10": 0, "b30": 0,
                                    "r10": 0, "A": 0, "B": 0, "C": 0})
        row["n"] += 1
        row["d10"] += d10; row["d30"] += d30; row["b10"] += b10; row["b30"] += b30; row["r10"] += r10
        row["A"] += d10 and not r10
        row["B"] += (not d10) and d30 and (not r10)
        row["C"] += not d30

    print(f"\n-- 0.7/0.3 下按 capability 分布 --")
    print(f"{'capability':<22s} {'n':>3s} {'d@10':>6s} {'d@30':>6s} {'b@10':>6s} {'b@30':>6s} "
          f"{'rrf@10':>6s} {'A':>3s} {'B':>3s} {'C':>3s}")
    print("-" * 76)
    for cap in sorted(dist):
        r = dist[cap]
        n = r["n"]
        print(f"{cap:<22s} {n:>3d} {r['d10']/n:>6.1%} {r['d30']/n:>6.1%} {r['b10']/n:>6.1%} "
              f"{r['b30']/n:>6.1%} {r['r10']/n:>6.1%} {r['A']:>3d} {r['B']:>3d} {r['C']:>3d}")

    # ── 权重网格 × capability ──
    print(f"\n-- 权重网格 × capability (rrf@10, * = 该类最优) --")
    print(f"{'capability':<22s}", end="")
    for w in WEIGHTS:
        print(f" {w:>5.2f}", end="")
    print(f"  {'最优':>6s} {'n':>4s} {'vs0.7':>6s}")
    print("-" * 126)
    caps = sorted(per_cap)
    grid = {}
    all_hits = {w: [] for w in WEIGHTS}
    for cap in caps:
        n = len(per_cap[cap][WEIGHTS[0]])
        rates = {}
        for w in WEIGHTS:
            hits = per_cap[cap][w]
            rates[w] = sum(hits) / n
            all_hits[w].extend(hits)
        best_w = max(rates, key=rates.get)
        gain = n * (rates[best_w] - rates[0.7])
        grid[cap] = {"n": n, "rates": rates, "best_w": best_w, "gain_vs_07_questions": gain}
        print(f"{cap:<22s}", end="")
        for w in WEIGHTS:
            mark = "*" if w == best_w else " "
            print(f" {rates[w]:>5.1%}{mark}", end="")
        print(f"  {best_w:>6.2f} {n:>4d} {gain:>+6.1f}")

    print("-" * 126)
    tot_rates = {w: sum(all_hits[w]) / len(all_hits[w]) for w in WEIGHTS}
    best_w = max(tot_rates, key=tot_rates.get)
    print(f"{'TOTAL':<22s}", end="")
    for w in WEIGHTS:
        print(f" {tot_rates[w]:>5.1%}", end="")
    n_all = len(all_hits[WEIGHTS[0]])
    print(f"  {best_w:>6.2f} {n_all:>4d} {n_all*(tot_rates[best_w]-tot_rates[0.7]):>+6.1f}")

    upper_bound = sum(g["gain_vs_07_questions"] for g in grid.values())
    json.dump({
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {"dense_k": DENSE_K, "bm25_k": BM25_K, "rrf_k": RRF_K,
                   "weights": WEIGHTS, "n": n_all},
        "dist_at_07": dist,
        "total_rates": tot_rates,
        "per_capability": grid,
        "in_sample_gain_upper_bound": {"questions": upper_bound,
                                       "pp": upper_bound / n_all},
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n[scan] 完成 ({time.time() - t0:.0f}s) | 按类完美权重 in-sample 增益上界: "
          f"+{upper_bound} 题 ({upper_bound / n_all:.1%})")
    print(f"结果已保存: {OUT}")


if __name__ == "__main__":
    main()
