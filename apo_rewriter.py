"""
APO (Automatic Prompt Optimization) V2 — Query Rewriter Prompt 优化

基于 prompt_evolution PDF 方法论重构：
  1. 强制分步推理评测（step1→step2→step3→step4）
  2. 容错机制（合理扩展 + 少数服从多数）
  3. 数据驱动的候选生成（基于逐题失败分析）
  4. 迭代精炼（Round 1 → 分析残留问题 → Round 2）
  5. 前后对比报告

用法:
  python3 apo_rewriter.py                      # 完整 APO 流程
  python3 apo_rewriter.py --eval-only          # 仅评测当前 prompt
  python3 apo_rewriter.py --apply              # 将最优 prompt 写入 query_rewriter.py
  python3 apo_rewriter.py --fast               # 快速模式（跳过 LLM-Judge，仅 GT 指标）
"""

import json
import os
import sys
import time
from pathlib import Path
from collections import defaultdict

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
GOLDEN_PATH = BASE_DIR / "data" / "golden_rewrite_val.json"
CHUNKS_PATH = BASE_DIR / "data" / "chunks.json"

_embed_model = None

# ── 当前 Rewrite Prompt ────────────────────────────────────

CURRENT_PROMPT = """你是农业气候区划领域检索优化专家。将用户问题中的口语/同义表达映射为知识库标准术语，生成关键词和多角度检索查询。

## 口语→术语映射表（必须参考）
- "光照好不好/阳光够不够" → "日照时数""日照百分率""太阳辐射""光合有效辐射"
- "怕不怕冷/会不会太冷/冻坏" → "低温冷害""霜冻""积温距平""低温冻害"
- "麦子冻不坏/冬天麦子/冬小麦越冬" → "冬小麦""越冬期""积雪覆盖""最低气温"
- "风刮得庄稼受不了" → "干热风""高温低湿""农业气象灾害"
- "水太多/田里水多" → "渍涝""土壤含水量"
- "什么时候种什么时候收" → "播种期""成熟期""生育期"
- "打药防虫/该不该打药" → "大豆食心虫""综合气象风险指数""风险等级""防治"
- "选什么地方最好/什么地方适合种" → "适宜性区划""≥10℃活动积温""降水量""坡度"
- "品质不好/好不好吃/为啥好吃" → "品质气候区划""日照时数""昼夜温差""糖分"
- "橘子怕什么天气/柑橘怕什么" → "柑橘""低温冻害""高温干旱""暴雨洪涝"
- "种苹果/苹果适合什么地方" → "苹果种植""适宜性区划""年平均气温""年降水量""坡度"

## 学术同义→标准术语映射
- "高温伤害/高温胁迫" → "高温热害指数""光合作用""蛋白质变性"
- "水分不足/缺水/干旱" → "干旱风险区划""水分亏缺指数""致灾因子危险性""承灾体脆弱性"
- "低温累积/持续低温" → "低温冷害""气温之和距平""积温"
- "生产潜力/产量估算" → "光合生产潜力""光温生产潜力""气候生产潜力"
- "危险性/敏感性" → "致灾因子危险性""孕灾环境敏感性""降水距平"
- "暴露度/脆弱性" → "承灾体暴露度""承灾体脆弱性""防灾减灾能力"
- "SPEI/CWDI" → "标准化降水""水分亏缺指数""时间尺度""作物需水量"
- "AHP/一致性" → "层次分析法""一致性检验""CR""判断矩阵"
- "GIS/叠加" → "空间分析""图层""主导因子""综合评判法""网格点"
- "蒸散/ET" → "参考作物蒸散量""实际蒸散量""Penman-Monteith""作物系数"
- "辐射/Rn" → "地表净辐射""地表净短波辐射""地表净长波辐射""反照率"
- "隶属度" → "隶属函数""适宜性指数""加权求和""综合评价"
- "光合有效/光合辐射/PAR" → "光合有效辐射""太阳总辐射""光合生产潜力""生产潜力"

## 规则
- 先判断原始问题最接近映射表中哪条，再提取对应标准术语作为关键词
- 关键词2-5个，逗号分隔
- 子查询2-3个，换行分隔，从不同检索角度切入

用户问题：{query}

仅输出JSON：{{"keywords": "kw1, kw2", "sub_queries": "sq1\\nsq2"}}"""


# ═══════════════════════════════════════════════════════════════
#  PDF 风格的评测 Prompt — 强制分步推理 + 容错机制
# ═══════════════════════════════════════════════════════════════

