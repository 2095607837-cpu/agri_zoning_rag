"""
RAG Generator（LangChain 实现）

使用 ChatPromptTemplate + LCEL chain 构建农业区划领域的答案生成器。

用法:
  from generator import generate, generate_rejection
  answer = generate("大豆冷害区划指标", results)
"""

from langchain_core.prompts import ChatPromptTemplate
from llm_client import call_llm

RAG_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是一个农业气候区划专家助手。请根据以下参考资料回答用户问题。

## 规则
- 如果参考资料足以回答问题，基于资料给出准确、专业的回答
- 如果参考资料不充分，必须明确说"根据现有资料无法回答"，不得编造
- 如果参考资料部分相关但不完整，说明已知信息并指出缺失的部分
- 回答要简洁专业，引用具体来源（省份、规范名称）
- 如果涉及数值、阈值、公式，务必准确引用原文
- 使用自然语言，不要提及"参考资料"、"chunk"等系统术语

## 参考资料
{context}

## 用户问题
{query}"""),
])


def build_context(results: list[dict]) -> str:
    """将检索结果格式化为 LLM 上下文。"""
    parts = []
    for i, r in enumerate(results, 1):
        meta = r.get("metadata", {})
        src_file = meta.get("source_file", "?")
        province = meta.get("province", "")
        zoning = meta.get("zoning_type", "")

        label_parts = [f"[源{i}]"]
        if province:
            label_parts.append(province)
        if zoning:
            label_parts.append(zoning)
        label_parts.append(src_file[:50])
        header = " | ".join(label_parts)

        parts.append(f"{header}\n{r['content']}")

    return "\n\n".join(parts)


def generate(query: str, results: list[dict], temperature: float = 0.6) -> str:
    """基于检索结果生成答案。"""
    context = build_context(results)
    if not context.strip():
        return "抱歉，根据现有资料无法回答这个问题。"

    prompt = RAG_PROMPT.format(context=context, query=query)
    prompt_str = prompt.to_string() if hasattr(prompt, 'to_string') else str(prompt)
    return call_llm(
        [{"role": "user", "content": prompt_str}],
        temperature=temperature,
        stream=False,
    )


def generate_rejection(query: str, reason: str = "") -> str:
    """生成拒答回复。"""
    if reason:
        return f"抱歉，根据现有农业气候区划资料无法回答这个问题。（{reason}）"
    return "抱歉，根据现有农业气候区划资料无法回答这个问题。建议提供更具体的区划指标或省份信息。"
