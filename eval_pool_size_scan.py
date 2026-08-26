#!/usr/bin/env python3
"""Phase 3 池大小扫描（50/60/80）：采集一次候选池 + CE 打分一次，解析式重放三个池大小。

口径：180 in-domain 原始 query（改写全部来自缓存，无 LLM 调用）；
候选收集 = 生产 _collect_candidates（Phase 1+2，含 Evidence + Retrieval Prior）；
每 query 按 max_pool=80 的 Phase 3 配额逻辑得到并集池（50/60 的池是它的前缀，
CE 逐对打分与池组成无关），CE 只打一次分；随后解析式重放 50/60/80 的
CE min-max 归一化 + final=α×prior+(1-α)×ce_norm 融合（α=生产默认，现为 0.3），评估最终 top-10。

中间召回结果全量落盘（data/pool_size_scan_report.json）：
  - 每题的池组成（quota 保留数 / global fill 数 / 全量候选数）
  - 每题 × 每池大小的 top-10 chunk_id 列表与 gold 命中
  - 汇总：MRR/R@5/R@10/Top1/R@10=0 + 按 capability + 逐题 50→60/50→80 变化

用法: python3 eval_pool_size_scan.py
"""
import json
import math
import time
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "data" / "pool_size_scan_report.json"

POOL_SIZES = [50, 60, 80]
ALPHA = 0.3  # 2026-08-26 起生产默认（原 0.2）
LAMBDA_LENGTH = 0.1
TOP_K = 10


def phase3_pool(cand_list, rw_list, sq_list, max_pool):
    """复刻 _coverage_reserve_and_rerank Phase 3 语义。

    Returns: (pool, n_reserved)。len(cand_list) ≤ max_pool 时不触发配额，
    n_reserved 为 None；否则 pool = (reserved + rest)[:max_pool]。
    """
    if len(cand_list) <= max_pool:
        return list(cand_list), None

    n_rw = len(rw_list)
    n_sq = len(sq_list)
    rw_qids = set(range(1, 1 + n_rw))
    sq_start = 1 + n_rw

    quota_orig = 20
    quota_rw = 10 if n_rw > 0 else 0
    sub_budget = 20 if n_sq > 0 else 0

    sq_quotas = {}
    if n_sq > 0:
        base = sub_budget // n_sq
        remainder = sub_budget % n_sq
        for i in range(n_sq):
            sq_quotas[sq_start + i] = base + (1 if i < remainder else 0)

    reserved, rest = [], []
    taken = {}
    rw_taken = 0

    for c in cand_list:
        placed = False
        # 1) Original: 20
        if 0 in c["query_hits"] and taken.get(0, 0) < quota_orig:
            reserved.append(c)
            taken[0] = taken.get(0, 0) + 1
            placed = True
        # 2) 各 SubQ 按均分配额
        elif not placed:
            for sq_qid, sq_quota in sq_quotas.items():
                if sq_qid in c["query_hits"] and taken.get(sq_qid, 0) < sq_quota:
                    reserved.append(c)
                    taken[sq_qid] = taken.get(sq_qid, 0) + 1
                    placed = True
                    break
        # 3) Rewrite 组: 10 共享
        if not placed and rw_qids and rw_taken < quota_rw and (rw_qids & c["query_hits"]):
            reserved.append(c)
            rw_taken += 1
            placed = True

        if not placed:
            rest.append(c)

    return (reserved + rest)[:max_pool], len(reserved)


