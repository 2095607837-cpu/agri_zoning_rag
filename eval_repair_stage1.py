#!/usr/bin/env python3
"""修复 query 实验 Stage 1：零 CE 召回端对比 A vs B vs B'。

A  = 生产现状（v2.9 召回口径）: orig + rw + sq + kw（复用 ce_query_quota_ab 候选缓存;
     plain 题 = 原问 top10）
B0 = 原问 + rw 通道移除（对照: 单独隔离 rw 移除效应）
B  = 合体 query: repair 替 orig, rw 通道移除, sq/kw 不变
B' = B + SubQ 也做程序化术语替换

指标（沿用 eval_gate_ab 口径）: retrievable = gold_in_union ∪ gold_in_fallback;
prior_rank / pool size / sec_in_union / best_channel。

输出: data/repair_stage1/collections_b.json、collections_bprime.json、
a_fallback.json、report.json。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import query_rewriter as qr
import eval_gate_ab as ab
import repair_query as rp

OUT_DIR = "data/repair_stage1"
COLL_B0 = os.path.join(OUT_DIR, "collections_b0.json")
COLL_B = os.path.join(OUT_DIR, "collections_b.json")
COLL_BP = os.path.join(OUT_DIR, "collections_bprime.json")
A_FALLBACK = os.path.join(OUT_DIR, "a_fallback.json")
REPORT = os.path.join(OUT_DIR, "report.json")

CAND_DIR = "data/ce_query_quota_ab/candidates"
TOPK = 10
LAMBDA_LEN = 0.1

SPOTLIGHT = ["Q_S07", "Q_S15", "Q_S23", "Q_SR03", "Q_SR06",  # 救5
             "Q_S13", "Q_D09", "Q_D12"]                       # 丢3


def load_repairs():
    with open(rp.CACHE_FILE, encoding="utf-8") as f:
        return json.load(f)


def row_from_cand(q, cand, judge_top10):
    gold, gold_sections = ab.gold_matches(q)
    row = {"id": q["id"], "pool": None if cand is None else len(cand),
           "gold_in_union": False, "sec_in_union": False, "prior_rank": None,
           "best_channel": None, "best_rank": None, "gold_first_src": None,
           "gold_in_fallback": False, "fallback_rank": None}
    if cand:
        for rank, c in enumerate(cand, 1):
            if c["chunk_id"] in gold and not row["gold_in_union"]:
                row["gold_in_union"] = True
                row["prior_rank"] = rank
                row["best_channel"] = c["best_channel"]
                row["best_rank"] = c["best_rank"]
                row["gold_first_src"] = sorted(c["sources"])
            sid = c.get("metadata", {}).get("section_id")
            if sid in gold_sections and not row["sec_in_union"]:
                row["sec_in_union"] = True
    else:
        for rank, r in enumerate(judge_top10, 1):
            if r["chunk_id"] in gold and not row["gold_in_fallback"]:
                row["gold_in_fallback"] = True
                row["fallback_rank"] = rank
            if r["section_id"] in gold_sections:
                row["sec_in_union"] = True
    return row


def plain_top10(query, keywords=None):
    res = ab.searcher0.search(query, top_k=TOPK, expand_context=False,
                              lambda_length=LAMBDA_LEN, keywords=keywords)
    return [{"chunk_id": r.get("metadata", {}).get("chunk_id"),
             "section_id": r.get("metadata", {}).get("section_id")}
            for r in res[:TOPK]]


def load_a_rows():
    """A 臂: 复用候选缓存; plain 题重算原问 top10（确定性, 缓存）。"""
    a_fb = {}
    if os.path.exists(A_FALLBACK):
        with open(A_FALLBACK, encoding="utf-8") as f:
            a_fb = json.load(f)
    rows = {}
    for q in ab.gs:
        qid = q["id"]
        with open(os.path.join(CAND_DIR, qid + ".json"), encoding="utf-8") as f:
            rec = json.load(f)
        if rec["plain"] or rec["cand"] is None:
            if qid not in a_fb:
                a_fb[qid] = plain_top10(q["question"], keywords=rec["kw"])
            rows[qid] = row_from_cand(q, None, a_fb[qid])
        else:
            rows[qid] = row_from_cand(q, rec["cand"], None)
    with open(A_FALLBACK, "w", encoding="utf-8") as f:
        json.dump(a_fb, f, ensure_ascii=False, indent=1)
    return rows


def collect_side(repairs, sq_replace, coll_path, b0=False):
    """B0/B/B' 臂: rw 移除; b0 用原问, 否则 repair 替 orig; sq_replace=True 时 SubQ 术语替换。"""
    cache = {}
    if os.path.exists(coll_path):
        with open(coll_path, encoding="utf-8") as f:
            cache = json.load(f)
    qr._load_term_map()
    t0 = time.time()
    for i, q in enumerate(ab.gs, 1):
        qid = q["id"]
        repair = q["question"] if b0 else repairs[q["question"]]["repair_query"]
        with open(os.path.join(CAND_DIR, qid + ".json"), encoding="utf-8") as f:
            rec = json.load(f)
        sq = [rp.term_replace(s, qr._term_map)[0] if sq_replace else s
              for s in rec["sq"]]
        key = f'{qid}|{repair}|{"|".join(sq)}'
        if qid in cache and cache[qid].get("_key") == key:
            continue
        cand = None
        if sq:
            _, cand_list, _, _ = ab.searcher0._collect_candidates(
                repair, TOPK, False, [], sq, rec["kw"], None, LAMBDA_LEN,
                30, 20, 20, 10)
            cand = None if cand_list is None else [{
                "chunk_id": c["chunk_id"], "retrieval_prior": c["retrieval_prior"],
                "best_channel": c["best_channel"], "best_rank": c["best_rank"],
                "sources": sorted(c["sources"]),
                "metadata": {"section_id": c["metadata"].get("section_id", "")},
            } for c in cand_list]
        # plain 路径对齐生产口径: search(query, keywords=kw)
        top10 = plain_top10(repair, keywords=rec["kw"])
        cache[qid] = {"_key": key, "repair": repair, "sq": sq,
                      "cand": cand, "top10": top10}
        print(f"[{i}/{len(ab.gs)}] {qid} pool={None if cand is None else len(cand)} "
              f"({time.time() - t0:.0f}s)", flush=True)
        if i % 20 == 0:
            with open(coll_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=1)
    with open(coll_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    return cache


def rows_from_coll(collect):
    rows = {}
    for q in ab.gs:
        rec = collect[q["id"]]
        rows[q["id"]] = row_from_cand(q, rec["cand"], rec["top10"])
    return rows


def agg(rows, name):
    rs = list(rows.values())
    n = len(rs)
    u = sum(r["gold_in_union"] for r in rs)
    fb = sum(r["gold_in_fallback"] for r in rs)
    sec = sum(r["sec_in_union"] for r in rs)
    ranks = sorted(r["prior_rank"] for r in rs if r["prior_rank"])
    pools = sorted(r["pool"] for r in rs if r["pool"])
    med = ranks[len(ranks) // 2] if ranks else None
    return {"arm": name, "n": n, "union": u, "union_pct": round(u / n, 4),
            "fallback": fb, "retrievable": u + fb,
            "retrievable_pct": round((u + fb) / n, 4), "sec_union": sec,
            "median_rank": med, "rank>50": sum(1 for r in ranks if r > 50),
            "median_pool": pools[len(pools) // 2] if pools else None,
            "n_multi": len(pools), "n_plain": n - len(pools)}


def retrievable(r):
    return r["gold_in_union"] or r["gold_in_fallback"]


def diffs(ra, rb, a_name, b_name):
    rescued, lost, improved, worsened = [], [], [], []
    for qid in sorted(ra):
        a, b = ra[qid], rb[qid]
        if not retrievable(a) and retrievable(b):
            rescued.append({"id": qid,
                            "via": "union" if b["gold_in_union"] else "fallback",
                            "channel": b["best_channel"], "src": b["gold_first_src"],
                            "prior_rank": b["prior_rank"]})
        elif retrievable(a) and not retrievable(b):
            lost.append({"id": qid,
                         "via": "union" if a["gold_in_union"] else "fallback",
                         "channel": a["best_channel"], "src": a["gold_first_src"],
                         "prior_rank": a["prior_rank"]})
        elif a["gold_in_union"] and b["gold_in_union"]:
            delta = a["prior_rank"] - b["prior_rank"]
            if delta >= 3:
                improved.append({"id": qid, f"{a_name}_rank": a["prior_rank"],
                                 f"{b_name}_rank": b["prior_rank"]})
            elif delta <= -3:
                worsened.append({"id": qid, f"{a_name}_rank": a["prior_rank"],
                                 f"{b_name}_rank": b["prior_rank"]})
    return {"rescued": rescued, "lost": lost,
            "rank_improved": improved, "rank_worsened": worsened}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-bprime", action="store_true", help="不跑 B' 臂")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    repairs = load_repairs()
    for q in ab.gs:
        if q["question"] not in repairs:
            sys.exit(f"缺修复记录: {q['id']}")

    print("[A] 复用 ce_query_quota_ab 候选缓存…", flush=True)
    rows_a = load_a_rows()
    print("[B0] 原问召回（rw 移除, 对照）…", flush=True)
    coll_b0 = collect_side(repairs, sq_replace=False, coll_path=COLL_B0, b0=True)
    rows_b0 = rows_from_coll(coll_b0)
    print("[B] 修复句召回（rw 移除）…", flush=True)
    coll_b = collect_side(repairs, sq_replace=False, coll_path=COLL_B)
    rows_b = rows_from_coll(coll_b)
    rows_bp = None
    if not args.skip_bprime:
        print("[B'] 修复句 + SubQ 术语替换召回…", flush=True)
        coll_bp = collect_side(repairs, sq_replace=True, coll_path=COLL_BP)
        rows_bp = rows_from_coll(coll_bp)

    report = {"meta": {"date": time.strftime("%Y-%m-%d %H:%M"),
                       "n": len(ab.gs)},
              "agg": {"A": agg(rows_a, "A"), "B0": agg(rows_b0, "B0"),
                      "B": agg(rows_b, "B")},
              "diffs": {"A_vs_B0": diffs(rows_a, rows_b0, "A", "B0"),
                        "B0_vs_B": diffs(rows_b0, rows_b, "B0", "B"),
                        "A_vs_B": diffs(rows_a, rows_b, "A", "B")},
              "per_question": {qid: {"A": rows_a[qid], "B0": rows_b0[qid],
                                     "B": rows_b[qid]}
                               for qid in rows_a}}
    if rows_bp:
        report["agg"]["B'"] = agg(rows_bp, "B'")
        report["diffs"]["A_vs_B'"] = diffs(rows_a, rows_bp, "A", "B'")
        report["diffs"]["B_vs_B'"] = diffs(rows_b, rows_bp, "B", "B'")
        for qid in report["per_question"]:
            report["per_question"][qid]["B'"] = rows_bp[qid]

    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    # ── 控制台摘要 ──
    print("\n" + "=" * 88)
    print("Stage 1 汇总（零 CE 召回端, retrievable = union ∪ fallback）")
    print("=" * 88)
    hdr = f'{"臂":<4}{"n":>5}{"union":>8}{"fb":>5}{"可检索":>8}{"sec":>6}{"中位rank":>9}{"rank>50":>8}{"中位池":>8}'
    print(hdr)
    for name in ["A", "B0", "B"] + (["B'"] if rows_bp else []):
        a = report["agg"][name]
        print(f'{name:<4}{a["n"]:>5}{a["union"]:>8}{a["fallback"]:>5}'
              f'{a["retrievable"]:>8}{a["sec_union"]:>6}'
              f'{str(a["median_rank"]):>9}{a["rank>50"]:>8}'
              f'{str(a["median_pool"]):>8}')
    for name in ["A_vs_B0", "B0_vs_B", "A_vs_B"] + \
                (["A_vs_B'", "B_vs_B'"] if rows_bp else []):
        d = report["diffs"][name]
        print(f"\n[{name}] 救 {len(d['rescued'])} | 丢 {len(d['lost'])} | "
              f"rank↑ {len(d['rank_improved'])} | rank↓ {len(d['rank_worsened'])}")
        for tag in ["rescued", "lost"]:
            for r in d[tag]:
                print(f"  {tag} {r['id']} via={r['via']} ch={r['channel']} "
                      f"src={r['src']} rank={r['prior_rank']}")
        for r in d["rank_improved"]:
            print(f"  rank↑ {r['id']} {r[list(r)[1]]}→{r[list(r)[2]]}")
        for r in d["rank_worsened"]:
            print(f"  rank↓ {r['id']} {r[list(r)[1]]}→{r[list(r)[2]]}")

    print("\n" + "=" * 88)
    print("专项检查（救5 丢3）")
    print("=" * 88)
    for qid in SPOTLIGHT:
        pq = report["per_question"][qid]
        parts = []
        for name in ["A", "B0", "B"] + (["B'"] if rows_bp else []):
            r = pq[name]
            hit = "U" if r["gold_in_union"] else ("F" if r["gold_in_fallback"] else "✗")
            parts.append(f'{name}:{hit}@{r["prior_rank"] or r["fallback_rank"] or "-"}'
                         f'/pool{r["pool"] or "-"}')
        print(f'{qid:<8} {"  ".join(parts)}')
    print(f"\n→ {REPORT}", flush=True)


if __name__ == "__main__":
    main()
