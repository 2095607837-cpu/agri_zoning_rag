#!/usr/bin/env python3
"""8 道丢题 v2 合体句快速验证: 召回 + CE 重放, 对比 A / C_v1 / C_v2。

C_v2 口径与 Stage 2 C 臂一致（统一 multi、rw 移除、sq/kw 生产原样、
Dense50/BM25原句30/BM25kw20、SubQ 20/10、配额 30、池上限 60/50），
仅主 query 与 CE query 换成 v2 合体句（文档风格改写）。

用法: python3 eval_repair_v2_spot.py
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import eval_gate_ab as ab
import query_rewriter as qr
from eval_repair_stage2 import (
    C_BM25_K, C_DENSE_K, C_KW_K, C_QUOTA_ORIG, C_SUBQ_BM25_K, C_SUBQ_DENSE_K,
    LAMBDA_LEN, TOPK, gold_metrics, quota_select, replay_top10)
from hybrid_search import HybridSearcher
from llm_client import call_llm
from repair_v2_sample import V2_PROMPT

LOST8 = ["Q_E24", "Q_S07", "Q_S15", "Q_D26", "Q_N20", "Q_L07", "Q_SR06", "Q_T28"]
OUT_DIR = "data/repair_v2_spot"
CE_FILE = os.path.join(OUT_DIR, "ce_scores.json")


def gen_v2(query, mapped):
    prompt = V2_PROMPT.format(query=query, mapped=mapped)
    resp = call_llm([{"role": "user", "content": prompt}],
                    temperature=0, stream=False, json_mode=True)
    if isinstance(resp, str):
        s, e = resp.find("{"), resp.rfind("}") + 1
        resp = json.loads(resp[s:e])
    return str(resp.get("repair_query", "")).strip()


def collect(srch, v2, sq, kw):
    _, cand_list, _, _ = srch._collect_candidates(
        v2, TOPK, False, [], sq, kw, None, LAMBDA_LEN,
        C_DENSE_K, C_BM25_K, C_SUBQ_DENSE_K, C_SUBQ_BM25_K,
        orig_kw_k=C_KW_K, force_multi=True)
    return [{
        "chunk_id": c["chunk_id"],
        "retrieval_prior": c["retrieval_prior"],
        "query_hits": sorted(c["query_hits"]),
        "text_len": len(c["text"]),
        "text": c["text"],
    } for c in cand_list]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    by_id = {}
    for f in glob.glob("data/ce_query_quota_ab/candidates/Q_*.json"):
        d = json.load(open(f, encoding="utf-8"))
        by_id[d["qid"]] = d
    q_by_id = {q["id"]: q for q in ab.gs}
    stage2_top = {}
    for qid in LOST8:
        with open(f"data/repair_stage2/top10/{qid}.json", encoding="utf-8") as f:
            stage2_top[qid] = json.load(f)
    qr._load_term_map()
    from repair_query import term_replace

    ce_cache = {}
    if os.path.exists(CE_FILE):
        ce_cache = json.load(open(CE_FILE, encoding="utf-8")).get("scores", {})

    srch = HybridSearcher(enable_reranker=True)
    srch._reranker._load()

    rows = []
    for qid in LOST8:
        rec = by_id[qid]
        q = q_by_id[qid]
        question = rec["question"]
        mapped, _ = term_replace(question, qr._term_map)
        v2 = gen_v2(question, mapped)
        print(f"[gen] {qid}: {v2}", flush=True)

        cand = collect(srch, v2, rec["sq"], rec["kw"])
        cand_by_id = {c["chunk_id"]: c for c in cand}
        pool_ids, active = quota_select(cand, 0, len(rec["sq"]), C_QUOTA_ORIG)

        missing = [cid for cid in pool_ids
                   if v2 not in ce_cache or cid not in ce_cache[v2]]
        if missing:
            pairs = [(v2, cand_by_id[cid]["text"][:500]) for cid in missing]
            with srch._reranker._infer_lock:
                raw = [float(x) for x in srch._reranker._model.predict(
                    pairs, show_progress_bar=False)]
            ce_cache.setdefault(v2, {})
            for cid, val in zip(missing, raw):
                ce_cache[v2][cid] = val
            json.dump({"version": 1, "scores": ce_cache},
                      open(CE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

        top = replay_top10(cand_by_id, pool_ids, v2, ce_cache)
        v2_ids = [cid for cid, _ in top]

        gold, gold_sections = ab.gold_matches(q)
        gold_pool_rank = None
        for i, cid in enumerate(pool_ids, 1):
            if cid in gold:
                gold_pool_rank = i
                break
        gold_final_rank = None
        for i, cid in enumerate(v2_ids, 1):
            if cid in gold:
                gold_final_rank = i
                break

        st = stage2_top[qid]
        ma = gold_metrics(q, st["A"]["ids"])
        mc1 = gold_metrics(q, st["C"]["ids"])
        mc2 = gold_metrics(q, v2_ids)
        rows.append({"qid": qid, "A": ma["rr"], "C_v1": mc1["rr"],
                     "C_v2": mc2["rr"], "pool": len(pool_ids),
                     "gold_pool_rank": gold_pool_rank,
                     "gold_final_rank": gold_final_rank,
                     "v2": v2, "v1": st["C"]["texts"][:1]})
        print(f"  A rr={ma['rr']} | C_v1 rr={mc1['rr']} | C_v2 rr={mc2['rr']} "
              f"| pool={len(pool_ids)} | gold池内rank={gold_pool_rank} "
              f"| CE后rank={gold_final_rank}", flush=True)

    print("\n" + "=" * 88)
    print(f"{'题':<8}{'A':>8}{'C_v1':>8}{'C_v2':>8}{'池':>5}{'池内rank':>8}{'CE后rank':>8}")
    for r in rows:
        print(f"{r['qid']:<8}{r['A']:>8}{r['C_v1']:>8}{r['C_v2']:>8}"
              f"{r['pool']:>5}{str(r['gold_pool_rank']):>8}"
              f"{str(r['gold_final_rank']):>8}")
    mrr = lambda key: round(sum(r[key] for r in rows) / len(rows), 4)
    print(f"\n8题均值: A={mrr('A')}  C_v1={mrr('C_v1')}  C_v2={mrr('C_v2')}")
    print(f"\n[句子对照]")
    for r in rows:
        print(f"\n[{r['qid']}]")
        print(f"  原问 : {by_id[r['qid']]['question']}")
        print(f"  生产rw: {by_id[r['qid']]['rw']}")
        print(f"  v2合体: {r['v2']}")


if __name__ == "__main__":
    main()
