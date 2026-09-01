#!/usr/bin/env python3
"""合体句 v2 生成器：文档风格改写（术语对齐 + 语域对齐 + 语义保真 + 适当增量）。

与 v1（repair_query.py 最小修复）的区别:
  v2 允许句式重构（问句→标题风格）、词汇文档化、疑问词隐含维度显式化（增量），
  但语义要素全保留、术语硬对齐、数值/专名保真（防 rw 收窄副作用）。

流程: 程序化术语替换（mapped）→ LLM 改写（V2_PROMPT, temperature=0）→
      校验（数字/符号/长度/术语回退）→ 违规重试 1 次 → 仍违规回退 mapped。

缓存: data/repair_cache_v2.json（键 = 原问，gitignore）。
用法: python3 repair_query_v2.py [--limit N] [--force] [--samples N]
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import eval_gate_ab as ab
import query_rewriter as qr
from llm_client import call_llm
from repair_query import term_replace
from repair_v2_sample import V2_PROMPT

CACHE_FILE = "data/repair_cache_v2.json"


def validate_v2(orig: str, v2: str, replaced_terms: list, mapped: str = "") -> list:
    issues = []
    nums = lambda s: sorted(re.findall(r"\d+(?:\.\d+)?", s))
    if nums(orig) != nums(v2):
        issues.append("数字集合不一致")
    syms = lambda s: sorted(re.findall(r"[≥≤℃％%]", s))
    if syms(orig) != syms(v2):
        issues.append("符号集合不一致")
    ratio = len(v2) / max(1, len(orig))
    if not (0.4 <= ratio <= 2.0):
        issues.append(f"长度比 {ratio:.2f}")
    ref = mapped if mapped else orig
    for key, std in replaced_terms:
        if v2.count(std) < ref.count(std):
            issues.append(f"术语回退: {key}")
    return issues


def _call_v2(orig: str, mapped: str, extra: str = ""):
    prompt = V2_PROMPT.format(query=orig, mapped=mapped)
    if extra:
        prompt += "\n\n## 上次输出违规，请修正\n" + extra
    try:
        resp = call_llm([{"role": "user", "content": prompt}],
                        temperature=0, stream=False, json_mode=True)
        if isinstance(resp, str):
            s, e = resp.find("{"), resp.rfind("}") + 1
            if s < 0 or e <= s:
                return None
            resp = json.loads(resp[s:e])
        if not isinstance(resp, dict) or not resp.get("repair_query"):
            return None
        return resp
    except Exception:
        return None


def generate_v2(question: str, cache: dict, force: bool = False) -> dict:
    if question in cache and not force:
        return cache[question]
    qr._load_term_map()
    mapped, replaced_keys = term_replace(question, qr._term_map)
    replaced_terms = [(k, qr._term_map[k]) for k in replaced_keys]
    rec = {"mapped": mapped, "replaced_keys": replaced_keys,
           "v2_query": mapped, "changes": [], "retries": 0, "issues": []}
    resp = _call_v2(question, mapped)
    if resp is not None:
        cand = str(resp["repair_query"]).strip()
        issues = validate_v2(question, cand, replaced_terms, mapped)
        if issues:
            retry = _call_v2(question, mapped,
                             "; ".join(issues) + "。保持其余内容不变，仅修正上述问题。")
            rec["retries"] = 1
            if retry is not None:
                cand2 = str(retry["repair_query"]).strip()
                issues2 = validate_v2(question, cand2, replaced_terms, mapped)
                if not issues2:
                    cand, issues, resp = cand2, [], retry
                else:
                    rec["issues"] = issues2
            else:
                rec["issues"] = issues
        if not issues and cand != mapped:
            rec.update({"v2_query": cand, "changes": resp.get("changes", []) or []})
        elif not issues and cand == mapped:
            rec["changes"] = ["原问已达标，未改写"]
    else:
        rec["issues"] = ["LLM 调用失败，回退映射版"]
    cache[question] = rec
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 题（0=全部）")
    ap.add_argument("--force", action="store_true", help="忽略缓存重跑")
    ap.add_argument("--samples", type=int, default=12, help="打印抽查样本数")
    args = ap.parse_args()

    cache = {}
    if os.path.exists(CACHE_FILE):
        cache = json.load(open(CACHE_FILE, encoding="utf-8"))

    qs = ab.gs[:args.limit] if args.limit else ab.gs
    t0 = time.time()
    for i, q in enumerate(qs, 1):
        rec = generate_v2(q["question"], cache, args.force)
        n_changed = len(rec["changes"])
        print(f"[{i}/{len(qs)}] {q['id']} retries={rec['retries']} "
              f"issues={len(rec['issues'])} changes={n_changed} "
              f"({time.time() - t0:.0f}s)", flush=True)
        if i % 10 == 0:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=1)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)

    recs = [cache[q["question"]] for q in qs]
    n_term = sum(1 for r in recs if r["replaced_keys"])
    n_changed = sum(1 for r, q in zip(recs, qs) if r["v2_query"] != q["question"])
    n_retry = sum(r["retries"] for r in recs)
    n_issue = sum(1 for r in recs if r["issues"])
    print(f"\n[done] {len(recs)} 题 | 术语替换 {n_term} | 改写 {n_changed} | "
          f"重试 {n_retry} | 残留违规 {n_issue} → {CACHE_FILE}", flush=True)
    print("\n" + "=" * 88)
    print("抽查样本（原问 → 映射 → v2 合体句）")
    print("=" * 88)
    shown = 0
    for q in qs:
        rec = cache[q["question"]]
        if shown >= args.samples:
            break
        if rec["v2_query"] != q["question"]:
            shown += 1
            print(f"\n[{q['id']}] 原问 : {q['question']}")
            if rec["mapped"] != q["question"]:
                print(f"        映射 : {rec['mapped']}")
            print(f"        合体 : {rec['v2_query']}")
            if rec["changes"]:
                print(f"        修改 : {rec['changes']}")


if __name__ == "__main__":
    main()
