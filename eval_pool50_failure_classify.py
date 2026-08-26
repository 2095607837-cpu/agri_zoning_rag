#!/usr/bin/env python3
"""MP=50 下 23 个 R@10=0 的失败分层与机制可救上限。

分类（question 级，any-gold 规则）：
  A: 所有 gold ∉ Global Union → 检索信号不足（Dense/BM25/Rewrite/SubQuery）
     A + oracle_rank ≤ 30 → 表示空间可分，是 Dense top-K / 管线问题
     A + oracle_rank > 30 → 真正的表示/语义鸿沟
  B1: 有 gold ∈ Union 且 prior 排名 ≤50，但未进 50 池 → 被 quota 淘汰（Soft Protection 目标）
  B2: 有 gold ∈ Union 但 prior 排名 >50，且 quota 未救回 → 检索排序问题（Retrieval Prior / Fusion）
  C: 有 gold 进 50 池但 CE 后 Top10 外 → CE / Retrieval Prior 问题

关键统计：Soft Protection theoretical rescue = B1 中 gold 被原始 query 强证据命中
（Dense top-10 / top-30 / 任一原始通道 top-10）的题数，并对满足条件的题做
force-include CE 重放验证（pool+gold 后 gold 是否进 top-10）。

复用 data/pool_size_scan_report.json 的 zero_qids；重跑生产候选收集（_collect_candidates），
按生产 Phase 3 (max_pool=50) 语义重放。rank 口径统一 1-indexed。

输出 data/pool50_failure_classification.json。

用法: python3 eval_pool50_failure_classify.py
"""
import json
import math
import time
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "data" / "pool50_failure_classification.json"
TOP_K = 10
ALPHA = 0.2
LAMBDA_LENGTH = 0.1
MAX_POOL = 50


def phase3_trace(cand_list, rw_list, sq_list, max_pool, gold_c=None):
    """复刻 Phase 3 配额逻辑，返回 (pool, reserved, rest, gold_snapshot)。

    gold_snapshot: gold_c 轮次时各配额占用快照（taken 副本 + rw_taken），
    gold_c 不在 cand_list 中时为 None。
    """
    if len(cand_list) <= max_pool:
        return list(cand_list), None, None, None

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
    gold_snapshot = None

    for c in cand_list:
        if gold_c is not None and c is gold_c:
            gold_snapshot = {"taken": dict(taken), "rw_taken": rw_taken}
        placed = False
        if 0 in c["query_hits"] and taken.get(0, 0) < quota_orig:
            reserved.append(c)
            taken[0] = taken.get(0, 0) + 1
            placed = True
        elif not placed:
            for sq_qid, sq_quota in sq_quotas.items():
                if sq_qid in c["query_hits"] and taken.get(sq_qid, 0) < sq_quota:
                    reserved.append(c)
                    taken[sq_qid] = taken.get(sq_qid, 0) + 1
                    placed = True
                    break
        if not placed and rw_qids and rw_taken < quota_rw and (rw_qids & c["query_hits"]):
            reserved.append(c)
            rw_taken += 1
            placed = True
        if not placed:
            rest.append(c)

    return (reserved + rest)[:max_pool], reserved, rest, gold_snapshot


def ce_fuse_replay(query, pool, top_k=TOP_K):
    """对池做 CE 打分 + 0.2×prior+0.8×ce_norm 融合，返回 [(final, idx)] 降序。"""
    pairs = [(query, c["text"][:500]) for c in pool]
    with CE_SER._reranker._infer_lock:
        raw = [float(x) for x in CE_SER._reranker._model.predict(
            pairs, show_progress_bar=False)]
    ce_adj = [r - LAMBDA_LENGTH * math.log(len(c["text"]))
              for c, r in zip(pool, raw)]
    ce_min, ce_max = min(ce_adj), max(ce_adj)
    ce_range = ce_max - ce_min or 1e-8
    finals = [(ALPHA * c["retrieval_prior"] + (1 - ALPHA) * (s - ce_min) / ce_range, i)
              for i, (c, s) in enumerate(zip(pool, ce_adj))]
    finals.sort(key=lambda x: -x[0])
    return finals, ce_adj


CE_SER = None  # 模块级 searcher 引用（main 中注入）


