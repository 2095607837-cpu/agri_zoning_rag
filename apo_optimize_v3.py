#!/usr/bin/env python3
"""APO 优化 V3 改写 Prompt —— 双数据集（155 优化 / 25 验证门槛）。

- 优化集：data/golden_rewrite_test.json（155，LLM 生成）
- 验证集：data/golden_rewrite_val.json（25，人工精标，回归 gate）
- 目标函数：GT term_weighted_recall（规范加权 0.45/0.30/0.10/0.10/0.05），fast 模式无 LLM-Judge
- 接受准则：候选在 155 上加权召回提升 且 25 上不低于 baseline-ε 才晋升
- 只出报告，不自动改 query_rewriter.py

用法:
  python3 apo_optimize_v3.py --eval-only          # 仅 baseline 双集冒烟
  python3 apo_optimize_v3.py --rounds 2            # 完整优化
"""

import argparse
import json
import sys
from pathlib import Path
from datetime import date

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from query_rewriter import REWRITE_PROMPT
from apo_rewriter import (
    run_evaluation,
    generate_candidates_v2,
    FAILURE_CATEGORIES,
)

TEST_PATH = BASE_DIR / "data" / "golden_rewrite_test.json"   # 155 优化集
VAL_PATH = BASE_DIR / "data" / "golden_rewrite_val.json"     # 25 验证集
REPORT_MD = BASE_DIR / "apo_v3_optimize_results.md"
REPORT_JSON = BASE_DIR / "apo_v3_optimize.json"
BEST_PROMPT_TXT = BASE_DIR / "data" / "candidate_best_prompt_v3.txt"

RECALL_THRESHOLD = 0.5   # 低于此加权召回视为失败样本
VAL_EPSILON = 0.005      # 验证集允许的微小回退容差


def extract_v3_prompt() -> str:
    """从 query_rewriter.REWRITE_PROMPT 取纯字符串模板（保留 {query}，还原 JSON 单括号）。"""
    tmpl = REWRITE_PROMPT.messages[0].prompt.template
    return (tmpl.replace("{query}", "\x00Q\x00")
                .replace("{{", "{").replace("}}", "}")
                .replace("\x00Q\x00", "{query}"))


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def objective(results):
    """目标函数：逐题 term_weighted_recall 求均值。"""
    vals = [r["gt_metrics"].get("term_weighted_recall", 0) for r in results]
    return float(np.mean(vals)) if vals else 0.0


def mean_kw(results):
    return float(np.mean([len(r["keywords"]) for r in results])) if results else 0.0


def eval_prompt(cases, prompt, label):
    """在给定数据集上评测 prompt，返回 (obj, results, summary)。"""
    results, summary, _ = run_evaluation(cases, prompt, label, fast_mode=True)
    return objective(results), results, summary


def build_failure_cats(results):
    """从 155 结果里取低加权召回样本，按 term_gap 归类，供候选生成。"""
    low = [r for r in results if r["gt_metrics"].get("term_weighted_recall", 1) < RECALL_THRESHOLD]
    low.sort(key=lambda r: r["gt_metrics"].get("term_weighted_recall", 0))
    if not low:
        return "无明显失败模式（加权召回均达标）", {}
    report = "## 术语加权召回不足（< %.2f）\n" % RECALL_THRESHOLD + "\n".join(
        f"  - [{r['id']}] {r['question'][:44]} → kw={r['keywords']} "
        f"(wr={r['gt_metrics'].get('term_weighted_recall',0):.2f})"
        for r in low[:8]
    )
    cats = {"term_gap": [
        {"id": r["id"], "question": r["question"],
         "keywords": r["keywords"], "sub_queries": r["sub_queries"],
         "problem": f"术语加权召回={r['gt_metrics'].get('term_weighted_recall',0):.2f}，"
                    f"must={r['gt_metrics'].get('term_hit_must_have',0)}/{r['gt_metrics'].get('term_must_have_count',0)} "
                    f"core={r['gt_metrics'].get('term_hit_core',0)}/{r['gt_metrics'].get('term_core_count',0)} "
                    f"precision={r['gt_metrics'].get('term_hit_precision',0)}/{r['gt_metrics'].get('term_precision_count',0)}",
         "suggestion": "补充口语/同义→知识库精确术语映射，尤其 core_concept 与 precision_term",
         "rewrite_type": r.get("rewrite_type", "?")}
        for r in low[:12]
    ]}
    return report, cats


