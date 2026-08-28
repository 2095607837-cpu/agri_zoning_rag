#!/usr/bin/env python3
"""CE query 消融 × 配额方案 A/B 联合实验（2×2 因子 + answerability 指标）。

因子 1 CE query: orig = CE 精排用原 query（现行生产）; rw = CE 精排用 rw[0]（改写句）
因子 2 quota:    even = SubQ 均分 20（现行生产, max_pool=50）;
                 planC = 每 SubQ 至少 5 + 剩余按 retrieval_prior 全局分配,
                         SubQ 总预算 30; 多个 SubQuery(≥2) 池上限扩到 60,
                         其余（0/1 SubQ）保持 50（此时与 even 同池）

流程:
  Phase 0 回放 G1 改写池（rewrite_cache.json query|g1, 0 LLM）
  Phase 1 候选采集（无 CE, 确定性）→ data/ce_query_quota_ab/candidates/{qid}.json
  Phase 2 CE 原始分 → data/ce_query_quota_ab/ce_scores.json（(query,chunk) 键, 断点续跑）
  Phase 3 离线回放 4 变体（复刻 _rrf_ce_fusion: final=0.3×prior+0.7×minmax(CE−0.1·log(len))）;
          kw-only 题走生产 plain 路径一次, 4 变体同值 → top10/{qid}.json
  Phase 4 金标指标（MRR/R@5/R@10/sec）+ 面板差异（rw 面板 / quota-active 面板）
  Phase 5 answerability judge（--answerability on|off, 默认 on; 3 级 full/partial/no,
          按 top10 签名去重, 缓存 judge_cache.json）

用法:
  python3 eval_ce_query_quota_ab.py --limit 5 --smoke   # 冒烟: 前5题 + 回放一致性校验
  python3 eval_ce_query_quota_ab.py                     # 全量（断点续跑）

回放一致性: --smoke 时用生产 search_multi_query 对比 (even, orig) 变体 top10,
必须逐位一致（离线回放 = 生产管线）。
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

import query_rewriter as qr
import eval_gate_ab as ab
from llm_client import call_llm
from hybrid_search import HybridSearcher

OUT_DIR = "data/ce_query_quota_ab"
CAND_DIR = os.path.join(OUT_DIR, "candidates")
TOP10_DIR = os.path.join(OUT_DIR, "top10")
CE_SCORES = os.path.join(OUT_DIR, "ce_scores.json")
JUDGE_CACHE = os.path.join(OUT_DIR, "judge_cache.json")
REPORT = os.path.join(OUT_DIR, "report.json")

ALPHA = 0.3
LAMBDA_LEN = 0.1
TOPK = 10
VARIANTS = [("even", "orig"), ("even", "rw"), ("planC", "orig"), ("planC", "rw")]

qr._AUTO_SAVE = True   # Phase 0 若出现 cache miss, 新条目落盘

# ────────────────────────── Phase 0: G1 改写池回放 ──────────────────────────

def phase0(qs):
    pools = {}
    n_llm = 0
    for q in qs:
        t = q["question"]
        qr.expand_query(t, mode="all")   # cache hit → 注册 registry, 0 LLM
        info = qr.get_gate_info(t)
        if info.get("called_llm"):
            n_llm += 1
        pools[q["id"]] = {
            "rw": qr.get_rewrite_queries(t),
            "sq": qr.get_sub_queries(t),
            "kw": qr.get_keywords(t),
        }
    print(f"[phase0] G1 池回放 {len(pools)} 题, LLM 实际调用 {n_llm} 次 (应≈0)", flush=True)
    return pools


# ────────────────────────── Phase 1: 候选采集 ──────────────────────────

def collect_one(q, p):
    query = q["question"]
    _, cand_list, _, _ = ab.searcher0._collect_candidates(
        query, TOPK, False, p["rw"], p["sq"], p["kw"], None, LAMBDA_LEN, 30, 20, 20, 10)
    rec = {
        "qid": q["id"], "question": query, "answer": q.get("answer", ""),
        "rw": p["rw"], "sq": p["sq"], "kw": p["kw"],
        "n_rw": len(p["rw"]), "n_sq": len(p["sq"]),
        "plain": cand_list is None,
        "cand": None if cand_list is None else [{
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
        } for c in cand_list],
    }
    path = os.path.join(CAND_DIR, q["id"] + ".json")
    _atomic_json(path, rec)
    return rec


def phase1(qs, pools):
    os.makedirs(CAND_DIR, exist_ok=True)
    records = {}
    n_new = 0
    for q in qs:
        path = os.path.join(CAND_DIR, q["id"] + ".json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                records[q["id"]] = json.load(f)
        else:
            records[q["id"]] = collect_one(q, pools[q["id"]])
            n_new += 1
    n_plain = sum(r["plain"] for r in records.values())
    print(f"[phase1] 候选采集: 新算 {n_new}, 回放 {len(qs) - n_new} | "
          f"plain {n_plain} | multi {len(qs) - n_plain}", flush=True)
    return records


# ────────────────────────── 配额选择（复刻生产 + 方案C） ──────────────────────────

def quota_select(cand, n_rw, n_sq, scheme):
    """返回 (pool_chunk_ids, quota_active)。cand 按 retrieval_prior 降序。"""
    max_pool = 50 if scheme == "even" else (60 if n_sq >= 2 else 50)
    if len(cand) <= max_pool:
        return [c["chunk_id"] for c in cand], False

    rw_qids = set(range(1, 1 + n_rw))
    sq_start = 1 + n_rw
    quota_orig = 20
    quota_rw = 10 if n_rw > 0 else 0
    sub_budget = 0
    sq_quotas = {}
    if n_sq > 0:
        sub_budget = 20 if scheme == "even" else (30 if n_sq >= 2 else 20)
        if scheme == "even":
            base, rem = divmod(sub_budget, n_sq)
            sq_quotas = {sq_start + i: base + (1 if i < rem else 0) for i in range(n_sq)}
        else:
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

    if scheme == "planC" and n_sq > 0:
        # 剩余额度（含未填满的最低保护）按 retrieval_prior 全局分配
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


# ────────────────────────── Phase 2: CE 原始分缓存 ──────────────────────────

def load_ce_scores():
    if os.path.exists(CE_SCORES):
        with open(CE_SCORES, encoding="utf-8") as f:
            return json.load(f).get("scores", {})
    return {}


def save_ce_scores(scores):
    _atomic_json(CE_SCORES, {"version": 1, "scores": scores})


def phase2(srch, records):
    """multi-query 题: 4 变体池并集 U × {orig, rw[0]} 的 CE 原始分, 增量缓存。"""
    scores = load_ce_scores()
    srch._reranker._load()
    multi = [r for r in records.values() if not r["plain"]]
    t0 = time.time()
    n_pairs = 0
    for i, r in enumerate(multi, 1):
        cand = r["cand"]
        cand_by_id = {c["chunk_id"]: c for c in cand}
        u_ids, seen = [], set()
        for scheme in ("even", "planC"):
            ids, _ = quota_select(cand, r["n_rw"], r["n_sq"], scheme)
            for cid in ids:
                if cid not in seen:
                    u_ids.append(cid)
                    seen.add(cid)
        qtexts = [r["question"]]
        if r["n_rw"] > 0 and r["rw"][0] != r["question"]:
            qtexts.append(r["rw"][0])
        for qt in qtexts:
            missing = [cid for cid in u_ids
                       if qt not in scores or cid not in scores[qt]]
            if not missing:
                continue
            pairs = [(qt, cand_by_id[cid]["text"][:500]) for cid in missing]
            with srch._reranker._infer_lock:
                raw = [float(x) for x in srch._reranker._model.predict(
                    pairs, show_progress_bar=False)]
            scores.setdefault(qt, {})
            for cid, v in zip(missing, raw):
                scores[qt][cid] = v
            n_pairs += len(missing)
        save_ce_scores(scores)
        if i % 20 == 0 or i == len(multi):
            print(f"[phase2] {i}/{len(multi)} 题 | 累计新 CE pairs {n_pairs} "
                  f"({time.time() - t0:.0f}s)", flush=True)
    print(f"[phase2] CE 原始分完成: 新算 {n_pairs} pairs → {CE_SCORES}", flush=True)
    return scores


# ────────────────────────── Phase 3: 离线回放 4 变体 ──────────────────────────

def replay_top10(cand_by_id, pool_ids, ce_query, scores):
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


def phase3(srch, records, scores, smoke=False, qs=None):
    os.makedirs(TOP10_DIR, exist_ok=True)
    qs = qs or list(records)
    smoke_report = {}
    for r_id in records:
        r = records[r_id]
        variants = {}
        if r["plain"]:
            # kw-only: 生产 plain 路径（merged = search(keywords=...)）, 4 变体同值
            res = srch.search(r["question"], top_k=TOPK, expand_context=False,
                              alpha=ALPHA, lambda_length=LAMBDA_LEN,
                              keywords=r["kw"] or None)
            ids = [x["metadata"].get("chunk_id", "") for x in res[:TOPK]]
            texts = [x["content"][:600] for x in res[:TOPK]]
            for v in VARIANTS:
                variants["|".join(v)] = {"ids": ids, "texts": texts}
        else:
            cand = r["cand"]
            cand_by_id = {c["chunk_id"]: c for c in cand}
            for scheme, ceq in VARIANTS:
                pool_ids, active = quota_select(cand, r["n_rw"], r["n_sq"], scheme)
                ce_query = r["question"] if ceq == "orig" else (r["rw"][0] if r["n_rw"] else r["question"])
                top = replay_top10(cand_by_id, pool_ids, ce_query, scores)
                variants["|".join((scheme, ceq))] = {
                    "ids": [cid for cid, _ in top],
                    "texts": [cand_by_id[cid]["text"][:600] for cid, _ in top],
                    "pool": len(pool_ids), "quota_active": active,
                }
        path = os.path.join(TOP10_DIR, r_id + ".json")
        _atomic_json(path, {"qid": r_id, "question": r["question"],
                            "answer": r.get("answer", ""), "variants": variants})

        if smoke:
            # 生产 search_multi_query 对比 (even, orig)
            _, prod = srch.search_multi_query(
                r["question"], top_k=TOPK, expand_context=False,
                rewrite_queries=r["rw"], sub_queries=r["sq"], keyword_queries=r["kw"])
            prod_ids = [x["metadata"].get("chunk_id", "") for x in prod[:TOPK]]
            replay_ids = variants["even|orig"]["ids"]
            smoke_report[r_id] = {"match": prod_ids == replay_ids,
                                  "prod": prod_ids, "replay": replay_ids}
    print(f"[phase3] 4 变体回放完成 ({len(records)} 题)", flush=True)
    return smoke_report


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
    return {"rescued": rescued, "lost": lost, "improved": improved, "worsened": worsened}


def phase4(records):
    q_by_id = {q["id"]: q for q in ab.gs}
    per_variant = {v: [] for v in VARIANTS}
    per_q = {}
    for qid, r in records.items():
        path = os.path.join(TOP10_DIR, qid + ".json")
        with open(path, encoding="utf-8") as f:
            top10 = json.load(f)
        q = q_by_id[qid]
        row = {"qid": qid}
        for v in VARIANTS:
            key = "|".join(v)
            m = gold_metrics(q, top10["variants"][key]["ids"])
            row[key] = m
            per_variant[v].append({"qid": qid, **m})
        row["n_rw"] = r["n_rw"]
        row["n_sq"] = r["n_sq"]
        row["plain"] = r["plain"]
        row["pool"] = None if r["cand"] is None else len(r["cand"])
        per_q[qid] = row

    aggs = {v: agg(per_variant[v]) for v in VARIANTS}
    # 面板: CE query 消融（有 rw 的题）; quota 消融（有 sq 且 pool>50, 即 even 臂配额激活）
    rw_panel = [r for r in per_variant[("even", "orig")] if per_q[r["qid"]]["n_rw"] > 0]
    quota_panel = [r for r in per_variant[("even", "orig")] if
                   per_q[r["qid"]]["n_sq"] > 0
                   and (per_q[r["qid"]]["pool"] or 0) > 50]
    def rows_of(panel, v):
        ids = {r["qid"] for r in panel}
        return [r for r in per_variant[v] if r["qid"] in ids]

    panels = {
        "rw_panel_n": len(rw_panel),
        "quota_panel_n": len(quota_panel),
        "ce_query": {
        "even": pair_diff(rows_of(rw_panel, ("even", "orig")),
                          rows_of(rw_panel, ("even", "rw")), "orig", "rw"),
        "planC": pair_diff(rows_of(rw_panel, ("planC", "orig")),
                           rows_of(rw_panel, ("planC", "rw")), "orig", "rw"),
        "agg": {"even_orig": agg(rows_of(rw_panel, ("even", "orig"))),
                "even_rw": agg(rows_of(rw_panel, ("even", "rw"))),
                "planC_orig": agg(rows_of(rw_panel, ("planC", "orig"))),
                "planC_rw": agg(rows_of(rw_panel, ("planC", "rw")))},
    },
    }
    panels["quota"] = {
        "orig": pair_diff(rows_of(quota_panel, ("even", "orig")),
                          rows_of(quota_panel, ("planC", "orig")), "even", "planC"),
        "rw": pair_diff(rows_of(quota_panel, ("even", "rw")),
                        rows_of(quota_panel, ("planC", "rw")), "even", "planC"),
        "agg": {"even_orig": agg(rows_of(quota_panel, ("even", "orig"))),
                "planC_orig": agg(rows_of(quota_panel, ("planC", "orig"))),
                "even_rw": agg(rows_of(quota_panel, ("even", "rw"))),
                "planC_rw": agg(rows_of(quota_panel, ("planC", "rw")))},
    }
    return aggs, panels, per_q


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
    if os.path.exists(JUDGE_CACHE):
        with open(JUDGE_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    else:
        cache = {}
    tasks = []
    for qid in records:
        path = os.path.join(TOP10_DIR, qid + ".json")
        with open(path, encoding="utf-8") as f:
            top10 = json.load(f)
        seen = {}
        for key, v in top10["variants"].items():
            sig = hashlib.sha1("|".join(v["ids"]).encode()).hexdigest()[:12]
            full_key = f"{qid}|{sig}"
            v["judge_sig"] = sig
            if full_key not in cache and sig not in seen:
                seen[sig] = 1
                tasks.append((qid, sig, top10["question"],
                              top10.get("answer", ""), v["texts"]))
    # 回写新增 sig 到 top10 文件
    for qid in records:
        path = os.path.join(TOP10_DIR, qid + ".json")
        with open(path, encoding="utf-8") as f:
            top10 = json.load(f)
        changed = False
        for key, v in top10["variants"].items():
            if "judge_sig" not in v:
                v["judge_sig"] = hashlib.sha1("|".join(v["ids"]).encode()).hexdigest()[:12]
                changed = True
        if changed:
            _atomic_json(path, top10)

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
                    print(f"[phase5] judge {done}/{n_new} ({time.time() - t0:.0f}s)",
                          flush=True)
        _atomic_json(JUDGE_CACHE, cache)
    print(f"[phase5] answerability judge: 新判 {n_new}, 缓存复用 {len(cache) - n_new}",
          flush=True)
    return cache


def answerability_stats(records, cache):
    stats = {}
    for v in VARIANTS:
        key = "|".join(v)
        full = partial = no = 0
        for qid in records:
            path = os.path.join(TOP10_DIR, qid + ".json")
            with open(path, encoding="utf-8") as f:
                top10 = json.load(f)
            sig = top10["variants"][key].get("judge_sig", "")
            level = cache.get(f"{qid}|{sig}", {}).get("level", "no")
            if level == "full":
                full += 1
            elif level == "partial":
                partial += 1
            else:
                no += 1
        n = full + partial + no
        stats[key] = {"n": n, "full": full, "partial": partial,
                      "pct_full": round(full / n, 4) if n else 0,
                      "pct_answerable": round((full + partial) / n, 4) if n else 0}
    return stats


# ────────────────────────── 工具 & main ──────────────────────────

def _atomic_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（冒烟）")
    ap.add_argument("--smoke", action="store_true", help="回放 vs 生产一致性校验")
    ap.add_argument("--answerability", choices=["on", "off"], default="on",
                    help="answerability judge 开关（默认 on）")
    ap.add_argument("--workers", type=int, default=6, help="judge 并发数")
    args = ap.parse_args()

    qs = ab.gs[:args.limit] if args.limit else ab.gs
    print(f"[start] {len(qs)} 题 | smoke={args.smoke} | "
          f"answerability={args.answerability}", flush=True)

    pools = phase0(qs)
    records = phase1(qs, pools)
    srch = HybridSearcher(enable_reranker=True)
    scores = phase2(srch, records)
    smoke_report = phase3(srch, records, scores, smoke=args.smoke)
    if args.smoke:
        n_ok = sum(r["match"] for r in smoke_report.values())
        print(f"[smoke] 回放 vs 生产一致性: {n_ok}/{len(smoke_report)}", flush=True)
        for qid, r in smoke_report.items():
            print(f"  {qid} match={r['match']}", flush=True)
            if not r["match"]:
                print(f"    prod  : {r['prod']}", flush=True)
                print(f"    replay: {r['replay']}", flush=True)

    aggs, panels, per_q = phase4(records)

    report = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {"alpha": ALPHA, "lambda_length": LAMBDA_LEN, "top_k": TOPK,
                   "variants": VARIANTS,
                   "planC": "每SubQ至少5 + 剩余按retrieval_prior全局分配, SubQ预算30; n_sq>=2 池上限60, 其余50",
                   "n": len(qs)},
        "aggregates": {f"{s}|{c}": aggs[(s, c)] for s, c in VARIANTS},
        "panels": panels,
    }
    if args.answerability == "on":
        cache = phase5(records, workers=args.workers)
        report["answerability"] = answerability_stats(records, cache)
    report["per_question"] = per_q
    report["smoke"] = smoke_report if args.smoke else None
    _atomic_json(REPORT, report)

    print("\n===== 汇总 =====")
    print(f"{'variant':<14} {'MRR':>7} {'R@5':>7} {'R@10':>7} {'secMRR':>8} {'可回答%':>8}")
    for s, c in VARIANTS:
        a = aggs[(s, c)]
        ans = report.get("answerability", {}).get(f"{s}|{c}", {}).get("pct_answerable", "-")
        print(f"{s+'/'+c:<14} {a['mrr']:>7} {a['recall_5']:>7.4f} {a['recall_10']:>7.4f} "
              f"{a['sec_mrr']:>8} {ans if isinstance(ans, str) else f'{ans:.4f}':>8}")
    print(f"\n[CE query 面板 n={panels['rw_panel_n']}] orig vs rw: "
          f"救 {len(panels['ce_query']['even']['rescued'])} "
          f"丢 {len(panels['ce_query']['even']['lost'])} "
          f"升 {len(panels['ce_query']['even']['improved'])} "
          f"降 {len(panels['ce_query']['even']['worsened'])}")
    print(f"[quota 面板 n={panels['quota_panel_n']}] even vs planC: "
          f"救 {len(panels['quota']['orig']['rescued'])} "
          f"丢 {len(panels['quota']['orig']['lost'])} "
          f"升 {len(panels['quota']['orig']['improved'])} "
          f"降 {len(panels['quota']['orig']['worsened'])}")
    print(f"\n[done] → {REPORT}")


if __name__ == "__main__":
    main()
