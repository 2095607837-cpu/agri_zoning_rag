#!/usr/bin/env python3
"""修复 query 生成器（合体 query 实验配套）。

合体 query = 原问最小修复版:
  ① 程序化术语替换: terminology_mapping.json 口语词→规范术语（确定性）——
     标准术语已在句中出现的 span 保护、最长口语词优先、非重叠匹配;
  ② LLM 病句修复/表述补全——硬约束: 不动语序 / 不合并分句 / 不删减 / 不压缩 /
     数值保真 / 专名保真 / 禁新增概念 / 已替换术语不得回退; temperature=0,
     输出 changes 修改点列表;
  ③ 校验器: 问号数一致 / 长度 ±50% / 数字集合一致 / 术语回退检查——
     违规重试 1 次（带违规原因），仍违规回退纯程序化版。

缓存: data/repair_cache.json（键 = 原问，gitignore）。
"""
import argparse
import json
import os
import re
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import query_rewriter as qr
import eval_gate_ab as ab
from llm_client import call_llm

CACHE_FILE = "data/repair_cache.json"

REPAIR_PROMPT = """你是农业气候区划领域的查询修复助手。

## 任务
对用户问题做"最小修复"，只允许两类修改：
① 采用给定的术语映射结果（口语词→规范术语），不得改动映射结果；
② 病句修正与表述补全——仅限语法成分（缺省的主语/宾语/指代、不通顺的表述）。

## 输入
原始问题：
{query}

术语映射后的问题（请直接在此基础上修改；若与原始问题相同说明无口语词命中）：
{mapped}

## 硬性约束（违反任何一条即失败）
1. 禁止重排语序、禁止合并分句、禁止删减内容、禁止压缩或总结原句；
2. 禁止引入原始问题中不存在的概念、术语、数值或场景限定（如补充"在气候区划中"）。
   例外：术语替换造成疑问结构缺失或语句残缺时，允许补全最小的疑问词/结构助词
   （吗、是否、如何、是什么、的、了、进行）使语句恢复完整疑问句，此类补全不视为新概念；
3. 数值/单位/符号（如 ≥10℃、140℃·d、1961-1990、15-25度）必须逐字保持原样；
4. 专有名词（省份、作物、文档名）与缩写（CWDI、DEM 等）不改写、不展开、不缩写；
5. 已按映射表替换的规范术语不得改回口语表达；
6. 问句数量（问号个数）必须与原始问题一致；
7. 若映射后的问题已经完整规范（无病句、无缺省、无需补全），直接原样输出，禁止任何"优化"；
   若因术语替换出现残缺（如名词短语直接加问号），必须按约束 2 的例外条款修复。

## 修复示例
- 映射"陕西苹果品质气候区划？和天气有关系吗？" → "陕西苹果的品质气候区划如何？和天气有关系吗？"
- 映射"大豆怎么知道该不该病虫害防治？" → "大豆怎么知道该不该进行病虫害防治？"
- 映射"黑龙江省气候资源普查中……空间推算采用什么方法？具体技术路线？"
  → "黑龙江省气候资源普查中……空间推算采用什么方法？具体技术路线是什么？"
- 映射"大豆低温冷害？怎么判断一个地方低温冷害？"
  → "大豆怕低温冷害吗？怎么判断一个地方会不会发生低温冷害？"
- 映射"种大豆适宜区划？" → "种大豆的适宜区划是什么？"

## 输出格式（仅输出 JSON）
{{"repair_query": "修复后的问题", "changes": ["修改点1", "修改点2"], "repaired": true/false}}

changes 逐条说明（如 "口感好→品质形成（术语映射）"、"补全省略的主语"）；
repaired=false 表示未做任何修改。"""


def _span_overlaps(span, spans):
    s, e = span
    return any(ps <= s < pe or ps < e <= pe for ps, pe in spans)


# 标准术语保护: 口语键是这些标准术语的真子串时跳过（如 辐射⊂总辐射、越冬⊂越冬期）
PROTECTED_TERMS = ["光合有效辐射", "净辐射", "总辐射", "越冬期", "越冬前",
                   "越冬后", "病虫害", "适宜性区划", "气候综合区划",
                   "风险区划", "灾害区划", "干旱区划", "霜冻区划"]