def valid_candidate(text: str) -> bool:
    """加固：候选必须保留 {query} 占位并要求 JSON keywords 输出契约。"""
    return "{query}" in text and "keywords" in text


def main():
    ap = argparse.ArgumentParser(description="APO 优化 V3 改写 Prompt（155优化/25验证）")
    ap.add_argument("--eval-only", action="store_true", help="仅跑 baseline 双集冒烟")
    ap.add_argument("--rounds", type=int, default=2, help="迭代轮数 (default 2)")
    ap.add_argument("--limit", type=int, default=None, help="限制优化集题数（调试用）")
    args = ap.parse_args()

    v3 = extract_v3_prompt()
    test_cases = load(TEST_PATH)
    val_cases = load(VAL_PATH)
    if args.limit:
        test_cases = test_cases[:args.limit]

    print("=" * 60)
    print("  APO 优化 V3 改写 Prompt")
    print(f"  优化集 155 → {len(test_cases)} 题 | 验证集 {len(val_cases)} 题")
    print(f"  目标: term_weighted_recall | 验证门槛 ε={VAL_EPSILON}")
    print("=" * 60)

    # ── Baseline 双集 ──
    print("\n[Baseline] V3 在优化集(155)")
    obj155_base, res155_base, _ = eval_prompt(test_cases, v3, "baseline-155")
    print("\n[Baseline] V3 在验证集(25)")
    obj25_base, res25_base, _ = eval_prompt(val_cases, v3, "baseline-25")
    print(f"\n  Baseline 加权召回:  155={obj155_base:.4f}  25={obj25_base:.4f}  "
          f"(155 kw/题={mean_kw(res155_base):.1f})")

    trace = {
        "date": str(date.today()),
        "baseline": {"obj155": round(obj155_base, 4), "obj25": round(obj25_base, 4)},
        "rounds": [],
    }

    if args.eval_only:
        print("\n[eval-only] 完成。baseline 加权召回非 0 即表示 call_rewriter 数组修复生效。")
        return

    # ── 迭代优化 ──
    best_prompt = v3
    best_obj155 = obj155_base
    best_obj25 = obj25_base
    best_name = "baseline(V3)"
    best_res155 = res155_base

    for rnd in range(1, args.rounds + 1):
        print(f"\n{'='*60}\n  Round {rnd}\n{'='*60}")
        report, cats = build_failure_cats(best_res155)
        print(report[:600])
        if not cats:
            print("  无失败模式，停止。")
            break

        n = 3 if rnd == 1 else 2
        cands = generate_candidates_v2(best_prompt, report, cats, best_res155, n_candidates=n)
        cands = [c for c in cands if valid_candidate(c["prompt"])]
        if not cands:
            print("  无有效候选（未保留输出契约），停止。")
            break

        round_rec = {"round": rnd, "candidates": []}
        round_best = None
        for c in cands:
            label = f"R{rnd}_{c['id']}"
            print(f"\n  ── 评测 {label}: {c['direction'][:44]}")
            o155, r155, _ = eval_prompt(test_cases, c["prompt"], f"{label}-155")
            o25, _, _ = eval_prompt(val_cases, c["prompt"], f"{label}-25")
            passed_val = o25 >= best_obj25 - VAL_EPSILON
            improved = o155 > best_obj155
            print(f"     155={o155:.4f} (Δ{o155-best_obj155:+.4f})  "
                  f"25={o25:.4f} (门槛≥{best_obj25-VAL_EPSILON:.4f} {'PASS' if passed_val else 'FAIL'})  "
                  f"kw/题={mean_kw(r155):.1f}")
            rec = {"name": label, "direction": c["direction"],
                   "obj155": round(o155, 4), "obj25": round(o25, 4),
                   "improved": improved, "passed_val": passed_val,
                   "prompt": c["prompt"]}
            round_rec["candidates"].append(rec)
            if improved and passed_val:
                if round_best is None or o155 > round_best["_o155"]:
                    round_best = {**rec, "_o155": o155, "_o25": o25, "_res155": r155}

        trace["rounds"].append(round_rec)

        if round_best is None:
            print(f"\n  Round {rnd}: 无候选同时满足『155提升 且 25不下滑』，停止。")
            break

        best_prompt = round_best["prompt"]
        best_obj155 = round_best["_o155"]
        best_obj25 = round_best["_o25"]
        best_name = round_best["name"]
        best_res155 = round_best["_res155"]
        print(f"\n  Round {rnd} 采纳 {best_name}: 155={best_obj155:.4f} 25={best_obj25:.4f}")

    # ── 汇总 ──
    trace["best"] = {"name": best_name, "obj155": round(best_obj155, 4),
                     "obj25": round(best_obj25, 4),
                     "delta155": round(best_obj155 - obj155_base, 4),
                     "delta25": round(best_obj25 - obj25_base, 4)}
    applied = best_name != "baseline(V3)"

    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(trace, f, ensure_ascii=False, indent=2)

    if applied:
        with open(BEST_PROMPT_TXT, "w", encoding="utf-8") as f:
            f.write(best_prompt)

    write_markdown(trace, applied)
    print(f"\n{'='*60}")
    print(f"  完成。最优: {best_name}")
    print(f"  155 加权召回: {obj155_base:.4f} → {best_obj155:.4f} ({best_obj155-obj155_base:+.4f})")
    print(f"  25  加权召回: {obj25_base:.4f} → {best_obj25:.4f} ({best_obj25-obj25_base:+.4f})")
    print(f"  报告: {REPORT_MD.name} | {REPORT_JSON.name}"
          + (f" | {BEST_PROMPT_TXT.name}" if applied else " | 未产生更优候选，无 prompt 文件"))
    print(f"{'='*60}")


