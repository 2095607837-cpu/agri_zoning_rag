#!/usr/bin/env python3
"""+Rewrite + Reranker + OOD 检测（子查询跳过 reranker，仅原始查询过 CrossEncoder）"""

import json, sys, time
import numpy as np
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

print("[eval] 加载数据...", flush=True)
with open("data/golden_set_v2.json") as f:
    gs = json.load(f)
with open("data/chunks.json") as f:
    chunks = json.load(f)

sec_to_cids = {}
for c in chunks:
    sid = c["metadata"]["section_id"]
    sec_to_cids.setdefault(sid, []).append(c["id"])

ood_qs = [q for q in gs if q["capability"] == "ood_detection"]
indomain_qs = [q for q in gs if q["capability"] != "ood_detection"]
print(f"[eval] In-domain: {len(indomain_qs)} | OOD: {len(ood_qs)}", flush=True)

# Load rewrite cache
from query_rewriter import expand_query
from hybrid_search import HybridSearcher
_searcher = HybridSearcher(enable_reranker=True)
rewrite_map = {}
for q in gs:
    initial = _searcher.search(q["question"], top_k=2, expand_context=True)
    top1_sim = initial[0].get("similarity", 0) if len(initial) > 0 else 0
    top2_sim = initial[1].get("similarity", 0) if len(initial) > 1 else 0
    expanded = expand_query(q["question"], mode="all", top1_sim=top1_sim, top2_sim=top2_sim)
    rewrite_map[q["question"]] = expanded[1:] if len(expanded) > 1 else []
print(f"[eval] Rewrite: {sum(1 for v in rewrite_map.values() if v)}/{len(gs)} 题有改写", flush=True)

print("[eval] 开始评测 +Rewrite + Reranker (子查询跳过reranker)...", flush=True)
searcher = _searcher


def process_one(q):
    query = q["question"]
    gold = set(q["gold_chunks"])
    top_k = 10
    extra = rewrite_map.get(query, [])

    _, merged = searcher.search_multi_query(
        query, top_k=top_k, expand_context=True, extra_queries=extra
    )

    retrieved_ids = []
    for rr in merged:
        sid = rr.get("metadata", {}).get("section_id", "")
        retrieved_ids.extend(sec_to_cids.get(sid, []))

    rr_val = 0.0
    for i, rid in enumerate(retrieved_ids):
        if rid in gold:
            rr_val = 1.0 / (i + 1)
            break
    return {
        "id": q["id"], "capability": q["capability"], "difficulty": q["difficulty"],
        "rr": rr_val,
        "recall_5": any(rid in gold for rid in retrieved_ids[:5]),
        "recall_10": any(rid in gold for rid in retrieved_ids[:10]),
        "hit_count": sum(1 for rid in retrieved_ids[:10] if rid in gold),
        "gold_count": len(gold),
    }


t0 = time.time()
results = []
workers = 4
with ThreadPoolExecutor(max_workers=workers) as ex:
    futures = {ex.submit(process_one, q): q["id"] for q in indomain_qs}
    done = 0
    for f in as_completed(futures):
        results.append(f.result())
        done += 1
        if done % 20 == 0:
            print(f"  进度: {done}/{len(indomain_qs)}", flush=True)

elapsed = time.time() - t0
n = len(results)

mrr = np.mean([r["rr"] for r in results])
recall_5 = sum(r["recall_5"] for r in results) / n
recall_10 = sum(r["recall_10"] for r in results) / n
top1_hit = sum(r["rr"] >= 1.0 for r in results) / n
avg_hits = np.mean([r["hit_count"] for r in results])

cap_stats = defaultdict(lambda: {"count": 0, "mrr": 0, "recall_5": 0, "recall_10": 0, "top1": 0})
for r in results:
    c = r["capability"]
    cap_stats[c]["count"] += 1
    cap_stats[c]["mrr"] += r["rr"]
    cap_stats[c]["recall_5"] += r["recall_5"]
    cap_stats[c]["recall_10"] += r["recall_10"]
    cap_stats[c]["top1"] += (1.0 if r["rr"] >= 1.0 else 0.0)
for c in cap_stats:
    nc = cap_stats[c]["count"]
    for k in ["mrr", "recall_5", "recall_10", "top1"]:
        cap_stats[c][k] /= nc

diff_stats = defaultdict(lambda: {"count": 0, "mrr": 0, "recall_5": 0, "recall_10": 0})
for r in results:
    d = r["difficulty"]
    diff_stats[d]["count"] += 1
    diff_stats[d]["mrr"] += r["rr"]
    diff_stats[d]["recall_5"] += r["recall_5"]
    diff_stats[d]["recall_10"] += r["recall_10"]
