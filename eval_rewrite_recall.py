#!/usr/bin/env python3
"""端到端检索评测：Baseline vs +Rewrite（无 Reranker），量化当前生产 prompt 改写对召回的影响。

只跑两配置，避免 eval_v2_full.py 的 Reranker 长耗时。
改写查询来自 query_rewriter.expand_query（当前已 apply 的 prompt）。
"""
import json, sys, time
import numpy as np
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, ".")

with open("data/golden_set_v2.json") as f:
    gs = json.load(f)
with open("data/chunks.json") as f:
    chunks = json.load(f)

sec_to_cids = {}
for c in chunks:
    sec_to_cids.setdefault(c["metadata"]["section_id"], []).append(c["id"])

indomain_qs = [q for q in gs if q["capability"] != "ood_detection"]
print(f"[eval] In-domain: {len(indomain_qs)} 题", flush=True)

from query_rewriter import expand_query
rewrite_map = {}
for q in indomain_qs:
    expanded = expand_query(q["question"], mode="all")
    rewrite_map[q["question"]] = expanded[1:] if len(expanded) > 1 else []
n_rw = sum(1 for v in rewrite_map.values() if v)
print(f"[eval] Rewrite: {n_rw}/{len(indomain_qs)} 题有改写查询", flush=True)


def run_one(q, searcher, use_rewrite):
    query = q["question"]
    gold = set(q["gold_chunks"])
    if use_rewrite:
        extra = rewrite_map.get(query, [])
        _, results = searcher.search_multi_query(query, top_k=10, expand_context=True, extra_queries=extra)
    else:
        results = searcher.search(query, top_k=10, expand_context=True)
    rids = []
    for rr in results:
        rids.extend(sec_to_cids.get(rr.get("metadata", {}).get("section_id", ""), []))
    rr_score = 0.0
    for i, rid in enumerate(rids):
        if rid in gold:
            rr_score = 1.0 / (i + 1); break
    return {"id": q["id"], "capability": q["capability"], "difficulty": q["difficulty"],
            "rr": rr_score,
            "recall_5": any(rid in gold for rid in rids[:5]),
            "recall_10": any(rid in gold for rid in rids[:10])}


def run_eval(name, searcher, use_rewrite):
    t0 = time.time()
    res = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(run_one, q, searcher, use_rewrite): q["id"] for q in indomain_qs}
        for f in as_completed(futs):
            res.append(f.result())
    n = len(res)
    mrr = np.mean([r["rr"] for r in res])
    r5 = sum(r["recall_5"] for r in res) / n
    r10 = sum(r["recall_10"] for r in res) / n
    top1 = sum(r["rr"] >= 1.0 for r in res) / n
    z10 = sum(1 for r in res if not r["recall_10"])
    cap = defaultdict(lambda: {"n": 0, "mrr": 0, "r10": 0})
    for r in res:
        cap[r["capability"]]["n"] += 1
        cap[r["capability"]]["mrr"] += r["rr"]
        cap[r["capability"]]["r10"] += r["recall_10"]
    for c in cap:
        cap[c]["mrr"] /= cap[c]["n"]; cap[c]["r10"] /= cap[c]["n"]
    print(f"\n[{name}] {time.time()-t0:.0f}s  MRR={mrr:.4f} R@5={r5:.4f} R@10={r10:.4f} Top1={top1:.4f} R@10=0:{z10}/{n}", flush=True)
    return {"name": name, "mrr": mrr, "r5": r5, "r10": r10, "top1": top1, "zero10": z10, "cap": dict(cap)}


from hybrid_search import HybridSearcher
searcher = HybridSearcher(enable_reranker=False)

base = run_eval("Baseline (no rewrite)", searcher, False)
rw = run_eval("+Rewrite (current prompt)", searcher, True)

print("\n" + "=" * 64)
print("  召回对比：Baseline vs +Rewrite（无 Reranker）")
print("=" * 64)
print(f"  {'指标':<10s} {'Baseline':>10s} {'+Rewrite':>10s} {'Δ':>10s}")
for k, name in [("mrr", "MRR"), ("r5", "Recall@5"), ("r10", "Recall@10"), ("top1", "Top-1")]:
    d = rw[k] - base[k]
    print(f"  {name:<10s} {base[k]:>10.4f} {rw[k]:>10.4f} {d:>+10.4f}")
print(f"  {'R@10=0':<10s} {base['zero10']:>10d} {rw['zero10']:>10d} {rw['zero10']-base['zero10']:>+10d}")

print(f"\n  ── 按 Capability（MRR / R@10）──")
print(f"  {'Capability':<20s} {'Base MRR':>9s} {'RW MRR':>9s} {'Base R10':>9s} {'RW R10':>9s}")
for c in ["exact_retrieval", "context_expansion", "cross_section", "cross_document",
          "table_retrieval", "numeric_retrieval", "query_rewrite"]:
    b = base["cap"].get(c, {"mrr": 0, "r10": 0}); w = rw["cap"].get(c, {"mrr": 0, "r10": 0})
    print(f"  {c:<20s} {b['mrr']:>9.3f} {w['mrr']:>9.3f} {b['r10']:>9.3f} {w['r10']:>9.3f}")

with open("eval_rewrite_recall.json", "w", encoding="utf-8") as f:
    json.dump({"baseline": base, "rewrite": rw}, f, ensure_ascii=False, indent=2)
print("\n  saved: eval_rewrite_recall.json")
