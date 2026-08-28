#!/usr/bin/env python3
"""三臂 Gate A/B 评测

臂定义:
  BASE = 数值 Gate（0.72/0.03）+ 原 prompt      —— 现行生产口径
  G1   = 结构 Gate + 复杂结构强制改写 prompt     —— 预期救回 Q_S03/Q_S14 类零召回
  G2   = 结构 Gate + 原 prompt                   —— 隔离 gate 变量（G1 vs G2 = prompt 净效应）

用法:
  python3 eval_gate_ab.py                        # 快速版: 仅 Phase 1+2 候选采集（无 CE）
  python3 eval_gate_ab.py --full-ce              # 全 CE 版: 全流程 MRR/R@K（决胜指标，先跑快速版）
  python3 eval_gate_ab.py --arms BASE,G1 --full-ce

缓存: BASE 复用现有裸键缓存（0 LLM 成本）；G1/G2 使用 query|g1 / query|g2 独立键，
      G2 复用 BASE 的 LLM 产物条目。跑完统一持久化回 rewrite_cache.json。
      注意: 若调整强制改写指令内容，需先清掉缓存中的 query|g1 键。
"""

import argparse
import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import query_rewriter as qr
from hybrid_search import HybridSearcher

OUT = "data/gate_ab_report.json"
OUT_CE = "data/gate_ab_ce_report.json"

ARMS = {
    "BASE": dict(gate_mode="base", struct_force=False),
    "G1":   dict(gate_mode="struct", struct_force=True),
    "G2":   dict(gate_mode="struct", struct_force=False),
}

qr.CACHE_MAX = 5000    # 三臂并行缓存写入不触发 LRU 淘汰（保 BASE 裸键条目稳定）
qr._AUTO_SAVE = False  # 并行阶段不落盘，结束后统一 _save_cache()

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

searcher0 = HybridSearcher(enable_reranker=False)


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


# ── Phase 0a: 每题 top1/top2（三臂共享；base 数值 Gate 的 margin 信号）──
def sims_one(q):
    initial = searcher0.search(q["question"], top_k=2, expand_context=True)
    top1 = initial[0].get("similarity", 0) if len(initial) > 0 else 0
    top2 = initial[1].get("similarity", 0) if len(initial) > 1 else 0
    return q["id"], (top1, top2)


# ── Phase 0b: 三臂改写池（G1/G2 触发 LLM；BASE 命中裸键缓存 0 成本）──
pools = {}   # (qid, arm) -> {"rw","sq","kw","info"}


def rewrite_one(q, arm, top1, top2):
    cfg = ARMS[arm]
    query = q["question"]
    qr.expand_query(query, mode="all", top1_sim=top1, top2_sim=top2, **cfg)
    return {
        "key": (q["id"], arm),
        "rw": qr.get_rewrite_queries(query, **cfg),
        "sq": qr.get_sub_queries(query, **cfg),
        "kw": qr.get_keywords(query, **cfg),
        "info": qr.get_gate_info(query, **cfg),
    }


def phase0(arms, workers=6):
    global pools
    t0 = time.time()
    sims = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for qid, pair in ex.map(sims_one, gs):
            sims[qid] = pair
    print(f"[sims] top1/top2 完成 {time.time()-t0:.0f}s", flush=True)

    t0 = time.time()
    tasks = [(q, arm) for q in gs for arm in arms]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(rewrite_one, q, arm, sims[q["id"]][0], sims[q["id"]][1])
                   : (q["id"], arm) for q, arm in tasks}
        done = 0
        for f in as_completed(futures):
            r = f.result()
            pools[r["key"]] = {"rw": r["rw"], "sq": r["sq"], "kw": r["kw"], "info": r["info"]}
            done += 1
            if done % 60 == 0 or done == len(tasks):
                print(f"[rewrite] {done}/{len(tasks)} ({time.time()-t0:.0f}s)", flush=True)
    qr._save_cache()
    print(f"[rewrite] 完成 {time.time()-t0:.0f}s | 缓存已持久化", flush=True)
    return sims


