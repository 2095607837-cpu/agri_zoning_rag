#!/usr/bin/env python3
"""CE 精排专用改写句生成器（问题关系五要素保真 + 专业化表达 + 完整句）。

与生产 CE query（rw[0]）的区别:
  rw 服务于召回（标准化压缩），CE 端用它有收窄副作用（丢 Q_S13/Q_D09/Q_D12）；
  ce_query 服务于 CE 精排：完整保留问题关系五要素（谁+做什么+对谁+什么条件+问什么），
  专业化表达对齐文档，保持完整句（禁止标题化/关键词列表化——v2 教训：分差扁平化），
  长度受控（v2 教训：长度惩罚反噬）。

流程: 程序化术语替换（mapped）→ LLM 改写（CE_RW_PROMPT, temperature=0）→
      校验（数字/符号/长度/术语回退/标题化/五要素）→ 违规重试 1 次 →
      仍违规回退生产 CE query（rw[0]，无 rw 用原问题）。

缓存: data/repair_ce_rw/cache.json（键 = 原问）。
用法: python3 repair_ce_query.py [--limit N] [--force] [--samples N]
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

CACHE_FILE = "data/repair_ce_rw/cache.json"

TITLE_SUFFIX = ("对比分析", "对比", "分析", "清单", "划分方法", "方法", "特征",
                "差异", "分布", "依据", "情况", "现状", "适宜性", "等级",
                "指标体系", "指标")

CE_RW_PROMPT = """你是农业气候区划领域的查询改写专家。

## 任务
把用户问题改写为"CE 精排专用查询句"，用于 CrossEncoder 对候选 chunk 做语义精排。
CE 精排要求查询句与文档 chunk 的语义精确匹配、且对不同候选有区分度，因此查询句必须：
① 完整保留问题关系五要素：谁（主体/区域）+ 做什么（动作/方法/指标）+ 对谁（对象/作物）
   + 什么条件（时间/地域/灾害等限定）+ 问什么（所问点）——这是最重要的要求；
② 表达专业化：口语词替换为规范术语（按术语映射，必须采用），语义以原始问题为准；
③ 保持完整句形式（有主语和谓语），禁止改写为标题、名词短语或关键词罗列。

## 为什么这样要求（改写时注意规避两类已知失败）
- 标题化/关键词列表化（如"…适宜性的对比分析"）会让查询句与任何 chunk 都沾边，
  精排分差被压平，检索变差——必须写成含谓语的完整句；
- 句子过长会与长文档段落产生系统性偏差，被精排的长度惩罚反噬——只保留必要要素，
  禁止膨胀堆砌。

## 输入
原始问题：
{query}

术语映射后的版本（术语部分必须采用，其余仅参考）：
{mapped}

标准化改写参考（生产召回用改写句，仅参考其简洁的句式风格，不继承其语义收窄）：
{rw}

## 硬性约束（违反任何一条即失败）
1. 问题关系五要素全保留：谁、做什么、对谁、什么条件、问什么，缺一个即失败；
   对比类问句的两个对比对象都要保留；列举类问句保留列举语义；
   定义类问句（"什么是X"）的"谁"填被定义的概念 X（主体即被定义对象）；
2. 数值/单位/符号（如 ≥10℃、140℃·d、1961-1990）必须逐字保持原样；
3. 专有名词（省份、作物、文档名）与缩写（CWDI、DEM 等）不改写、不展开、不缩写；
4. 已按映射表替换的规范术语不得退回口语表达；
5. 必须是含主谓的完整疑问句或陈述句，禁止标题化与关键词列表化——
   不得以"…的对比分析/…的清单/…的划分方法"等名词短语结尾；
6. 长度控制在原始问题的 0.6~1.4 倍之间；
7. 语义范围与原始问题完全一致：不缩小、不扩大、不新增概念或场景限定；
8. "为什么/原因"类问句必须显式保留依据/原因要素，不得改写成纯现象描述。

## 改写示例
- 原问"新疆冬小麦区划中南疆和北疆的冬小麦种植适宜性有什么差异？"
  → "南疆和北疆的冬小麦种植适宜性存在哪些差异？"
- 原问"黑龙江大豆冷害区划采用了哪些数据来源？"
  → "黑龙江大豆冷害区划采用的数据来源包括哪些？"