def write_markdown(trace, applied):
    b = trace["baseline"]
    best = trace["best"]
    lines = []
    lines.append("# APO 优化 V3 改写 Prompt 报告（155 优化 / 25 验证门槛）\n")
    lines.append(f"生成时间：{trace['date']} | 优化集：`data/golden_rewrite_test.json`(155) | "
                 f"验证集：`data/golden_rewrite_val.json`(25)")
    lines.append("目标函数：GT `term_weighted_recall`（0.45 must+0.30 core+0.10 precision+0.10 important+0.05 optional）| "
                 f"验证门槛 ε={VAL_EPSILON}\n")

    lines.append("## 变更日志\n")
    lines.append("| 日期 | 变更 |")
    lines.append("|------|------|")
    verdict = ("产生更优 prompt（已存 `data/candidate_best_prompt_v3.txt`，未自动 apply）"
               if applied else "未产生同时满足『155提升+25不下滑』的候选，V3 保持最优")
    lines.append(f"| {trace['date']} | APO 首次双集优化：{verdict} |\n")

    lines.append("---\n\n## 一、核心结果\n")
    lines.append("| | 优化集155 加权召回 | 验证集25 加权召回 |")
    lines.append("|---|---|---|")
    lines.append(f"| Baseline V3 | {b['obj155']:.4f} | {b['obj25']:.4f} |")
    lines.append(f"| 最优 ({best['name']}) | {best['obj155']:.4f} | {best['obj25']:.4f} |")
    lines.append(f"| Δ | {best['delta155']:+.4f} | {best['delta25']:+.4f} |\n")
    if applied:
        lines.append(f"> 验证集未回归（Δ25={best['delta25']:+.4f} ≥ -{VAL_EPSILON}），"
                     f"优化集提升 {best['delta155']:+.4f}。最优 prompt 待人工审阅后决定是否 apply。\n")
    else:
        lines.append("> 所有候选未能在不牺牲验证集的前提下提升优化集，V3 已是当前最优，无需改动。\n")

    lines.append("---\n\n## 二、逐轮候选\n")
    for rr in trace["rounds"]:
        lines.append(f"### Round {rr['round']}\n")
        lines.append("| 候选 | 方向 | 155 | 25 | 155提升 | 25达标 |")
        lines.append("|------|------|-----|-----|--------|--------|")
        for c in rr["candidates"]:
            lines.append(f"| {c['name']} | {c['direction'][:30]} | {c['obj155']:.4f} | {c['obj25']:.4f} | "
                         f"{'✅' if c['improved'] else '❌'} | {'✅' if c['passed_val'] else '❌'} |")
        lines.append("")

    lines.append("---\n\n## 三、方法\n")
    lines.append("- 优化信号仅取自 155 生成集；每个候选都在 25 人工精标集上回归验证，"
                 "只有『155 加权召回提升 且 25 不低于 baseline−ε』才晋升，保证质量不下滑。")
    lines.append("- fast 模式（GT 加权召回，无 LLM-Judge），确定性、低成本。")
    lines.append("- 候选由 `generate_candidates_v2` 基于 155 低召回失败样本生成，"
                 "并校验保留 `{query}` 占位与 JSON `keywords` 输出契约。")
    lines.append("- **未修改** `query_rewriter.py`；最优 prompt 原文见 `data/candidate_best_prompt_v3.txt`。\n")

    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
