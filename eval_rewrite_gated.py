#!/usr/bin/env python3
"""按需触发改写评测：仅当原查询 top-1 相似度低时才改写（_needs_rewrite 门槛）。

对每题一次性算出 base 与 merged 两种召回，再按多个相似度阈值扫描：
每题在阈值下选 merged(触发) 或 base(不触发)，统计触发数(成本)与召回(收益)。
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
from hybrid_search import HybridSearcher
_searcher = HybridSearcher(enable_reranker=False)
rewrite_map = {}
for q in indomain_qs:
    initial = _searcher.search(q["question"], top_k=2, expand_context=True)
    top1_sim = initial[0].get("similarity", 0) if len(initial) > 0 else 0
    top2_sim = initial[1].get("similarity", 0) if len(initial) > 1 else 0
    ex = expand_query(q["question"], mode="all", top1_sim=top1_sim, top2_sim=top2_sim)
    rewrite_map[q["question"]] = ex[1:] if len(ex) > 1 else []
print(f"[eval] Rewrite 缓存就绪：{sum(1 for v in rewrite_map.values() if v)}/{len(indomain_qs)} 有改写", flush=True)

searcher = _searcher


def recall_of(results, gold):
    rids = []
    for rr in results:
        rids.extend(sec_to_cids.get(rr.get("metadata", {}).get("section_id", ""), []))
    rr_score = 0.0
    for i, rid in enumerate(rids):
        if rid in gold:
            rr_score = 1.0 / (i + 1); break
    return rr_score, any(r in gold for r in rids[:5]), any(r in gold for r in rids[:10])


def process(q):
    query = q["question"]
    gold = set(q["gold_chunks"])
    base = searcher.search(query, top_k=10, expand_context=True)
    top1 = base[0].get("similarity", 0) if base else 0.0
    extra = rewrite_map.get(query, [])
    if extra:
        _, merged = searcher.search_multi_query(query, top_k=10, expand_context=True, extra_queries=extra)
    else:
        merged = base
    return {
        "id": q["id"], "capability": q["capability"], "top1": top1,
        "has_rw": bool(extra), "len": len(query),
        "base": recall_of(base, gold), "merged": recall_of(merged, gold),
    }


t0 = time.time()
rows = []
with ThreadPoolExecutor(max_workers=4) as ex:
    futs = {ex.submit(process, q): q["id"] for q in indomain_qs}
    for f in as_completed(futs):
        rows.append(f.result())
print(f"[eval] per-query base+merged 计算完成 {time.time()-t0:.0f}s", flush=True)

n = len(rows)


def agg(pick):  # pick(row) -> (rr, r5, r10)
    rr = np.mean([pick(r)[0] for r in rows])
    r5 = sum(pick(r)[1] for r in rows) / n
    r10 = sum(pick(r)[2] for r in rows) / n
    top1 = sum(pick(r)[0] >= 1.0 for r in rows) / n
    z10 = sum(1 for r in rows if not pick(r)[2])
    return rr, r5, r10, top1, z10


base_m = agg(lambda r: r["base"])
full_m = agg(lambda r: r["merged"] if r["has_rw"] else r["base"])

print("\n" + "=" * 72)
print("  按需触发改写 · 阈值扫描（仅 top1_sim < 阈值 且 len>12 才用改写）")
print("=" * 72)
print(f"  {'配置':<20s} {'触发数':>6s} {'MRR':>8s} {'R@5':>8s} {'R@10':>8s} {'Top1':>8s} {'R@10=0':>7s}")
print(f"  {'Baseline(不改写)':<20s} {0:>6d} {base_m[0]:>8.4f} {base_m[1]:>8.4f} {base_m[2]:>8.4f} {base_m[3]:>8.4f} {base_m[4]:>7d}")
n_full = sum(1 for r in rows if r["has_rw"])
print(f"  {'全量改写':<20s} {n_full:>6d} {full_m[0]:>8.4f} {full_m[1]:>8.4f} {full_m[2]:>8.4f} {full_m[3]:>8.4f} {full_m[4]:>7d}")

for thr in [0.55, 0.60, 0.65, 0.70, 0.75]:
    def pick(r, thr=thr):
        trig = r["has_rw"] and r["len"] > 12 and r["top1"] < thr
        return r["merged"] if trig else r["base"]
    trig_n = sum(1 for r in rows if r["has_rw"] and r["len"] > 12 and r["top1"] < thr)
    m = agg(pick)
    print(f"  {'门槛 top1<'+str(thr):<20s} {trig_n:>6d} {m[0]:>8.4f} {m[1]:>8.4f} {m[2]:>8.4f} {m[3]:>8.4f} {m[4]:>7d}")

# 触发子集上的净效果（只看被触发的题，改写 vs 不改写）
print("\n  ── 被触发子集分析（门槛 top1<0.70）──")
trig_rows = [r for r in rows if r["has_rw"] and r["len"] > 12 and r["top1"] < 0.70]
if trig_rows:
    b_r10 = sum(r["base"][2] for r in trig_rows) / len(trig_rows)
    m_r10 = sum(r["merged"][2] for r in trig_rows) / len(trig_rows)
    b_mrr = np.mean([r["base"][0] for r in trig_rows])
    m_mrr = np.mean([r["merged"][0] for r in trig_rows])
    gained = sum(1 for r in trig_rows if not r["base"][2] and r["merged"][2])
    lost = sum(1 for r in trig_rows if r["base"][2] and not r["merged"][2])
    print(f"  触发 {len(trig_rows)} 题：MRR {b_mrr:.4f}→{m_mrr:.4f}  R@10 {b_r10:.3f}→{m_r10:.3f}  "
          f"新召回 +{gained} 题 / 丢失 -{lost} 题")

with open("eval_rewrite_gated.json", "w", encoding="utf-8") as f:
    json.dump({"rows": rows, "baseline": base_m, "full": full_m}, f, ensure_ascii=False, indent=2)
print("\n  saved: eval_rewrite_gated.json")
