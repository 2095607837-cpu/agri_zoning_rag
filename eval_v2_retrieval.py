#!/usr/bin/env python3
"""V2 Golden Set 检索质量评测 — 测试 gold_chunks 能否被实际检索召回。"""

import json
import time
import numpy as np
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

with open("data/golden_set_v2.json") as f:
    gs = json.load(f)

with open("data/chunks.json") as f:
    chunks = json.load(f)

# Build section_id → [chunk_id] mapping (one section can have multiple chunks)
sec_to_cids = {}
for c in chunks:
    sid = c["metadata"]["section_id"]
    sec_to_cids.setdefault(sid, []).append(c["id"])

# Separate OOD and in-domain
ood_qs = [q for q in gs if q["capability"] == "ood_detection"]
indomain_qs = [q for q in gs if q["capability"] != "ood_detection"]

print(f"总题数: {len(gs)} | In-domain: {len(indomain_qs)} | OOD: {len(ood_qs)}")
print(f"有 gold_chunks: {sum(1 for q in gs if q['gold_chunks'])}")
print()

# ── Init searcher ──
from hybrid_search import HybridSearcher
from judge import judge

searcher = HybridSearcher()

def process_one(q):
    """Search and check gold_chunk recall."""
    query = q["question"]
    gold = set(q["gold_chunks"])
    top_k = 10

    results = searcher.search(query, top_k=top_k, expand_context=True)

    # Map section_id → chunk IDs for matching
    retrieved_ids = []
    for rr in results:
        sid = rr.get("metadata", {}).get("section_id", "")
        cids = sec_to_cids.get(sid, [])
        retrieved_ids.extend(cids)

    # MRR / Recall
    rr = 0.0
    for i, rid in enumerate(retrieved_ids):
        if rid in gold:
            rr = 1.0 / (i + 1)
            break
    recall_5 = any(rid in gold for rid in retrieved_ids[:5])
    recall_10 = any(rid in gold for rid in retrieved_ids[:10])
    hit_count = sum(1 for rid in retrieved_ids[:10] if rid in gold)

    return {
        "id": q["id"],
        "capability": q["capability"],
        "difficulty": q["difficulty"],
        "rr": rr,
        "recall_5": recall_5,
        "recall_10": recall_10,
        "hit_count": hit_count,
        "gold_count": len(gold),
        "top1_sec": results[0].get("metadata",{}).get("section_id","") if results else "",
        "top1_in_gold": retrieved_ids[0] in gold if retrieved_ids else False,
    }

# ── Run ──
print("检索评测中...")
t0 = time.time()

results = []
workers = 4
with ThreadPoolExecutor(max_workers=workers) as ex:
    futures = {ex.submit(process_one, q): q["id"] for q in indomain_qs}
    for f in as_completed(futures):
        results.append(f.result())

elapsed = time.time() - t0
n = len(results)

# ── Aggregate ──
mrr = np.mean([r["rr"] for r in results])
recall_5 = sum(r["recall_5"] for r in results) / n
recall_10 = sum(r["recall_10"] for r in results) / n
top1_hit = sum(r["top1_in_gold"] for r in results) / n
avg_hits = np.mean([r["hit_count"] for r in results])

# By capability
cap_stats = defaultdict(lambda: {"count": 0, "mrr": 0, "recall_5": 0, "recall_10": 0, "top1": 0})
for r in results:
    cap = r["capability"]
    cap_stats[cap]["count"] += 1
    cap_stats[cap]["mrr"] += r["rr"]
    cap_stats[cap]["recall_5"] += r["recall_5"]
    cap_stats[cap]["recall_10"] += r["recall_10"]
    cap_stats[cap]["top1"] += r["top1_in_gold"]

for cap in cap_stats:
    c = cap_stats[cap]["count"]
    cap_stats[cap]["mrr"] /= c
    cap_stats[cap]["recall_5"] /= c
    cap_stats[cap]["recall_10"] /= c
    cap_stats[cap]["top1"] /= c

# By difficulty
diff_stats = defaultdict(lambda: {"count": 0, "mrr": 0, "recall_5": 0, "recall_10": 0})
for r in results:
    d = r["difficulty"]
    diff_stats[d]["count"] += 1
    diff_stats[d]["mrr"] += r["rr"]
    diff_stats[d]["recall_5"] += r["recall_5"]
    diff_stats[d]["recall_10"] += r["recall_10"]