EVAL_PROMPT_V3 = """你是农业气候区划领域的 RAG Query Rewrite 评估专家。你的任务不是评价最终答案，而是判断改写是否提取了正确关键词、是否覆盖了用户核心意图、子查询策略是否与改写类型匹配。

**V3 Rewrite Prompt 类型定义（评估时必须以此为基准）：**
- **none**: 问题已是标准术语+完整检索语句，直接检索即可。sub_queries 应为空。
- **normalize**: 问题需术语标准化（口语→术语、简称→全称），但结构不变。sub_queries 应为空。
- **expand**: 问题需多角度独立检索（多对象对比/多维度比较/跨文档）。sub_queries 应覆盖 Gold 中的检索方向。

当前问题的 Gold 改写类型：**{rewrite_type}**

---

# 评估维度

## 零、Type-Aware 评分模式选择

**在开始评分前，先确认当前题目适用的评分模式（非常重要）：**

- gold_rewrite_type = **none 或 normalize** → 使用「精简模式」：仅评估关键词质量 + sub_queries 正确性（空即满分）
- gold_rewrite_type = **expand** → 使用「完整模式」：评估关键词 + sub_queries 覆盖度 + 扩展方向

精简模式下，sub_queries 为空是**正确行为**，不是缺陷。严禁因 sub_queries 为空而扣分。

---

## 一、Keyword Quality（所有模式通用）

对照 Gold Keyword 分层评估预测关键词的质量。

**Gold Keyword 层级及权重：**

| 层级 | 权重 | 含义 |
|---|---|---|
| must_have | 0.45 | 必须包含的实体/主题词，缺失说明改写偏离意图 |
| core_concept | 0.30 | 核心专业概念，判断是否理解真实语义 |
| precision_term | 0.10 | 精确指标/阈值/限定条件 |
| important_terms | 0.10 | 重要补充，不致命但影响完整性 |
| optional_terms | 0.05 | 锦上添花 |

**Weighted Recall 公式：**
= 0.45 × (must_have命中数/must_have总数)
+ 0.30 × (core_concept命中数/core_concept总数)
+ 0.10 × (precision_term命中数/precision_term总数)
+ 0.10 × (important_terms命中数/important_terms总数)
+ 0.05 × (optional_terms命中数/optional_terms总数)

**匹配规则：**
- 支持同义词匹配（"柑橘"≈"橘子"）
- 支持上下位概念匹配（"低温冷害"≈"低温冻害"）
- 支持部分匹配（"活动积温"≈"≥10℃活动积温"）
- 支持缩写匹配

**Keyword Precision：**
= 预测关键词中有效命中的数量 / 预测关键词总数量

**Keyword Score = 0.7 × Weighted Recall + 0.3 × Precision**

---

## 二、Sub-Query Quality（仅 expand 类型生效）

### 2a. Canonical 覆盖度

判断预测的 sub_queries 是否覆盖了 Gold canonical_queries 表达的搜索意图。**用语义匹配，不用关键词匹配。**

**评分档位：**
- 1.0 — 完全覆盖：语义完全等价，能命中同一类 Chunk
- 0.75 — 基本覆盖：意图对齐，仅差术语精度或角度略有偏差
- 0.5 — 部分覆盖：主题相关但角度不同
- 0 — 无关：完全没有覆盖该意图

**Canonical Score = Σ(每条 Gold canonical 的最佳匹配分) / Gold canonical 总数**

### 2b. Expansion 扩展方向

评价预测 sub_queries 是否覆盖了 Gold reference_sub_queries 的扩展方向（定义类/方法类/应用类）。

**命中判定：**
- 每条预测 sub_query 与 Gold reference 做语义匹配
- 命中标准：语义方向一致（定义类对定义类、方法类对方法类）
- 平方根映射：sqrt(命中方向数 / reference 总方向数)

| 命中 | 1/3 | 2/3 | 3/3 | 1/2 | 2/2 |
|---|---|---|---|---|---|
| 平方根 | 0.58 | 0.82 | 1.0 | 0.71 | 1.0 |

**Diversity 惩罚：**
如果 sub_queries 之间语义高度重叠（同义反复），Expansion Score 扣 0.2：
Expansion Score = max(0, sqrt(hits/total) - 0.2) 若有重叠，否则 = sqrt(hits/total)

### 2c. Sub-Query 综合

**SubQuery Score = 0.6 × Canonical Score + 0.4 × Expansion Score**

---

## 三、Type Consistency（精简模式下权重更高）

评估预测输出与 gold 类型的一致性：

- sub_queries 与类型期望一致（normalize→空，expand→非空）→ 1.0
- 基本一致但有瑕疵（expand 但 sub_queries 偏少/角度单一）→ 0.7
- 部分不一致（normalize 但有少量 sub_queries/expand 但 sub_queries 为空）→ 0.3
- 严重不一致 → 0.0

---

## 四、最终 Rewrite Quality Score（按类型调整权重）

### normalize/none 类型（精简模式）：
**RQ = 0.75 × Keyword Score + 0.25 × Type Consistency**

sub_queries 为空是正确行为，不评估 Canonical 和 Expansion。

### expand 类型（完整模式）：
**RQ = 0.35 × Keyword Score + 0.45 × SubQuery Score + 0.20 × Type Consistency**

---

**等级（两种模式通用）：**
- 0.00-0.30：Rewrite 严重错误，无法用于召回
- 0.30-0.60：部分理解用户意图，需要优化
- 0.60-0.80：基本满足召回需求
- 0.80-1.00：高质量改写，可用于生产 RAG 召回

---

# 输入

<Gold改写类型>
{rewrite_type}
</Gold改写类型>

<用户问题>
{query}
</用户问题>

<Gold参考答案>
{golden_answer}
</Gold参考答案>

<预测Keywords>
{keywords}
</预测Keywords>

<预测SubQueries>
{sub_queries}
</预测SubQueries>

---

# 输出格式（严格 JSON）

**输出要求（必须严格遵守，违规将导致解析失败）：**
1. 只输出 JSON，不允许输出任何其他内容
2. 禁止使用 Markdown 代码块（禁止 ```json 或 ``` 包裹）
3. 禁止在 JSON 前后添加任何解释文字
4. 所有字段必须存在，不得省略
5. 数值类型必须是**计算结果**（如 0.45），**严禁输出公式**（如 0.45*1+0.30*0 是错误的），保留两位小数
6. analysis 字段必须用中文填写，不可留空
7. 数组字段若无匹配项填 []，不可省略

直接输出以下 JSON（不要 ```json 包裹）：

{{
  "scoring_mode": "compact 或 full（根据 gold_rewrite_type 选择）",
  "keyword_evaluation": {{
    "matched_terms": {{"must_have": [], "core_concept": [], "precision_term": [], "important_terms": [], "optional_terms": []}},
    "missing_terms": {{"must_have": [], "core_concept": [], "precision_term": [], "important_terms": [], "optional_terms": []}},
    "weighted_recall": 0.0,
    "keyword_precision": 0.0,
    "keyword_score": 0.0,
    "analysis": "逐层分析预测关键词覆盖情况"
  }},
  "canonical_evaluation": {{
    "per_query_scores": [],
    "canonical_score": 0.0,
    "analysis": "expand类型：逐条语义匹配分析；normalize类型：标注N/A"
  }},
  "expansion_evaluation": {{
    "matched_directions": [],
    "expansion_score": 0.0,
    "diversity_penalty": false,
    "analysis": "expand类型：扩展方向分析；normalize类型：标注N/A"
  }},
  "type_consistency": {{
    "consistency_score": 0.0,
    "analysis": "sub_queries输出与gold类型是否一致"
  }},
  "final_score": {{
    "rewrite_quality_score": 0.0,
    "grade": "",
    "analysis": "一句话总结主要优点和问题"
  }}
}}
"""

# ── 数据驱动的候选生成 Prompt ─────────────────────────────

CANDIDATE_GEN_V2 = """你是 Prompt Engineering 专家。以下是一个农业气候区划 Query Rewriter Prompt 的评估结果，请基于失败模式生成改进版 Prompt。

## 当前 Prompt
{current_prompt}

## 失败模式分析
{failure_analysis}

## 具体失败案例（改写结果 + 问题诊断）
{failure_examples}

## 优秀案例（改写结果 + 诊断）
{good_examples}

## 改进方向
{improvement_direction}

## 约束
- 必须保留 JSON 输出格式：{{"keywords": "...", "sub_queries": "..."}}
- 关键词 2-5 个，子查询 2-3 个
- 明确区分 normalize（口语→术语）和 expand（同义→标准）两种场景
- 增加口语→术语的具体映射示例（不是泛泛说"口语化表达映射为专业术语"，而是给出"光照好不好→日照时数/日照百分率"这样的具体例子）
- 检索角度建议扩展：不止"指标/方法/阈值"，增加"空间分布/影响因素/等级划分/计算公式/历史变化"
- 保持简洁，不超过 400 字

## 仅输出改进后的完整 Prompt 文本（不要 markdown 代码块，不要任何解释）"""


# ═══════════════════════════════════════════════════════════════
#  LLM 调用 & 改写执行
# ═══════════════════════════════════════════════════════════════

def load_test_cases():
    with open(GOLDEN_PATH, encoding="utf-8") as f:
        return json.load(f)


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
    return _embed_model


