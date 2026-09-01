#!/usr/bin/env python3
"""合体句 v2 抽查: 文档风格改写（术语对齐 + 语域对齐 + 语义保真）。

v1 修复句 = 最小修复（禁重排/禁压缩）→ 多数题只改 1-2 词，CE 端拿不到 rw 的标准化压制。
v2 = 允许句式重构（问句→标题风格）+ 词汇文档化，同时术语硬对齐 + 语义要素保真（防 rw 收窄）。

抽查题: Q_S15/Q_S07/Q_E24（rw 压制案例）+ Q_S13/Q_D09/Q_D12（rw 收窄丢 3）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import query_rewriter as qr
from llm_client import call_llm
from repair_query import term_replace

SAMPLE_QIDS = ["Q_S15", "Q_S07", "Q_E24", "Q_S13", "Q_D09", "Q_D12"]

V2_PROMPT = """你是农业气候区划领域的查询改写专家。

## 任务
把用户问题改写为"文档风格查询句"，用于检索农业气候区划报告（书面语，章节标题/正文段落风格）。
改写目标：
① 口语表达替换为规范术语（按给定术语映射，必须采用）；
② 整体表达文档化：口语问句改写为报告/标题风格（如"有什么差异？"→"对比分析"、
   "共性和差异"→"对比"），可保留疑问形式，但用词必须书面化；
③ 完整保留原问题的全部语义要素：所有概念、限定条件、对比对象、数值一个都不能丢；
④ 不改变原问题的意图与范围：不缩小、不扩大、不新增概念或场景限定。

## 输入
原始问题：
{query}

术语映射后的版本（术语部分必须采用，其余仅参考）：
{mapped}

## 硬性约束（违反任何一条即失败）
1. 数值/单位/符号（如 ≥10℃、140℃·d、1961-1990）必须逐字保持原样；
2. 专有名词（省份、作物、文档名）与缩写（CWDI、DEM 等）不改写、不展开、不缩写；
3. 已按映射表替换的规范术语不得退回口语表达；
4. 禁止引入与原问题无关的新概念或场景限定；
5. 改写后每个语义要素都必须能在原问题中找到对应（如"南疆和北疆的差异"两个对比对象
   都要保留），不得省略任何一个问点；
6. 允许适当增量：把疑问词隐含的语义维度显式化以增强检索与精排效果
   （如"为什么有的地方适合种苹果"→ 必须包含"适宜性差异的原因/区划依据"，
   "有什么差异"→"差异对比"），此类显式化不视为新概念；
7. "为什么/凭什么/原因"类问句的依据/指标/原因要素必须显式保留，不得改写成纯现象描述
   （反例：原问"为什么有的地方适合种苹果有的地方不行？"不得写成
   "苹果种植适宜性的地域差异分析"，必须包含"区划依据/原因"）；
8. 列举类问句（"哪些/哪种/几个/几级"）改写后必须保留列举语义，不得只写总称
   （反例："采用了哪些数据来源？"不得写成"数据来源分析"，须保留"哪些"或改写为
   "数据来源清单"）；"分别/各自/各类型/各级"的逐项对应关系不得合并
   （"各类型的算法核心"不得写成"算法核心"，"各级阈值"不得写成"阈值"）；
9. 方法/计算/判断类问句（"如何计算/如何划分/如何建立/能否/是否"）改写后必须保留
   方法/公式/判断语义（"如何计算"→"计算方法"，"是否通用"→"通用性判断"），
   不得写成丢"方法/判断"的纯名词短语。

## 改写示例
- 原问"新疆冬小麦区划中南疆和北疆的冬小麦种植适宜性有什么差异？"
  → "新疆南疆与北疆冬小麦种植适宜性的对比分析"
- 原问"内蒙古大豆多种灾害（干旱、霜冻、食心虫）风险区划在指标体系和方法上有什么共性和差异？"
  → "内蒙古大豆干旱、霜冻、食心虫风险区划的指标体系与方法对比"
- 原问"为什么有的地方适合种苹果有的地方不行？"
  → "苹果种植适宜性的地域差异及其区划依据"
- 原问"黑龙江大豆冷害区划采用了哪些数据来源？"
  → "黑龙江大豆冷害区划采用的数据来源清单"
- 原问"黑龙江省大豆低温冷害的气候风险等级是如何划分的？"
  → "黑龙江省大豆低温冷害气候风险等级的划分方法"

## 输出格式（仅输出 JSON）
{{"repair_query": "文档风格的改写句", "changes": ["修改点说明"]}}"""


def main():
    cache = json.load(open("data/repair_cache.json", encoding="utf-8"))
    ab_cand = json.load(open("data/ce_query_quota_ab/candidates/Q_S15.json", encoding="utf-8"))
    import glob
    by_id = {}
    for f in glob.glob("data/ce_query_quota_ab/candidates/Q_*.json"):
        d = json.load(open(f, encoding="utf-8"))
        by_id[d["qid"]] = d
    qr._load_term_map()

    for qid in SAMPLE_QIDS:
        d = by_id[qid]
        q = d["question"]
        rw = d["rw"] if d["rw"] else "(无 rw)"
        mapped, replaced = term_replace(q, qr._term_map)
        rec = cache.get(q, {})
        prompt = V2_PROMPT.format(query=q, mapped=mapped)
        resp = call_llm([{"role": "user", "content": prompt}],
                        temperature=0, stream=False, json_mode=True)
        if isinstance(resp, str):
            s, e = resp.find("{"), resp.rfind("}") + 1
            resp = json.loads(resp[s:e])
        print("=" * 88)
        print(f"[{qid}]")
        print(f"  原问 : {q}")
        print(f"  生产rw: {rw}")
        print(f"  v1修复: {rec.get('repair_query', '(无)')}")
        print(f"  术语替换: {[f'{k}→{qr._term_map[k]}' for k in replaced] or '无'}")
        print(f"  v2合体: {resp.get('repair_query')}")
        print(f"  修改点: {resp.get('changes')}")
        print()


if __name__ == "__main__":
    main()
