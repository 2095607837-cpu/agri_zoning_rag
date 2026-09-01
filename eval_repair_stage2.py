#!/usr/bin/env python3
"""修复 query 实验 Stage 2：合体 query 端到端对比（统一 multi 路径）vs 生产 A。

C 臂（合体）: 180 题全部走 multi 机制——
  主 query = 修复句（data/repair_cache.json 最新）; rw 通道移除; sq/kw 生产原样;
  Original 通道 Dense50 + BM25原句30 + BM25kw20; SubQ 每路 20/10（有 sq 时）;
  配额方案C: 修复句 30 + SubQ 预算 30/20; 池上限 60（n_sq≥2）/50;
  CE query = 修复句。
A 臂（生产 v2.9）: 复用 data/ce_query_quota_ab 存量回放（planC|rw, MRR 0.5932）;
  新脚本按 quota_orig=20 重放 A 与存量 top10 逐位比对（一致性自检）。

流程:
  Phase 0 读修复句 + 生产候选记录（复用 sq/kw）
  Phase 1 C 候选采集（force_multi + orig_kw_k, 全字段, 增量缓存 collections_c.json）
  Phase 2 CE 原始分（修复句文本键, 复用存量键, 增量缓存 ce_scores.json）
  Phase 3 A 一致性自检 + C 回放 top10 → top10/{qid}.json
  Phase 4 金标指标（MRR/R@5/R@10/sec/零召回/逐题 diff/面板）
  Phase 5 answerability judge（种子=存量 judge_cache, 签名去重）

用法:
  python3 eval_repair_stage2.py --limit 5 --answerability off   # 冒烟
  python3 eval_repair_stage2.py                                  # 全量（断点续跑）
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import eval_gate_ab as ab
import repair_query as rp
from hybrid_search import HybridSearcher
from llm_client import call_llm

OUT_DIR = "data/repair_stage2"
CAND_DIR = os.path.join(OUT_DIR, "candidates")
TOP10_DIR = os.path.join(OUT_DIR, "top10")
CE_SCORES = os.path.join(OUT_DIR, "ce_scores.json")
JUDGE_CACHE = os.path.join(OUT_DIR, "judge_cache.json")
COLL_C = os.path.join(OUT_DIR, "collections_c.json")
REPORT = os.path.join(OUT_DIR, "report.json")

PROD_DIR = "data/ce_query_quota_ab"
PROD_CAND_DIR = os.path.join(PROD_DIR, "candidates")
PROD_TOP10_DIR = os.path.join(PROD_DIR, "top10")
PROD_CE_SCORES = os.path.join(PROD_DIR, "ce_scores.json")
PROD_JUDGE_CACHE = os.path.join(PROD_DIR, "judge_cache.json")

ALPHA = 0.3
LAMBDA_LEN = 0.1
TOPK = 10

# C 臂参数
C_DENSE_K = 50
C_BM25_K = 30
C_KW_K = 20
C_SUBQ_DENSE_K = 20
C_SUBQ_BM25_K = 10
C_QUOTA_ORIG = 30
A_QUOTA_ORIG = 20

SPOTLIGHT = ["Q_S07", "Q_S15", "Q_S23", "Q_SR03", "Q_SR06",  # rw-CE 救5
             "Q_S13", "Q_D09", "Q_D12"]                       # rw-CE 丢3


def _atomic_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ────────────────────────── 配额选择（2×2 复刻 + quota_orig 参数化） ──────────────────────────

def quota_select(cand, n_rw, n_sq, quota_orig):
    """方案C: 主 query 配额 quota_orig, 每 SubQ 最低 5, SubQ 预算 30/20,
    池上限 60（n_sq≥2）/50。cand 按 retrieval_prior 降序。"""
    max_pool = 60 if n_sq >= 2 else 50
    if len(cand) <= max_pool:
        return [c["chunk_id"] for c in cand], False
    rw_qids = set(range(1, 1 + n_rw))
    sq_start = 1 + n_rw
    quota_rw = 10 if n_rw > 0 else 0
    sq_quotas = {}
    if n_sq > 0:
        sub_budget = 30 if n_sq >= 2 else 20
        min_q = 5 if 5 * n_sq <= sub_budget else sub_budget // n_sq
        sq_quotas = {sq_start + i: min_q for i in range(n_sq)}

    hits = [set(c["query_hits"]) for c in cand]
    reserved, rest, taken, rw_taken = [], [], {}, 0
    for c, h in zip(cand, hits):
        placed = False
        if 0 in h and taken.get(0, 0) < quota_orig:
            reserved.append(c)
            taken[0] = taken.get(0, 0) + 1
            placed = True
        elif not placed:
            for sq_qid, sq_quota in sq_quotas.items():
                if sq_qid in h and taken.get(sq_qid, 0) < sq_quota:
                    reserved.append(c)
                    taken[sq_qid] = taken.get(sq_qid, 0) + 1
                    placed = True
                    break
        if not placed and rw_qids and rw_taken < quota_rw and (rw_qids & h):
            reserved.append(c)
            rw_taken += 1
            placed = True
        if not placed:
            rest.append(c)

    if n_sq > 0:
        sq_need = sub_budget - sum(taken.get(q, 0) for q in sq_quotas)
        rest_ids = {id(c) for c in rest}
        new_rest = []
        for c, h in zip(cand, hits):
            if id(c) not in rest_ids:
                continue
            if sq_need > 0 and any(q in h for q in sq_quotas):
                reserved.append(c)
                sq_need -= 1
            else:
                new_rest.append(c)
        rest = new_rest

    pool = reserved + rest
    return [c["chunk_id"] for c in pool[:max_pool]], True


def replay_top10(cand_by_id, pool_ids, ce_query, scores):
    """复刻 _rrf_ce_fusion: final = 0.3×prior + 0.7×minmax(CE−0.1·log(len))。"""
    ce_scores = []
    for cid in pool_ids:
        raw = scores[ce_query][cid]
        ce_scores.append(raw - LAMBDA_LEN * math.log(cand_by_id[cid]["text_len"]))
    ce_min, ce_max = min(ce_scores), max(ce_scores)
    ce_range = ce_max - ce_min or 1e-8
    scored = []
    for i, cid in enumerate(pool_ids):
        ce_norm = (ce_scores[i] - ce_min) / ce_range
        final = ALPHA * cand_by_id[cid]["retrieval_prior"] + (1 - ALPHA) * ce_norm
        scored.append((final, cid))
    scored.sort(key=lambda x: -x[0])
    return [(cid, round(final, 4)) for final, cid in scored[:TOPK]]


# ────────────────────────── Phase 0/1: 候选采集 ──────────────────────────

def load_prod_records():
    records = {}
    for q in ab.gs:
        with open(os.path.join(PROD_CAND_DIR, q["id"] + ".json"),
                  encoding="utf-8") as f:
            records[q["id"]] = json.load(f)
    return records


def collect_c(srch, q, repair, sq, kw, cache):
    qid = q["id"]
    key = f'{qid}|{repair}|{"|".join(sq)}'
    if qid in cache and cache[qid].get("_key") == key:
        return cache[qid]
    _, cand_list, _, _ = srch._collect_candidates(
        repair, TOPK, False, [], sq, kw, None, LAMBDA_LEN,
        C_DENSE_K, C_BM25_K, C_SUBQ_DENSE_K, C_SUBQ_BM25_K,
        orig_kw_k=C_KW_K, force_multi=True)
    if cand_list is None:
        sys.exit(f"[phase1] {qid} force_multi 未生效, cand=None")
    cand = [{
        "chunk_id": c["chunk_id"],
        "retrieval_prior": c["retrieval_prior"],
        "query_hits": sorted(c["query_hits"]),
        "rrf_prior": c["rrf_prior"],
        "evidence_score": c.get("evidence_score", 0.0),
        "best_channel": c["best_channel"],
        "best_rank": c["best_rank"],
        "sources": sorted(c["sources"]),
        "cosine_sim": c["cosine_sim"],
        "text_len": len(c["text"]),
        "text": c["text"],
        "metadata": c["metadata"],
    } for c in cand_list]
    cache[qid] = {"_key": key, "repair": repair, "sq": sq, "kw": kw,
                  "cand": cand}
    return cache[qid]


def phase1(srch, qs, repairs, records):
    os.makedirs(OUT_DIR, exist_ok=True)
    cache = {}
    if os.path.exists(COLL_C):
        with open(COLL_C, encoding="utf-8") as f:
            cache = json.load(f)
    t0 = time.time()
    for i, q in enumerate(qs, 1):
        rec = records[q["id"]]
        rr = repairs[q["question"]]
        repair = rr.get("repair_query") or rr.get("v2_query")
        collect_c(srch, q, repair, rec["sq"], rec["kw"], cache)
        coll = cache[q["id"]]
        pool = coll["cand"]
        print(f"[phase1] {i}/{len(qs)} {q['id']} cand={len(pool)} "
              f"(eta {(time.time() - t0) / i * len(qs) - (time.time() - t0):.0f}s)",
              flush=True)
        if i % 10 == 0:
            _atomic_json(COLL_C, cache)
    _atomic_json(COLL_C, cache)
    print(f"[phase1] C 候选采集完成 {len(qs)} 题 ({time.time() - t0:.0f}s)",
          flush=True)
    return cache


# ────────────────────────── Phase 2: CE 原始分 ──────────────────────────

def load_prod_ce_scores():
    with open(PROD_CE_SCORES, encoding="utf-8") as f:
        return json.load(f).get("scores", {})


def load_own_ce_scores():
    if os.path.exists(CE_SCORES):
        with open(CE_SCORES, encoding="utf-8") as f:
            return json.load(f).get("scores", {})
    return {}


def phase2(srch, records, colls):
    prod = load_prod_ce_scores()
    own = load_own_ce_scores()
    srch._reranker._load()
    t0 = time.time()
    n_pairs = 0
    for i, qid in enumerate(records, 1):
        coll = colls[qid]
        cand = coll["cand"]
        cand_by_id = {c["chunk_id"]: c for c in cand}
        pool_ids, _ = quota_select(cand, 0, len(coll["sq"]), C_QUOTA_ORIG)
        qt = coll["repair"]
        missing = [cid for cid in pool_ids
                   if (qt not in prod or cid not in prod[qt])
                   and (qt not in own or cid not in own[qt])]
        if not missing:
            continue
        pairs = [(qt, cand_by_id[cid]["text"][:500]) for cid in missing]
        with srch._reranker._infer_lock:
            raw = [float(x) for x in srch._reranker._model.predict(
                pairs, show_progress_bar=False)]
        own.setdefault(qt, {})
        for cid, v in zip(missing, raw):
            own[qt][cid] = v
        n_pairs += len(missing)
        _atomic_json(CE_SCORES, {"version": 1, "scores": own})
        if i % 10 == 0 or i == len(records):
            print(f"[phase2] {i}/{len(records)} 题 | 新 CE pairs {n_pairs} "
                  f"({time.time() - t0:.0f}s, "
                  f"{n_pairs / max(1, time.time() - t0):.1f} 对/s)", flush=True)
    print(f"[phase2] CE 原始分完成: 新算 {n_pairs} pairs → {CE_SCORES}", flush=True)
    return prod, own


def get_ce(own, prod, qt, cid):
    if qt in own and cid in own[qt]:
        return own[qt][cid]
    return prod[qt][cid]


# ────────────────────────── Phase 3: 回放 ──────────────────────────

def phase3(records, colls, prod_scores, own_scores):
    os.makedirs(TOP10_DIR, exist_ok=True)
    mismatches = []
    for qid in records:
        rec = records[qid]
        coll = colls[qid]
        # ── A 一致性自检: 重放生产 planC|rw vs 存量 top10 逐位比对 ──
        if not rec["plain"]:
            cand = rec["cand"]
            cand_by_id = {c["chunk_id"]: c for c in cand}
            pool_ids, _ = quota_select(cand, rec["n_rw"], rec["n_sq"],
                                       A_QUOTA_ORIG)
            a_ce = rec["rw"][0] if rec["n_rw"] else rec["question"]
            merged = {a_ce: {cid: prod_scores[a_ce][cid] for cid in pool_ids}}
            top = replay_top10(cand_by_id, pool_ids, a_ce, merged)
            replay_ids = [cid for cid, _ in top]
            with open(os.path.join(PROD_TOP10_DIR, qid + ".json"),
                      encoding="utf-8") as f:
                stored = json.load(f)["variants"]["planC|rw"]["ids"]
            if replay_ids != stored:
                mismatches.append({"qid": qid, "stored": stored,
                                   "replay": replay_ids})
        # ── C 臂回放 ──
        cand = coll["cand"]
        cand_by_id = {c["chunk_id"]: c for c in cand}
        n_sq = len(coll["sq"])
        pool_ids, active = quota_select(cand, 0, n_sq, C_QUOTA_ORIG)
        qt = coll["repair"]
        merged = {qt: {cid: get_ce(own_scores, prod_scores, qt, cid)
                       for cid in pool_ids}}
        top = replay_top10(cand_by_id, pool_ids, qt, merged)
        c_ids = [cid for cid, _ in top]
        c_texts = [cand_by_id[cid]["text"][:600] for cid in c_ids]
        with open(os.path.join(PROD_TOP10_DIR, qid + ".json"),
                  encoding="utf-8") as f:
            stored = json.load(f)
        a_v = stored["variants"]["planC|rw"]
        _atomic_json(os.path.join(TOP10_DIR, qid + ".json"), {
            "qid": qid, "question": rec["question"],
            "answer": rec.get("answer", ""),
            "A": {"ids": a_v["ids"], "texts": a_v["texts"],
                  "sig": hashlib.sha1("|".join(a_v["ids"]).encode()).hexdigest()[:12]},
            "C": {"ids": c_ids, "texts": c_texts,
                  "sig": hashlib.sha1("|".join(c_ids).encode()).hexdigest()[:12],
                  "pool": len(pool_ids), "quota_active": active},
        })
    print(f"[phase3] A 一致性自检: {len(mismatches)} 不一致 / {len(records)} 题",
          flush=True)
    for m in mismatches[:10]:
        print(f"  MISMATCH {m['qid']}\n    stored : {m['stored']}\n"
              f"    replay : {m['replay']}", flush=True)
    return mismatches


# ────────────────────────── Phase 4: 金标指标 ──────────────────────────

def gold_metrics(q, top_ids):
    gold, gold_sections = ab.gold_matches(q)
    rr = 0.0
    for i, cid in enumerate(top_ids):
        if cid in gold:
            rr = 1.0 / (i + 1)
            break
    sec_rr = 0.0
    cid_to_sid = ab.cid_to_sid
    for i, cid in enumerate(top_ids):
        if cid in cid_to_sid and cid_to_sid[cid] in gold_sections:
            sec_rr = 1.0 / (i + 1)
            break
    return {"rr": rr,
            "recall_5": any(cid in gold for cid in top_ids[:5]),
            "recall_10": any(cid in gold for cid in top_ids[:10]),
            "hit_count": sum(1 for cid in top_ids[:10] if cid in gold),
            "gold_count": len(gold),
            "sec_rr": sec_rr,
            "sec_recall_10": any(cid in cid_to_sid and cid_to_sid[cid] in gold_sections
                                 for cid in top_ids[:10])}


def agg(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0}
    return {"n": n, "mrr": round(sum(r["rr"] for r in rows) / n, 4),
            "recall_5": round(sum(r["recall_5"] for r in rows) / n, 4),
            "recall_10": round(sum(r["recall_10"] for r in rows) / n, 4),
            "hit_count": sum(r["hit_count"] for r in rows),
            "zero_recall": sum(1 for r in rows if r["rr"] == 0),
            "sec_mrr": round(sum(r["sec_rr"] for r in rows) / n, 4),
            "sec_recall_10": round(sum(r["sec_recall_10"] for r in rows) / n, 4)}


def pair_diff(rows_a, rows_b, key_a, key_b):
    by = {r["qid"]: r for r in rows_b}
    rescued, lost, improved, worsened = [], [], [], []
    for ra in rows_a:
        rb = by[ra["qid"]]
        if ra["rr"] == 0 and rb["rr"] > 0:
            rescued.append({"id": ra["qid"], f"{key_b}_rr": round(rb["rr"], 4)})
        elif ra["rr"] > 0 and rb["rr"] == 0:
            lost.append({"id": ra["qid"], f"{key_a}_rr": round(ra["rr"], 4)})
        elif ra["rr"] > 0 and rb["rr"] > ra["rr"]:
            improved.append({"id": ra["qid"], f"{key_a}_rr": round(ra["rr"], 4),
                             f"{key_b}_rr": round(rb["rr"], 4)})
        elif rb["rr"] > 0 and rb["rr"] < ra["rr"]:
            worsened.append({"id": ra["qid"], f"{key_a}_rr": round(ra["rr"], 4),
                             f"{key_b}_rr": round(rb["rr"], 4)})
    return {"rescued": rescued, "lost": lost, "improved": improved,
            "worsened": worsened}


def phase4(records, colls):
    q_by_id = {q["id"]: q for q in ab.gs}
    rows_a, rows_c, per = [], [], {}
    for qid in records:
        with open(os.path.join(TOP10_DIR, qid + ".json"), encoding="utf-8") as f:
            t = json.load(f)
        q = q_by_id[qid]
        ma = gold_metrics(q, t["A"]["ids"])
        mc = gold_metrics(q, t["C"]["ids"])
        coll = colls[qid]
        per[qid] = {"qid": qid, "A": ma, "C": mc,
                    "n_sq": len(coll["sq"]),
                    "repair_changed": coll["repair"] != records[qid]["question"],
                    "pool": t["C"].get("pool"),
                    "prod_plain": records[qid]["plain"]}
        rows_a.append({"qid": qid, **ma})
        rows_c.append({"qid": qid, **mc})

    def panel(ids):
        ra = [r for r in rows_a if r["qid"] in ids]
        rc = [r for r in rows_c if r["qid"] in ids]
        return {"n": len(ra), "A": agg(ra), "C": agg(rc),
                "diff": pair_diff(ra, rc, "A", "C")}

    panels = {
        "sq>0": panel({qid for qid, r in per.items() if r["n_sq"] > 0}),
        "no_sq": panel({qid for qid, r in per.items() if r["n_sq"] == 0}),
        "no_sq_prod_rw": panel({qid for qid, r in per.items()
                                if r["n_sq"] == 0 and not r["prod_plain"]}),
        "no_sq_prod_plain": panel({qid for qid, r in per.items()
                                   if r["n_sq"] == 0 and r["prod_plain"]}),
        "repair_changed": panel({qid for qid, r in per.items()
                                 if r["repair_changed"]}),
        "repair_unchanged": panel({qid for qid, r in per.items()
                                   if not r["repair_changed"]}),
    }
    diff = pair_diff(rows_a, rows_c, "A", "C")
    zero_a = [r["qid"] for r in rows_a if r["rr"] == 0]
    zero_c = [r["qid"] for r in rows_c if r["rr"] == 0]
    return agg(rows_a), agg(rows_c), diff, panels, per, zero_a, zero_c


# ────────────────────────── Phase 5: answerability judge ──────────────────────────

JUDGE_PROMPT = """你是农业气候区划领域的检索质量评估员。判断仅凭给定的检索结果能否回答下面的问题。