for d in diff_stats:
    c = diff_stats[d]["count"]
    diff_stats[d]["mrr"] /= c
    diff_stats[d]["recall_5"] /= c
    diff_stats[d]["recall_10"] /= c

# Worst performing questions
results.sort(key=lambda r: r["rr"])
worst = [r for r in results if r["rr"] == 0][:10]

print(f"\n{'='*65}")
print(f"  V2 Golden Set 检索质量评测")
print(f"{'='*65}")
print(f"  题目数: {n}  耗时: {elapsed:.1f}s")
print(f"  Top-K: 10")
print(f"")
print(f"  整体指标:")
print(f"    MRR:             {mrr:.4f}")
print(f"    Recall@5:        {recall_5:.4f} ({recall_5*100:.1f}%)")
print(f"    Recall@10:       {recall_10:.4f} ({recall_10*100:.1f}%)")
print(f"    Top-1 命中率:    {top1_hit:.4f} ({top1_hit*100:.1f}%)")
print(f"    平均命中 chunk:  {avg_hits:.1f} / {np.mean([r['gold_count'] for r in results]):.1f}")

print(f"\n  ── 按 Capability ──")
print(f"  {'Capability':<22s} {'n':>3s}  {'MRR':>6s}  {'R@5':>6s}  {'R@10':>6s}  {'Top1':>6s}")
print(f"  {'-'*55}")
for cap in ["exact_retrieval","context_expansion","cross_section","cross_document",
            "table_retrieval","numeric_retrieval","query_rewrite"]:
    s = cap_stats[cap]
    print(f"  {cap:<22s} {s['count']:>3d}  {s['mrr']:>6.3f}  {s['recall_5']:>6.3f}  {s['recall_10']:>6.3f}  {s['top1']:>6.3f}")

print(f"\n  ── 按 Difficulty ──")
for d in ["easy","medium","hard"]:
    s = diff_stats[d]
    if s["count"] > 0:
        print(f"  {d:<8s} n={s['count']:>3d}  MRR={s['mrr']:.4f}  R@5={s['recall_5']:.4f}  R@10={s['recall_10']:.4f}")

# Zero-recall questions
zero_recall = [r for r in results if r["recall_10"] == False]
print(f"\n  ── Recall@10=0 的题目 ({len(zero_recall)}/{n}) ──")
if zero_recall:
    for r in zero_recall[:15]:
        q = next(x for x in gs if x["id"] == r["id"])
        print(f"  {r['id']} ({r['capability']}, {r['difficulty']}): {q['question'][:80]}")
        print(f"    gold_section: {q['gold_sections']}")
        print(f"    top1_sec: {r['top1_sec']}")

# ── OOD 检测 ──
print(f"\n{'='*65}")
print(f"  OOD 检测评测 ({len(ood_qs)} 题)")
print(f"{'='*65}")

ood_results = {"detected": 0, "missed": 0, "by_method": defaultdict(int)}
for q in ood_qs:
    results_ = searcher.search(q["question"], top_k=5, expand_context=True)
    j = judge(q["question"], results_)
    if j["decision"] == "reject":
        ood_results["detected"] += 1
    else:
        ood_results["missed"] += 1
    ood_results["by_method"][j["method"]] += 1

ood_n = len(ood_qs)
print(f"  OOD 召回率: {ood_results['detected']}/{ood_n} ({ood_results['detected']/ood_n*100:.1f}%)")
print(f"  漏判: {ood_results['missed']}")
print(f"  Judge 分层: signal={ood_results['by_method'].get('signal',0)}, "
      f"high_sim={ood_results['by_method'].get('high_sim',0)}, "
      f"score={ood_results['by_method'].get('score',0)}, "
      f"llm={ood_results['by_method'].get('llm',0)}")

# Also test: do OOD queries accidentally hit relevant chunks?
print(f"\n  OOD 查询 top1 内容检查 (是否意外命中相关 chunk):")
for q in ood_qs[:5]:
    results_ = searcher.search(q["question"], top_k=1, expand_context=True)
    top1_content = results_[0].get("content", "")[:80] if results_ else ""
    print(f"  {q['id']} ({q.get('ood_type','')}): sim={results_[0].get('similarity',0):.3f} | {top1_content}...")

print(f"\n{'='*65}")
print(f"  评测完成")
print(f"{'='*65}")
