#!/usr/bin/env python3
"""CK-guided Query Understanding A/B 评测

BASE (use_ck=False) vs CK (use_ck=True)，同一检索管线（+Rewrite + Reranker）。

指标链:
  ① Rewrite 成功率（产出非空改写的题占比）
  ② CK 命中率（CK Matcher 返回 ≥1 条的题占比）
  ③ CK 命中 Gold 比例（top1 CK 的 chunk_id ∈ gold_chunks 的题占比）
  ④ Rewrite 后 Gold Dense Cosine（CK 版 rewrite_queries[0] 对 gold chunk 的余弦）
  ⑤ BM25 Recall@10（原始 query 独立）
  ⑥ Dense Recall@10（原始 query 独立）
  ⑦ RRF Recall@10（原始 query，无改写）
  ⑧ 最终 Recall@10 / MRR / R@5 / Top1（search_multi_query 带改写）
  ⑨ CK Rescue Rate（BASE 最终 R@10=0 的题中 CK 版救回的比例）
  ⑩ CK top1/top2/top3 sim + margin 分布

用法:
  python3 eval_v2_ck_ab.py                # 全量 in-domain + OOD
  python3 eval_v2_ck_ab.py --limit 10     # 小样本冒烟
"""

import json
import sys
import time
from collections import defaultdict

import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed

LIMIT = None
if "--limit" in sys.argv:
    LIMIT = int(sys.argv[sys.argv.index("--limit") + 1])

print("[eval] 加载数据...", flush=True)
with open("data/golden_set_v2.json") as f:
    gs = json.load(f)
if LIMIT:
    gs = gs[:LIMIT]

ood_qs = [q for q in gs if q["capability"] == "ood_detection"]
indomain_qs = [q for q in gs if q["capability"] != "ood_detection"]
print(f"[eval] In-domain: {len(indomain_qs)} | OOD: {len(ood_qs)}", flush=True)

from hybrid_search import HybridSearcher
from query_rewriter import expand_query, get_keywords, get_rewrite_queries, get_sub_queries, _save_cache
import query_rewriter as qrw
import ck_matcher
from ck_matcher import match as ck_match, context_from_matches

searcher = HybridSearcher(enable_reranker=True)

# ── 阶段 1a: 初始检索（并行）+ CK 匹配 / Knowledge Context 预计算（主线程，embedding 不并发）──
print("[eval] 阶段 1a: 初始检索 + CK 匹配预计算...", flush=True)
t0 = time.time()
ck_matcher._load()
_ = ck_match(gs[0]["question"], topk=3)  # 预热 embedding 模型


def _initial_search(q):
    query = q["question"]
    initial = searcher.search(query, top_k=2, expand_context=False)
    top1_sim = initial[0].get("dense_similarity", initial[0].get("similarity", 0)) if initial else 0
    top2_sim = initial[1].get("dense_similarity", initial[1].get("similarity", 0)) if len(initial) > 1 else 0
    return q["id"], top1_sim, top2_sim


init_sims = {}
with ThreadPoolExecutor(max_workers=4) as ex:
    for qid, t1, t2 in ex.map(_initial_search, gs):
        init_sims[qid] = (t1, t2)

rewrite_meta = {}  # qid → {base: {...}, ck: {...}, ck_match: {...}, _sims, _kctx}
for q in gs:
    ck_m = ck_match(q["question"], topk=3)
    rewrite_meta[q["id"]] = {
        "ck_match": [{"chunk_id": m["chunk_id"], "sim": m["sim"]} for m in ck_m],
        "_sims": init_sims[q["id"]],
        "_kctx": context_from_matches(ck_m),
    }
print(f"[eval] 初始检索 + CK 匹配完成 ({time.time()-t0:.0f}s)", flush=True)

# ── 阶段 1b: 改写预计算（并行；每题的 base/ck 在同一 worker 串行，registry 无竞争）──
print("[eval] 阶段 1b: 改写预计算 (BASE + CK, 8 workers)...", flush=True)
t0 = time.time()


def _run_rewrite(q):
    query = q["question"]
    t1, t2 = rewrite_meta[q["id"]]["_sims"]
    kctx = rewrite_meta[q["id"]]["_kctx"]
    out = {}
    for tag, use_ck, tag_kctx in (("base", False, ""), ("ck", True, kctx)):
        expand_query(query, mode="all", top1_sim=t1, top2_sim=t2,
                     use_ck=use_ck, knowledge_context=tag_kctx)
        out[tag] = {
            "keywords": get_keywords(query),
            "rewrite_queries": get_rewrite_queries(query),
            "sub_queries": get_sub_queries(query),
        }
    return q["id"], out


