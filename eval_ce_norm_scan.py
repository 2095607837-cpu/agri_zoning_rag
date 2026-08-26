#!/usr/bin/env python3
"""CE 融合两阶段顺序实验（180 题离线重放）。

final = α × retrieval_prior + (1-α) × CE_norm（prior 已在 [0,1]，与生产一致）

实验 1（先定权重）：归一化固定 minmax（生产现状），扫 α = prior 权重
  {0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7}，按 (R@10, MRR) 选出最优 α*。

实验 2（再定归一化）：权重固定 α*，扫归一化方法
  minmax / rank / zsigmoid / softmax(T=0.05/0.1/0.2)。

增量缓存：每题（候选收集 + CE 打分）完成后立即落盘 data/ce_norm_cache.json，
中断后重跑自动跳过已缓存题（断点续跑）。跑满 180 题后全部组合纯缓存重放。

评价（vs 基线 minmax@α=0.2）：
  第一层（180 题）：MRR / R@5 / R@10 / Top1 / R@10=0
  第二层：基线 23 零召回救回数、C 类 17 题救回数（near-miss / prior-strong /
          CE-hard 分组）、新增失败数（基线命中被搅黄）、净变化

输出 data/ce_norm_scan_report.json。

用法: python3 eval_ce_norm_scan.py
"""
import json
import math
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "data" / "ce_norm_scan_report.json"
CACHE = BASE / "data" / "ce_norm_cache.json"

TOP_K = 10
MAX_POOL = 50
LAMBDA_LENGTH = 0.1
ALPHAS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
NORM_METHODS = ["minmax", "rank", "zsigmoid",
                "softmax0.05", "softmax0.1", "softmax0.2"]

C_CLASS = {"Q_C08", "Q_C25", "Q_S05", "Q_S07", "Q_S23", "Q_D04", "Q_D10",
           "Q_D20", "Q_D26", "Q_N11", "Q_L07", "Q_L08", "Q_L11",
           "Q_SR03", "Q_SR04", "Q_SR08", "Q_SR11"}
NEAR_MISS = {"Q_C08", "Q_S23", "Q_D26", "Q_SR04"}
PRIOR_STRONG = {"Q_L07", "Q_L08", "Q_SR04", "Q_D26"}
CE_HARD = {"Q_S05", "Q_D04", "Q_N11", "Q_SR11", "Q_SR03"}


def ce_norms(method, xs):
    n = len(xs)
    if method == "minmax":
        lo, hi = min(xs), max(xs)
        rng = hi - lo or 1e-8
        return [(x - lo) / rng for x in xs]
    if method == "rank":
        if n == 1:
            return [1.0]
        order = sorted(range(n), key=lambda i: -xs[i])
        out = [0.0] * n
        for pos, i in enumerate(order):
            out[i] = 1.0 - pos / (n - 1)
        return out
    if method == "zsigmoid":
        mu = sum(xs) / n
        var = sum((x - mu) ** 2 for x in xs) / n
        sd = math.sqrt(var) or 1e-8
        return [1.0 / (1.0 + math.exp(-(x - mu) / sd)) for x in xs]
    if method.startswith("softmax"):
        t = float(method[len("softmax"):])
        mx = max(xs)
        exps = [math.exp((x - mx) / t) for x in xs]
        tot = sum(exps)
        return [e / tot for e in exps]
    raise ValueError(method)


def top10_of(prior, norm, alpha):
    finals = [(alpha * p + (1 - alpha) * nn, i)
              for i, (p, nn) in enumerate(zip(prior, norm))]
    finals.sort(key=lambda x: -x[0])
    return [i for _, i in finals[:TOP_K]]


def load_cache():
    if CACHE.exists():
        return json.load(open(CACHE, encoding="utf-8")).get("questions", {})
    return {}


def save_cache(questions):
    json.dump({"version": 1, "questions": questions},
              open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)


