"""
OOD Judge

四层判断：
  1. 信号层: 无结果 → reject
  2. 高置信层: similarity >= 0.60 → answer（跳过 LLM，基于真实数据标定）
  3. 分数层: similarity < 0.46 → reject
  4. LLM 层: 模糊区间 [0.46, 0.60) → LLM 细判

阈值来源: calibrate_judge_threshold.py 对 200 题 golden set 实测标定
  （相似度公式为 1.0 - cosine_distance，重新运行校准脚本以更新阈值）

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
         "confidence": float, "method": "signal"|"score"|"high_sim"|"llm"}
    """
    if not results:
        return {"decision": "reject", "reason": "无检索结果", "confidence": 1.0, "method": "signal"}

    top1 = results[0]
    # 优先用 dense_similarity（余弦相似度，不受 reranker 影响），fallback 到 similarity
    sim = top1.get("dense_similarity", top1.get("similarity", 0))

    # Layer 2: 高置信度放行（sim >= 0.63, 新标定: OOD max=0.588, In-domain min=0.602, 完全可分）
    if sim >= 0.63:
        return {
            "decision": "answer",
            "reason": f"similarity={sim:.3f} >= 0.63, 高置信度",
            "confidence": float(min(1.0, sim)),
            "method": "high_sim",
        }

    # Layer 3: 分数层拒绝（OOD max=0.588, 低于此值全为 OOD）
    if sim < 0.59:
        return {
            "decision": "reject",
            "reason": f"similarity={sim:.3f} < 0.59",
            "confidence": 0.9,
            "method": "score",
        }

    # Layer 4: LLM 细判（仅 sim ∈ [0.59, 0.63) 的低置信度 in-domain）
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

    decision_map = {"YES": "answer", "NO": "reject", "PARTIAL": "fallback"}
    return {
        "decision": decision_map.get(parsed.get("decision", "NO"), "reject"),
        "reason": parsed.get("reason", "LLM 判定"),
        "confidence": parsed.get("confidence", 0.5),
        "method": "llm",
    }
