#!/usr/bin/env python3
"""multi 路径 BM25 权重网格（W_BM25_ORIG × W_BM25_KW）+ kw 通道消融实验。

背景: plain 路径 0.7/0.3 有扫描实验支撑（eval_v2_results.md §9.4 2026-07-10、
§24 2026-08-24）；multi 路径 W_BM25_ORIG=0.15（6f0ee16 引入）与 W_BM25_KW=0.3
无任何实验。本实验用 record_ch_ranks 采集 per-channel 原始 rank（权重无关），
解析式重放 8 组权重配置，回答: 0.15 是否最优? kw 通道增益多大?

流程:
  Phase 1 采集: 122 道 multi 题, 生产参数(30/20/20/10) + record_ch_ranks=True
              → data/bm25_weight_grid/cands/{qid}.json（权重无关原始 rank）
  Phase 2 CE:  复用 data/ce_query_quota_ab/ce_scores.json, 缺失对补算到本目录
  Phase 3 重放: {w_o∈0,0.05,0.15,0.3}×{w_kw∈0,0.3}（dense 固定 0.7）
              → 方案C配额 → CE top10; plain 题 58 道不经过这些权重,
              各配置同值, 复用生产 top10
  Phase 4 指标: 全量 180 题 MRR/R@5/R@10/sec/zero + multi 题 pool recall
              + kw 消融增益 + 0.15 网格对比
Sanity: (0.15,0.3) 重放 prior 与生产存量逐值相等, top10 与生产 planC|rw 逐位一致

用法:
  python3 eval_bm25_weight_grid.py --limit 5 --smoke   # 冒烟 + 一致性校验
  python3 eval_bm25_weight_grid.py                     # 全量（断点续跑）
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import eval_gate_ab as ab
from eval_repair_stage2 import (quota_select, replay_top10, gold_metrics,
                                agg, pair_diff, A_QUOTA_ORIG, LAMBDA_LEN, TOPK)
from hybrid_search import HybridSearcher

OUT_DIR = "data/bm25_weight_grid"
CAND_DIR = os.path.join(OUT_DIR, "cands")
CE_FILE = os.path.join(OUT_DIR, "ce_scores.json")
REPORT = os.path.join(OUT_DIR, "report.json")
PROD_CAND_DIR = "data/ce_query_quota_ab/candidates"
PROD_TOP10_DIR = "data/ce_query_quota_ab/top10"
PROD_CE = "data/ce_query_quota_ab/ce_scores.json"

W_DENSE = 0.7
RRF_K = 60
GRID_O = [0.0, 0.05, 0.15, 0.3]
GRID_K = [0.0, 0.3]
PROD_KEY = "o0.15|k0.3"


def cfg_key(wo, wk):
    return f"o{wo}|k{wk}"


def _atomic_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ─────────── Phase 1: 采集 per-channel 原始 rank（权重无关） ───────────

def collect_one(qid):
    with open(os.path.join(PROD_CAND_DIR, qid + ".json"), encoding="utf-8") as f:
        prod = json.load(f)
    _, cand_list, _, _ = ab.searcher0._collect_candidates(
        prod["question"], TOPK, False, prod["rw"], prod["sq"], prod["kw"], None,
        LAMBDA_LEN, 30, 20, 20, 10, record_ch_ranks=True)
    if cand_list is None:
        sys.exit(f"[phase1] {qid} 非 multi 题, 不应采集")
    rec = {
        "qid": qid, "question": prod["question"],
        "rw": prod["rw"], "sq": prod["sq"], "kw": prod["kw"],
        "n_rw": prod["n_rw"], "n_sq": prod["n_sq"],
        "cand": [{
            "chunk_id": c["chunk_id"],
            "ch_ranks": c["ch_ranks"],
            "query_hits": sorted(c["query_hits"]),
            "text_len": len(c["text"]),
            "text": c["text"],
            "cosine_sim": c["cosine_sim"],
        } for c in cand_list],
    }
    _atomic_json(os.path.join(CAND_DIR, qid + ".json"), rec)
    return rec


def phase1(limit):
    os.makedirs(CAND_DIR, exist_ok=True)
    records = {}
    todo = [q["id"] for q in ab.gs[:limit]] if limit else [q["id"] for q in ab.gs]
    t0 = time.time()
    n_new = 0
    for i, qid in enumerate(todo, 1):
        path = os.path.join(CAND_DIR, qid + ".json")
        if os.path.exists(path):
            records[qid] = json.load(open(path, encoding="utf-8"))
            continue
        prod = json.load(open(os.path.join(PROD_CAND_DIR, qid + ".json"),
                              encoding="utf-8"))
        if prod["plain"]:
            records[qid] = prod   # plain 题不走 multi 权重, 复用存量
            continue
        records[qid] = collect_one(qid)
        n_new += 1
        if i % 10 == 0 or i == len(todo):
            print(f"[phase1] {i}/{len(todo)} 新采集 {n_new} "
                  f"({time.time() - t0:.0f}s)", flush=True)
    return records


# ─────────── 解析式重放: 权重 → rrf_prior / retrieval_prior ───────────

def _key_order(k):
    """channel key → 采集顺序 (qtype, qid, chan)。"""
    qkey, chan = k.rsplit("-", 1)
    if qkey.startswith("Original"):
        t, q = 0, int(qkey[8:])
    elif qkey.startswith("Rewrite"):
        t, q = 1, int(qkey[7:])
    else:
        t, q = 2, int(qkey[4:])   # SubQ
    return (t, q, {"Dense": 0, "BM25o": 1, "BM25kw": 2}[chan])


def insert_order(cand):
    """恢复采集插入顺序 = 每 chunk 最早出现通道 (顺序, rank) 升序。"""
    return sorted(cand, key=lambda c: min(
        (*_key_order(k), r) for k, r in c["ch_ranks"].items()))


def replay_prior(cand, w_o, w_kw):
    """复刻 _collect: local_rrf 按通道累加 → /total_w 归一化 → max → rrf_prior
    → evidence（query_hits 权重无关）→ minmax → retrieval_prior → 稳定排序。
    累加顺序与生产一致（Dense→BM25o→BM25kw, ch_ranks 按录制序）。"""
    CHAN_W = {"Dense": W_DENSE, "BM25o": w_o, "BM25kw": w_kw}
    out = []
    for c in cand:
        per_q = defaultdict(float)
        hit_w = defaultdict(float)
        for k, rank in c["ch_ranks"].items():
            qkey, chan = k.rsplit("-", 1)
            w = CHAN_W[chan]
            if w <= 0:
                continue
            per_q[qkey] += w / (RRF_K + rank)
            hit_w[qkey] += w
        best = 0.0
        for qkey, s in per_q.items():
            if hit_w[qkey] > 0:
                v = s / hit_w[qkey]
                if v > best:
                    best = v
        n = len(c["query_hits"])
        ev = 0.004 if n >= 3 else 0.002 if n >= 2 else 0.0
        out.append({**c, "rrf_prior": round(best, 6), "evidence_score": ev})
    if len(out) > 1:
        rrf_vals = [c["rrf_prior"] for c in out]
        ev_vals = [c["evidence_score"] for c in out]
        rrf_min, rrf_max = min(rrf_vals), max(rrf_vals)
        ev_min, ev_max = min(ev_vals), max(ev_vals)
        rrf_range = rrf_max - rrf_min or 1e-8
        ev_range = ev_max - ev_min or 1e-8
        for c in out:
            c["retrieval_prior"] = round(
                0.7 * (c["rrf_prior"] - rrf_min) / rrf_range
                + 0.3 * (c["evidence_score"] - ev_min) / ev_range, 6)
    else:
        out[0]["retrieval_prior"] = 0.0
    # 生产 sort(key=-prior) 稳定排序, 平手保持插入顺序
    out = insert_order(out)
    out.sort(key=lambda c: -c["retrieval_prior"])
    return out


# ─────────── Phase 2+3: CE 补齐 + 重放 8 配置 ───────────

def phase23(records, smoke):
    scores = {}
    with open(PROD_CE, encoding="utf-8") as f:
        scores.update(json.load(f)["scores"])
    extra = {}
    if os.path.exists(CE_FILE):
        with open(CE_FILE, encoding="utf-8") as f:
            extra = json.load(f).get("scores", {})
    scores.update(extra)

    srch = HybridSearcher(enable_reranker=True)
    srch._reranker._load()

    configs = [(wo, wk) for wo in GRID_O for wk in GRID_K]
    multi_ids = [qid for qid, r in records.items() if not r.get("plain")]
    plain_ids = [qid for qid, r in records.items() if r.get("plain")]

    q_by_id = {q["id"]: q for q in ab.gs}
    prod_top = {}
    for qid in list(multi_ids) + list(plain_ids):
        with open(os.path.join(PROD_TOP10_DIR, qid + ".json"),
                  encoding="utf-8") as f:
            prod_top[qid] = json.load(f)["variants"]["planC|rw"]["ids"]

    results = {cfg_key(wo, wk): {} for wo, wk in configs}
    pool_info = {cfg_key(wo, wk): {} for wo, wk in configs}
    prior_mismatch, top_mismatch = [], []
    n_ce, t0 = 0, time.time()
    for i, qid in enumerate(multi_ids, 1):
        r = records[qid]
        prior_by_cfg = {cfg_key(wo, wk): replay_prior(r["cand"], wo, wk)
                        for wo, wk in configs}
        if smoke:
            prod = json.load(open(os.path.join(PROD_CAND_DIR, qid + ".json"),
                                  encoding="utf-8"))
            stored = {c["chunk_id"]: c for c in prod["cand"]}
            mine = {c["chunk_id"]: c for c in prior_by_cfg[PROD_KEY]}
            for cid, c in mine.items():
                s = stored[cid]
                if (abs(c["rrf_prior"] - s["rrf_prior"]) > 1e-9
                        or abs(c["retrieval_prior"] - s["retrieval_prior"]) > 1e-9):
                    prior_mismatch.append((qid, cid))
                    break

        ce_query = r["rw"][0] if r["n_rw"] > 0 else r["question"]
        need = set()
        pools = {}
        for wo, wk in configs:
            key = cfg_key(wo, wk)
            pool, _ = quota_select(prior_by_cfg[key], r["n_rw"], r["n_sq"],
                                   A_QUOTA_ORIG)
            pools[key] = pool
            need |= {cid for cid in pool
                     if ce_query not in scores or cid not in scores[ce_query]}
        missing = [cid for cid in need
                   if ce_query not in extra or cid not in extra[ce_query]]
        if missing:
            by_id = {c["chunk_id"]: c for c in r["cand"]}
            pairs = [(ce_query, by_id[cid]["text"][:500]) for cid in missing]
            with srch._reranker._infer_lock:
                raw = [float(x) for x in srch._reranker._model.predict(
                    pairs, show_progress_bar=False)]
            extra.setdefault(ce_query, {})
            for cid, v in zip(missing, raw):
                extra[ce_query][cid] = v
                scores.setdefault(ce_query, {})[cid] = v
            n_ce += len(missing)
            _atomic_json(CE_FILE, {"version": 1, "scores": extra})

        gold, _ = ab.gold_matches(q_by_id[qid])
        for wo, wk in configs:
            key = cfg_key(wo, wk)
            pool = pools[key]
            cand_by_id = {c["chunk_id"]: c for c in prior_by_cfg[key]}
            merged = {ce_query: {cid: scores[ce_query][cid] for cid in pool}}
            top = replay_top10(cand_by_id, pool, ce_query, merged)
            ids = [cid for cid, _ in top]
            results[key][qid] = ids
            pool_info[key][qid] = {
                "pool_n": len(pool),
                "gold_in_pool": len(set(pool) & gold),
            }
            if smoke and key == PROD_KEY and ids != prod_top[qid]:
                top_mismatch.append(qid)
        if i % 20 == 0 or i == len(multi_ids):
            print(f"[phase23] {i}/{len(multi_ids)} 新CE {n_ce} "
                  f"({time.time() - t0:.0f}s)", flush=True)

    for key in results:          # plain 题各配置同值, 复用生产 top10
        for qid in plain_ids:
            results[key][qid] = prod_top[qid]
    return results, pool_info, prior_mismatch, top_mismatch, configs


# ─────────── Phase 4: 指标 + 消融 ───────────

def phase4(results, pool_info, configs, prior_mismatch, top_mismatch):
    q_by_id = {q["id"]: q for q in ab.gs}
    qids = sorted(results[PROD_KEY].keys())   # smoke 时为子集
    rows_by_cfg, pools_by_cfg = {}, {}
    for wo, wk in configs:
        key = cfg_key(wo, wk)
        rows_by_cfg[key] = [{"qid": qid,
                             **gold_metrics(q_by_id[qid], results[key][qid])}
                            for qid in qids]
        pools_by_cfg[key] = pool_info[key]

    n_multi = len([qid for qid in qids
                   if pool_info[PROD_KEY].get(qid) is not None])
    report = {"configs": configs, "aggregates": {}, "pool": {},
              "kw_ablation": {}, "grid_vs_prod": {},
              "sanity": {"prior_mismatch": prior_mismatch,
                         "top_mismatch": top_mismatch}}
    for wo, wk in configs:
        key = cfg_key(wo, wk)
        report["aggregates"][key] = agg(rows_by_cfg[key])
        pinfo = pools_by_cfg[key]
        n_pool_gold = sum(1 for qid, p in pinfo.items() if p["gold_in_pool"] > 0)
        report["pool"][key] = {
            "n_multi": n_multi,
            "gold_in_pool": n_pool_gold,
            "pool_recall": round(n_pool_gold / n_multi, 4) if n_multi else None,
            "med_pool_n": sorted(p["pool_n"] for p in pinfo.values())[
                len(pinfo) // 2] if pinfo else None,
        }

    # kw 消融: 每个 w_o 档 (k0.3 − k0) 端到端 + pool 层
    for wo in GRID_O:
        base, kw = cfg_key(wo, 0.0), cfg_key(wo, 0.3)
        d = pair_diff(rows_by_cfg[base], rows_by_cfg[kw], base, kw)
        d["delta"] = {m: round(report["aggregates"][kw][m]
                                - report["aggregates"][base][m], 4)
                      for m in ("mrr", "recall_5", "recall_10", "sec_mrr")}
        d["zero_recall_delta"] = (report["aggregates"][kw]["zero_recall"]
                                  - report["aggregates"][base]["zero_recall"])
        d["pool_recall_delta"] = round(report["pool"][kw]["pool_recall"]
                                       - report["pool"][base]["pool_recall"], 4)
        report["kw_ablation"][f"w_o={wo}"] = d

    # 0.15 网格对比: 各配置 vs 生产(0.15, 0.3)
    for wo, wk in configs:
        key = cfg_key(wo, wk)
        if key == PROD_KEY:
            continue
        d = pair_diff(rows_by_cfg[PROD_KEY], rows_by_cfg[key], PROD_KEY, key)
        d["delta"] = {m: round(report["aggregates"][key][m]
                                - report["aggregates"][PROD_KEY][m], 4)
                      for m in ("mrr", "recall_5", "recall_10", "sec_mrr")}
        d["zero_recall_delta"] = (report["aggregates"][key]["zero_recall"]
                                  - report["aggregates"][PROD_KEY]["zero_recall"])
        report["grid_vs_prod"][key] = d

    _atomic_json(REPORT, report)
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（冒烟）")
    ap.add_argument("--smoke", action="store_true", help="sanity 校验（限 --limit）")
    args = ap.parse_args()

    print(f"[start] multi 路径 BM25 权重网格 + kw 消融"
          f"{' (limit=%d)' % args.limit if args.limit else ''}", flush=True)
    records = phase1(args.limit)
    results, pool_info, prior_mismatch, top_mismatch, configs = \
        phase23(records, args.smoke)
    report = phase4(results, pool_info, configs, prior_mismatch, top_mismatch)
    s = report["sanity"]
    print(f"\n[sanity] prior 逐值不一致 {len(s['prior_mismatch'])} 题: "
          f"{s['prior_mismatch'][:10]}")
    print(f"[sanity] (0.15,0.3) top10 与生产 planC|rw 不一致 "
          f"{len(s['top_mismatch'])} 题: {s['top_mismatch'][:10]}")
    print("\n[aggregates] 配置 | MRR | R@5 | R@10 | secMRR | zero | pool_recall")
    for wo, wk in configs:
        key = cfg_key(wo, wk)
        a, p = report["aggregates"][key], report["pool"][key]
        print(f"  {key:<10} {a['mrr']:.4f} {a['recall_5']:.4f} "
              f"{a['recall_10']:.4f} {a['sec_mrr']:.4f} {a['zero_recall']:>2} "
              f"{p['pool_recall']:.4f}")
    print("\n[kw 消融 Δ (k0.3 − k0)]")
    for wo in GRID_O:
        d = report["kw_ablation"][f"w_o={wo}"]
        print(f"  w_o={wo:<4} mrr {d['delta']['mrr']:+.4f} "
              f"zero {d['zero_recall_delta']:+d} "
              f"pool_recall {d['pool_recall_delta']:+.4f} "
              f"救 {len(d['rescued'])} 丢 {len(d['lost'])}")
    print(f"\n[report] {REPORT}")


if __name__ == "__main__":
    main()