def call_llm(messages, temperature=0.3, json_mode=False):
    from llm_client import call_llm as _call
    if json_mode:
        return _call(messages, temperature=temperature, stream=False, json_mode=True)
    return _call(messages, temperature=temperature, stream=False)


def call_rewriter(query, prompt_template):
    prompt_str = prompt_template.replace("{query}", query)
    try:
        resp = call_llm([{"role": "user", "content": prompt_str}], temperature=0.3)
        start = resp.find("{")
        end = resp.rfind("}") + 1
        parsed = json.loads(resp[start:end]) if start >= 0 and end > start else {}
    except Exception:
        return [], []
    # 兼容两种输出契约：V3 数组，或旧版逗号/换行字符串
    raw_kw = parsed.get("keywords", [])
    if isinstance(raw_kw, str):
        keywords = [kw.strip() for kw in raw_kw.replace("\n", ",").split(",") if kw.strip()]
    else:
        keywords = [str(kw).strip() for kw in raw_kw if kw and str(kw).strip()]

    raw_sq = parsed.get("sub_queries", [])
    if isinstance(raw_sq, str):
        sub_queries = [sq.strip() for sq in raw_sq.split("\n") if sq.strip()]
    else:
        sub_queries = [str(sq).strip() for sq in raw_sq if sq and str(sq).strip()]

    return keywords, sub_queries


def evaluate_rewrite(query, golden_answer, keywords, sub_queries, rewrite_type="normalize"):
    """V3 对齐评测：感知改写类型，normalize 时 sub_queries 为空不扣分。"""
    eval_prompt = EVAL_PROMPT_V3.format(
        query=query,
        golden_answer=golden_answer[:1200],
        rewrite_type=rewrite_type,
        keywords=", ".join(keywords) if keywords else "(空)",
        sub_queries="\n".join(sub_queries) if sub_queries else "(空)",
    )
    try:
        resp = call_llm([{"role": "user", "content": eval_prompt}], temperature=0.1, json_mode=True)
        start = resp.find("{")
        end = resp.rfind("}") + 1
        parsed = json.loads(resp[start:end]) if start >= 0 and end > start else {}
    except Exception:
        return _empty_eval_result()

    if not parsed:
        return _empty_eval_result()

    # 提取新格式字段
    kw = parsed.get("keyword_evaluation", {})
    cn = parsed.get("canonical_evaluation", {})
    ex = parsed.get("expansion_evaluation", {})
    tc = parsed.get("type_consistency", {})
    fs = parsed.get("final_score", {})

    keyword_score = _safe_float(kw, "keyword_score")
    keyword_precision = _safe_float(kw, "keyword_precision")
    canonical_score = _safe_float(cn, "canonical_score")
    expansion_score = _safe_float(ex, "expansion_score")
    consistency_score = _safe_float(tc, "consistency_score")
    rq_score = _safe_float(fs, "rewrite_quality_score")
    scoring_mode = parsed.get("scoring_mode", "compact")

    # 向后兼容：当 LLM 未正确填 canonical/expansion 但 scoring_mode=compact 时，用 type_consistency 替代
    if scoring_mode == "compact" or rewrite_type in ("none", "normalize"):
        # 精简模式：canonical/expansion 不适用
        canonical_score = canonical_score if canonical_score > 0 else 1.0
        expansion_score = expansion_score if expansion_score > 0 else 1.0

    return {
        "step2_覆盖度": round(keyword_score * 2, 2),
        "step2_精确度": round(keyword_precision * 2, 2),
        "step3_角度多样性": round(expansion_score * 2, 2),
        "step3_语义保真度": round(canonical_score * 2, 2),
        "step4_综合评分": round(rq_score * 10, 2),
        "主要问题": fs.get("analysis", ""),
        "改进建议": kw.get("analysis", ""),
        "scoring_mode": scoring_mode,
        "type_consistency": consistency_score,
        "_new": {
            "keyword_score": keyword_score,
            "keyword_precision": keyword_precision,
            "canonical_score": canonical_score,
            "expansion_score": expansion_score,
            "rewrite_quality_score": rq_score,
            "consistency_score": consistency_score,
            "grade": fs.get("grade", ""),
            "scoring_mode": scoring_mode,
        }
    }


def _safe_float(d, key):
    try:
        return float(d.get(key, 0))
    except (ValueError, TypeError):
        return 0.0


def _empty_eval_result():
    return {
        "step2_覆盖度": 0, "step2_精确度": 0,
        "step3_角度多样性": 0, "step3_语义保真度": 0,
        "step4_综合评分": 0, "主要问题": "eval_error",
        "_new": {"keyword_score": 0, "keyword_precision": 0,
                 "canonical_score": 0, "expansion_score": 0,
                 "rewrite_quality_score": 0, "grade": "eval_error"}
    }


# ═══════════════════════════════════════════════════════════════
#  Ground-Truth 评估（对照 golden_rewrite.json）
# ═══════════════════════════════════════════════════════════════

