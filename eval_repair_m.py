#!/usr/bin/env python3
"""M 臂：生产召回原样保留 + CE 精排 query 从 rw[0] 换成 v2 合体句。

A 臂（生产 v2.9）: 存量回放 planC|rw（MRR 0.5932）。
M 臂: 候选/配额/池与 A 完全一致（生产 candidates + 方案C + 配额 20/10 + 池上限 60/50），
      仅 CE query = v2 合体句（data/repair_cache_v2.json）。
CE 原始分复用 data/repair_stage2_v2/ce_scores.json（v2 句已算 9672 pairs），
生产池差集部分补算。

用法: python3 eval_repair_m.py [--limit N]
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import eval_gate_ab as ab
from eval_repair_stage2 import (A_QUOTA_ORIG, LAMBDA_LEN, TOPK, gold_metrics,
                                quota_select, replay_top10)
from hybrid_search import HybridSearcher

PROD_CAND_DIR = "data/ce_query_quota_ab/candidates"
PROD_TOP10_DIR = "data/ce_query_quota_ab/top10"
V2_CACHE = "data/repair_cache_v2.json"
V2_CE = "data/repair_stage2_v2/ce_scores.json"
OUT_DIR = "data/repair_m"
CE_FILE = os.path.join(OUT_DIR, "ce_scores.json")
REPORT = os.path.join(OUT_DIR, "report.json")

SPOTLIGHT = ["Q_S07", "Q_S15", "Q_S23", "Q_SR03", "Q_SR06",
             "Q_S13", "Q_D09", "Q_D12"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（冒烟）")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    v2_cache = json.load(open(V2_CACHE, encoding="utf-8"))
    v2_ce = json.load(open(V2_CE, encoding="utf-8")).get("scores", {})
    own_ce = {}
    if os.path.exists(CE_FILE):
        own_ce = json.load(open(CE_FILE, encoding="utf-8")).get("scores", {})

    qs = ab.gs[:args.limit] if args.limit else ab.gs
    q_by_id = {q["id"]: q for q in ab.gs}
    print(f"[start] {len(qs)} 题 M 臂回放", flush=True)

    srch = HybridSearcher(enable_reranker=True)
    srch._reranker._load()

    rows_a, rows_m = [], []
    t0 = time.time()
    n_ce = 0
    for i, q in enumerate(qs, 1):
        qid = q["id"]
        with open(os.path.join(PROD_CAND_DIR, qid + ".json"),
                  encoding="utf-8") as f:
            rec = json.load(f)
        v2 = v2_cache[rec["question"]]["v2_query"]

        if rec["plain"]:
            # plain 题（58 道 kw-only）: 生产候选无缓存——
            # 复刻 search() 候选采集（RRF top30 ∪ Dense top5），CE query 换 v2 句
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
            results = srch._rrf_ce_fusion(v2, rrf_scores, doc_store,
                                          sorted_keys, TOPK, 0.3, LAMBDA_LEN)
            m_ids = [r["metadata"].get("chunk_id", "") for r in results]
        else:
            cand = rec["cand"]
            cand_by_id = {c["chunk_id"]: c for c in cand}
            pool_ids, active = quota_select(cand, rec["n_rw"], rec["n_sq"],
                                            A_QUOTA_ORIG)

            missing = [cid for cid in pool_ids
                       if v2 not in v2_ce or cid not in v2_ce[v2]]
            missing = [cid for cid in missing
                       if v2 not in own_ce or cid not in own_ce[v2]]
            if missing:
                pairs = [(v2, cand_by_id[cid]["text"][:500]) for cid in missing]
                with srch._reranker._infer_lock:
                    raw = [float(x) for x in srch._reranker._model.predict(
                        pairs, show_progress_bar=False)]
                own_ce.setdefault(v2, {})
                for cid, val in zip(missing, raw):
                    own_ce[v2][cid] = val
                n_ce += len(missing)
                with open(CE_FILE, "w", encoding="utf-8") as f:
                    json.dump({"version": 1, "scores": own_ce}, f,
                              ensure_ascii=False, indent=1)

            def get_ce(cid):
                if v2 in own_ce and cid in own_ce[v2]:
                    return own_ce[v2][cid]
                return v2_ce[v2][cid]

            merged = {v2: {cid: get_ce(cid) for cid in pool_ids}}
            top = replay_top10(cand_by_id, pool_ids, v2, merged)
            m_ids = [cid for cid, _ in top]

        with open(os.path.join(PROD_TOP10_DIR, qid + ".json"),
                  encoding="utf-8") as f:
            stored = json.load(f)
        a_ids = stored["variants"]["planC|rw"]["ids"]

        ma = gold_metrics(q, a_ids)
        mm = gold_metrics(q, m_ids)
        rows_a.append({"qid": qid, **ma})
        rows_m.append({"qid": qid, **mm})
        if i % 20 == 0 or i == len(qs):
            print(f"[M] {i}/{len(qs)} 新CE {n_ce} "
                  f"({time.time() - t0:.0f}s)", flush=True)

    def agg(rows):
        n = len(rows)
        return {"n": n, "mrr": round(sum(r["rr"] for r in rows) / n, 4),
                "recall_5": round(sum(r["recall_5"] for r in rows) / n, 4),
                "recall_10": round(sum(r["recall_10"] for r in rows) / n, 4),
                "sec_mrr": round(sum(r["sec_rr"] for r in rows) / n, 4),
                "zero_recall": sum(1 for r in rows if r["rr"] == 0)}

    def diff(ra_rows, rb_rows):
        by = {r["qid"]: r for r in rb_rows}
        rescued, lost, improved, worsened = [], [], [], []
        for ra in ra_rows:
            rb = by[ra["qid"]]
            if ra["rr"] == 0 and rb["rr"] > 0:
                rescued.append((ra["qid"], rb["rr"]))
            elif ra["rr"] > 0 and rb["rr"] == 0:
                lost.append((ra["qid"], ra["rr"]))
            elif rb["rr"] > ra["rr"]:
                improved.append((ra["qid"], ra["rr"], rb["rr"]))
            elif 0 < rb["rr"] < ra["rr"]:
                worsened.append((ra["qid"], ra["rr"], rb["rr"]))
        return rescued, lost, improved, worsened

    agg_a, agg_m = agg(rows_a), agg(rows_m)
    rescued, lost, improved, worsened = diff(rows_a, rows_m)
    zero_a = [r["qid"] for r in rows_a if r["rr"] == 0]
    zero_m = [r["qid"] for r in rows_m if r["rr"] == 0]

    report = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {"a_arm": "生产 v2.9 planC|rw 存量回放",
                   "m_arm": "生产候选/配额 + CE query=v2合体句",
                   "quota_orig": A_QUOTA_ORIG, "n": len(qs)},
        "aggregates": {"A": agg_a, "M": agg_m},
        "diffs": {"rescued": rescued, "lost": lost,
                  "improved": improved, "worsened": worsened},
        "zero_recall": {"A": zero_a, "M": zero_m},
    }
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    print("\n" + "=" * 88)
    print("M 臂汇总（生产召回 + CE query=v2合体句）vs 生产 A")
    print("=" * 88)
    print(f"{'臂':<4}{'n':>5}{'MRR':>8}{'R@5':>8}{'R@10':>8}{'secMRR':>8}"
          f"{'零召回':>7}")
    for name, a in (("A", agg_a), ("M", agg_m)):
        print(f"{name:<4}{a['n']:>5}{a['mrr']:>8}{a['recall_5']:>8.4f}"
              f"{a['recall_10']:>8.4f}{a['sec_mrr']:>8}{a['zero_recall']:>7}")
    print(f"\n[A_vs_M] 救 {len(rescued)} | 丢 {len(lost)} | "
          f"升 {len(improved)} | 降 {len(worsened)}")
    for qid, rr in rescued:
        print(f"  rescued {qid} {round(rr, 4)}")
    for qid, rr in lost:
        print(f"  lost {qid} {round(rr, 4)}")
    print(f"\n[零召回] A({len(zero_a)}): {zero_a}")
    print(f"          M({len(zero_m)}): {zero_m}")
    print("\n[专项检查（rw-CE 救5 丢3）]")
    by_a = {r["qid"]: r for r in rows_a}
    by_m = {r["qid"]: r for r in rows_m}
    for qid in SPOTLIGHT:
        if qid in by_a:
            fa = "✓" if by_a[qid]["rr"] > 0 else "✗"
            fm = "✓" if by_m[qid]["rr"] > 0 else "✗"
            print(f"  {qid:<8} A:rr={by_a[qid]['rr']:<6} M:rr={by_m[qid]['rr']:<6}"
                  f" (A{fa} M{fm})")
    print(f"\n→ {REPORT}", flush=True)


if __name__ == "__main__":
    main()