def main():
    global CE_SER
    t0 = time.time()
    scan = json.load(open(BASE / "data" / "pool_size_scan_report.json", encoding="utf-8"))
    zero_qids = scan["summary"]["50"]["zero_qids"]
    gs = {q["id"]: q for q in json.load(open(BASE / "data" / "golden_set_v2.json", encoding="utf-8"))}
    oracle_rows = {x["qid"]: x for x in json.load(
        open(BASE / "data" / "eval_oracle_rank_results.json", encoding="utf-8"))["per_question"]}
    print(f"[cls] {len(zero_qids)} 个零召回题", flush=True)

    # ── 改写映射（仅 23 题，缓存命中）──
    from query_rewriter import expand_query, get_keywords, get_rewrite_queries, get_sub_queries
    from hybrid_search import HybridSearcher
    _searcher = HybridSearcher(enable_reranker=False)
    rw_map, sq_map, kw_map = {}, {}, {}
    for qid in zero_qids:
        query = gs[qid]["question"]
        initial = _searcher.search(query, top_k=2, expand_context=True)
        t1 = initial[0].get("similarity", 0) if len(initial) > 0 else 0
        t2 = initial[1].get("similarity", 0) if len(initial) > 1 else 0
        expand_query(query, mode="all", top1_sim=t1, top2_sim=t2)
        rw_map[qid] = get_rewrite_queries(query)
        sq_map[qid] = get_sub_queries(query)
        kw_map[qid] = get_keywords(query)
    print(f"[cls] 改写映射就绪", flush=True)

    CE_SER = HybridSearcher(enable_reranker=True)
    CE_SER._reranker._load()

    rows = []
    for i, qid in enumerate(zero_qids):
        q = gs[qid]
        query = q["question"]
        gold = list(q["gold_chunks"])
        rw_q, sq_q, kws = rw_map[qid], sq_map[qid], kw_map[qid]
        orc = oracle_rows.get(qid, {})

        judge_results, cand_list, rw_list, sq_list = CE_SER._collect_candidates(
            query, TOP_K, False, rw_q, sq_q, kws, None, LAMBDA_LENGTH, 30, 20, 20, 10)

        row = {"qid": qid, "capability": q["capability"], "query": query,
               "n_union": 0 if cand_list is None else len(cand_list),
               "gold_chunks": []}

        if cand_list is None:
            row["cls"] = "A"
            row["note"] = "无改写输入，生产直接返回原始 search Top10"
            for g in gold:
                det = {"chunk_id": g,
                       "global_union": {"in_union": False},
                       "pool50": {"in_pool": False, "exclusion_reason": None},
                       "ce": {"in_ce": False, "final_rank": None, "ce_rank": None,
                              "retrieval_prior_rank": None, "top10_margin": None},
                       "oracle": None}
                if g == orc.get("best_gold"):
                    det["oracle"] = {"oracle_rank": orc.get("oracle_rank"),
                                     "dense_rank": orc.get("dense_rank"),
                                     "bm25_rank": orc.get("bm25_rank"),
                                     "rrf_rank": orc.get("rrf_rank"),
                                     "rw_oracle_rank": orc.get("rw_oracle_rank")}
                row["gold_chunks"].append(det)
            rows.append(row)
            print(f"  {i + 1}/{len(zero_qids)} {qid} → A (no rewrite inputs)", flush=True)
            continue

        union = {c["chunk_id"]: c for c in cand_list}
        pool, reserved, rest, _ = phase3_trace(cand_list, rw_list, sq_list, MAX_POOL)
        pool_ids = [c["chunk_id"] for c in pool]

        # 逐 gold 细节
        gold_details = []
        for g in gold:
            det = {"chunk_id": g,
                   "global_union": {}, "pool50": {}, "ce": {}, "oracle": None}
            if g in union:
                c = union[g]
                ur = cand_list.index(c) + 1
                det["global_union"] = {
                    "in_union": True,
                    "query_hits": sorted(c["query_hits"]),
                    "channels": list(dict.fromkeys(c["sources"])),
                    "best_channel": c["best_channel"],
                    "best_rank": c["best_rank"] + 1,  # 0-indexed → 1-indexed
                    "prior_rank": ur,
                }
                in_pool = g in pool_ids
                if in_pool:
                    det["pool50"] = {"in_pool": True, "exclusion_reason": None,
                                     "pool_pos": pool_ids.index(g) + 1}
                else:
                    # B1: prior 本身能进 top-50 却被配额重排挤出；B2: prior 就在填充线以下
                    reason = "quota" if ur <= MAX_POOL else "global_fill"
                    res_rest_rank = len(reserved) + rest.index(c) + 1
                    det["pool50"] = {"in_pool": False, "exclusion_reason": reason,
                                     "res_rest_rank": res_rest_rank}
                det["ce"] = {"in_ce": in_pool, "final_rank": None, "ce_rank": None,
                             "retrieval_prior_rank": None, "top10_margin": None}
                if in_pool:
                    det["ce"]["retrieval_prior_rank"] = pool.index(c) + 1
            else:
                det["global_union"] = {"in_union": False}
                det["pool50"] = {"in_pool": False, "exclusion_reason": None}
                det["ce"] = {"in_ce": False, "final_rank": None, "ce_rank": None,
                             "retrieval_prior_rank": None, "top10_margin": None}
                if g == orc.get("best_gold"):
                    det["oracle"] = {"oracle_rank": orc.get("oracle_rank"),
                                     "dense_rank": orc.get("dense_rank"),
                                     "bm25_rank": orc.get("bm25_rank"),
                                     "rrf_rank": orc.get("rrf_rank"),
                                     "rw_oracle_rank": orc.get("rw_oracle_rank")}
            gold_details.append(det)

        row["gold_chunks"] = gold_details

        # ── question 级分类（any-gold）──
        in_union = [d for d in gold_details if d["global_union"].get("in_union")]
        in_pool = [d for d in gold_details if d["pool50"].get("in_pool")]
        if not in_union:
            row["cls"] = "A"
        elif in_pool:
            row["cls"] = "C"
        elif any(d["global_union"]["prior_rank"] <= MAX_POOL for d in in_union):
            row["cls"] = "B1"
        else:
            row["cls"] = "B2"

        # ── C 类：CE 重放算 final_rank / ce_rank / margin ──
        if row["cls"] == "C":
            finals, ce_adj = ce_fuse_replay(query, pool)
            for d in gold_details:
                if d["pool50"].get("in_pool"):
                    c = union[d["chunk_id"]]
                    pos = pool.index(c)
                    fr = next(j + 1 for j, (_, idx) in enumerate(finals) if idx == pos)
                    ce_rank = sorted(range(len(pool)), key=lambda j: -ce_adj[j]).index(pos) + 1
                    d["ce"]["final_rank"] = fr
                    d["ce"]["ce_rank"] = ce_rank
                    d["ce"]["top10_margin"] = round(finals[9][0] - finals[fr - 1][0], 4)

        # ── B1 类：Soft Protection force-include CE 验证 ──
        if row["cls"] == "B1":
            verified = []
            for d in gold_details:
                if not d["global_union"].get("in_union"):
                    continue
                c = union[d["chunk_id"]]
                hits = d["global_union"]["query_hits"]
                # 保护条件：原始 query（qid 0）强证据命中
                cond = {"dense10": 0 in hits and c["dense_rank"] <= 9,
                        "dense30": 0 in hits and c["dense_rank"] <= 29,
                        "chan10": 0 in hits and min(c["dense_rank"], c["bm25_rank"],
                                                    c.get("bm25_kw_rank", 999)) <= 9}
                for k, v in cond.items():
                    d.setdefault("soft_protection_cond", {})[k] = v
                if cond["dense10"]:
                    pool51 = list(pool) + [c]
                    finals51, _ = ce_fuse_replay(query, pool51)
                    fr = next(j + 1 for j, (_, idx) in enumerate(finals51)
                              if idx == len(pool))
                    d["sp_verified"] = {"final_rank_with_protection": fr,
                                        "rescued": fr <= TOP_K}
                    if fr <= TOP_K:
                        verified.append(d["chunk_id"])
            row["sp_verified_rescued"] = verified

        rows.append(row)
        print(f"  {i + 1}/{len(zero_qids)} {qid} → {row['cls']} "
              f"(union={row['n_union']}, pool={len(pool)})", flush=True)

    # ── 汇总 ──
    cls_count = defaultdict(list)
    for r in rows:
        cls_count[r["cls"]].append(r["qid"])

    # A 类 oracle 拆分（best_gold）
    a_split = {"oracle_le30": [], "oracle_gt30": [], "oracle_na": []}
    for r in rows:
        if r["cls"] != "A":
            continue
        orc = oracle_rows.get(r["qid"], {})
        o = orc.get("oracle_rank")
        if o is None:
            a_split["oracle_na"].append(r["qid"])
        elif o <= 30:
            a_split["oracle_le30"].append(r["qid"])
        else:
            a_split["oracle_gt30"].append(r["qid"])

    # Soft Protection theoretical rescue（B1）
    sp = {"dense10": [], "dense30": [], "chan10": [], "verified": []}
    for r in rows:
        if r["cls"] != "B1":
            continue
        for d in r["gold_chunks"]:
            cond = d.get("soft_protection_cond", {})
            for k in ("dense10", "dense30", "chan10"):
                if cond.get(k):
                    sp[k].append((r["qid"], d["chunk_id"]))
            if d.get("sp_verified", {}).get("rescued"):
                sp["verified"].append((r["qid"], d["chunk_id"]))

    # C 类 near-miss（min margin < 0.02）
    near_miss = []
    for r in rows:
        if r["cls"] != "C":
            continue
        margins = [d["ce"]["top10_margin"] for d in r["gold_chunks"]
                   if d["ce"].get("top10_margin") is not None]
        if margins and min(margins) < 0.02:
            near_miss.append((r["qid"], min(margins)))

    # capability × class
    cap_by_cls = defaultdict(lambda: defaultdict(list))
    for r in rows:
        cap_by_cls[r["capability"]][r["cls"]].append(r["qid"])

    summary = {
        "n": len(rows),
        "by_class": {c: {"count": len(v), "qids": v} for c, v in sorted(cls_count.items())},
        "A_oracle_split": {k: {"count": len(v), "qids": v} for k, v in a_split.items()},
        "soft_protection_rescue": {k: {"count": len(v), "pairs": v} for k, v in sp.items()},
        "C_near_miss_margin_lt_002": {"count": len(near_miss), "pairs": near_miss},
        "capability_by_class": {c: {k: v for k, v in d.items()} for c, d in cap_by_cls.items()},
    }
    json.dump({"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
               "config": {"max_pool": MAX_POOL, "top_k": TOP_K, "alpha": ALPHA,
                          "lambda_length": LAMBDA_LENGTH, "rank_convention": "1-indexed"},
               "summary": summary, "per_question": rows},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # ── 打印 ──
    print("\n" + "=" * 80)
    print("  MP=50 零召回分层（23 题）—— 机制可救上限诊断")
    print("=" * 80)
    labels = {"A": "Gold ∉ Union（检索信号）", "B1": "∉ Pool50 且被 quota 淘汰（配额）",
              "B2": "∉ Pool50 且 prior 太低（排序）", "C": "∈ Pool50 但 CE 掉出 Top10（CE/Prior）"}
    for cls in ("A", "B1", "B2", "C"):
        n = len(cls_count[cls])
        print(f"\n  {cls} = {n:>2d}   {labels[cls]}: {', '.join(cls_count[cls])}")
    print(f"\n  A 类 oracle 拆分: ≤30（空间可分→管线/Dense top-K）{len(a_split['oracle_le30'])} 题 "
          f"{a_split['oracle_le30']} | >30（真语义鸿沟）{len(a_split['oracle_gt30'])} 题 "
          f"{a_split['oracle_gt30']} | 无 oracle {len(a_split['oracle_na'])} 题 {a_split['oracle_na']}")
    print(f"\n  Soft Protection theoretical rescue（B1）:")
    print(f"    dense top-10 条件: {len(sp['dense10'])} 题 {sp['dense10']}")
    print(f"    dense top-30 条件: {len(sp['dense30'])} 题 {sp['dense30']}")
    print(f"    任一原始通道 top-10: {len(sp['chan10'])} 题 {sp['chan10']}")
    print(f"    force-include CE 验证可救: {len(sp['verified'])} 题 {sp['verified']}")
    print(f"\n  C 类 near-miss（top10_margin < 0.02）: {len(near_miss)} 题 {near_miss}")
    print(f"\n  capability × class:")
    for cap in sorted(cap_by_cls):
        row_str = "  ".join(f"{c}:{len(v)}" for c, v in sorted(cap_by_cls[cap].items()))
        print(f"    {cap:<22s} {row_str}")
    print(f"\n[cls] 完成 ({time.time() - t0:.0f}s) | 结果已保存: {OUT}")


if __name__ == "__main__":
    main()