def compute_gt_term_metrics(rewrite_eval, generated_keywords):
    """对照 rewrite_eval 的分层术语体系评估术语质量。

    required_terms 分为三层（权重合计 0.85）：
      must_have: 召回锚点，必须命中（权重 0.45）
      core_concept: 核心概念，概念正确即可（权重 0.30）
      precision_term: 精确术语，加分（权重 0.10）
    important_terms: 重要补充（权重 0.10）
    optional_terms: 可选加分（权重 0.05）

    加权召回 = 0.45×must_have + 0.30×core + 0.10×precision + 0.10×important + 0.05×optional
    """
    if not rewrite_eval or not generated_keywords:
        return {"term_precision": 0.0, "term_recall": 0.0, "term_f1": 0.0,
                "term_weighted_recall": 0.0,
                "term_hit_must_have": 0, "term_hit_core": 0, "term_hit_precision": 0,
                "term_hit_important": 0, "term_hit_optional": 0,
                "term_must_have_count": 0, "term_core_count": 0, "term_precision_count": 0,
                "term_important_count": 0, "term_optional_count": 0}

    # 解析 required_terms（兼容旧版数组和新版字典）
    req = rewrite_eval.get("required_terms", {})
    if isinstance(req, list):
        # 兼容旧格式：全部视为 core_concept
        must_have, core_concept, precision = [], req, []
    else:
        must_have = [t.lower().strip() for t in req.get("must_have", [])]
        core_concept = [t.lower().strip() for t in req.get("core_concept", [])]
        precision = [t.lower().strip() for t in req.get("precision_term", [])]

    important = [t.lower().strip() for t in rewrite_eval.get("important_terms", [])]
    optional = [t.lower().strip() for t in rewrite_eval.get("optional_terms", [])]
    gen_set = set(kw.lower().strip() for kw in generated_keywords)

    nm, nc, np_, ni, no = len(must_have), len(core_concept), len(precision), len(important), len(optional)

    if not gen_set:
        return {"term_precision": 0.0, "term_recall": 0.0, "term_f1": 0.0,
                "term_weighted_recall": 0.0,
                "term_hit_must_have": 0, "term_hit_core": 0, "term_hit_precision": 0,
                "term_hit_important": 0, "term_hit_optional": 0,
                "term_must_have_count": nm, "term_core_count": nc, "term_precision_count": np_,
                "term_important_count": ni, "term_optional_count": no}

    def _is_match(term, gk_set):
        return any(term == gk or term in gk or gk in term for gk in gk_set)

    def _has_match(gk, vt_set):
        return any(gk == vt or gk in vt or vt in gk for vt in vt_set)

    hit_must_have = sum(1 for t in must_have if _is_match(t, gen_set))
    hit_core = sum(1 for t in core_concept if _is_match(t, gen_set))
    hit_precision = sum(1 for t in precision if _is_match(t, gen_set))
    hit_important = sum(1 for t in important if _is_match(t, gen_set))
    hit_optional = sum(1 for t in optional if _is_match(t, gen_set))

    all_valid = set(must_have + core_concept + precision + important + optional)
    hit_gen = sum(1 for gk in gen_set if _has_match(gk, all_valid))
    precision_val = hit_gen / len(gen_set) if gen_set else 0.0

    total_terms = nm + nc + np_ + ni + no
    total_hit = hit_must_have + hit_core + hit_precision + hit_important + hit_optional
    recall = total_hit / total_terms if total_terms > 0 else 0.0

    wr = 0.0
    if nm > 0:
        wr += 0.45 * hit_must_have / nm
    if nc > 0:
        wr += 0.30 * hit_core / nc
    if np_ > 0:
        wr += 0.10 * hit_precision / np_
    if ni > 0:
        wr += 0.10 * hit_important / ni
    if no > 0:
        wr += 0.05 * hit_optional / no

    f1 = 2 * precision_val * recall / (precision_val + recall) if (precision_val + recall) > 0 else 0.0

    return {"term_precision": round(precision_val, 4), "term_recall": round(recall, 4),
            "term_f1": round(f1, 4),
            "term_weighted_recall": round(wr, 4),
            "term_hit_must_have": hit_must_have, "term_hit_core": hit_core,
            "term_hit_precision": hit_precision,
            "term_hit_important": hit_important, "term_hit_optional": hit_optional,
            "term_must_have_count": nm, "term_core_count": nc, "term_precision_count": np_,
            "term_important_count": ni, "term_optional_count": no}


def compute_gt_sq_similarity(expected_sqs, generated_sqs):
    if not generated_sqs or not expected_sqs:
        return {"sq_precision": 0.0, "sq_recall": 0.0, "sq_f1": 0.0}

    try:
        model = _get_embed_model()
        exp_embs = model.encode(expected_sqs, normalize_embeddings=True)
        gen_embs = model.encode(generated_sqs, normalize_embeddings=True)
    except Exception:
        return {"sq_precision": 0.0, "sq_recall": 0.0, "sq_f1": 0.0}

    sim = np.dot(gen_embs, exp_embs.T)
    precision = float(np.mean(sim.max(axis=1)))
    recall = float(np.mean(sim.max(axis=0)))
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return {"sq_precision": round(precision, 4), "sq_recall": round(recall, 4),
            "sq_f1": round(f1, 4)}


def build_golden_answer(q):
    parts = []
    # ── Rewrite Type ──
    parts.append(f"Gold 改写类型: {q.get('rewrite_type', 'normalize')}")
    # ── Keyword Gold ──
    re = q.get("rewrite_eval", {})
    parts.append("## Keyword Gold")
    req = re.get("required_terms", {})
    if isinstance(req, dict):
        for key, label in [("must_have", "must_have"), ("core_concept", "core_concept"),
                           ("precision_term", "precision_term")]:
            terms = req.get(key, [])
            parts.append(f"{label}: {json.dumps(terms, ensure_ascii=False)}")
    elif isinstance(req, list):
        if req:
            parts.append(f"required: {json.dumps(req, ensure_ascii=False)}")
    for key, label in [("important_terms", "important_terms"), ("optional_terms", "optional_terms")]:
        terms = re.get(key, [])
        parts.append(f"{label}: {json.dumps(terms, ensure_ascii=False)}")

    # ── Canonical Query Gold ──
    cqs = q.get("canonical_queries", [])
    if cqs:
        parts.append("\n## Canonical Query Gold")
        for i, cq in enumerate(cqs, 1):
            parts.append(f"{i}. {cq}")

    # ── Reference Sub Query Gold ──
    ref_sqs = q.get("reference_sub_queries", [])
    if ref_sqs:
        parts.append("\n## Reference Sub Query Gold")
        for i, sq in enumerate(ref_sqs, 1):
            parts.append(f"{i}. {sq}")
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
#  评测执行（并行）
# ═══════════════════════════════════════════════════════════════

