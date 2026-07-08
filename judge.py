"""
OOD Judge

四层判断：
  1. 信号层: 无结果 → reject
  2. 高置信层: similarity >= 0.75 → answer（极高置信度，OOD 仅 3/20 误过）
  3. 分数层: similarity < 0.65 → reject（OOD 6/20 在此层，In-domain 误拒 ~6%）
  4. LLM 层: 模糊区间 [0.65, 0.75) → LLM 细判（约 25% 查询）

阈值来源: 2026-07-02 对 golden_set_v2 200 题实测标定
  OOD sim ∈ [0.51, 0.79], In-domain sim ∈ [0.49, 0.91]
  两者高度重叠，无法纯阈值分离，需 LLM 层处理模糊区间。

用法:
  from judge import judge
  decision = judge("大豆区划用什么指标", results)
"""

import json
from langchain_core.prompts import ChatPromptTemplate
from llm_client import call_llm

JUDGE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是一个农业气候区划领域的检索质量判定器。判断以下参考资料能否回答用户问题。

## 判定标准
- YES: 参考资料包含问题的直接答案，或包含可推断出答案的关键信息（即使不完整也可以判YES）
- NO: 参考资料与问题**完全无关**（话题不匹配、询问域外内容如其他省份/作物/政策/未来年份）
- PARTIAL: 参考资料话题匹配但**缺少核心信息**（如问了具体数值但资料没给出、问了对比但只有单方信息）

## 特别注意
- 如果参考资料包含问题涉及的概念、方法、指标的说明，即使不是逐字回答也应判 YES
- 只有以下情况才判 NO：询问知识库范围外的内容（其他省份的作物、未覆盖的年份、农业政策等）
- 如果参考资料来自相关文档但被截断或未包含完整细节 → PARTIAL

## 用户问题
{query}

## 参考资料
{context}

输出 JSON（不要输出其他内容）：
{{"decision": "YES"|"NO"|"PARTIAL", "reason": "一句话理由", "confidence": 0.0-1.0}}"""),
])

# 2026-07-02 标定: OOD P95=0.79 但 In-domain P5=0.65, 重叠严重
HIGH_SIM = 0.75   # ≥此值直接放行（OOD 3/20 误过, In-domain ~69% 快速通过）
LOW_SIM = 0.65    # <此值直接拒绝（OOD 6/20 在此层, In-domain 误拒 ~6%）


def judge(query: str, results: list[dict]) -> dict:
    """
    判定检索结果是否足以回答农业区划问题。

    Returns:
        {"decision": "answer"|"reject"|"fallback", "reason": "...",
         "confidence": float, "method": "signal"|"score"|"high_sim"|"llm"}
    """
    if not results:
        return {"decision": "reject", "reason": "无检索结果", "confidence": 1.0, "method": "signal"}

    top1 = results[0]
    # 优先用 dense_similarity（余弦相似度），fallback 到 similarity
    sim = top1.get("dense_similarity", top1.get("similarity", 0))

    # Layer 2: 高置信度放行
    if sim >= HIGH_SIM:
        return {
            "decision": "answer",
            "reason": f"similarity={sim:.3f} >= {HIGH_SIM}, 高置信度",
            "confidence": float(min(1.0, sim)),
            "method": "high_sim",
        }

    # Layer 3: 低分直接拒绝
    if sim < LOW_SIM:
        return {
            "decision": "reject",
            "reason": f"similarity={sim:.3f} < {LOW_SIM}",
            "confidence": 0.9,
            "method": "score",
        }

    # Layer 4: LLM 细判 — 模糊区间 [0.65, 0.75)，约 25% 查询
    try:
        return _llm_judge(query, results[:3])
    except Exception:
        return {"decision": "fallback", "reason": f"similarity={sim:.3f} 模糊区间", "confidence": 0.5, "method": "score"}


def _llm_judge(query: str, results: list[dict]) -> dict:
    """LLM 精细判定。"""
    context_parts = []
    for i, r in enumerate(results, 1):
        meta = r.get("metadata", {})
        src = meta.get("source_file", "?")
        sim = r.get("similarity", 0)
        context_parts.append(f"[参考{i}] 来源={src} 分数={sim:.4f}\n{r['content'][:300]}")

    prompt = JUDGE_PROMPT.format(context="\n\n".join(context_parts), query=query)
    prompt_str = prompt.to_string() if hasattr(prompt, 'to_string') else str(prompt)
    response = call_llm([{"role": "user", "content": prompt_str}], temperature=0.1, stream=False)

    try:
        start = response.find("{")
        end = response.rfind("}") + 1
        parsed = json.loads(response[start:end]) if start >= 0 and end > start else {}
    except (json.JSONDecodeError, ValueError):
        parsed = {}

    decision_map = {"YES": "answer", "NO": "reject", "PARTIAL": "answer"}
    return {
        "decision": decision_map.get(parsed.get("decision", "NO"), "reject"),
        "reason": parsed.get("reason", "LLM 判定"),
        "confidence": parsed.get("confidence", 0.5),
        "method": "llm",
    }
