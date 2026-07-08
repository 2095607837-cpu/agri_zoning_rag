#!/usr/bin/env python3
"""按 GOLDEN_REWRITE_GUIDE.md 规范，批量生成剩余 155 条 golden rewrite 条目。

batch_size=10，每批送入 3 个 few-shot + GOLDEN_REWRITE_GUIDE 核心规范。
输出: data/golden_rewrite_test.json（含已有 25 条 + 新 155 条 = 180 条）
"""

import json
import sys
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from llm_client import call_llm

# ── 加载已有 25 条 gold ──
with open(BASE_DIR / "data" / "golden_rewrite_val.json", encoding="utf-8") as f:
    existing = json.load(f)

# ── 加载 golden_set_v2，取不在已有 25 条中的 ──
with open(BASE_DIR / "data" / "golden_set_v2.json", encoding="utf-8") as f:
    all_qs = json.load(f)

existing_ids = set(q["id"] for q in existing)
existing_qs = set(q["question"] for q in existing)
remaining = [q for q in all_qs if q["question"] not in existing_qs and q.get("capability") != "ood_detection"]

print(f"已有: {len(existing)} | 待生成: {len(remaining)} | 总计: {len(existing) + len(remaining)}")

# ── Few-shot examples ──
FEWSHOT = json.dumps(existing[:3], ensure_ascii=False, indent=2)

# ── 核心规范提取 ──
GUIDE_RULES = """
## Keywords 标注规范
- 五级分层：must_have(0.45) > core_concept(0.30) > precision_term(0.10) > important_terms(0.10) > optional_terms(0.05)
- must_have：实体锚点，0-1个，缺失严重扣分（如"大豆""冬小麦""干热风"）
- core_concept：核心概念，1-3个，概念正确即可不要求字面一致（如"活动积温""低温冻害"）
- precision_term：精确术语，1-2个（如"≥10℃""灌浆期""Penman-Monteith"）
- important_terms：重要补充，0-2个（如"CWDI""防灾减灾能力"）
- optional_terms：可选加分，0-2个（如"黄土高原""时间尺度"）
- 每条 keyword 必须能在知识库 Chunk 中实际找到，禁止专家脑补
- 数量建议：总计 3-7 个，不是越多越好

## Canonical Queries
- 同一信息需求的不同表述，2-4条
- 等价但词序/用词不同的表述方式

## Reference Sub-queries
- 覆盖不同 Chunk，不互相包含，2-3条
- 尽量覆盖：定义类、方法类、应用类
- 语义不重叠才有增量价值

## rewrite_type
- normalize：口语/白话/学术同义 → 专业术语（sub_queries 留空）
- expand：多对象对比/多维度比较/跨文档对比（sub_queries 填入）
- 仅一个实体+单一维度的普通查询，不改写仅提取关键词

## 输出为 JSON 数组，每个元素格式：
{
  "id": "原 golden_set_v2 中的 id",
  "question": "原问题文本",
  "rewrite_type": "normalize 或 expand",
  "rewrite_eval": {
    "required_terms": {
      "must_have": ["实体锚点，无则空数组"],
      "core_concept": ["核心概念1-3个"],
      "precision_term": ["精确术语1-2个或无则空数组"]
    },
    "important_terms": ["重要补充"],
    "optional_terms": ["可选加分"]
  },
  "canonical_queries": ["等价表述1", "等价表述2", "等价表述3"],
  "reference_sub_queries": ["定义类", "方法类", "应用类"]
}

注意：normalize 类型 => reference_sub_queries 为空数组 []
     expand 类型 => reference_sub_queries 为 2-3 条不同角度的查询
"""


def generate_batch(questions, batch_idx):
    """每批 10 题，送入 LLM 生成 golden entries。"""
    qs_formatted = "\n".join([
        f"  {i+1}. [{q['id']}] capability={q.get('capability','?')} difficulty={q.get('difficulty','?')} query_type={q.get('query_type','?')} | {q['question']}"
        for i, q in enumerate(questions)
    ])

    prompt = f"""你是农业气候区划 RAG 评测数据集标注专家。

## 标注规范
{GUIDE_RULES}

## Few-shot 参考
{FEWSHOT[:2000]}

## 待标注题目（共 {len(questions)} 题）
{qs_formatted}

请严格按规范为以上 {len(questions)} 题生成 golden rewrite 条目。仅输出 JSON 数组，不要 Markdown，不要解释。

直接输出 JSON 数组：
[{{"id": "...", "question": "...", ...}}, ...]"""

    print(f"  [batch {batch_idx}] 生成 {len(questions)} 题...", end=" ", flush=True)
    try:
        resp = call_llm([{"role": "user", "content": prompt}], temperature=0.3, stream=False)
        start = resp.find("[")
        end = resp.rfind("]") + 1
        if start >= 0 and end > start:
            parsed = json.loads(resp[start:end])
            # 验证结构
            valid = 0
            for item in parsed:
                if "id" in item and "rewrite_eval" in item and "canonical_queries" in item:
                    valid += 1
            print(f"OK ({valid}/{len(questions)} valid)")
            return parsed
        else:
            print(f"FAIL (no JSON array, resp={resp[:100]})")
            return []
    except Exception as e:
        print(f"FAIL ({e})")
        return []


# ── 分批生成 ──
BATCH_SIZE = 10
all_results = list(existing)
generated_ids = set()

for batch_idx in range(0, len(remaining), BATCH_SIZE):
    batch = remaining[batch_idx:batch_idx + BATCH_SIZE]
    results = generate_batch(batch, batch_idx // BATCH_SIZE + 1)
    for item in results:
        rid = item.get("id", "")
        if rid and rid not in generated_ids and rid not in existing_ids:
            # 确保 rewrite_type 合法
            if item.get("rewrite_type") not in ("normalize", "expand"):
                item["rewrite_type"] = "normalize"
            # 确保字段存在
            re = item.get("rewrite_eval", {})
            rt = re.get("required_terms", {})
            if not isinstance(rt, dict):
                rt = {"must_have": [], "core_concept": [], "precision_term": []}
            rt.setdefault("must_have", [])
            rt.setdefault("core_concept", [])
            rt.setdefault("precision_term", [])
            re["required_terms"] = rt
            re.setdefault("important_terms", [])
            re.setdefault("optional_terms", [])
            item["rewrite_eval"] = re
            item.setdefault("canonical_queries", [])
            item.setdefault("reference_sub_queries", [])
            if item["rewrite_type"] == "normalize":
                item["reference_sub_queries"] = []
            all_results.append(item)
            generated_ids.add(rid)

    # 中间保存
    with open(BASE_DIR / "data" / "golden_rewrite_test.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    progress = len(all_results)
    target = len(existing) + len(remaining)
    print(f"  进度: {progress}/{target} ({progress*100/target:.0f}%)")

print(f"\n最终: {len(all_results)} 条 → data/golden_rewrite_test.json")
print(f"其中 normalize: {sum(1 for q in all_results if q.get('rewrite_type')=='normalize')}")
print(f"其中 expand: {sum(1 for q in all_results if q.get('rewrite_type')=='expand')}")
