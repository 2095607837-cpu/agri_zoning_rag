#!/usr/bin/env python3
"""CE 专用改写句 R 臂回放：生产召回/配额完全不变，仅 CE query = ce_query 句。

A 臂（生产 planC|rw）= 读 data/ce_query_quota_ab/top10 存量；
R 臂 = 生产候选 + quota_select(20) + CE(query=ce_query)（multi 题）；
      plain 题实时复刻 plain 路径（RRF top30 ∪ Dense top5），CE query 换 ce_query。

sanity: A 臂重放（multi: CE=rw[0]/原问; plain: CE=原问）必须与存量逐位一致。

缓存: data/repair_ce_rw/ce_scores.json（新句 CE 原始分, 键=ce_query 句, 断点续跑）。
报告: data/repair_ce_rw/report.json
用法: python3 eval_repair_ce_rw.py [--limit N]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import eval_gate_ab as ab
from eval_repair_stage2 import (quota_select, replay_top10, gold_metrics,
                                agg, pair_diff, A_QUOTA_ORIG, LAMBDA_LEN, TOPK)
from hybrid_search import HybridSearcher

OUT_DIR = "data/repair_ce_rw"
CE_CACHE = os.path.join(OUT_DIR, "cache.json")
CE_FILE = os.path.join(OUT_DIR, "ce_scores.json")
REPORT = os.path.join(OUT_DIR, "report.json")
PROD_CAND_DIR = "data/ce_query_quota_ab/candidates"
PROD_TOP10_DIR = "data/ce_query_quota_ab/top10"
PROD_CE = "data/ce_query_quota_ab/ce_scores.json"

SPOTLIGHT = ["Q_S07", "Q_S15", "Q_S23", "Q_SR03", "Q_SR06",   # rw 压制救5
             "Q_S13", "Q_D09", "Q_D12"]                        # rw 收窄丢3


def _atomic_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（冒烟）")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    ce_cache = json.load(open(CE_CACHE, encoding="utf-8"))
    prod_ce = json.load(open(PROD_CE, encoding="utf-8")).get("scores", {})
    own_ce = {}
    if os.path.exists(CE_FILE):
        own_ce = json.load(open(CE_FILE, encoding="utf-8")).get("scores", {})

    qs = ab.gs[:args.limit] if args.limit else ab.gs
    q_by_id = {q["id"]: q for q in ab.gs}
    print(f"[start] {len(qs)} 题 CE 专用改写 R 臂回放", flush=True)

    srch = HybridSearcher(enable_reranker=True)
    srch._reranker._load()

    rows_a, rows_r = [], []
    per_q = {}
    sanity_multi, sanity_plain = [], []
    t0, n_ce = time.time(), 0
    for i, q in enumerate(qs, 1):
        qid = q["id"]
        with open(os.path.join(PROD_CAND_DIR, qid + ".json"),
                  encoding="utf-8") as f:
            rec = json.load(f)
        ceq = ce_cache[q["question"]]["ce_query"]

        if rec["plain"]:
            # plain 题: 复刻 search() plain 路径, CE query 换 ce_query
            pool_size = max(TOPK * 4, 20)
            rrf_scores, doc_store, chroma_raw = srch._rrf_retrieve(
                rec["question"], dense_k=pool_size, bm25_k=pool_size,
                keywords=rec["kw"] or None)
            rrf_top30 = sorted(rrf_scores, key=lambda k: -rrf_scores[k])[:30]
            rrf_top30_set = set(rrf_top30)
            dense_top5 = []
            for doc, _ in chroma_raw[:5]:
                key = srch._chunk_key(doc)
                if key not in rrf_top30_set and key not in dense_top5:
                    dense_top5.append(key)
            sorted_keys = rrf_top30 + dense_top5

            def _fuse(ce_q):
                out = srch._rrf_ce_fusion(ce_q, rrf_scores, doc_store,
                                          sorted_keys, TOPK, 0.3, LAMBDA_LEN)
                return [r["metadata"].get("chunk_id", "") for r in out]

            r_ids = _fuse(ceq)
            a_ids = _fuse(rec["question"])          # A 臂重放（生产 CE=原问）
        else:
            cand = rec["cand"]
            cand_by_id = {c["chunk_id"]: c for c in cand}
            pool_ids, active = quota_select(cand, rec["n_rw"], rec["n_sq"],
                                            A_QUOTA_ORIG)

            def get_ce(ce_q, cid):
                if ce_q in own_ce and cid in own_ce[ce_q]:
                    return own_ce[ce_q][cid]
                return prod_ce[ce_q][cid]

            def has_ce(ce_q, cid):
                return ((ce_q in own_ce and cid in own_ce[ce_q])
                        or (ce_q in prod_ce and cid in prod_ce[ce_q]))

            missing = [cid for cid in pool_ids if not has_ce(ceq, cid)]
            if missing:
                pairs = [(ceq, cand_by_id[cid]["text"][:500]) for cid in missing]
                with srch._reranker._infer_lock:
                    raw = [float(x) for x in srch._reranker._model.predict(
                        pairs, show_progress_bar=False)]
                own_ce.setdefault(ceq, {})
                for cid, v in zip(missing, raw):
                    own_ce[ceq][cid] = v
                n_ce += len(missing)
                _atomic_json(CE_FILE, {"version": 1, "scores": own_ce})

            merged = {ceq: {cid: get_ce(ceq, cid) for cid in pool_ids}}
            r_ids = [cid for cid, _ in
                     replay_top10(cand_by_id, pool_ids, ceq, merged)]
            # A 臂重放（生产 CE=rw[0]/原问, 复用生产 CE 缓存）
            ceq_a = rec["rw"][0] if rec["n_rw"] > 0 else rec["question"]
            merged_a = {ceq_a: {cid: get_ce(ceq_a, cid) for cid in pool_ids}}
            a_ids = [cid for cid, _ in
                     replay_top10(cand_by_id, pool_ids, ceq_a, merged_a)]

        with open(os.path.join(PROD_TOP10_DIR, qid + ".json"),
                  encoding="utf-8") as f:
            stored = json.load(f)["variants"]["planC|rw"]["ids"]
        if a_ids != stored:
            (sanity_plain if rec["plain"] else sanity_multi).append(qid)

        ma = gold_metrics(q, stored)
        mr = gold_metrics(q, r_ids)
        rows_a.append({"qid": qid, **ma})
        rows_r.append({"qid": qid, **mr})
        per_q[qid] = {"a": stored, "r": r_ids}
        if i % 20 == 0 or i == len(qs):
            print(f"[R] {i}/{len(qs)} 新CE {n_ce} ({time.time() - t0:.0f}s)",
                  flush=True)

    a_agg, r_agg = agg(rows_a), agg(rows_r)
    diff = pair_diff(rows_a, rows_r, "a", "r")
    zero_a = [r["qid"] for r in rows_a if r["rr"] == 0]
    zero_r = [r["qid"] for r in rows_r if r["rr"] == 0]
    spotlight = {}
    by_a = {r["qid"]: r for r in rows_a}
    by_r = {r["qid"]: r for r in rows_r}
    for qid in SPOTLIGHT:
        if qid in by_a:
            spotlight[qid] = {"a_rr": by_a[qid]["rr"], "r_rr": by_r[qid]["rr"]}
    report = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {"a_arm": "生产 v2.9 planC|rw 存量", "r_arm": "生产召回+配额不变, CE query=ce_query 五要素句", "n": len(qs)},
        "aggregates": {"A": a_agg, "R": r_agg},
        "diffs": diff,
        "zero_recall": {"A": zero_a, "R": zero_r},
        "spotlight": spotlight,
        "sanity": {"multi_mismatch": sanity_multi, "plain_mismatch": sanity_plain},
    }
    _atomic_json(REPORT, report)
    _atomic_json(os.path.join(OUT_DIR, "per_q.json"), per_q)

    print(f"\n[sanity] A 臂重放 vs 存量: multi 不一致 {len(sanity_multi)} 题 "
          f"{sanity_multi[:10]}, plain 不一致 {len(sanity_plain)} 题 "
          f"{sanity_plain[:10]}")
    print(f"\n[A] MRR {a_agg['mrr']:.4f} R@10 {a_agg['recall_10']:.4f} "
          f"zero {a_agg['zero_recall']}")
    print(f"[R] MRR {r_agg['mrr']:.4f} R@10 {r_agg['recall_10']:.4f} "
          f"zero {r_agg['zero_recall']}")
    print(f"救 {len(diff['rescued'])} 丢 {len(diff['lost'])} "
          f"升 {len(diff['improved'])} 降 {len(diff['worsened'])}")
    print(f"[spotlight] {spotlight}")
    print(f"[report] {REPORT}")


if __name__ == "__main__":
    main()