qrw._AUTO_SAVE = False
try:
    with ThreadPoolExecutor(max_workers=8) as ex:
        done = 0
        for qid, tag_meta in ex.map(_run_rewrite, gs):
            rewrite_meta[qid].update(tag_meta)
            done += 1
            if done % 50 == 0:
                print(f"  进度: {done}/{len(gs)}", flush=True)
finally:
    qrw._AUTO_SAVE = True
_save_cache()
print(f"[eval] 改写完成 ({time.time()-t0:.0f}s)", flush=True)


# ── 阶段 2: 阶段召回 + 最终检索（每路）──
def stage_recall(q, gold):
    """原始 query 的阶段召回: bm25 / dense / rrf (chunk_id 直接比对 gold)。"""
    query = q["question"]
    out = {}
    for stage, cids in [
        ("bm25", [d.metadata["chunk_id"] for d in searcher._bm25_retriever.invoke(query)[:10]]),
        ("dense", [searcher._chunk_key(d) for d, _ in
                   searcher._vectorstore.similarity_search_with_score(query, k=10)]),
    ]:
        out[f"{stage}_recall10"] = any(c in gold for c in cids)

    rrf_scores, doc_store, _ = searcher._rrf_retrieve(query, dense_k=30, bm25_k=20)
    rrf_top10 = sorted(rrf_scores.items(), key=lambda x: -x[1])[:10]
    out["rrf_recall10"] = any(k in gold for k, _ in rrf_top10)
    return out


def process_one(q):
    query = q["question"]
    gold = set(q["gold_chunks"])
    r = {"id": q["id"], "capability": q["capability"], "difficulty": q["difficulty"],
         "gold_count": len(gold), "gold": sorted(gold)}
    r.update(stage_recall(q, gold))

    for tag in ("base", "ck"):
        m = rewrite_meta[q["id"]][tag]
        judge, merged = searcher.search_multi_query(
            query, top_k=10, expand_context=True,
            rewrite_queries=m["rewrite_queries"],
            sub_queries=m["sub_queries"],
            keyword_queries=m["keywords"],
        )
        retrieved_ids = [rr.get("metadata", {}).get("chunk_id", "") for rr in merged]
        rr_val = 0.0
        for i, rid in enumerate(retrieved_ids):
            if rid in gold:
                rr_val = 1.0 / (i + 1)
                break
        r[tag] = {
            "rewrite_hit": bool(m["rewrite_queries"] or m["sub_queries"] or m["keywords"]),
            "n_rewrite": len(m["rewrite_queries"]), "n_subq": len(m["sub_queries"]),
            "n_kw": len(m["keywords"]),
            "mrr": rr_val,
            "recall_5": any(rid in gold for rid in retrieved_ids[:5]),
            "recall_10": any(rid in gold for rid in retrieved_ids[:10]),
            "top1": rr_val >= 1.0,
            "hit_count": sum(1 for rid in retrieved_ids[:10] if rid in gold),
        }

    # ④ CK 版 rewrite_queries[0] 对 gold chunk 的 dense cosine
    rw_q = rewrite_meta[q["id"]]["ck"]["rewrite_queries"]
    r["ck_rw_gold_cosine"] = 0.0
    if rw_q:
        chroma_raw = searcher._vectorstore.similarity_search_with_score(rw_q[0], k=50)
        for doc, l2 in chroma_raw:
            cid = searcher._chunk_key(doc)
            if cid in gold:
                r["ck_rw_gold_cosine"] = round(1.0 - l2, 4)
                break
    return r


print("[eval] 阶段 2: 阶段召回 + 最终检索...", flush=True)
t0 = time.time()
results = []
with ThreadPoolExecutor(max_workers=4) as ex:
    futures = {ex.submit(process_one, q): q["id"] for q in indomain_qs}
    done = 0
    for f in as_completed(futures):
        results.append(f.result())
        done += 1
        if done % 20 == 0:
            print(f"  进度: {done}/{len(indomain_qs)}", flush=True)
