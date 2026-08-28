#!/usr/bin/env python3
"""生产口径 A/B 快速对比（无 CE，仅 Phase 1+2 候选采集）

Path A = 旧生产口径: rag_pipeline 修复前 —— extra_queries=混池(改写+子查询)，无 keyword_queries
Path B = 新生产口径: 三池分路传递（= 评测口径 eval_v2_full）

对比: gold 进 union 数量、retrieval_prior rank、逐题差异。
不跑 CE（_coverage_reserve_and_rerank 之外），成本 ~1-2 分钟。
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from query_rewriter import expand_query, get_keywords, get_rewrite_queries, get_sub_queries
from hybrid_search import HybridSearcher

OUT = "data/prod_ab_quick_report.json"

with open("data/golden_set_v2.json") as f:
    gs_all = json.load(f)
gs = [q for q in gs_all if q["capability"] != "ood_detection"]
with open("data/chunks_split.json") as f:
    chunks = json.load(f)

cid_to_sid = {c["id"]: c["metadata"]["section_id"] for c in chunks}
source_to_sid = {}
for c in chunks:
    sid = c.get("source_id", c["metadata"].get("section_id", ""))
    if sid:
        source_to_sid.setdefault(sid, set()).add(c["metadata"]["section_id"])

searcher = HybridSearcher(enable_reranker=False)

# ── Phase 0: 改写三池（缓存命中，无 LLM 调用）──
t0 = time.time()
pools = {}
for i, q in enumerate(gs):
    query = q["question"]
    initial = searcher.search(query, top_k=2, expand_context=True)
    top1_sim = initial[0].get("similarity", 0) if len(initial) > 0 else 0
    top2_sim = initial[1].get("similarity", 0) if len(initial) > 1 else 0
    expanded = expand_query(query, mode="all", top1_sim=top1_sim, top2_sim=top2_sim)
    pools[query] = {
        "merged": [x for x in expanded if x != query],
        "rw": get_rewrite_queries(query),
        "sq": get_sub_queries(query),
        "kw": get_keywords(query),
    }
    if (i + 1) % 30 == 0:
        print(f"[rewrite] {i+1}/{len(gs)} ({time.time()-t0:.0f}s)", flush=True)
n_kw = sum(1 for p in pools.values() if p["kw"])
n_rw = sum(1 for p in pools.values() if p["rw"])
n_sq = sum(1 for p in pools.values() if p["sq"])
print(f"[rewrite] 完成 {time.time()-t0:.0f}s | 有kw {n_kw} | 有rw {n_rw} | 有sq {n_sq} | "
      f"kw总量 {sum(len(p['kw']) for p in pools.values())}", flush=True)


def gold_matches(q):
    gold = set(q["gold_chunks"])
    gold_sections = {cid_to_sid[cid] for cid in gold if cid in cid_to_sid}
    if not gold_sections:
        for cid in gold:
            doc_stem = (cid.rsplit("_s", 1)[0] if "_s" in cid
                        else cid.rsplit("_t", 1)[0] if "_t" in cid else cid)
            for sid, secs in source_to_sid.items():
                if sid.startswith(doc_stem):
                    gold_sections.update(secs)
    return gold, gold_sections


def run_one(q, path):
    query = q["question"]
    p = pools[query]
    if path == "A":
        judge_results, cand_list, _, _ = searcher._collect_candidates(
            query, 10, False, [], [], [], p["merged"], 0.1, 30, 20, 20, 10)
    else:
        judge_results, cand_list, _, _ = searcher._collect_candidates(
            query, 10, False, p["rw"], p["sq"], p["kw"], None, 0.1, 30, 20, 20, 10)

    gold, gold_sections = gold_matches(q)
    row = {"id": q["id"], "path": path, "pool": None if cand_list is None else len(cand_list),
           "gold_in_union": False, "sec_in_union": False, "prior_rank": None,
           "best_channel": None, "best_rank": None, "gold_first_src": None,
           "gold_in_fallback": False, "fallback_rank": None}
    if cand_list:
        for rank, c in enumerate(cand_list, 1):
            if c["chunk_id"] in gold and not row["gold_in_union"]:
                row["gold_in_union"] = True
                row["prior_rank"] = rank
                row["best_channel"] = c["best_channel"]
                row["best_rank"] = c["best_rank"]
                row["gold_first_src"] = sorted(c["sources"])
            if (c["metadata"].get("section_id") in gold_sections
                    and not row["sec_in_union"]):
                row["sec_in_union"] = True
    else:
        # 管线行为：无任何改写输入时回退 plain search 结果（hybrid_search.py:447-448）
        for rank, r in enumerate(judge_results[:10], 1):
            if r.get("metadata", {}).get("chunk_id") in gold and not row["gold_in_fallback"]:
                row["gold_in_fallback"] = True
                row["fallback_rank"] = rank
            if r.get("metadata", {}).get("section_id") in gold_sections:
                row["sec_in_union"] = True
    return row


# ── Phase 1+2: 两口径各跑一遍候选采集 ──
t0 = time.time()
rows = []
workers = 6
tasks = [(q, path) for q in gs for path in ("A", "B")]
with ThreadPoolExecutor(max_workers=workers) as ex:
    futures = {ex.submit(run_one, q, path): (q["id"], path) for q, path in tasks}
    done = 0
    for f in as_completed(futures):
        rows.append(f.result())
        done += 1
        if done % 60 == 0 or done == len(tasks):
            print(f"[collect] {done}/{len(tasks)} ({time.time()-t0:.0f}s)", flush=True)
print(f"[collect] 完成 {time.time()-t0:.0f}s", flush=True)

by_qid = {}
for r in rows:
    by_qid.setdefault(r["id"], {})[r["path"]] = r

# ── 汇总 ──
def agg(path):
    rs = [r for r in rows if r["path"] == path]
    n = len(rs)
    u = sum(r["gold_in_union"] for r in rs)
    fb = sum(r["gold_in_fallback"] for r in rs)
    retr = u + fb  # union ∪ plain-search 回退 = 管线可检索集合
    sec = sum(r["sec_in_union"] for r in rs)
    ranks = [r["prior_rank"] for r in rs if r["prior_rank"]]
    ranks_sorted = sorted(ranks)
    med = ranks_sorted[len(ranks_sorted) // 2] if ranks_sorted else None
    over50 = sum(1 for r in rs if r["prior_rank"] and r["prior_rank"] > 50)
    kw_rescued = sum(1 for r in rs if r["gold_in_union"] and r["best_channel"] == "BM25-KW")
    return {"n": n, "union": u, "union_pct": u / n, "fallback": fb,
            "retrievable": retr, "retrievable_pct": retr / n, "sec_union": sec,
            "median_rank": med, "rank>50": over50, "kw_channel_hits": kw_rescued}

summ = {"A": agg("A"), "B": agg("B")}

# 逐题差异（以"管线可检索集合"= union ∪ fallback 为准）
def retrievable(r):
    return r["gold_in_union"] or r["gold_in_fallback"]

rescued_by_B = []   # A 不可检索、B 可检索（修复救回的信号）
lost_by_B = []      # A 可检索、B 不可检索
rank_improved = []  # 两口径都在 union，B 的 prior rank 更好
for qid in sorted(by_qid):
    a, b = by_qid[qid]["A"], by_qid[qid]["B"]
    if not retrievable(a) and retrievable(b):
        rescued_by_B.append({"id": qid, "via": "union" if b["gold_in_union"] else "fallback",
                             "channel": b["best_channel"],
                             "src": b["gold_first_src"], "prior_rank": b["prior_rank"]})
    elif retrievable(a) and not retrievable(b):
        lost_by_B.append({"id": qid, "via": "union" if a["gold_in_union"] else "fallback",
                          "channel": a["best_channel"], "src": a["gold_first_src"]})
    elif a["gold_in_union"] and b["gold_in_union"]:
        delta = a["prior_rank"] - b["prior_rank"]
        if delta >= 3:
            rank_improved.append({"id": qid, "A_rank": a["prior_rank"],
                                  "B_rank": b["prior_rank"], "B_channel": b["best_channel"]})

report = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "summary": summ,
          "rescued_by_B": rescued_by_B, "lost_by_B": lost_by_B,
          "rank_improved": rank_improved,
          "per_question": {qid: by_qid[qid] for qid in sorted(by_qid)}}
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)

print(f"\n{'='*64}")
print(f"  生产口径 A/B 快速对比（无 CE，{summ['A']['n']} 题）")
print(f"{'='*64}")
print(f"  {'口径':<24s} {'union':>6s} {'fallback':>8s} {'可检索':>7s} {'sec':>5s} {'rank>50':>7s} {'kw命中':>6s}")
print(f"  {'-'*70}")
for path, label in (("A", "A 旧生产(混池无kw)"), ("B", "B 新生产(三池=评测)")):
    s = summ[path]
    print(f"  {label:<24s} {s['union']:>6d} {s['fallback']:>8d} {s['retrievable']:>5d} "
          f"({s['retrievable_pct']*100:>4.1f}%) {s['sec_union']:>5d} {s['rank>50']:>7d} {s['kw_channel_hits']:>6d}")
print(f"\n  B 救回（A 无 gold ∈ union，B 有）: {len(rescued_by_B)} 题")
for r in rescued_by_B:
    print(f"    {r['id']}: channel={r['channel']} prior_rank={r['prior_rank']} src={r['src']}")
print(f"  B 丢失（A 有，B 无）: {len(lost_by_B)} 题")
for r in lost_by_B:
    print(f"    {r['id']}: channel={r['channel']} src={r['src']}")
print(f"  prior rank 改善 ≥3（两口径都有 gold）: {len(rank_improved)} 题")
for r in rank_improved:
    print(f"    {r['id']}: rank {r['A_rank']} → {r['B_rank']} ({r['B_channel']})")
print(f"\n  报告已保存: {OUT}")