def run_evaluation(test_cases, prompt_template, label="eval", fast_mode=False):
    """并行改写 + 评测，返回详细结果、汇总、失败列表。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = []
    all_llm = defaultdict(list)
    all_gt = defaultdict(list)
    failures = []

    print(f"\n{'='*60}")
    mode_str = " [fast, GT-only]" if fast_mode else ""
    print(f"  [{label}]{mode_str} 评测 {len(test_cases)} 条 query_rewrite")
    print(f"{'='*60}")

    def _process_one(q):
        keywords, sub_queries = call_rewriter(q["question"], prompt_template)
        gt_term = compute_gt_term_metrics(q.get("rewrite_eval", {}), keywords)
        gt_sq = compute_gt_sq_similarity(q.get("reference_sub_queries", []), sub_queries)
        gt_metrics = {**gt_term, **gt_sq}

        if fast_mode:
            scores = {"step4_综合评分": gt_metrics.get("term_f1", 0) * 10,
                      "step2_覆盖度": gt_metrics.get("term_recall", 0) * 2,
                      "step2_精确度": gt_metrics.get("term_precision", 0) * 2,
                      "step3_角度多样性": 0, "step3_语义保真度": 0}
        else:
            golden = build_golden_answer(q)
            rwt = q.get("rewrite_type", "normalize")
            scores = evaluate_rewrite(q["question"], golden, keywords, sub_queries, rewrite_type=rwt)

        return q, keywords, sub_queries, gt_metrics, scores

    workers = 8
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_process_one, q): q["id"] for q in test_cases}
        for i, f in enumerate(as_completed(futures), 1):
            q, keywords, sub_queries, gt_metrics, scores = f.result()
            qid = q["id"]

            results.append({
                "id": qid, "question": q["question"],
                "keywords": keywords, "sub_queries": sub_queries,
                "scores": scores, "gt_metrics": gt_metrics,
                "rewrite_type": q.get("rewrite_type", "?"),
            })

            problem = scores.get("主要问题", "")
            if problem and problem != "无":
                failures.append({
                    "id": qid, "question": q["question"],
                    "problem": problem,
                    "suggestion": scores.get("改进建议", ""),
                    "keywords": keywords, "sub_queries": sub_queries,
                    "gt_metrics": gt_metrics,
                    "rewrite_type": q.get("rewrite_type", "?"),
                })

            for k, v in scores.items():
                if isinstance(v, (int, float)):
                    all_llm[k].append(v)
            for k, v in gt_metrics.items():
                if isinstance(v, (int, float)):
                    all_gt[k].append(v)

            if i % 10 == 0:
                print(f"  [{i}/{len(test_cases)}] 已完成")

    # ── 汇总 ──
    def summarize(agg_dict, precision=2):
        return {k: {"mean": round(np.mean(v), precision),
                    "median": round(np.median(v), precision),
                    "min": round(min(v), precision),
                    "max": round(max(v), precision),
                    "std": round(np.std(v), precision)}
                for k, v in agg_dict.items()}

    summary = {**summarize(all_llm), "_gt": summarize(all_gt, 4)}

    # 分 lex/sem
    lex_llm = defaultdict(list)
    sem_llm = defaultdict(list)
    for r in results:
        target = lex_llm if r["rewrite_type"] == "normalize" else sem_llm
        for k, v in r["scores"].items():
            if isinstance(v, (int, float)):
                target[k].append(v)

    # ── 打印 ──
    print(f"\n  ── {label} LLM-Judge 汇总 ──")
    print(f"  {'指标':<16s} {'均值':>6s} {'中位':>6s} {'最低':>6s} {'最高':>6s}")
    for k in ["step2_覆盖度", "step2_精确度", "step3_角度多样性", "step3_语义保真度", "step4_综合评分"]:
        s = summary.get(k, {})
        print(f"  {k:<14s} {s.get('mean',0):>6.2f} {s.get('median',0):>6.2f} "
              f"{s.get('min',0):>6.2f} {s.get('max',0):>6.2f}")

    gt = summary.get("_gt", {})
    print(f"\n  ── {label} GT 指标 ──")
    for key, name in [("term_f1","术语F1"), ("sq_f1","子查询F1")]:
        s = gt.get(key, {})
        print(f"  {name:<10s} mean={s.get('mean',0):.4f} median={s.get('median',0):.4f}")

    if lex_llm.get("step4_综合评分") and sem_llm.get("step4_综合评分"):
        print(f"  normalize: {np.mean(lex_llm['step4_综合评分']):.2f}  "
              f"expand: {np.mean(sem_llm['step4_综合评分']):.2f}")

    return results, summary, failures


# ═══════════════════════════════════════════════════════════════
#  数据驱动的失败分析（PDF 风格：逐题归类）
# ═══════════════════════════════════════════════════════════════

FAILURE_CATEGORIES = {
    "term_gap": "口语/同义词未映射到专业术语",
    "wrong_term": "术语选择错误（选到了不相关的术语）",
    "generic_kw": "关键词过于泛化，不是可检索的专业术语",
    "angle_narrow": "子查询角度单一（仅指标/方法/阈值）",
    "semantic_drift": "子查询偏离原问题核心意图",
    "missing_angle": "遗漏了参考答案中的关键检索角度",
    "over_generation": "生成了过多无关或冗余的关键词/子查询",
}


def analyze_failures_v2(failures, results, fast_mode=False):
    """PDF 风格：逐题归类失败模式，附带具体案例和改进建议。

    Returns:
        failure_report: 结构化失败分析报告
        category_examples: {category: [示例列表]}
    """
    if not failures:
        return "当前 prompt 表现优秀，无明显失败模式。", {}

    # 分类失败
    categorized = defaultdict(list)
    for f in failures:
        problem = f["problem"].lower()
        matched = False
        # 关键词问题
        if any(w in problem for w in ["关键词", "术语", "映射", "口语", "同义"]):
            if any(w in problem for w in ["缺失", "遗漏", "不足", "未"]):
                categorized["term_gap"].append(f)
                matched = True
            elif any(w in problem for w in ["错误", "不对", "选错"]):
                categorized["wrong_term"].append(f)
                matched = True
            elif any(w in problem for w in ["泛化", "太泛", "笼统"]):
                categorized["generic_kw"].append(f)
                matched = True
        # 角度问题
        if not matched and any(w in problem for w in ["角度", "单一", "多样性", "检索角度"]):
            categorized["angle_narrow"].append(f)
            matched = True
        # 语义偏移
        if not matched and any(w in problem for w in ["偏离", "意图", "语义", "保真", "核心"]):
            categorized["semantic_drift"].append(f)
            matched = True
        # 遗漏角度
        if not matched and any(w in problem for w in ["遗漏", "缺少", "没覆盖"]):
            categorized["missing_angle"].append(f)
            matched = True
        # 过度生成
        if not matched and any(w in problem for w in ["过多", "冗余", "无关"]):
            categorized["over_generation"].append(f)
            matched = True
        # fallback
        if not matched:
            categorized["term_gap"].append(f)

    # 构建报告
    parts = []
    category_examples = {}

    # 按失败数降序排列
    sorted_cats = sorted(categorized.items(), key=lambda x: -len(x[1]))

    for cat, items in sorted_cats:
        desc = FAILURE_CATEGORIES.get(cat, cat)
        parts.append(f"## {desc} ({len(items)} 题)")
        examples = items[:5]
        for ex in examples:
            parts.append(f"  - [{ex['id']}] {ex['question'][:50]}")
            parts.append(f"    生成关键词: {ex['keywords']}")
            parts.append(f"    问题: {ex['problem'][:80]}")
            if ex.get("suggestion"):
                parts.append(f"    建议: {ex['suggestion'][:80]}")
        category_examples[cat] = items

    # 低分题目
    low_score = [r for r in results if r["scores"].get("step4_综合评分", 10) < 6]
    if low_score:
        parts.append(f"\n## 综合低分 ({len(low_score)} 题，综合<6)")
        parts.append(f"失败题目: {[r['id'] for r in low_score]}")

    return "\n".join(parts), category_examples


# ═══════════════════════════════════════════════════════════════
#  数据驱动的候选 Prompt 生成
# ═══════════════════════════════════════════════════════════════

def generate_candidates_v2(current_prompt, failure_report, category_examples, results, n_candidates=3):
    """数据驱动：基于实际失败案例生成针对性候选。

    不再使用 5 个硬编码方向，而是：
    1. 按失败严重程度排序类别
    2. 对 top-N 类别各生成 1 个候选
    3. 每个候选附带该类别具体失败案例
    """
    sorted_cats = sorted(category_examples.items(), key=lambda x: -len(x[1]))
    if not sorted_cats:
        print("  无失败模式，跳过候选生成。")
        return []

    # 优秀案例
    good = [r for r in results if r["scores"].get("step4_综合评分", 0) >= 7][:3]
    good_examples = "\n".join([
        f"  [{r['id']}] Q: {r['question'][:50]}\n  keywords: {r['keywords']}\n  sub_queries: {r['sub_queries']}"
        for r in good
    ]) if good else "（暂无高分示例）"

    # 对 top-N 类别生成候选
    candidates = []
    for i in range(min(n_candidates, len(sorted_cats))):
        cat, items = sorted_cats[i]
        desc = FAILURE_CATEGORIES.get(cat, cat)

        # 构建该类别具体案例
        case_examples = []
        for ex in items[:3]:
            case_examples.append(
                f"  [{ex['id']}] 问题: {ex['question']}\n"
                f"  生成关键词: {ex['keywords']}\n"
                f"  生成子查询: {ex['sub_queries']}\n"
                f"  诊断: {ex['problem']}\n"
                f"  建议: {ex.get('suggestion', '无')}"
            )
        failure_examples = "\n\n".join(case_examples)

        direction = f"重点解决 [{desc}] 问题（共 {len(items)} 题），同时保持其他维度不退化。"

        gen_prompt = CANDIDATE_GEN_V2.format(
            current_prompt=current_prompt,
            failure_analysis=failure_report[:1500],
            failure_examples=failure_examples,
            good_examples=good_examples,
            improvement_direction=direction,
        )
        try:
            raw = call_llm([{"role": "user", "content": gen_prompt}], temperature=0.7)
            # 清理可能的 markdown 代码块包裹
            candidate_text = raw.strip()
            if candidate_text.startswith("```"):
                lines = candidate_text.split("\n")
                # 去掉首行的 ``` 和末行的 ```
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                candidate_text = "\n".join(lines)
            candidates.append({
                "id": f"candidate_{i+1}",
                "category": cat,
                "direction": f"针对 [{desc}]（{len(items)} 题）",
                "prompt": candidate_text,
            })
            print(f"  [候选{i+1}] 针对 [{desc}]（{len(items)} 题）已生成")
        except Exception as e:
            print(f"  [候选{i+1}] 生成失败: {e}")

    # 额外：综合改进候选（融合所有失败模式）
    if len(sorted_cats) > 1:
        all_cases = []
        for cat, items in sorted_cats[:3]:
            for ex in items[:2]:
                all_cases.append(
                    f"  [{ex['id']}] {ex['question'][:50]}\n"
                    f"  keywords={ex['keywords']} | 问题: {ex['problem'][:60]}"
                )
        direction = "综合解决上述所有失败模式，全面提升关键词覆盖度、精确度、角度多样性、语义保真度。"

        gen_prompt = CANDIDATE_GEN_V2.format(
            current_prompt=current_prompt,
            failure_analysis=failure_report[:1500],
            failure_examples="\n\n".join(all_cases),
            good_examples=good_examples,
            improvement_direction=direction,
        )
        try:
            raw = call_llm([{"role": "user", "content": gen_prompt}], temperature=0.7)
            candidate_text = raw.strip()
            if candidate_text.startswith("```"):
                lines = candidate_text.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                candidate_text = "\n".join(lines)
            idx = len(candidates) + 1
            candidates.append({
                "id": f"candidate_{idx}",
                "category": "综合",
                "direction": "综合所有失败模式全面改进",
                "prompt": candidate_text,
            })
            print(f"  [候选{idx}] 综合改进已生成")
        except Exception as e:
            print(f"  [候选{idx}] 生成失败: {e}")

    return candidates


# ═══════════════════════════════════════════════════════════════
#  迭代精炼（PDF 风格：Round 1 → 分析残留 → Round 2）
# ═══════════════════════════════════════════════════════════════

def iterative_refinement(test_cases, baseline_summary, baseline_failures, baseline_results,
                         fast_mode=False, max_rounds=2):
    """迭代精炼：生成候选 → 评测 → 选最优 → 分析残留问题 → 再次生成 → 评测。"""
    all_candidates = []
    round_reports = []

    current_best_prompt = CURRENT_PROMPT
    current_best_score = baseline_summary.get("step4_综合评分", {}).get("mean", 0)
    current_best_name = "baseline"
    current_results = baseline_results
    current_failures = baseline_failures

    for round_num in range(1, max_rounds + 1):
        print(f"\n{'='*60}")
        print(f"  Round {round_num}: 迭代精炼")
        print(f"{'='*60}")

        # 分析当前失败
        if fast_mode:
            low_recall = [r for r in current_results
                         if r["gt_metrics"].get("term_recall", 1) < 0.5]
            if low_recall:
                failure_report = "## 术语召回不足\n" + "\n".join(
                    f"  - [{r['id']}] {r['question'][:40]} → {r['keywords']}" for r in low_recall[:5])
                category_examples = {"term_gap": [
                    {"id": r["id"], "question": r["question"],
                     "keywords": r["keywords"], "sub_queries": r["sub_queries"],
                     "problem": f"术语召回率={r['gt_metrics'].get('term_recall',0):.2f}",
                     "suggestion": "需要增加口语→术语映射", "rewrite_type": r.get("rewrite_type","?")}
                    for r in low_recall[:10]
                ]}
            else:
                failure_report = "无明显失败模式"
                category_examples = {}
        else:
            failure_report, category_examples = analyze_failures_v2(current_failures, current_results, fast_mode)

        print(f"\n[Round {round_num} 失败分析]\n{failure_report[:600]}")

        # 生成候选
        if not category_examples:
            print(f"  Round {round_num}: 无失败模式，停止迭代。")
            break

        n_candidates = 3 if round_num == 1 else 2
        candidates = generate_candidates_v2(
            current_best_prompt, failure_report, category_examples, current_results,
            n_candidates=n_candidates,
        )

        if not candidates:
            print(f"  Round {round_num}: 候选生成失败，停止迭代。")
            break

        # 评测候选
        round_best = None
        round_best_score = -1
        round_best_name = ""
        round_best_data = None

        for cand in candidates:
            cand_label = f"R{round_num}_{cand['id']}"
            print(f"\n  ── 评测 {cand_label}: {cand['direction'][:50]}...")
            res, summary, fails = run_evaluation(test_cases, cand["prompt"], cand_label, fast_mode=fast_mode)

            score = summary.get("step4_综合评分", {}).get("mean", 0)
            cand["results"] = res
            cand["summary"] = summary
            cand["failures"] = fails
            cand["label"] = cand_label
            all_candidates.append(cand)

            if score > round_best_score:
                round_best_score = score
                round_best_name = cand_label
                round_best_data = cand
                round_best = cand

        # 更新最优
        if round_best_score > current_best_score:
            improvement = round_best_score - current_best_score
            print(f"\n  Round {round_num} 最优: {round_best_name} (综合={round_best_score:.2f}, Δ=+{improvement:.2f})")
            current_best_score = round_best_score
            current_best_name = round_best_name
            current_best_prompt = round_best["prompt"]
            current_results = round_best["results"]
            current_failures = round_best["failures"]

            round_reports.append({
                "round": round_num,
                "best_name": round_best_name,
                "score": round_best_score,
                "improvement": improvement,
                "direction": round_best["direction"],
            })
        else:
            print(f"\n  Round {round_num}: 无改进 (最优={round_best_score:.2f} ≤ 当前={current_best_score:.2f})，停止迭代。")
            break

    return all_candidates, round_reports, current_best_prompt, current_best_score


# ═══════════════════════════════════════════════════════════════
#  报告 & 应用
# ═══════════════════════════════════════════════════════════════

# 向后兼容的 LLM-Judge 分数字段映射
SCORE_MAP_LEGACY = {
    "step2_覆盖度": "关键词覆盖度",
    "step2_精确度": "关键词精确度",
    "step3_角度多样性": "改写角度多样性",
    "step3_语义保真度": "改写语义保真度",
    "step4_综合评分": "综合评分",
}


def print_calibration_report(all_results_map, baseline_results):
    """校准报告：LLM 评分 vs GT 指标的相关性。"""
    print(f"\n  ── LLM-as-Judge 校准（综合评分 vs GT 指标）──")

    for name, results in all_results_map.items():
        if not results:
            continue
        llm_scores = []
        gt_term = []
        gt_sq = []
        for r in results:
            llm_scores.append(r["scores"].get("step4_综合评分", 0))
            gt_term.append(r["gt_metrics"].get("term_f1", 0))
            gt_sq.append(r["gt_metrics"].get("sq_f1", 0))

        n = len(llm_scores)
        if n < 2:
            continue
        t_corr = round(np.corrcoef(llm_scores, gt_term)[0, 1], 4)
        s_corr = round(np.corrcoef(llm_scores, gt_sq)[0, 1], 4)
        print(f"  [{name}] LLM均值={np.mean(llm_scores):.2f} | "
              f"Corr(termF1)={t_corr:.4f} Corr(sqF1)={s_corr:.4f}")

        # 大偏差样本
        gaps = []
        for r in results:
            llm_n = r["scores"].get("step4_综合评分", 0) / 10.0
            gt = r["gt_metrics"].get("term_f1", 0)
            if abs(llm_n - gt) > 0.3:
                gaps.append((r["id"], r["scores"].get("step4_综合评分", 0), gt))
        if gaps:
            print(f"    大偏差样本 (|LLM_norm - termF1| > 0.3, {len(gaps)} 题):")
            for qid, llm, gt in gaps[:5]:
                print(f"      {qid}: LLM={llm:.1f} termF1={gt:.2f} Δ={abs(llm/10-gt):.2f}")


def print_apo_report_v2(baseline_summary, baseline_results, baseline_failures,
                        all_candidates, round_reports, best_prompt, best_score, best_name):
    """PDF 风格的前后对比报告。"""
    print(f"\n\n{'='*70}")
    print(f"  APO V2 Prompt 优化报告（prompt_evolution 方法论）")
    print(f"{'='*70}")

    # ── 迭代过程 ──
    if round_reports:
        print(f"\n  ── 迭代精炼过程 ──")
        for rr in round_reports:
            print(f"  Round {rr['round']}: {rr['best_name']} → 综合={rr['score']:.2f} "
                  f"(Δ=+{rr['improvement']:.2f}) [{rr['direction'][:40]}]")

    # ── 全部排名 ──
    print(f"\n  ── 全部候选排名 ──")
    baseline_mean = baseline_summary.get("step4_综合评分", {}).get("mean", 0)

    ranked = [("baseline", baseline_mean, {
        "summary": baseline_summary, "direction": "当前 prompt",
        "results": baseline_results, "failures": baseline_failures,
    })]
    for cand in all_candidates:
        score = cand["summary"].get("step4_综合评分", {}).get("mean", 0)
        ranked.append((cand.get("label", cand["id"]), score, cand))
    ranked.sort(key=lambda x: -x[1])

    print(f"  {'Rank':<5s} {'名称':<22s} {'综合':>6s} {'覆盖度':>6s} {'精确度':>6s} "
          f"{'多样性':>6s} {'保真度':>6s} {'Δbase':>8s}")
    print(f"  {'-'*70}")
    for rank, (name, score, data) in enumerate(ranked, 1):
        s = data.get("summary", data) if name == "baseline" else data["summary"]
        cov = s.get("step2_覆盖度", {}).get("mean", 0)
        prec = s.get("step2_精确度", {}).get("mean", 0)
        div = s.get("step3_角度多样性", {}).get("mean", 0)
        fid = s.get("step3_语义保真度", {}).get("mean", 0)
        delta = f"+{score - baseline_mean:.2f}" if score >= baseline_mean else f"{score - baseline_mean:.2f}"
        marker = ""
        if name == "baseline":
            marker = " ← baseline"
        elif rank == 1 and name != "baseline":
            marker = " ★ BEST"
        print(f"  {rank:<5d} {name:<22s} {score:>6.2f} {cov:>6.2f} {prec:>6.2f} "
              f"{div:>6.2f} {fid:>6.2f} {delta:>8s}{marker}")

    # ── GT 指标排名 ──
    print(f"\n  ── GT 指标排名（对照 golden_rewrite 标签）──")
    print(f"  {'名称':<22s} {'termF1':>8s} {'sqF1':>8s} {'termR':>8s}")
    print(f"  {'-'*48}")
    for name, score, data in ranked:
        s = baseline_summary if name == "baseline" else data.get("summary", {})
        gt = s.get("_gt", {})
        tf1 = gt.get("term_f1", {}).get("mean", 0)
        sf1 = gt.get("sq_f1", {}).get("mean", 0)
        tr = gt.get("term_recall", {}).get("mean", 0)
        print(f"  {name:<22s} {tf1:>8.4f} {sf1:>8.4f} {tr:>8.4f}")

    # ── 最优 Prompt ──
    if best_name != "baseline":
        print(f"\n  ── 最优 Prompt: {best_name} ──")
        print(f"  综合评分: {best_score:.2f} (baseline: {baseline_mean:.2f}, Δ=+{best_score - baseline_mean:.2f})")
        print(f"\n  [最优 Prompt 全文]:")
        print(f"  {'-'*60}")
        for line in best_prompt.split("\n"):
            print(f"  {line}")
        print(f"  {'-'*60}")

    # ── 前后对比：维度级 ──
    print(f"\n  ── 前后对比：维度级改进 ──")
    print(f"  {'维度':<18s} {'baseline':>8s} {'最优':>8s} {'改进':>8s}")
    print(f"  {'-'*46}")
    for new_k, old_k in [("step2_覆盖度","关键词覆盖度"), ("step2_精确度","关键词精确度"),
                          ("step3_角度多样性","改写角度多样性"), ("step3_语义保真度","改写语义保真度"),
                          ("step4_综合评分","综合评分")]:
        bl = baseline_summary.get(new_k, {}).get("mean", 0)
        if best_name != "baseline":
            best_data = next((c for c in all_candidates if c.get("label", c["id"]) == best_name), None)
            opt = best_data["summary"].get(new_k, {}).get("mean", 0) if best_data else bl
        else:
            opt = bl
        diff = f"+{opt - bl:.2f}" if opt >= bl else f"{opt - bl:.2f}"
        print(f"  {new_k:<18s} {bl:>8.2f} {opt:>8.2f} {diff:>8s}")

    # ── LLM-Judge 校准 ──
    all_results_map = {"baseline": baseline_results}
    for cand in all_candidates:
        name = cand.get("label", cand["id"])
        all_results_map[name] = cand.get("results", [])
    print_calibration_report(all_results_map, baseline_results)

    # ── 质量检查 ──
    print(f"\n  ── 质量检查 ──")
    best_results = baseline_results
    if best_name != "baseline":
        best_data = next((c for c in all_candidates if c.get("label", c["id"]) == best_name), None)
        if best_data:
            best_results = best_data.get("results", baseline_results)

    lex = [r for r in best_results if r.get("rewrite_type") == "normalize"]
    sem = [r for r in best_results if r.get("rewrite_type") == "expand"]
    if lex and sem:
        lex_s = [r["scores"].get("step4_综合评分", 0) for r in lex]
        sem_s = [r["scores"].get("step4_综合评分", 0) for r in sem]
        print(f"  normalize 均分: {np.mean(lex_s):.2f} (n={len(lex)})")
        print(f"  expand 均分: {np.mean(sem_s):.2f} (n={len(sem)})")
    zero_s = [r for r in best_results if r["scores"].get("step4_综合评分", 0) < 3]
    if zero_s:
        print(f"  严重低分(<3): {len(zero_s)} 题 - {[r['id'] for r in zero_s]}")
    else:
        print(f"  无严重低分(<3)题目")

    # ── 残留失败 ──
    best_fails = baseline_failures
    if best_name != "baseline":
        best_data = next((c for c in all_candidates if c.get("label", c["id"]) == best_name), None)
        if best_data:
            best_fails = best_data.get("failures", baseline_failures)
    if best_fails:
        print(f"\n  ── 残留失败 ({len(best_fails)} 题) ──")
        for f in best_fails[:8]:
            print(f"  [{f['id']}] {f['question'][:50]}: {f['problem'][:80]}")


def apply_best_prompt(best_prompt):
    """将最优 prompt 写入 query_rewriter.py。"""
    import re
    target = BASE_DIR / "query_rewriter.py"
    with open(target, "r", encoding="utf-8") as f:
        content = f.read()

    pattern = r'(REWRITE_PROMPT = ChatPromptTemplate\.from_messages\(\[\s*\n\s*\("user",\s*""")(.*?)("""\)\])'
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        print("无法解析 REWRITE_PROMPT 结构，请手动替换。")
        return

    new_block = f'{match.group(1)}{best_prompt}{match.group(3)}'
    new_content = content[:match.start()] + new_block + content[match.end():]

    with open(target, "w", encoding="utf-8") as f:
        f.write(new_content)
    print(f"\n已更新 {target}")


# ═══════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="APO V2 — Query Rewriter Prompt 优化（prompt_evolution 方法论）")
    parser.add_argument("--eval-only", action="store_true", help="仅评测当前 prompt")
    parser.add_argument("--fast", action="store_true", help="快速模式：跳过 LLM-Judge（仅 GT 指标）")
    parser.add_argument("--apply", action="store_true", help="将最优 prompt 写入 query_rewriter.py")
    parser.add_argument("--limit", type=int, default=None, help="限制评测题目数")
    parser.add_argument("--rounds", type=int, default=2, help="迭代精炼轮数 (default: 2)")
    parser.add_argument("--output", type=str, default=None, help="保存评测报告 JSON 路径")
    args = parser.parse_args()

    has_key = (os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
               or "sk-0596c163fcb64ed788f6ab5b651af91c")
    if not has_key:
        print("请设置 LLM_API_KEY 或 DEEPSEEK_API_KEY 环境变量")
        sys.exit(1)

    print("=" * 60)
    print("  APO V2 — Query Rewriter Prompt 自动优化")
    print("  (prompt_evolution 方法论: 强制分步推理 + 容错机制 + 迭代精炼)")
    print("=" * 60)

    test_cases = load_test_cases()
    if args.limit:
        test_cases = test_cases[:args.limit]
    print(f"\n测试集: {len(test_cases)} 题 (normalize: {sum(1 for q in test_cases if q.get('rewrite_type')=='normalize')}, "
          f"expand: {sum(1 for q in test_cases if q.get('rewrite_type')=='expand')})")

    fast = args.fast

    # Step 1: Baseline 评测
    print(f"\n[Phase 1] Baseline 评测")
    results, baseline_summary, failures = run_evaluation(test_cases, CURRENT_PROMPT, "baseline", fast_mode=fast)

    if args.eval_only:
        print(f"\n[失败模式分析]")
        failure_report, cat_examples = analyze_failures_v2(failures, results, fast)
        print(failure_report)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump({
                    "summary": baseline_summary, "results": [
                        {"id": r["id"], "keywords": r["keywords"], "sub_queries": r["sub_queries"],
                         "scores": r["scores"], "gt_metrics": r["gt_metrics"]} for r in results
                    ],
                    "failures": [{"id": f["id"], "problem": f["problem"]} for f in failures],
                }, f, ensure_ascii=False, indent=2)
            print(f"\n报告已保存至: {args.output}")
        return

    # Step 2: 失败分析
    print(f"\n[Phase 2] 失败模式分析")
    failure_report, category_examples = analyze_failures_v2(failures, results, fast)
    print(failure_report[:800])

    # Step 3: 数据驱动候选生成 + 迭代精炼
    print(f"\n[Phase 3] 候选生成 + 迭代精炼 (max {args.rounds} rounds)")
    all_candidates, round_reports, best_prompt, best_score = iterative_refinement(
        test_cases, baseline_summary, failures, results,
        fast_mode=fast, max_rounds=args.rounds,
    )

    best_name = round_reports[-1]["best_name"] if round_reports else "baseline"

    # Step 4: 报告
    print(f"\n[Phase 4] 生成报告")
    print_apo_report_v2(
        baseline_summary, results, failures,
        all_candidates, round_reports,
        best_prompt, best_score, best_name,
    )

    if args.apply:
        if best_name != "baseline":
            apply_best_prompt(best_prompt)
        else:
            print("最优即为当前 prompt，无需更新。")

    if args.output:
        out = {
            "baseline": baseline_summary,
            "rounds": round_reports,
            "best_name": best_name,
            "best_score": best_score,
            "best_prompt": best_prompt if best_name != "baseline" else CURRENT_PROMPT,
            "candidates": [{
                "name": c.get("label", c["id"]), "direction": c["direction"],
                "summary": c["summary"], "prompt": c["prompt"],
            } for c in all_candidates],
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\n报告已保存至: {args.output}")


if __name__ == "__main__":
    main()
