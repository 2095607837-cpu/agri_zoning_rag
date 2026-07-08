#!/usr/bin/env python3
"""用 APO 评测 prompt 评估 V3 rewrite prompt 质量。

1. 从 query_rewriter.py 加载 V3 REWRITE_PROMPT，对 golden_rewrite.json 的 25 题生成改写
2. 用 apo_rewriter.py 的 EVAL_PROMPT_V3 进行 LLM-Judge 评测
3. 同时计算 GT 指标（对照 golden labels）
"""

import json
import sys
from pathlib import Path
from collections import defaultdict

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# ── 加载 V3 prompt 和改写函数 ──
from query_rewriter import _llm_rewrite, _cache as rewrite_cache

# ── 加载 golden_rewrite 测试集 ──
with open(BASE_DIR / "data" / "golden_rewrite_val.json", encoding="utf-8") as f:
    golden = json.load(f)

print(f"测试集: {len(golden)} 题 ({sum(1 for q in golden if q.get('rewrite_type')=='normalize')} normalize, {sum(1 for q in golden if q.get('rewrite_type')=='expand')} expand)")

# ── 清除缓存，确保用最新 V3 prompt 生成 ──
cached_keys = list(rewrite_cache.keys())
for k in cached_keys:
    if k in rewrite_cache:
        del rewrite_cache[k]

# ── Step 1: 用 V3 prompt 生成改写 ──
print("\n[Step 1] 用 V3 prompt 改写 25 题...")
rewrites = {}
for i, q in enumerate(golden):
    query = q["question"]
    result = _llm_rewrite(query)
    rewrites[q["id"]] = {
        "question": query,
        "rewrite_type": q.get("rewrite_type", "?"),
        "keywords": result.get("keywords", []),
        "sub_queries": result.get("sub_queries", []),
        "confidence": result.get("confidence", 0),
        "pred_rewrite_type": result.get("rewrite_type", "none"),
    }
    rwt = result.get("rewrite_type", "none")
    n_kw = len(result.get("keywords", []))
    n_sq = len(result.get("sub_queries", []))
    print(f"  [{i+1:2d}/25] {q['id']}: type={rwt:<9s} kw={n_kw} sq={n_sq} conf={result.get('confidence',0):.2f} | {query[:40]}")

# ── Step 2: GT 指标（对照 golden labels）──
print("\n[Step 2] GT 指标计算...")
from apo_rewriter import compute_gt_term_metrics, compute_gt_sq_similarity

gt_scores = defaultdict(list)
for q in golden:
    rw = rewrites[q["id"]]
    term_m = compute_gt_term_metrics(q.get("rewrite_eval", {}), rw["keywords"])
    sq_m = compute_gt_sq_similarity(q.get("reference_sub_queries", []), rw["sub_queries"])
    for k, v in {**term_m, **sq_m}.items():
        gt_scores[k].append(v)

print(f"  {'GT 指标':<22s} {'均值':>8s} {'中位':>8s}")
for key, name in [
    ("term_f1", "术语 F1"),
    ("term_precision", "术语 Precision"),
    ("term_recall", "术语 Recall"),
    ("term_weighted_recall", "术语加权 Recall"),
    ("sq_f1", "子查询 F1"),
    ("sq_precision", "子查询 Precision"),
    ("sq_recall", "子查询 Recall"),
]:
    vals = gt_scores.get(key, [])
    if vals:
        print(f"  {name:<22s} {np.mean(vals):>8.4f} {np.median(vals):>8.4f}")

# ── 分层（must_have/core/precision/important/optional）──
print(f"\n  ── 分层命中率 ──")
for level in ["must_have", "core_concept", "precision_term", "important_terms", "optional_terms"]:
    hit_k = f"term_hit_{level.split('_')[0]}" if level == "core_concept" else f"term_hit_{level.split('_')[0]}"
    count_k = f"term_{level.split('_')[0]}_count" if level == "core_concept" else f"term_{level.split('_')[0]}_count"
    # Map correctly: must_have→term_hit_must_have, core→term_hit_core, etc.
    level_map = {"must_have": "must_have", "core_concept": "core", "precision_term": "precision",
                 "important_terms": "important", "optional_terms": "optional"}
    prefix = level_map[level]
    hit_k = f"term_hit_{prefix}"
    count_k = f"term_{prefix}_count"
    hits = gt_scores.get(hit_k, [])
    counts = gt_scores.get(count_k, [])
    if hits and counts:
        total_hits = sum(hits)
        total_terms = sum(counts)
        rate = total_hits / total_terms if total_terms > 0 else 0
        print(f"  {level:<20s}: {total_hits}/{total_terms} = {rate:.2%}")

# ── Step 3: LLM-Judge 评测（用 APO EVAL_PROMPT_V2）──
print("\n[Step 3] LLM-Judge 评测（EVAL_PROMPT_V3）...")
from apo_rewriter import evaluate_rewrite, build_golden_answer, call_llm as apo_call_llm

llm_scores = defaultdict(list)
details = []
for i, q in enumerate(golden):
    rw = rewrites[q["id"]]
    golden_answer = build_golden_answer(q)
    rwt = q.get("rewrite_type", "normalize")
    scores = evaluate_rewrite(q["question"], golden_answer, rw["keywords"], rw["sub_queries"], rewrite_type=rwt)

    details.append({
        "id": q["id"],
        "question": q["question"],
        "keywords": rw["keywords"],
        "sub_queries": rw["sub_queries"],
        "scores": scores,
    })

    for k, v in scores.items():
        if isinstance(v, (int, float)):
            llm_scores[k].append(v)

    rq = scores.get("step4_综合评分", 0)
    print(f"  [{i+1:2d}/25] {q['id']}: RQ={rq:.1f} | {scores.get('主要问题', '')[:60]}")

# ── Step 4: 汇总 ──
print(f"\n{'='*60}")
print(f"  V3 Prompt 质量评估汇总")
print(f"{'='*60}")

print(f"\n  ── LLM-Judge 评分 ──")
for k in ["step2_覆盖度", "step2_精确度", "step3_角度多样性", "step3_语义保真度", "step4_综合评分"]:
    vals = llm_scores.get(k, [])
    if vals:
        print(f"  {k:<16s} mean={np.mean(vals):.2f} median={np.median(vals):.2f} min={min(vals):.2f} max={max(vals):.2f}")

# 分类统计
lex_scores = []
sem_scores = []
for d in details:
    rw_type = golden[[q["id"] for q in golden].index(d["id"])]["rewrite_type"]
    rq = d["scores"].get("step4_综合评分", 0)
    if rw_type == "normalize":
        lex_scores.append(rq)
    else:
        sem_scores.append(rq)

if lex_scores:
    print(f"\n  normalize (口语→术语): mean={np.mean(lex_scores):.2f} n={len(lex_scores)}")
if sem_scores:
    print(f"  expand (同义→标准):   mean={np.mean(sem_scores):.2f} n={len(sem_scores)}")

# 低分详情
low_scores = [d for d in details if d["scores"].get("step4_综合评分", 10) < 5]
if low_scores:
    print(f"\n  ── 低分题目 (RQ<5, {len(low_scores)} 题) ──")
    for d in low_scores:
        s = d["scores"]
        print(f"  {d['id']}: RQ={s.get('step4_综合评分',0):.1f}")
        print(f"    Q: {d['question'][:60]}")
        print(f"    kw={d['keywords']}  sq={d['sub_queries']}")
        print(f"    issue: {s.get('主要问题','')[:80]}")

print(f"\n  评估完成")
