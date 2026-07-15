#!/usr/bin/env python3
"""V2 Golden Set 全配置检索评测 — Baseline / +Rewrite / +Reranker / +Both"""

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
print(f"[eval] 总题数: {len(gs)} | In-domain: {len(indomain_qs)} | OOD: {len(ood_qs)}", flush=True)

# Load rewrite cache (all 200 questions already cached)
from query_rewriter import expand_query
from hybrid_search import HybridSearcher
_searcher = HybridSearcher(enable_reranker=False)
rewrite_map = {}
for q in gs:
    initial = _searcher.search(q["question"], top_k=2, expand_context=True)
    top1_sim = initial[0].get("similarity", 0) if len(initial) > 0 else 0
    top2_sim = initial[1].get("similarity", 0) if len(initial) > 1 else 0
    expanded = expand_query(q["question"], mode="all", top1_sim=top1_sim, top2_sim=top2_sim)
    rewrite_map[q["question"]] = expanded[1:] if len(expanded) > 1 else []
print(f"[eval] Rewrite: {sum(1 for v in rewrite_map.values() if v)}/{len(gs)} 题有改写查询", flush=True)


def run_one(q, searcher, use_rewrite):
    query = q["question"]
    gold = set(q["gold_chunks"])
    top_k = 10

    if use_rewrite:
        extra = rewrite_map.get(query, [])
        judge_results, merged = searcher.search_multi_query(
            query, top_k=top_k, expand_context=True, extra_queries=extra
        )
        results = merged
    else:
        results = searcher.search(query, top_k=top_k, expand_context=True)

    retrieved_ids = []
    for rr in results:
        sid = rr.get("metadata", {}).get("section_id", "")
        retrieved_ids.extend(sec_to_cids.get(sid, []))

    rr = 0.0
    for i, rid in enumerate(retrieved_ids):
        if rid in gold:
            rr = 1.0 / (i + 1)
            break
    recall_5 = any(rid in gold for rid in retrieved_ids[:5])
    recall_10 = any(rid in gold for rid in retrieved_ids[:10])
    hit_count = sum(1 for rid in retrieved_ids[:10] if rid in gold)

    return {
        "id": q["id"], "capability": q["capability"], "difficulty": q["difficulty"],
        "rr": rr, "recall_5": recall_5, "recall_10": recall_10,
        "hit_count": hit_count, "gold_count": len(gold),
    }


def run_eval(name, searcher, use_rewrite):
    print(f"\n[{'='*60}]", flush=True)
    print(f"  {name}", flush=True)
    print(f"[{'='*60}]", flush=True)

    t0 = time.time()
    results = []
    workers = 4
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(run_one, q, searcher, use_rewrite): q["id"] for q in indomain_qs}
        for f in as_completed(futures):
            results.append(f.result())

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
        n_c = cap_stats[c]["count"]
        for k in ["mrr", "recall_5", "recall_10", "top1"]:
            cap_stats[c][k] /= n_c

    diff_stats = defaultdict(lambda: {"count": 0, "mrr": 0, "recall_5": 0, "recall_10": 0})
    for r in results:
        d = r["difficulty"]
        diff_stats[d]["count"] += 1
        diff_stats[d]["mrr"] += r["rr"]
        diff_stats[d]["recall_5"] += r["recall_5"]
        diff_stats[d]["recall_10"] += r["recall_10"]
    for d in diff_stats:
        n_d = diff_stats[d]["count"]
        for k in ["mrr", "recall_5", "recall_10"]:
            diff_stats[d][k] /= n_d

    zero_recall = [r for r in results if not r["recall_10"]]

    print(f"\n  题目数: {n}  耗时: {elapsed:.1f}s")
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

    sys.stdout.flush()
    return {"name": name, "mrr": mrr, "recall_5": recall_5, "recall_10": recall_10,
            "top1_hit": top1_hit, "zero_recall": len(zero_recall), "elapsed": elapsed}


# ── Init searchers ──

print("\n[eval] 加载 Searcher (no reranker)...", flush=True)
searcher_base = _searcher  # 复用 rewrite 阶段已初始化的 searcher

print("[eval] 加载 Searcher (with reranker)...", flush=True)
searcher_rerank = HybridSearcher(enable_reranker=True)

# ── Run 4 configs ──
all_results = []
all_results.append(run_eval("Baseline (no rewrite, no reranker)", searcher_base, use_rewrite=False))
all_results.append(run_eval("+Rewrite only", searcher_base, use_rewrite=True))
all_results.append(run_eval("+Reranker only", searcher_rerank, use_rewrite=False))
all_results.append(run_eval("+Rewrite + Reranker", searcher_rerank, use_rewrite=True))

# ── OOD Detection ──
print(f"\n[{'='*60}]")
print(f"  OOD 检测评测 ({len(ood_qs)} 题) — +Rewrite + Reranker")
print(f"[{'='*60}]")

from judge import judge

ood_results = {"detected": 0, "missed": 0, "by_method": defaultdict(int), "details": []}
for q in ood_qs:
    query = q["question"]
    extra = rewrite_map.get(query, [])
    judge_results, merged = searcher_rerank.search_multi_query(
        query, top_k=5, expand_context=True, extra_queries=extra
    )
    j = judge(query, judge_results)
    if j["decision"] == "reject":
        ood_results["detected"] += 1
    else:
        ood_results["missed"] += 1
    ood_results["by_method"][j["method"]] += 1
    ood_results["details"].append({
        "id": q["id"], "ood_type": q.get("ood_type", ""),
        "question": query[:60], "decision": j["decision"],
        "method": j["method"], "reason": j["reason"],
        "top1_sim": judge_results[0].get("dense_similarity", judge_results[0].get("similarity", 0)) if judge_results else 0,
    })

ood_n = len(ood_qs)
print(f"\n  OOD 召回率: {ood_results['detected']}/{ood_n} ({ood_results['detected']/ood_n*100:.1f}%)")
print(f"  漏判: {ood_results['missed']}")
print(f"  Judge 分层: signal={ood_results['by_method'].get('signal',0)}, "
      f"high_sim={ood_results['by_method'].get('high_sim',0)}, "
      f"score={ood_results['by_method'].get('score',0)}, "
      f"llm={ood_results['by_method'].get('llm',0)}")

missed = [d for d in ood_results["details"] if d["decision"] != "reject"]
if missed:
    print(f"\n  OOD 漏判详情:")
    for d in missed:
        print(f"  {d['id']} ({d['ood_type']}): {d['question']}")
        print(f"    decision={d['decision']} method={d['method']} top1_sim={d['top1_sim']:.3f} reason={d['reason']}")

# ── Summary ──
print(f"\n[{'='*60}]")
print(f"  全配置对比汇总")
print(f"[{'='*60}]")
print(f"  {'Config':<38s} {'MRR':>6s}  {'R@5':>6s}  {'R@10':>6s}  {'Top1':>6s}  {'R@10=0':>7s}  {'Time':>6s}")
print(f"  {'-'*80}")
for r in all_results:
    print(f"  {r['name']:<38s} {r['mrr']:>6.4f}  {r['recall_5']:>6.4f}  {r['recall_10']:>6.4f}  {r['top1_hit']:>6.4f}  {r['zero_recall']:>7d}  {r['elapsed']:>5.1f}s")

print(f"\n  评测完成")
print(f"[{'='*60}]")