for d in diff_stats:
    nd = diff_stats[d]["count"]
    for k in ["mrr", "recall_5", "recall_10"]:
        diff_stats[d][k] /= nd

zero_recall = [r for r in results if not r["recall_10"]]

print(f"\n  === +Rewrite + Reranker (优化后) ===")
print(f"  题目数: {n}  耗时: {elapsed:.1f}s")
print(f"  整体指标:")
print(f"    MRR:             {mrr:.4f}")
print(f"    Recall@5:        {recall_5:.4f} ({recall_5*100:.1f}%)")
print(f"    Recall@10:       {recall_10:.4f} ({recall_10*100:.1f}%)")
print(f"    Top-1 命中率:    {top1_hit:.4f} ({top1_hit*100:.1f}%)")
print(f"    平均命中 chunk:  {avg_hits:.1f} / {np.mean([r['gold_count'] for r in results]):.1f}")
print(f"    Recall@10=0:     {len(zero_recall)}/{n}")

print(f"\n  -- 按 Capability --")
print(f"  {'Capability':<22s} {'n':>3s}  {'MRR':>6s}  {'R@5':>6s}  {'R@10':>6s}  {'Top1':>6s}")
print(f"  {'-'*55}")
for cap in ["exact_retrieval","context_expansion","cross_section","cross_document",
            "table_retrieval","numeric_retrieval","query_rewrite"]:
    s = cap_stats[cap]
    print(f"  {cap:<22s} {s['count']:>3d}  {s['mrr']:>6.3f}  {s['recall_5']:>6.3f}  {s['recall_10']:>6.3f}  {s['top1']:>6.3f}")

print(f"\n  -- 按 Difficulty --")
for d in ["easy","medium","hard"]:
    s = diff_stats[d]
    if s["count"] > 0:
        print(f"  {d:<8s} n={s['count']:>3d}  MRR={s['mrr']:.4f}  R@5={s['recall_5']:.4f}  R@10={s['recall_10']:.4f}")

if zero_recall:
    print(f"\n  -- Recall@10=0 ({len(zero_recall)} 题) --")
    for r in zero_recall[:10]:
        q = next(x for x in gs if x["id"] == r["id"])
        print(f"  {r['id']} ({r['capability']}, {r['difficulty']}): {q['question'][:80]}")

# ── OOD Detection ──
print(f"\n{'='*60}")
print(f"  OOD 检测评测 ({len(ood_qs)} 题) — +Rewrite + Reranker")
print(f"{'='*60}")

from judge import judge

ood_result = {"detected": 0, "missed": 0, "by_method": defaultdict(int)}
for q in ood_qs:
    query = q["question"]
    extra = rewrite_map.get(query, [])
    judge_results, _ = searcher.search_multi_query(
        query, top_k=5, expand_context=True, extra_queries=extra
    )
    j = judge(query, judge_results)
    if j["decision"] == "reject":
        ood_result["detected"] += 1
    else:
        ood_result["missed"] += 1
        print(f"  MISS {q['id']} ({q.get('ood_type','')}): {query[:60]} | method={j['method']} reason={j['reason']}")
    ood_result["by_method"][j["method"]] += 1

ood_n = len(ood_qs)
print(f"\n  OOD 召回率: {ood_result['detected']}/{ood_n} ({ood_result['detected']/ood_n*100:.1f}%)")
print(f"  漏判: {ood_result['missed']}")
print(f"  分层: signal={ood_result['by_method'].get('signal',0)} high_sim={ood_result['by_method'].get('high_sim',0)} score={ood_result['by_method'].get('score',0)} llm={ood_result['by_method'].get('llm',0)}")

# ── Final Summary ──
print(f"\n{'='*60}")
print(f"  全配置对比汇总")
print(f"{'='*60}")
print(f"  {'Config':<38s} {'MRR':>6s}  {'R@5':>6s}  {'R@10':>6s}  {'Top1':>6s}  {'R@10=0':>7s}")
print(f"  {'-'*78}")
print(f"  {'Baseline':<38s} {'0.5692':>6s}  {'0.7056':>6s}  {'0.7833':>6s}  {'0.4778':>6s}  {'39':>7s}")
print(f"  {'+Rewrite only':<38s} {'0.4785':>6s}  {'0.5889':>6s}  {'0.6889':>6s}  {'0.3833':>6s}  {'56':>7s}")
print(f"  {'+Reranker only':<38s} {'0.6092':>6s}  {'0.7278':>6s}  {'0.8000':>6s}  {'0.5167':>6s}  {'36':>7s}")
print(f"  {'+Rewrite + Reranker':<38s} {mrr:>6.4f}  {recall_5:>6.4f}  {recall_10:>6.4f}  {top1_hit:>6.4f}  {len(zero_recall):>7d}")

print(f"\n  评测完成")