# ── 门控触发统计 ──
def gate_stats(arm):
    infos = [pools[(q["id"], arm)]["info"] for q in gs]
    n = len(infos)
    return {
        "n": n,
        "struct_hit": sum(1 for i in infos if i.get("struct_hit")),
        "llm_product": sum(1 for i in infos if i.get("llm_entry")),
        "fresh_llm_calls": sum(1 for i in infos if i.get("called_llm")),
        "reused_base": sum(1 for i in infos if i.get("reused_base")),
        "gate_skipped": sum(1 for i in infos if not i.get("llm_entry")),
        "rewrite_type": dict(Counter(i.get("rewrite_type") for i in infos)),
        "n_rw": sum(1 for q in gs if pools[(q["id"], arm)]["rw"]),
        "n_sq": sum(1 for q in gs if pools[(q["id"], arm)]["sq"]),
        "n_kw": sum(1 for q in gs if pools[(q["id"], arm)]["kw"]),
        "rw_total": sum(len(pools[(q["id"], arm)]["rw"]) for q in gs),
        "sq_total": sum(len(pools[(q["id"], arm)]["sq"]) for q in gs),
        "kw_total": sum(len(pools[(q["id"], arm)]["kw"]) for q in gs),
    }


# ── 快速版: Phase 1+2 候选采集 ──
def run_quick_one(q, arm):
    query = q["question"]
    p = pools[(q["id"], arm)]
    judge_results, cand_list, _, _ = searcher0._collect_candidates(
        query, 10, False, p["rw"], p["sq"], p["kw"], None, 0.1, 30, 20, 20, 10)
    gold, gold_sections = gold_matches(q)
    row = {"id": q["id"], "arm": arm, "pool": None if cand_list is None else len(cand_list),
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
        # 无任何改写输入时回退 plain search 结果（hybrid_search.py:447-448）
        for rank, r in enumerate(judge_results[:10], 1):
            if r.get("metadata", {}).get("chunk_id") in gold and not row["gold_in_fallback"]:
                row["gold_in_fallback"] = True
                row["fallback_rank"] = rank
            if r.get("metadata", {}).get("section_id") in gold_sections:
                row["sec_in_union"] = True
    return row


def agg_quick(rows, arm):
    rs = [r for r in rows if r["arm"] == arm]
    n = len(rs)
    u = sum(r["gold_in_union"] for r in rs)
    fb = sum(r["gold_in_fallback"] for r in rs)
    retr = u + fb
    sec = sum(r["sec_in_union"] for r in rs)
    ranks = sorted(r["prior_rank"] for r in rs if r["prior_rank"])
    med = ranks[len(ranks) // 2] if ranks else None
    over50 = sum(1 for r in rs if r["prior_rank"] and r["prior_rank"] > 50)
    kw_rescued = sum(1 for r in rs if r["gold_in_union"] and r["best_channel"] == "BM25-KW")
    return {"n": n, "union": u, "union_pct": u / n, "fallback": fb,
            "retrievable": retr, "retrievable_pct": retr / n, "sec_union": sec,
            "median_rank": med, "rank>50": over50, "kw_channel_hits": kw_rescued}


def retrievable(r):
    return r["gold_in_union"] or r["gold_in_fallback"]


def pair_diffs_quick(rows, a, b):
    by = defaultdict(dict)
    for r in rows:
        by[r["id"]][r["arm"]] = r
    rescued, lost, improved = [], [], []
    for qid in sorted(by):
        ra, rb = by[qid][a], by[qid][b]
        if not retrievable(ra) and retrievable(rb):
            rescued.append({"id": qid, "via": "union" if rb["gold_in_union"] else "fallback",
                            "channel": rb["best_channel"], "src": rb["gold_first_src"],
                            "prior_rank": rb["prior_rank"]})
        elif retrievable(ra) and not retrievable(rb):
            lost.append({"id": qid, "via": "union" if ra["gold_in_union"] else "fallback",
                         "channel": ra["best_channel"], "src": ra["gold_first_src"]})
        elif ra["gold_in_union"] and rb["gold_in_union"]:
            delta = ra["prior_rank"] - rb["prior_rank"]
            if delta >= 3:
                improved.append({"id": qid, f"{a}_rank": ra["prior_rank"],
                                 f"{b}_rank": rb["prior_rank"], f"{b}_channel": rb["best_channel"]})
    return {"rescued": rescued, "lost": lost, "rank_improved": improved}


# ── 全 CE 版: search_multi_query 全流程 ──
def run_ce_one(q, arm, searcher):
    query = q["question"]
    p = pools[(q["id"], arm)]
    gold, gold_sections = gold_matches(q)
    _, results = searcher.search_multi_query(
        query, top_k=10, expand_context=False,
        rewrite_queries=p["rw"], sub_queries=p["sq"], keyword_queries=p["kw"])
    chunk_ids = [r.get("metadata", {}).get("chunk_id", "") for r in results[:10]]
    rr = 0.0
    for i, cid in enumerate(chunk_ids):
        if cid in gold:
            rr = 1.0 / (i + 1)
            break
    section_ids = [r.get("metadata", {}).get("section_id", "") for r in results[:10]]
    sec_rr = 0.0
    for i, sid in enumerate(section_ids):
        if sid in gold_sections:
            sec_rr = 1.0 / (i + 1)
            break
    return {"id": q["id"], "arm": arm, "rr": rr,
            "recall_5": any(cid in gold for cid in chunk_ids[:5]),
            "recall_10": any(cid in gold for cid in chunk_ids[:10]),
            "hit_count": sum(1 for cid in chunk_ids[:10] if cid in gold),
            "gold_count": len(gold),
            "sec_rr": sec_rr,
            "sec_recall_10": any(sid in gold_sections for sid in section_ids[:10])}


def agg_ce(rows, arm):
    rs = [r for r in rows if r["arm"] == arm]
    n = len(rs)
    mrr = sum(r["rr"] for r in rs) / n
    sec_mrr = sum(r["sec_rr"] for r in rs) / n
    return {"n": n, "mrr": round(mrr, 4),
            "recall_5": sum(r["recall_5"] for r in rs) / n,
            "recall_10": sum(r["recall_10"] for r in rs) / n,
            "hit_count": sum(r["hit_count"] for r in rs),
            "sec_mrr": round(sec_mrr, 4),
            "sec_recall_10": sum(r["sec_recall_10"] for r in rs) / n}


def pair_diffs_ce(rows, a, b):
    by = defaultdict(dict)
    for r in rows:
        by[r["id"]][r["arm"]] = r
    rescued, lost = [], []
    for qid in sorted(by):
        ra, rb = by[qid][a], by[qid][b]
        if ra["rr"] == 0 and rb["rr"] > 0:
            rescued.append({"id": qid, f"{b}_rr": round(rb["rr"], 4), f"{b}_hit": rb["hit_count"]})
        elif ra["rr"] > 0 and rb["rr"] == 0:
            lost.append({"id": qid, f"{a}_rr": round(ra["rr"], 4)})
    return {"rescued": rescued, "lost": lost}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full-ce", action="store_true", help="跑全 CE 流程（MRR/R@K 决胜指标）")
    ap.add_argument("--arms", default="BASE,G1,G2", help="逗号分隔，如 BASE,G1")
    args = ap.parse_args()
    arms = [a.strip() for a in args.arms.split(",") if a.strip() in ARMS]
    if not arms:
        raise SystemExit(f"无效 --arms，可选: {list(ARMS)}")

    print(f"[gate_ab] 臂: {arms} | 模式: {'全 CE' if args.full_ce else '快速(无CE)'} | {len(gs)} 题")

    # ── Phase 0 ──
    phase0(arms)

    gstats = {arm: gate_stats(arm) for arm in arms}
    print(f"\n{'='*70}\n  门控触发统计\n{'='*70}")
    for arm in arms:
        s = gstats[arm]
        print(f"  {arm:<5s} struct命中 {s['struct_hit']:>3d} | LLM产物 {s['llm_product']:>3d} "
              f"| 新调用 {s['fresh_llm_calls']:>3d} | 复用BASE {s['reused_base']:>3d} "
              f"| gate跳过 {s['gate_skipped']:>3d} | 类型 {s['rewrite_type']}")
        print(f"       有rw {s['n_rw']:>3d} (总{s['rw_total']:>3d}) | 有sq {s['n_sq']:>3d} "
              f"(总{s['sq_total']:>3d}) | 有kw {s['n_kw']:>3d} (总{s['kw_total']:>3d})")

    if not args.full_ce:
        # ── 快速版: Phase 1+2 ──
        t0 = time.time()
        rows = []
        tasks = [(q, arm) for q in gs for arm in arms]
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(run_quick_one, q, arm): (q["id"], arm) for q, arm in tasks}
            done = 0
            for f in as_completed(futures):
                rows.append(f.result())
                done += 1
                if done % 120 == 0 or done == len(tasks):
                    print(f"[collect] {done}/{len(tasks)} ({time.time()-t0:.0f}s)", flush=True)
        print(f"[collect] 完成 {time.time()-t0:.0f}s", flush=True)

        summ = {arm: agg_quick(rows, arm) for arm in arms}
        print(f"\n{'='*70}\n  快速版汇总（union ∪ plain 回退 = 管线可检索）\n{'='*70}")
        print(f"  {'臂':<5s} {'union':>6s} {'fallback':>8s} {'可检索':>9s} {'sec':>5s} "
              f"{'median':>6s} {'rank>50':>7s} {'kw命中':>6s}")
        for arm in arms:
            s = summ[arm]
            print(f"  {arm:<5s} {s['union']:>6d} {s['fallback']:>8d} "
                  f"{s['retrievable']:>5d}({s['retrievable_pct']*100:>4.1f}%) "
                  f"{s['sec_union']:>5d} {str(s['median_rank']):>6s} {s['rank>50']:>7d} "
                  f"{s['kw_channel_hits']:>6d}")

        diffs = {}
        for i, a in enumerate(arms):
            for b in arms[i + 1:]:
                diffs[f"{a}_vs_{b}"] = pair_diffs_quick(rows, a, b)
        for key, d in diffs.items():
            a, b = key.split("_vs_")
            print(f"\n  {key}: 救回 {len(d['rescued'])} | 丢失 {len(d['lost'])} | rank改善≥3 {len(d['rank_improved'])}")
            for r in d["rescued"]:
                print(f"    救回 {r['id']}: channel={r['channel']} prior_rank={r['prior_rank']} src={r['src']}")
            for r in d["lost"]:
                print(f"    丢失 {r['id']}: channel={r['channel']} src={r['src']}")
            for r in d["rank_improved"]:
                print(f"    改善 {r['id']}: rank {r[f'{a}_rank']} → {r[f'{b}_rank']} ({r[f'{b}_channel']})")

        report = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "quick",
                  "arms": arms, "gate_stats": gstats, "summary": summ, "diffs": diffs,
                  "per_question": {qid: {r["arm"]: r for r in rows if r["id"] == qid}
                                   for qid in sorted(q["id"] for q in gs)}}
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n  报告已保存: {OUT}")
    else:
        # ── 全 CE 版 ──
        t0 = time.time()
        searcher = HybridSearcher(enable_reranker=True)
        print(f"[ce] reranker 加载完成 {time.time()-t0:.0f}s", flush=True)
        rows = []
        tasks = [(q, arm) for q in gs for arm in arms]
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(run_ce_one, q, arm, searcher): (q["id"], arm)
                       for q, arm in tasks}
            done = 0
            for f in as_completed(futures):
                rows.append(f.result())
                done += 1
                if done % 60 == 0 or done == len(tasks):
                    print(f"[ce] {done}/{len(tasks)} ({time.time()-t0:.0f}s)", flush=True)
        print(f"[ce] 完成 {time.time()-t0:.0f}s", flush=True)

        summ = {arm: agg_ce(rows, arm) for arm in arms}
        print(f"\n{'='*70}\n  全 CE 汇总\n{'='*70}")
        print(f"  {'臂':<5s} {'MRR':>8s} {'R@5':>8s} {'R@10':>8s} {'hit':>5s} "
              f"{'sec_MRR':>8s} {'sec_R@10':>9s}")
        for arm in arms:
            s = summ[arm]
            print(f"  {arm:<5s} {s['mrr']:>8.4f} {s['recall_5']:>7.1%} {s['recall_10']:>7.1%} "
                  f"{s['hit_count']:>5d} {s['sec_mrr']:>8.4f} {s['sec_recall_10']:>8.1%}")

        diffs = {}
        for i, a in enumerate(arms):
            for b in arms[i + 1:]:
                diffs[f"{a}_vs_{b}"] = pair_diffs_ce(rows, a, b)
        for key, d in diffs.items():
            a, b = key.split("_vs_")
            print(f"\n  {key}: MRR {summ[a]['mrr']:.4f} → {summ[b]['mrr']:.4f} "
                  f"(Δ {summ[b]['mrr']-summ[a]['mrr']:+.4f}) | 救回 {len(d['rescued'])} | 丢失 {len(d['lost'])}")
            for r in d["rescued"]:
                print(f"    救回 {r['id']}: {b}_rr={r[f'{b}_rr']} hit={r[f'{b}_hit']}")
            for r in d["lost"]:
                print(f"    丢失 {r['id']}: {a}_rr={r[f'{a}_rr']}")

        report = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "ce",
                  "arms": arms, "gate_stats": gstats, "summary": summ, "diffs": diffs,
                  "per_question": {qid: {r["arm"]: r for r in rows if r["id"] == qid}
                                   for qid in sorted(q["id"] for q in gs)}}
        with open(OUT_CE, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n  报告已保存: {OUT_CE}")


if __name__ == "__main__":
    main()