# 修复实验排除的键: 原地替换会引入错误概念或残留上下文引用
# （受灾果园→农业气象灾害果园; 有的地方适合种→有的适宜种植区）
EXCLUDED_KEYS = {"受灾", "地方适合种"}


def _protected_spans(question: str, term_map: dict) -> list[tuple[int, int]]:
    protected: list[tuple[int, int]] = []
    for std in set(term_map.values()) | set(PROTECTED_TERMS):
        if len(std) <= 1:
            continue
        start = 0
        while True:
            idx = question.find(std, start)
            if idx == -1:
                break
            protected.append((idx, idx + len(std)))
            start = idx + 1
    return protected


def term_replace(question: str, term_map: dict) -> tuple[str, list[str]]:
    """程序化术语替换: 标准术语已出现处保护（键 span ⊆ 保护 span 即跳过）,
    最长口语词优先, 非重叠, 相邻同值合并, 前后缀吸收防重复。

    Returns (mapped_question, replaced_keys)
    """
    protected = _protected_spans(question, term_map)
    keys = sorted((k for k in term_map
                   if len(k) > 1 and k not in EXCLUDED_KEYS), key=len, reverse=True)
    matches: list[tuple[int, int, str]] = []
    for key in keys:
        start = 0
        while True:
            idx = question.find(key, start)
            if idx == -1:
                break
            s, e = idx, idx + len(key)
            # 键与保护 span 相交即跳过, 除非键严格包含保护术语（如 越冬期长度⊃越冬期）
            blocked = any((ps < e and pe > s)
                          and not (ps >= s and pe <= e and (ps > s or pe < e))
                          for ps, pe in protected)
            if not blocked and not _span_overlaps((s, e), [m[:2] for m in matches]):
                std = term_map[key]
                # 前后缀吸收: 替换词与紧邻原文重复的部分并入替换范围
                # （如 "≥10℃积温" + 积温→≥10℃活动积温 ⇒ "≥10℃活动积温"）
                k_pre = 0
                for k in range(1, min(len(std), s) + 1):
                    if std[:k] == question[s - k:s]:
                        k_pre = k
                k_suf = 0
                for k in range(1, min(len(std), len(question) - e) + 1):
                    if std[-k:] == question[e:e + k]:
                        k_suf = k
                cand = (s - k_pre, e + k_suf)
                if not _span_overlaps(cand, protected) and \
                        not _span_overlaps(cand, [m[:2] for m in matches]):
                    s, e = cand
                matches.append((s, e, key))
            start = idx + 1

    # 相邻且映射到同一规范术语的匹配合并为一段（如 打药+防虫→病虫害防治×2）
    merged: list[tuple[int, int, str]] = []
    for m in sorted(matches, key=lambda x: x[0]):
        if merged and m[0] == merged[-1][1] and term_map[m[2]] == term_map[merged[-1][2]]:
            merged[-1] = (merged[-1][0], m[1], merged[-1][2])
        else:
            merged.append(m)

    out = question
    for s, e, key in sorted(merged, key=lambda m: -m[0]):
        out = out[:s] + term_map[key] + out[e:]
    return out, [k for _, _, k in merged]


def validate_repair(orig: str, repair: str, replaced_terms: list[tuple[str, str]],
                    mapped: str = "") -> list[str]:
    issues = []
    q_orig = orig.count("？") + orig.count("?")
    q_rep = repair.count("？") + repair.count("?")
    if q_orig != q_rep:
        issues.append(f"问号数 {q_orig}→{q_rep}")
    ratio = len(repair) / max(1, len(orig))
    if not (0.5 <= ratio <= 1.5):
        issues.append(f"长度比 {ratio:.2f}")
    nums = lambda s: sorted(re.findall(r"\d+(?:\.\d+)?", s))
    if nums(orig) != nums(repair):
        issues.append("数字集合不一致")
    syms = lambda s: sorted(re.findall(r"[≥≤℃％%]", s))
    if syms(orig) != syms(repair):
        issues.append("符号集合不一致")
    ref = mapped if mapped else orig
    for key, std in replaced_terms:
        if repair.count(std) < ref.count(std):
            issues.append(f"术语回退: {key}")
    return issues


