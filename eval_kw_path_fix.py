"""kw-only 路径修复（方案①）回归评测。

对比 G1 臂 kw-only 题（无 rw/sq 有 kw）修复前（kw 触发 multi-query 机制，
见 data/gate_ab_ce_report.json）与修复后（plain 路径 + kw 拼入 Original BM25）
的 CE 结果（rr / recall_5 / recall_10）。

输出: data/kw_path_fix_report.json
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import query_rewriter as qr
import eval_gate_ab as ab
from hybrid_search import HybridSearcher

OLD_REPORT = "data/gate_ab_ce_report.json"
OUT = "data/kw_path_fix_report.json"


def main():
    cache = qr._load_cache()
    kwonly_queries = [k[:-3] for k in cache if k.endswith("|g1")
                      and not cache[k].get("rewrite_queries")
                      and not cache[k].get("sub_queries")
                      and cache[k].get("keywords")]
    q_by_text = {q["question"]: q for q in ab.gs}
    affected = [q_by_text[t] for t in kwonly_queries if t in q_by_text]
    print(f"[kw-path-fix] G1 kw-only 题: cache {len(kwonly_queries)} → golden 命中 {len(affected)}",
          flush=True)

    for q in affected:
        t = q["question"]
        ab.pools[(q["id"], "G1")] = {
            "rw": qr.get_rewrite_queries(t), "sq": qr.get_sub_queries(t),
            "kw": qr.get_keywords(t), "info": qr.get_gate_info(t),
        }

    searcher = HybridSearcher(enable_reranker=True)
    old = json.load(open(OLD_REPORT, encoding="utf-8"))["per_question"]

    rows = []
    t0 = time.time()
    for i, q in enumerate(affected, 1):
        row = ab.run_ce_one(q, "G1", searcher)
        row["old_rr"] = old[q["id"]]["G1"]["rr"]
        row["old_recall_5"] = old[q["id"]]["G1"]["recall_5"]
        row["old_recall_10"] = old[q["id"]]["G1"]["recall_10"]
        rows.append(row)
        print(f"[{i}/{len(affected)}] {q['id']} old_rr={row['old_rr']} new_rr={row['rr']} "
              f"({time.time() - t0:.0f}s)", flush=True)

    n = len(rows)
    old_mrr = sum(r["old_rr"] for r in rows) / n
    new_mrr = sum(r["rr"] for r in rows) / n
    old_r10 = sum(r["old_recall_10"] for r in rows) / n
    new_r10 = sum(r["recall_10"] for r in rows) / n
    rescued = [r for r in rows if r["old_rr"] == 0 and r["rr"] > 0]
    lost = [r for r in rows if r["old_rr"] > 0 and r["rr"] == 0]
    improved = [r for r in rows if r["old_rr"] > 0 and r["rr"] > r["old_rr"]]
    worsened = [r for r in rows if r["rr"] > 0 and r["rr"] < r["old_rr"]]

    report = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "n": n,
              "old": {"mrr": round(old_mrr, 4), "recall_10": round(old_r10, 4)},
              "new": {"mrr": round(new_mrr, 4), "recall_10": round(new_r10, 4)},
              "rescued": [{"id": r["id"], "new_rr": r["rr"]} for r in rescued],
              "lost": [{"id": r["id"], "old_rr": r["old_rr"]} for r in lost],
              "improved": [{"id": r["id"], "old_rr": r["old_rr"], "new_rr": r["rr"]} for r in improved],
              "worsened": [{"id": r["id"], "old_rr": r["old_rr"], "new_rr": r["rr"]} for r in worsened],
              "per_question": {r["id"]: r for r in rows}}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n[done] MRR {old_mrr} → {new_mrr} | R@10 {old_r10} → {new_r10} | "
          f"救 {len(rescued)} 丢 {len(lost)} 升 {len(improved)} 降 {len(worsened)} → {OUT}",
          flush=True)


if __name__ == "__main__":
    main()