print(f"[eval] 检索完成 ({time.time()-t0:.0f}s)", flush=True)


# ── 阶段 3: 汇总 ──
def summarize(results, tag):
    n = len(results)
    mrr = np.mean([r[tag]["mrr"] for r in results])
    r5 = sum(r[tag]["recall_5"] for r in results) / n
    r10 = sum(r[tag]["recall_10"] for r in results) / n
    top1 = sum(r[tag]["top1"] for r in results) / n
    zero = [r for r in results if not r[tag]["recall_10"]]
    rw_hit = sum(r[tag]["rewrite_hit"] for r in results) / n
    return {"mrr": mrr, "r5": r5, "r10": r10, "top1": top1,
            "zero": zero, "rw_hit": rw_hit, "n": n}


s_base = summarize(results, "base")
s_ck = summarize(results, "ck")

# ⑨ CK Rescue Rate: BASE 最终 R@10=0 的题中，CK 版救回（R@10=1）的比例
base_zero_ids = {r["id"] for r in s_base["zero"]}
ck_zero_ids = {r["id"] for r in s_ck["zero"]}
rescue_ids = base_zero_ids - ck_zero_ids
rescue_rate = len(rescue_ids) / len(base_zero_ids) if base_zero_ids else 0.0

# ②③⑩ CK 匹配统计
ck_hit = 0          # 有 ≥1 条 CK 的题
ck_gold_top1 = 0    # top1 CK ∈ gold 的题
sims1, sims2, sims3, margins = [], [], [], []
for q in indomain_qs:
    m = rewrite_meta[q["id"]]["ck_match"]
    if m:
        ck_hit += 1
        gold = set(q["gold_chunks"])
        if m[0]["chunk_id"] in gold:
            ck_gold_top1 += 1
    s = [x["sim"] for x in m]
    if len(s) >= 1: sims1.append(s[0])
    if len(s) >= 2: sims2.append(s[1])
    if len(s) >= 3: sims3.append(s[2])
    if len(s) >= 2: margins.append(s[0] - s[1])

n_in = len(indomain_qs)
ck_gold_top1_rate = ck_gold_top1 / n_in
ck_hit_rate = ck_hit / n_in

# ⑥ 阶段 Recall（原始 query）
stages = ["bm25", "dense", "rrf"]
stage_stat = {st: sum(r[f"{st}_recall10"] for r in results) / n_in for st in stages}

# ④ gold cosine（仅 CK 版 rewrite 非空的题）
cos_list = [r["ck_rw_gold_cosine"] for r in results if r["ck_rw_gold_cosine"] > 0]
avg_cos = np.mean(cos_list) if cos_list else 0.0

print(f"\n{'='*70}")
print(f"  CK-guided Query Understanding A/B 评测 (in-domain {n_in} 题)")
print(f"{'='*70}")
print(f"\n  -- 最终指标对比 --")
print(f"  {'Config':<8s} {'MRR':>7s} {'R@5':>7s} {'R@10':>7s} {'Top1':>7s} {'R@10=0':>8s} {'RW命中':>7s}")
print(f"  {'-'*58}")
print(f"  {'BASE':<8s} {s_base['mrr']:>7.4f} {s_base['r5']:>7.4f} {s_base['r10']:>7.4f} "
      f"{s_base['top1']:>7.4f} {len(s_base['zero']):>8d} {s_base['rw_hit']:>7.4f}")
print(f"  {'CK':<8s} {s_ck['mrr']:>7.4f} {s_ck['r5']:>7.4f} {s_ck['r10']:>7.4f} "
      f"{s_ck['top1']:>7.4f} {len(s_ck['zero']):>8d} {s_ck['rw_hit']:>7.4f}")
print(f"  Δ         {s_ck['mrr']-s_base['mrr']:>+7.4f} {s_ck['r5']-s_base['r5']:>+7.4f} "
      f"{s_ck['r10']-s_base['r10']:>+7.4f} {s_ck['top1']-s_base['top1']:>+7.4f} "
      f"{len(s_ck['zero'])-len(s_base['zero']):>+8d}")

print(f"\n  -- 阶段 Recall@10（原始 query，无改写）--")
for st in stages:
    print(f"  {st.upper():<6s}: {stage_stat[st]:.4f}")

