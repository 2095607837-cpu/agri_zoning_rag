"""
OOD Judge（LangChain 实现）

三层判断：
  1. 信号层: 无结果或多源匹配时预判
  2. 分数层: similarity < 0.46 → reject
  3. LLM 层: ChatPromptTemplate → LLM chain → YES/NO/PARTIAL

用法:
  from judge import judge
  decision = judge("大豆区划用什么指标", results)
"""

import json
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from llm_client import call_llm

JUDGE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是一个农业气候区划领域的检索质量判定器。判断以下参考资料能否回答用户问题。

## 判定标准
- YES: 参考资料包含问题的直接答案或可推断出答案的关键信息
- NO: 参考资料与问题无关，或仅表面话题沾边但内容不包含答案
- PARTIAL: 参考资料部分相关，但缺少关键细节

## 用户问题
{query}

## 参考资料
{context}

输出 JSON（不要输出其他内容）：
{{"decision": "YES"|"NO"|"PARTIAL", "reason": "一句话理由", "confidence": 0.0-1.0}}"""),
])


def judge(query: str, results: list[dict]) -> dict:
    """
    判定检索结果是否足以回答农业区划问题。

    Returns:
        {"decision": "answer"|"reject"|"fallback", "reason": "...",
         "confidence": float, "method": "signal"|"score"|"llm"}
    """
    if not results:
        return {"decision": "reject", "reason": "无检索结果", "confidence": 1.0, "method": "signal"}

    top1 = results[0]
    sim = top1.get("similarity", 0)

    # Layer 2: 分数层（基于 Chroma 真实 L2 距离，sim=1/(1+L2)）
    # 实测: In-domain sim∈[0.74, 0.89], OOD sim∈[0.63, 0.71], 阈值 0.72 可完美分离
    if sim < 0.70:
        return {
            "decision": "reject",
            "reason": f"similarity={sim:.3f} < 0.46",
            "confidence": 0.9,
            "method": "score",
        }

    # Layer 3: LLM 细判
    try:
        return _llm_judge(query, results[:3])
    except Exception:
        if sim >= 0.65:
            return {"decision": "answer", "reason": f"similarity={sim:.3f} >= 0.65", "confidence": 0.7, "method": "score"}
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

    decision_map = {"YES": "answer", "NO": "reject", "PARTIAL": "fallback"}
    return {
        "decision": decision_map.get(parsed.get("decision", "NO"), "reject"),
        "reason": parsed.get("reason", "LLM 判定"),
        "confidence": parsed.get("confidence", 0.5),
        "method": "llm",
    }
