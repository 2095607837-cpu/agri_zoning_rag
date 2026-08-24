#!/usr/bin/env python3
"""RRF 损失诊断（P0）：复用 eval_oracle_rank_results.json，补采 BM25 top-30 一列。

输出每题的 dense/bm25/rrf 排名与 hit 矩阵，并按三类统计：
  A类: Dense Top10 命中, RRF Top10 不命中
  B类: Dense Top30 命中, RRF Top10 不命中
  C类: Dense Top30 未命中

不改任何线上代码，只读生产索引。结果写 data/rrf_loss_report.json。

用法: python3 eval_rrf_loss.py
"""
import json
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
ORACLE_RESULTS = BASE / "data" / "eval_oracle_rank_results.json"
OUT = BASE / "data" / "rrf_loss_report.json"


def hit(rank, k):
    return 1 if (rank is not None and rank <= k) else 0


def main():
    t0 = time.time()
    r = json.load(open(ORACLE_RESULTS, encoding="utf-8"))
    ok = [x for x in r["per_question"] if "oracle_rank" in x]
    print(f"[rrf_loss] 复用 oracle 结果 {len(ok)} 题")

    print("[rrf_loss] 初始化 HybridSearcher (reranker off, 仅补 BM25 top-30) ...")
    from hybrid_search import HybridSearcher
    searcher = HybridSearcher(enable_reranker=False)
    searcher._bm25_retriever.k = 30

    rows = []
    for i, x in enumerate(ok):
        gold_set = set(x["gold_chunks"])
        bm25_docs = searcher._bm25_retriever.invoke(x["query"])[:30]
        bm25_rank30 = None
        for j, d in enumerate(bm25_docs):
            if d.metadata.get("chunk_id") in gold_set:
                bm25_rank30 = j + 1
                break

        d = x.get("dense_rank")
        rrf = x.get("rrf_rank")
        b10 = x.get("bm25_rank")
        rows.append({
            "query_id": x["qid"],
            "query": x["query"],
            "gold_chunk_id": x.get("best_gold"),
            "oracle_rank": x["oracle_rank"],
            "dense_rank": d,
            "bm25_rank_top20": b10,
            "bm25_rank_top30": bm25_rank30,
            "rrf_rank": rrf,
            "dense_top10_hit": hit(d, 10),
            "dense_top30_hit": hit(d, 30),
            "bm25_top10_hit": hit(b10, 10),
            "bm25_top30_hit": hit(bm25_rank30, 30),
            "rrf_top10_hit": hit(rrf, 10),
            "dense_to_rrf_delta": (rrf - d) if (rrf is not None and d is not None) else None,
        })
        if (i + 1) % 60 == 0:
            print(f"[progress] {i + 1}/{len(ok)} ({time.time() - t0:.0f}s)", flush=True)

    A = [x for x in rows if x["dense_top10_hit"] and not x["rrf_top10_hit"]]
    B = [x for x in rows if not x["dense_top10_hit"] and x["dense_top30_hit"] and not x["rrf_top10_hit"]]
    C = [x for x in rows if not x["dense_top30_hit"]]
    dense_solved = [x for x in rows if x["dense_top30_hit"] and x["rrf_top10_hit"]]
    rrf_gain = [x for x in rows if not x["dense_top30_hit"] and x["rrf_top10_hit"]]

    n = len(rows)
    summary = {
        "n": n,
        "A_dense10_rrf_miss": {"count": len(A), "ratio": len(A) / n, "qids": [x["query_id"] for x in A]},
        "B_dense30_rrf_miss": {"count": len(B), "ratio": len(B) / n, "qids": [x["query_id"] for x in B]},
        "C_dense30_miss": {"count": len(C), "ratio": len(C) / n, "qids": [x["query_id"] for x in C]},
        "rrf_loss_A_plus_B": {"count": len(A) + len(B), "ratio": (len(A) + len(B)) / n},
        "dense_solved": {"count": len(dense_solved), "ratio": len(dense_solved) / n},
        "rrf_gain_by_bm25": {"count": len(rrf_gain), "ratio": len(rrf_gain) / n, "qids": [x["query_id"] for x in rrf_gain]},
    }
    json.dump({"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
               "summary": summary, "per_question": rows},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print("\n" + "=" * 60)
    print("RRF 损失诊断结果（180 题，原始 query，无改写）")
    print("=" * 60)
    print(f"A类 (Dense Top10 命中, RRF Top10 不命中): {len(A):>3} ({len(A)/n:.1%})")
    print(f"B类 (Dense Top30 命中, RRF Top10 不命中): {len(B):>3} ({len(B)/n:.1%})")
    print(f"C类 (Dense Top30 未命中):              {len(C):>3} ({len(C)/n:.1%})")
    print(f"A+B (RRF 损失):                         {len(A)+len(B):>3} ({(len(A)+len(B))/n:.1%})")
    print(f"Dense 已解决 (dense≤30 且 rrf≤10):     {len(dense_solved):>3} ({len(dense_solved)/n:.1%})")
    print(f"RRF 增益 (BM25 救回, dense>30 但 rrf≤10): {len(rrf_gain)}  {[x['query_id'] for x in rrf_gain]}")
    print(f"\n结果已保存: {OUT}")
    print(f"总耗时: {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