def _call_repair_llm(orig: str, mapped: str, extra: str = "") -> Optional[dict]:
    prompt = REPAIR_PROMPT.format(query=orig, mapped=mapped)
    if extra:
        prompt += "\n\n## 上次输出违规，请修正\n" + extra
    try:
        resp = call_llm([{"role": "user", "content": prompt}],
                        temperature=0, stream=False, json_mode=True)
        if isinstance(resp, str):
            start, end = resp.find("{"), resp.rfind("}") + 1
            if start < 0 or end <= start:
                return None
            resp = json.loads(resp[start:end])
        if not isinstance(resp, dict) or not resp.get("repair_query"):
            return None
        return resp
    except Exception:
        return None


def generate_repair(question: str, cache: dict, force: bool = False) -> dict:
    if question in cache and not force:
        return cache[question]
    qr._load_term_map()
    mapped, replaced_keys = term_replace(question, qr._term_map)
    replaced_terms = [(k, qr._term_map[k]) for k in replaced_keys]
    rec = {"mapped": mapped, "replaced_keys": replaced_keys,
           "repair_query": mapped, "changes": [], "source": "programmatic",
           "retries": 0, "issues": []}
    resp = _call_repair_llm(question, mapped)
    if resp is not None:
        candidate = str(resp["repair_query"]).strip()
        issues = validate_repair(question, candidate, replaced_terms, mapped)
        if issues:
            retry = _call_repair_llm(question, mapped,
                                     "; ".join(issues) + "。保持其余内容不变，仅修正上述问题。")
            rec["retries"] = 1
            if retry is not None:
                candidate2 = str(retry["repair_query"]).strip()
                issues2 = validate_repair(question, candidate2, replaced_terms, mapped)
                if not issues2:
                    candidate, issues, resp = candidate2, [], retry
                else:
                    rec["issues"] = issues2
            else:
                rec["issues"] = issues
        if not issues and resp.get("repaired", False) and candidate != mapped:
            rec.update({"repair_query": candidate,
                        "changes": resp.get("changes", []) or [],
                        "source": "llm"})
    if rec["source"] != "llm":
        rec["changes"] = (["; ".join(rec["issues"])] if rec["issues"] else [])
    cache[question] = rec
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 题（0=全部）")
    ap.add_argument("--force", action="store_true", help="忽略缓存重跑")
    ap.add_argument("--samples", type=int, default=10, help="打印抽查样本数")
    args = ap.parse_args()

    cache = {}
    if os.path.exists(CACHE_FILE):
        cache = json.load(open(CACHE_FILE, encoding="utf-8"))

    qs = ab.gs[:args.limit] if args.limit else ab.gs
    t0 = time.time()
    for i, q in enumerate(qs, 1):
        rec = generate_repair(q["question"], cache, args.force)
        print(f"[{i}/{len(qs)}] {q['id']} source={rec['source']} "
              f"retries={rec['retries']} ({time.time() - t0:.0f}s)", flush=True)
        if i % 20 == 0:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=1)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)

    recs = [cache[q["question"]] for q in qs]
    n_term = sum(1 for r in recs if r["replaced_keys"])
    n_llm = sum(1 for r in recs if r["source"] == "llm")
    n_retry = sum(r["retries"] for r in recs)
    n_issue = sum(1 for r in recs if r["issues"])
    print(f"\n[done] {len(recs)} 题 | 术语替换 {n_term} | LLM 修复 {n_llm} | "
          f"重试 {n_retry} | 残留违规 {n_issue} → {CACHE_FILE}", flush=True)
    print("\n" + "=" * 88)
    print("抽查样本（原问 → 映射 → 修复）")
    print("=" * 88)
    shown = 0
    for q in qs:
        rec = cache[q["question"]]
        if shown >= args.samples:
            break
        if rec["source"] == "llm" or rec["replaced_keys"]:
            shown += 1
            print(f"\n[{q['id']}] 原问 : {q['question']}")
            if rec["mapped"] != q["question"]:
                print(f"        映射 : {rec['mapped']}")
            print(f"        修复 : {rec['repair_query']}  ({rec['source']})")
            if rec["changes"]:
                print(f"        修改 : {rec['changes']}")


if __name__ == "__main__":
    main()