def main():
    t0 = time.time()
    from eval_pool_size_scan import phase3_pool
    gs = [q for q in json.load(open(BASE / "data" / "golden_set_v2.json", encoding="utf-8"))
          if q["capability"] != "ood_detection"]
    scan = json.load(open(BASE / "data" / "pool_size_scan_report.json", encoding="utf-8"))
    cache = load_cache()
    print(f"[scan] {len(gs)} 题 | 已缓存 {len(cache)} 题 | 实验1: α扫描(minmax) → "
          f"实验2: 归一化扫描(α*)", flush=True)

    # ── 采集阶段（缓存已满则跳过，直接重放）──
    missing = [q for q in gs if q["id"] not in cache]
    if missing:
        # 改写映射（LLM 缓存命中，无 API 调用）
        from query_rewriter import expand_query, get_keywords, get_rewrite_queries, get_sub_queries
        from hybrid_search import HybridSearcher
        _searcher = HybridSearcher(enable_reranker=False)
        kw_map, rw_map, sq_map = {}, {}, {}
        for q in gs:
            query = q["question"]
            initial = _searcher.search(query, top_k=2, expand_context=True)
            t1 = initial[0].get("similarity", 0) if len(initial) > 0 else 0
            t2 = initial[1].get("similarity", 0) if len(initial) > 1 else 0
            expand_query(query, mode="all", top1_sim=t1, top2_sim=t2)
            kw_map[query] = get_keywords(query)
            rw_map[query] = get_rewrite_queries(query)
            sq_map[query] = get_sub_queries(query)
        print(f"[scan] 改写映射就绪", flush=True)

        searcher = HybridSearcher(enable_reranker=True)
        searcher._reranker._load()

        n_cached = len(cache)
        for i, q in enumerate(gs):
            qid = q["id"]
            if qid in cache:
                continue
            query = q["question"]
            rw_q, sq_q, kws = rw_map[query], sq_map[query], kw_map[query]

            judge_results, cand_list, rw_list, sq_list = searcher._collect_candidates(
                query, TOP_K, False, rw_q, sq_q, kws, None, LAMBDA_LENGTH, 30, 20, 20, 10)

            if cand_list is None:
                top_ids = [r.get("metadata", {}).get("chunk_id", "")
                           for r in judge_results[:TOP_K]]
                cache[qid] = {"fixed": True, "top10": top_ids}
            else:
                pool, _ = phase3_pool(cand_list, rw_list, sq_list, MAX_POOL)
                pairs = [(query, c["text"][:500]) for c in pool]
                with searcher._reranker._infer_lock:
                    raw = [float(x) for x in searcher._reranker._model.predict(
                        pairs, show_progress_bar=False)]
                ce_adj = [r - LAMBDA_LENGTH * math.log(len(c["text"]))
                          for c, r in zip(pool, raw)]
                cache[qid] = {"fixed": False,
                              "chunk_ids": [c["chunk_id"] for c in pool],
                              "priors": [c["retrieval_prior"] for c in pool],
                              "ce_adj": ce_adj}
            save_cache(cache)
            if (len(cache) - n_cached) % 10 == 0:
                print(f"  {len(cache)}/{len(gs)} ({time.time() - t0:.0f}s)", flush=True)
    else:
        print(f"[scan] 缓存已满（{len(cache)} 题），跳过采集直接重放", flush=True)

    # ── 基线一致性校验：minmax@0.2 重放必须复现 pool_size_scan_report 的 top10 ──
    verify_ok, verify_bad = 0, []
    for i, q in enumerate(gs):
        qid = q["id"]
        stored = scan["per_question"][i]["per_pool"]["50"]["top10"]
        c = cache[qid]
        replay = c["top10"] if c["fixed"] else \
            [c["chunk_ids"][i] for i in top10_of(c["priors"], ce_norms("minmax", c["ce_adj"]), 0.2)]
        if replay == stored:
            verify_ok += 1
        else:
            verify_bad.append(qid)
    print(f"[scan] 基线一致性: {verify_ok}/{len(gs)}"
          + (f"，不一致: {verify_bad}" if verify_bad else ""), flush=True)

    # ── 解析式重放 ──
    def rows_of(norm, alpha):
        out = []
        for q in gs:
            c = cache[q["id"]]
            top = c["top10"] if c["fixed"] else \
                [c["chunk_ids"][i] for i in top10_of(c["priors"], ce_norms(norm, c["ce_adj"]), alpha)]
            out.append({"qid": q["id"], "gold": list(q["gold_chunks"]), "top10": top})
        return out

    def metrics(rows):
        n = len(rows)
        rr = r5 = r10 = top1 = 0.0
        zero = []
        hit_set = set()
        for r in rows:
            gold = set(r["gold"])
            hit_at = None
            for j, cid in enumerate(r["top10"]):
                if cid in gold:
                    hit_at = j + 1
                    break
            if hit_at:
                hit_set.add(r["qid"])
                rr += 1.0 / hit_at
                r5 += hit_at <= 5
                r10 += 1
                top1 += hit_at == 1
            else:
                zero.append(r["qid"])
        return {"mrr": rr / n, "r5": r5 / n, "r10": r10 / n, "top1": top1 / n,
                "zero": len(zero), "zero_qids": zero, "hit_qids": sorted(hit_set)}

    def layer2(m, base_m, base_hit):
        rescued = sorted(set(base_m["zero_qids"]) - set(m["zero_qids"]))
        new_fail = sorted(set(base_hit) - set(m["hit_qids"]))
        c_rescued = [x for x in rescued if x in C_CLASS]
        return {"rescued_from_zero": len(rescued), "rescued_qids": rescued,
                "rescued_C": len(c_rescued), "rescued_C_qids": c_rescued,
                "rescued_near_miss": [x for x in rescued if x in NEAR_MISS],
                "rescued_prior_strong": [x for x in rescued if x in PRIOR_STRONG],
                "rescued_ce_hard": [x for x in rescued if x in CE_HARD],
                "new_fail": len(new_fail), "new_fail_qids": new_fail,
                "net": len(rescued) - len(new_fail)}

    base_m = metrics(rows_of("minmax", 0.2))
    base_hit = base_m["hit_qids"]

    # 实验 1：α 扫描（minmax 固定）
    exp1 = {}
    for a in ALPHAS:
        m = metrics(rows_of("minmax", a))
        exp1[f"a{a}"] = {**{k: m[k] for k in ("mrr", "r5", "r10", "top1", "zero")},
                         **layer2(m, base_m, base_hit)}
    # 最优 α*：按 (R@10, MRR) 字典序
    alpha_best = max(ALPHAS, key=lambda a: (exp1[f"a{a}"]["r10"], exp1[f"a{a}"]["mrr"]))

    # 实验 2：归一化扫描（α* 固定）；对照组含 minmax@α*（隔离归一化增益）
    exp2 = {}
    for method in NORM_METHODS:
        m = metrics(rows_of(method, alpha_best))
        m_ab = metrics(rows_of("minmax", alpha_best))
        exp2[method] = {**{k: m[k] for k in ("mrr", "r5", "r10", "top1", "zero")},
                        **layer2(m, base_m, base_hit),
                        "gain_vs_minmax_at_alpha_best": {
                            "rescued": sorted(set(m_ab["zero_qids"]) - set(m["zero_qids"])),
                            "new_fail": sorted(set(m_ab["hit_qids"]) - set(m["hit_qids"]))}}

    json.dump({"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
               "config": {"alphas": ALPHAS, "norm_methods": NORM_METHODS,
                          "max_pool": MAX_POOL, "top_k": TOP_K,
                          "lambda_length": LAMBDA_LENGTH,
                          "baseline": "minmax@a0.2", "alpha_best": alpha_best,
                          "verify_ok": verify_ok, "verify_bad": verify_bad,
                          "C_class": sorted(C_CLASS),
                          "near_miss": sorted(NEAR_MISS),
                          "prior_strong": sorted(PRIOR_STRONG),
                          "ce_hard": sorted(CE_HARD)},
               "baseline_metrics": base_m,
               "exp1_alpha_scan": exp1,
               "exp2_norm_scan": exp2},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # ── 打印 ──
    print("\n" + "=" * 100)
    print("  实验 1：α 扫描（归一化固定 minmax，基线 α=0.2）")
    print("=" * 100)
    print(f"  {'α':>4s} {'MRR':>8s} {'R@5':>7s} {'R@10':>7s} {'Top1':>7s} "
          f"{'0':>3s} | {'救回':>4s} {'C救':>4s} {'near':>4s} {'prior':>5s} "
          f"{'CE硬':>4s} {'搅黄':>4s} {'净':>4s}")
    print("  " + "-" * 96)
    for a in ALPHAS:
        s = exp1[f"a{a}"]
        mark = " ← 当前" if a == 0.2 else (" ← α*" if a == alpha_best else "")
        print(f"  {a:>4.1f} {s['mrr']:>8.4f} {s['r5']:>7.1%} {s['r10']:>7.1%} "
              f"{s['top1']:>7.1%} {s['zero']:>3d} | {s['rescued_from_zero']:>4d} "
              f"{s['rescued_C']:>4d} {len(s['rescued_near_miss']):>4d} "
              f"{len(s['rescued_prior_strong']):>5d} {len(s['rescued_ce_hard']):>4d} "
              f"{s['new_fail']:>4d} {s['net']:>+4d}{mark}")
    print("\n  非 α=0.2 的救回/搅黄明细:")
    for a in ALPHAS:
        if a == 0.2:
            continue
        s = exp1[f"a{a}"]
        print(f"    α={a}: 救回 {s['rescued_qids']} | 搅黄 {s['new_fail_qids']}")

    print("\n" + "=" * 100)
    print(f"  实验 2：归一化扫描（α*={alpha_best} 固定，vs 基线 minmax@α=0.2）")
    print("=" * 100)
    print(f"  {'方法':>10s} {'MRR':>8s} {'R@5':>7s} {'R@10':>7s} {'Top1':>7s} "
          f"{'0':>3s} | {'救回':>4s} {'C救':>4s} {'near':>4s} {'prior':>5s} "
          f"{'CE硬':>4s} {'搅黄':>4s} {'净':>4s} | {'vs minmax@α*':>16s}")
    print("  " + "-" * 96)
    for method in NORM_METHODS:
        s = exp2[method]
        g = s["gain_vs_minmax_at_alpha_best"]
        print(f"  {method:>10s} {s['mrr']:>8.4f} {s['r5']:>7.1%} {s['r10']:>7.1%} "
              f"{s['top1']:>7.1%} {s['zero']:>3d} | {s['rescued_from_zero']:>4d} "
              f"{s['rescued_C']:>4d} {len(s['rescued_near_miss']):>4d} "
              f"{len(s['rescued_prior_strong']):>5d} {len(s['rescued_ce_hard']):>4d} "
              f"{s['new_fail']:>4d} {s['net']:>+4d} | "
              f"救{len(g['rescued'])} 搅{len(g['new_fail'])}")
    print("\n  非 minmax 方法的救回/搅黄明细（vs 基线）:")
    for method in NORM_METHODS:
        if method == "minmax":
            continue
        s = exp2[method]
        print(f"    {method:>10s}: 救回 {s['rescued_qids']} | 搅黄 {s['new_fail_qids']}")
    print(f"\n[scan] 完成 ({time.time() - t0:.0f}s) | 结果: {OUT} | 原始缓存: {CACHE}")


if __name__ == "__main__":
    main()