def main():
    t0 = time.time()
    gs = [q for q in json.load(open(BASE / "data" / "golden_set_v2.json", encoding="utf-8"))
          if q["capability"] != "ood_detection"]
    print(f"[scan] {len(gs)} 题 | 池大小 {POOL_SIZES}", flush=True)

    # ── 改写映射（缓存命中，与 eval_v2_full 同口径）──
    from query_rewriter import expand_query, get_keywords, get_rewrite_queries, get_sub_queries
    from hybrid_search import HybridSearcher
    _searcher = HybridSearcher(enable_reranker=False)
    keyword_map, rw_map, sq_map = {}, {}, {}
    for q in gs:
        query = q["question"]
        initial = _searcher.search(query, top_k=2, expand_context=True)
        top1_sim = initial[0].get("similarity", 0) if len(initial) > 0 else 0
        top2_sim = initial[1].get("similarity", 0) if len(initial) > 1 else 0
        expand_query(query, mode="all", top1_sim=top1_sim, top2_sim=top2_sim)
        keyword_map[query] = get_keywords(query)
        rw_map[query] = get_rewrite_queries(query)
        sq_map[query] = get_sub_queries(query)
    print(f"[scan] 改写映射就绪（缓存命中，无 LLM 调用）", flush=True)

    searcher = HybridSearcher(enable_reranker=True)
    searcher._reranker._load()

    per_question = []
    for i, q in enumerate(gs):
        query = q["question"]
        gold = list(q["gold_chunks"])
        rw_queries = rw_map.get(query, [])
        sq_queries = sq_map.get(query, [])
        kws = keyword_map.get(query, [])

        judge_results, cand_list, rw_list, sq_list = searcher._collect_candidates(
            query, TOP_K, False, rw_queries, sq_queries, kws, None, LAMBDA_LENGTH,
            30, 20, 20, 10)

        if cand_list is None:
            # 无任何改写输入：生产直接返回 judge_results（search 结果）
            top_ids = [r.get("metadata", {}).get("chunk_id", "")
                       for r in judge_results[:TOP_K]]
            per_question.append({
                "qid": q["id"], "query": query, "capability": q["capability"],
                "gold": gold, "n_candidates": 0,
                "per_pool": {mp: {"top10": top_ids,
                                  "hit": any(c in gold for c in top_ids),
                                  "n_reserved": None}
                             for mp in POOL_SIZES},
            })
            continue

        # 并集池（max_pool=80 口径）+ CE 一次打分
        pool80, _ = phase3_pool(cand_list, rw_list, sq_list, 80)
        ce_adj = {}
        if pool80:
            pairs = [(query, c["text"][:500]) for c in pool80]
            with searcher._reranker._infer_lock:
                raw_ce = [float(x) for x in searcher._reranker._model.predict(
                    pairs, show_progress_bar=False)]
            ce_adj = {c["chunk_id"]: r - LAMBDA_LENGTH * math.log(len(c["text"]))
                      for c, r in zip(pool80, raw_ce)}

        row = {"qid": q["id"], "query": query, "capability": q["capability"],
               "gold": gold, "n_candidates": len(cand_list), "per_pool": {}}
        for mp in POOL_SIZES:
            pool, n_reserved = phase3_pool(cand_list, rw_list, sq_list, mp)
            ce = [ce_adj[c["chunk_id"]] for c in pool]
            ce_min, ce_max = min(ce), max(ce)
            ce_range = ce_max - ce_min or 1e-8
            finals = [(ALPHA * c["retrieval_prior"] + (1 - ALPHA) * (s - ce_min) / ce_range, i)
                      for i, (c, s) in enumerate(zip(pool, ce))]
            finals.sort(key=lambda x: -x[0])
            top10 = [pool[idx]["chunk_id"] for _, idx in finals[:TOP_K]]
            row["per_pool"][mp] = {
                "top10": top10,
                "hit": any(c in gold for c in top10),
                "n_reserved": n_reserved,
                "n_fill": len(pool) - (n_reserved or 0),
            }
        per_question.append(row)

        if (i + 1) % 30 == 0:
            print(f"  {i + 1}/{len(gs)} ({time.time() - t0:.0f}s)", flush=True)

    # ── 汇总 ──
    def metrics(rows, mp):
        n = len(rows)
        rr_sum = 0.0
        r5 = r10 = top1 = 0
        zero = []
        cap_stat = defaultdict(lambda: [0, 0.0, 0])
        for r in rows:
            p = r["per_pool"][mp]
            top10 = p["top10"]
            gold = set(r["gold"])
            hit_at = None
            for j, cid in enumerate(top10):
                if cid in gold:
                    hit_at = j + 1
                    break
            rr_sum += 1.0 / hit_at if hit_at else 0.0
            r5 += 1 if hit_at and hit_at <= 5 else 0
            r10 += 1 if hit_at else 0
            top1 += 1 if hit_at == 1 else 0
            if not hit_at:
                zero.append(r["qid"])
            c = cap_stat[r["capability"]]
            c[0] += 1
            c[1] += 1.0 / hit_at if hit_at else 0.0
            c[2] += 1 if hit_at else 0
        caps = {k: {"n": v[0], "mrr": v[1] / v[0], "r10": v[2] / v[0]}
                for k, v in cap_stat.items()}
        return {"n": n, "mrr": rr_sum / n, "r5": r5 / n, "r10": r10 / n,
                "top1": top1 / n, "zero": len(zero), "zero_qids": zero, "per_cap": caps}

    summary = {mp: metrics(per_question, mp) for mp in POOL_SIZES}

    # 逐题变化 50→60 / 50→80
    deltas = {}
    for mp in (60, 80):
        gain = [r["qid"] for r in per_question
                if not r["per_pool"][50]["hit"] and r["per_pool"][mp]["hit"]]
        loss = [r["qid"] for r in per_question
                if r["per_pool"][50]["hit"] and not r["per_pool"][mp]["hit"]]
        deltas[mp] = {"gain": gain, "loss": loss}

    # 池成长统计
    grow = {mp: sum(1 for r in per_question if r["n_candidates"] > mp) for mp in POOL_SIZES}

    json.dump({"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
               "config": {"pool_sizes": POOL_SIZES, "alpha": ALPHA,
                          "lambda_length": LAMBDA_LENGTH, "top_k": TOP_K,
                          "n": len(per_question)},
               "summary": summary, "deltas": deltas,
               "pool_growth": {"n_candidates_gt": grow},
               "per_question": per_question},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # ── 打印 ──
    print("\n" + "=" * 92)
    print("  Phase 3 池大小扫描结果（+Rewrite+Reranker, top_k=10）")
    print("=" * 92)
    print(f"  {'Pool':>5s} {'MRR':>8s} {'R@5':>8s} {'R@10':>8s} {'Top1':>8s} "
          f"{'R@10=0':>7s} {'n>pool':>7s}")
    print("  " + "-" * 60)
    for mp in POOL_SIZES:
        s = summary[mp]
        print(f"  {mp:>5d} {s['mrr']:>8.4f} {s['r5']:>8.1%} {s['r10']:>8.1%} "
              f"{s['top1']:>8.1%} {s['zero']:>7d} {grow[mp]:>7d}")
    print("\n  50→60: 救回 " + ", ".join(deltas[60]["gain"]) +
          f" ({len(deltas[60]['gain'])} 题) | 搅黄 " + ", ".join(deltas[60]["loss"]) +
          f" ({len(deltas[60]['loss'])} 题)")
    print("  50→80: 救回 " + ", ".join(deltas[80]["gain"]) +
          f" ({len(deltas[80]['gain'])} 题) | 搅黄 " + ", ".join(deltas[80]["loss"]) +
          f" ({len(deltas[80]['loss'])} 题)")

    print("\n  -- 按 Capability R@10 --")
    print(f"  {'capability':<22s} {'n':>3s} " +
          " ".join(f"{'P' + str(mp):>6s}" for mp in POOL_SIZES))
    caps = sorted(summary[50]["per_cap"])
    for cap in caps:
        n = summary[50]["per_cap"][cap]["n"]
        vals = " ".join(f"{summary[mp]['per_cap'][cap]['r10']:>6.1%}" for mp in POOL_SIZES)
        print(f"  {cap:<22s} {n:>3d} {vals}")

    print(f"\n[scan] 完成 ({time.time() - t0:.0f}s) | 结果已保存: {OUT}")


if __name__ == "__main__":
    main()