## 输出格式（仅输出 JSON）
{{"ce_query": "CE 精排专用查询句", "who": "主体", "do": "动作/行为",
  "target": "对象", "condition": "限定条件（无则写'无'）", "ask": "所问点"}}"""


def _title_style(s: str) -> bool:
    t = s.rstrip("？?。.！! ")
    for suf in TITLE_SUFFIX:
        if t.endswith(suf):
            if not re.search(r"[吗呢哪什么如何怎样是否多少几]", s):
                return True
    return False


def validate_ce(orig: str, cand: str, elements: dict,
                replaced_terms: list, mapped: str = "") -> list:
    issues = []
    nums = lambda s: sorted(re.findall(r"\d+(?:\.\d+)?", s))
    if nums(orig) != nums(cand):
        issues.append("数字集合不一致")
    syms = lambda s: sorted(re.findall(r"[≥≤℃％%]", s))
    if syms(orig) != syms(cand):
        issues.append("符号集合不一致")
    ratio = len(cand) / max(1, len(orig))
    # 短口语题放行 len+8 下限（"橘子怕什么天气" 1.4× 只有 10 字, 完整句必然超）
    if not (0.6 <= ratio <= 1.4) and not (len(cand) <= len(orig) + 8):
        issues.append(f"长度比 {ratio:.2f}")
    ref = mapped if mapped else orig
    for key, std in replaced_terms:
        if cand.count(std) < ref.count(std):
            issues.append(f"术语回退: {key}")
    if _title_style(cand):
        issues.append("标题化/名词短语结尾")
    for k in ("who", "do", "target", "ask"):
        if not elements.get(k) or elements[k] in ("无", "—", "-"):
            issues.append(f"五要素缺失: {k}")
    return issues


def _call_ce(orig: str, mapped: str, rw_ref: str, extra: str = ""):
    prompt = CE_RW_PROMPT.format(query=orig, mapped=mapped,
                                 rw=rw_ref if rw_ref else "(无)")
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
        if not isinstance(resp, dict) or not resp.get("ce_query"):
            return None
        return resp
    except Exception:
        return None


def generate_ce(question: str, rw_ref: str, cache: dict, force: bool = False) -> dict:
    if question in cache and not force:
        return cache[question]
    qr._load_term_map()
    mapped, replaced_keys = term_replace(question, qr._term_map)
    replaced_terms = [(k, qr._term_map[k]) for k in replaced_keys]
    rec = {"mapped": mapped, "replaced_keys": replaced_keys, "rw_ref": rw_ref,
           "ce_query": "", "elements": {}, "changes": [],
           "retries": 0, "issues": [], "fallback": None}
    resp = _call_ce(question, mapped, rw_ref)
    if resp is not None:
        cand = str(resp["ce_query"]).strip()
        elements = {k: str(resp.get(k, "")).strip()
                    for k in ("who", "do", "target", "condition", "ask")}
        issues = validate_ce(question, cand, elements, replaced_terms, mapped)
        if issues:
            retry = _call_ce(question, mapped, rw_ref,
                             "; ".join(issues) + "。保持其余内容不变，仅修正上述问题。")
            rec["retries"] = 1
            if retry is not None:
                cand2 = str(retry.get("ce_query", "")).strip()
                elements2 = {k: str(retry.get(k, "")).strip()
                             for k in ("who", "do", "target", "condition", "ask")}
                issues2 = validate_ce(question, cand2, elements2,
                                      replaced_terms, mapped)
                if not issues2:
                    cand, elements, issues = cand2, elements2, []
                else:
                    rec["issues"] = issues2
            else:
                rec["issues"] = issues
        if not issues and cand:
            rec.update({"ce_query": cand, "elements": elements})
    else:
        rec["issues"] = ["LLM 调用失败"]
    if not rec["ce_query"]:
        rec["fallback"] = rw_ref or question
        rec["ce_query"] = rw_ref or question
    cache[question] = rec
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 题（0=全部）")
    ap.add_argument("--force", action="store_true", help="忽略缓存重跑")
    ap.add_argument("--samples", type=int, default=12, help="打印抽查样本数")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    cache = {}
    if os.path.exists(CACHE_FILE):
        cache = json.load(open(CACHE_FILE, encoding="utf-8"))

    prod = {}
    import glob
    for f in glob.glob("data/ce_query_quota_ab/candidates/Q_*.json"):
        d = json.load(open(f, encoding="utf-8"))
        prod[d["qid"]] = d

    qs = ab.gs[:args.limit] if args.limit else ab.gs
    t0 = time.time()
    for i, q in enumerate(qs, 1):
        rec = prod[q["id"]]
        rw_ref = rec["rw"][0] if rec["rw"] else ""
        generate_ce(q["question"], rw_ref, cache, args.force)
        c = cache[q["question"]]
        print(f"[{i}/{len(qs)}] {q['id']} retries={c['retries']} "
              f"issues={len(c['issues'])} fallback={c['fallback'] is not None} "
              f"({time.time() - t0:.0f}s)", flush=True)
        if i % 10 == 0:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=1)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)

    recs = [cache[q["question"]] for q in qs]
    n_term = sum(1 for r in recs if r["replaced_keys"])
    n_retry = sum(r["retries"] for r in recs)
    n_issue = sum(1 for r in recs if r["issues"])
    n_fb = sum(1 for r in recs if r["fallback"])
    print(f"\n[done] {len(recs)} 题 | 术语替换 {n_term} | 重试 {n_retry} | "
          f"残留违规 {n_issue} | 回退 {n_fb} → {CACHE_FILE}", flush=True)
    print("\n" + "=" * 88)
    print("抽查样本（原问 → rw → ce_query）")
    print("=" * 88)
    shown = 0
    for q in qs:
        rec = cache[q["question"]]
        if shown >= args.samples:
            break
        if rec["fallback"] is None:
            shown += 1
            print(f"\n[{q['id']}] 原问 : {q['question']}")
            if rec["mapped"] != q["question"]:
                print(f"        映射 : {rec['mapped']}")
            if rec["rw_ref"]:
                print(f"        rw  : {rec['rw_ref']}")
            print(f"        CE  : {rec['ce_query']}")
            el = rec.get("elements", {})
            print(f"        要素 : 谁={el.get('who')} | 做={el.get('do')} | "
                  f"对={el.get('target')} | 条件={el.get('condition')} | "
                  f"问={el.get('ask')}")


if __name__ == "__main__":
    main()