问题：{question}
参考答案：{answer}

检索结果（top-10，按检索排名）：
{passages}

判断标准：
- full: 检索结果包含回答问题所需的核心信息，足以得出参考答案的要点
- partial: 只包含部分核心信息，缺关键要素（如缺某地区/某指标/某时段的数据）
- no: 基本不含回答问题所需的信息

只输出 JSON：{{"level": "full"|"partial"|"no", "reason": "一句话理由"}}"""


def judge_one(qid, question, answer, texts, sig):
    passages = "\n".join(f"[{i + 1}] {t}" for i, t in enumerate(texts))
    prompt = JUDGE_PROMPT.format(question=question, answer=answer, passages=passages)
    for attempt in range(2):
        try:
            resp = call_llm([{"role": "user", "content": prompt}],
                            temperature=0, stream=False, json_mode=True)
            parsed = json.loads(resp[resp.find("{"):resp.rfind("}") + 1])
            level = parsed.get("level", "no")
            if level not in ("full", "partial", "no"):
                level = "no"
            return qid, sig, {"level": level, "reason": parsed.get("reason", "")}
        except Exception as e:
            if attempt == 0:
                continue
            return qid, sig, {"level": "no", "reason": f"parse_error: {e}"}


def phase5(records, workers=6):
    cache = {}
    if os.path.exists(PROD_JUDGE_CACHE):
        with open(PROD_JUDGE_CACHE, encoding="utf-8") as f:
            cache.update(json.load(f))
    if os.path.exists(JUDGE_CACHE):
        with open(JUDGE_CACHE, encoding="utf-8") as f:
            cache.update(json.load(f))
    tasks = []
    for qid in records:
        with open(os.path.join(TOP10_DIR, qid + ".json"), encoding="utf-8") as f:
            t = json.load(f)
        for arm in ("A", "C"):
            sig = t[arm]["sig"]
            if f"{qid}|{sig}" not in cache:
                tasks.append((qid, sig, t["question"], t.get("answer", ""),
                              t[arm]["texts"]))
    n_new = len(tasks)
    if n_new:
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(judge_one, qid, question, answer, texts, sig)
                       : (qid, sig) for qid, sig, question, answer, texts in tasks}
            done = 0
            for f in as_completed(futures):
                qid, sig, res = f.result()
                cache[f"{qid}|{sig}"] = res
                done += 1
                if done % 50 == 0 or done == n_new:
                    _atomic_json(JUDGE_CACHE, cache)
                    print(f"[phase5] judge {done}/{n_new} "
                          f"({time.time() - t0:.0f}s)", flush=True)
        _atomic_json(JUDGE_CACHE, cache)
    print(f"[phase5] answerability judge: 新判 {n_new}, "
          f"缓存复用 {len(cache) - n_new}", flush=True)
    return cache


def answerability_stats(records, cache):
    stats = {}
    for arm in ("A", "C"):
        full = partial = no = 0
        for qid in records:
            with open(os.path.join(TOP10_DIR, qid + ".json"), encoding="utf-8") as f:
                t = json.load(f)
            sig = t[arm]["sig"]
            level = cache.get(f"{qid}|{sig}", {}).get("level", "no")
            if level == "full":
                full += 1
            elif level == "partial":
                partial += 1
            else:
                no += 1
        n = full + partial + no
        stats[arm] = {"n": n, "full": full, "partial": partial, "no": no,
                      "pct_full": round(full / n, 4) if n else 0,
                      "pct_answerable": round((full + partial) / n, 4) if n else 0}
    return stats


# ────────────────────────── main ──────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（冒烟）")
    ap.add_argument("--answerability", choices=["on", "off"], default="on",
                    help="answerability judge 开关（默认 on）")
    ap.add_argument("--workers", type=int, default=6, help="judge 并发数")
    ap.add_argument("--cache", default=rp.CACHE_FILE, help="修复句缓存文件（v1/v2）")
    ap.add_argument("--out-dir", default="data/repair_stage2", help="输出目录")
    ap.add_argument("--label", default="", help="C 臂 query 标签（写入 report config）")
    args = ap.parse_args()

    global OUT_DIR, CAND_DIR, TOP10_DIR, CE_SCORES, JUDGE_CACHE, COLL_C, REPORT
    OUT_DIR = args.out_dir
    CAND_DIR = os.path.join(OUT_DIR, "candidates")
    TOP10_DIR = os.path.join(OUT_DIR, "top10")
    CE_SCORES = os.path.join(OUT_DIR, "ce_scores.json")
    JUDGE_CACHE = os.path.join(OUT_DIR, "judge_cache.json")
    COLL_C = os.path.join(OUT_DIR, "collections_c.json")
    REPORT = os.path.join(OUT_DIR, "report.json")

    qs = ab.gs[:args.limit] if args.limit else ab.gs
    print(f"[start] {len(qs)} 题 | answerability={args.answerability} "
          f"| cache={args.cache} | out={OUT_DIR}", flush=True)

    repairs = json.load(open(args.cache, encoding="utf-8"))
    for q in qs:
        if q["question"] not in repairs:
            sys.exit(f"缺修复记录: {q['id']}")

    records = load_prod_records()
    records = {qid: r for qid, r in records.items()
               if qid in {q["id"] for q in qs}}

    srch = HybridSearcher(enable_reranker=True)
    colls = phase1(srch, qs, repairs, records)
    prod_scores, own_scores = phase2(srch, records, colls)
    mismatches = phase3(records, colls, prod_scores, own_scores)
    agg_a, agg_c, diff, panels, per, zero_a, zero_c = phase4(records, colls)

    report = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {
            "alpha": ALPHA, "lambda_length": LAMBDA_LEN, "top_k": TOPK,
            "c_arm": {
                "query": args.label or "修复句(repair)", "rw": "移除", "sq/kw": "生产原样",
                "force_multi": True,
                "channels": {"dense_k": C_DENSE_K, "bm25_k": C_BM25_K,
                             "kw_k": C_KW_K,
                             "subq_dense_k": C_SUBQ_DENSE_K,
                             "subq_bm25_k": C_SUBQ_BM25_K},
                "quota": {"orig": C_QUOTA_ORIG, "sub_budget": "30/20",
                          "cap": "60(n_sq>=2)/50"},
                "ce_query": "修复句"},
            "a_arm": "生产 v2.9 存量回放 planC|rw",
            "n": len(qs),
        },
        "a_consistency": {"n_mismatch": len(mismatches), "mismatches": mismatches},
        "aggregates": {"A": agg_a, "C": agg_c},
        "panels": panels,
        "diffs": {"A_vs_C": diff},
        "zero_recall": {"A": zero_a, "C": zero_c},
        "per_question": per,
    }
    if args.answerability == "on":
        cache = phase5(records, workers=args.workers)
        report["answerability"] = answerability_stats(records, cache)
    _atomic_json(REPORT, report)

    # ── 控制台摘要 ──
    print("\n" + "=" * 88)
    print("Stage 2 汇总（合体 C 臂 vs 生产 A）")
    print("=" * 88)
    print(f"{'臂':<4}{'n':>5}{'MRR':>8}{'R@5':>8}{'R@10':>8}{'secMRR':>8}"
          f"{'零召回':>7}{'可回答%':>8}")
    for name in ("A", "C"):
        a = report["aggregates"][name]
        ans = report.get("answerability", {}).get(name, {}).get("pct_answerable", "-")
        print(f"{name:<4}{a['n']:>5}{a['mrr']:>8}{a['recall_5']:>8.4f}"
              f"{a['recall_10']:>8.4f}{a['sec_mrr']:>8}{a['zero_recall']:>7}"
              f"{ans if isinstance(ans, str) else f'{ans:.4f}':>8}")
    d = report["diffs"]["A_vs_C"]
    print(f"\n[A_vs_C] 救 {len(d['rescued'])} | 丢 {len(d['lost'])} | "
          f"升 {len(d['improved'])} | 降 {len(d['worsened'])}")
    for tag in ("rescued", "lost"):
        for r in d[tag]:
            print(f"  {tag} {r['id']} {r[list(r)[1]]}")
    print("\n[面板]")
    for pname, p in report["panels"].items():
        print(f"  {pname:<18} n={p['n']:<4} A_MRR={p['A'].get('mrr', '-'):<8} "
              f"C_MRR={p['C'].get('mrr', '-'):<8} "
              f"救{len(p['diff']['rescued'])} 丢{len(p['diff']['lost'])}")
    print(f"\n[零召回] A({len(zero_a)}): {zero_a}")
    print(f"          C({len(zero_c)}): {zero_c}")
    print("\n[专项检查（rw-CE 救5 丢3）]")
    for qid in SPOTLIGHT:
        if qid in per:
            pq = per[qid]
            fa = "✓" if pq["A"]["rr"] > 0 else "✗"
            fc = "✓" if pq["C"]["rr"] > 0 else "✗"
            print(f"  {qid:<8} A:rr={pq['A']['rr']:<6} C:rr={pq['C']['rr']:<6} "
                  f"(A{fa} C{fc}) pool={pq['pool']}")
    print(f"\n→ {REPORT}", flush=True)


if __name__ == "__main__":
    main()