print(f"\n  -- CK Matcher 统计 --")
print(f"  CK 命中率 (≥1条):     {ck_hit_rate:.4f} ({ck_hit}/{n_in})")
print(f"  CK top1 命中 Gold:    {ck_gold_top1_rate:.4f} ({ck_gold_top1}/{n_in})")
print(f"  top1 sim: 均值 {np.mean(sims1):.4f}  min {min(sims1):.4f}  max {max(sims1):.4f}" if sims1 else "  top1 sim: N/A")
print(f"  top2 sim: 均值 {np.mean(sims2):.4f}" if sims2 else "  top2 sim: N/A")
print(f"  top3 sim: 均值 {np.mean(sims3):.4f}" if sims3 else "  top3 sim: N/A")
print(f"  margin(top1-top2): 均值 {np.mean(margins):.4f}  min {min(margins):.4f}" if margins else "  margin: N/A")
print(f"  CK rewrite→gold cosine 均值: {avg_cos:.4f} (n={len(cos_list)})")

print(f"\n  -- CK Rescue Rate --")
print(f"  BASE 失败题: {len(base_zero_ids)}")
print(f"  CK 救回:     {len(rescue_ids)}")
print(f"  Rescue Rate: {rescue_rate:.4f} ({rescue_rate*100:.1f}%)")
if rescue_ids:
    print(f"  救回的题: {sorted(rescue_ids)}")
if base_zero_ids - rescue_ids:
    print(f"  仍未救回: {sorted(base_zero_ids - rescue_ids)}")

# 按 capability
cap_rows = defaultdict(list)
for r in results:
    cap_rows[r["capability"]].append(r)
print(f"\n  -- 按 Capability (MRR / R@10) --")
for cap in sorted(cap_rows):
    rows = cap_rows[cap]
    b_mrr = np.mean([r["base"]["mrr"] for r in rows])
    c_mrr = np.mean([r["ck"]["mrr"] for r in rows])
    b_r10 = sum(r["base"]["recall_10"] for r in rows) / len(rows)
    c_r10 = sum(r["ck"]["recall_10"] for r in rows) / len(rows)
    print(f"  {cap:<22s} n={len(rows):>3d}  BASE {b_mrr:.3f}/{b_r10:.3f}  "
          f"CK {c_mrr:.3f}/{c_r10:.3f}  Δ {c_mrr-b_mrr:+.3f}/{c_r10-b_r10:+.3f}")

# 保存明细
out_path = "eval_ck_ab_results.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump({
        "summary": {
            "base": {k: v for k, v in s_base.items() if k != "zero"},
            "ck": {k: v for k, v in s_ck.items() if k != "zero"},
            "stage_recall": stage_stat,
            "ck_hit_rate": ck_hit_rate,
            "ck_gold_top1_rate": ck_gold_top1_rate,
            "ck_sims": {"top1": sims1, "top2": sims2, "top3": sims3, "margin": margins},
            "ck_rw_gold_cosine_avg": avg_cos,
            "rescue": {"base_zero": sorted(base_zero_ids),
                       "rescued": sorted(rescue_ids),
                       "still_fail": sorted(base_zero_ids - rescue_ids),
                       "rate": rescue_rate},
        },
        "per_question": results,
    }, f, ensure_ascii=False, indent=2)
print(f"\n  saved: {out_path}")

# ── OOD ──
if ood_qs:
    print(f"\n{'='*70}")
    print(f"  OOD 检测 A/B ({len(ood_qs)} 题)")
    print(f"{'='*70}")
    from judge import judge

    def _ood_task(pair):
        q, tag = pair
        query = q["question"]
        m = rewrite_meta[q["id"]][tag]
        judge_results, _ = searcher.search_multi_query(
            query, top_k=5, expand_context=True,
            rewrite_queries=m["rewrite_queries"],
            sub_queries=m["sub_queries"],
            keyword_queries=m["keywords"],
        )
        j = judge(query, judge_results)
        return tag, j["decision"] == "reject"

    with ThreadPoolExecutor(max_workers=4) as ex:
        ood_results = list(ex.map(_ood_task, [(q, tag) for q in ood_qs for tag in ("base", "ck")]))
    for tag in ("base", "ck"):
        detected = sum(1 for t, d in ood_results if t == tag and d)
        print(f"  {tag.upper()}: {detected}/{len(ood_qs)} ({detected/len(ood_qs)*100:.1f}%)")

print(f"\n  评测完成")
