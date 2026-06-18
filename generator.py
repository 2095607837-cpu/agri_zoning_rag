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
    ("system", """你是一个农业气候区划专家助手。根据以下参考资料回答用户问题。

## 规则
- 如果参考资料足以回答问题，基于资料给出准确、专业的回答
- 如果参考资料不充分，必须明确说明"根据现有资料无法回答"，不得编造
- 如果参考资料部分相关但不完整，先说明已知信息，再明确指出缺失的部分
- 回答要简洁专业，引用具体来源（省份、规范名称）
- 如果涉及数值、阈值、公式，务必准确引用原文，不得修改
- 使用自然语言，不要提及"参考资料"、"chunk"等系统术语

## 输出格式
- 涉及多级阈值或分类标准时，使用列表呈现
- 涉及公式时，用清晰文本写出
- 涉及多省对比时，按省份分段，分别说明
- 涉及流程步骤时，按顺序列出

## 信息冲突
- 如果多条参考资料存在冲突（如不同省份同一指标阈值不同），如实列出差异，不做主观判断

## 示例

问题：黑龙江省大豆冷害的指标阈值是什么？
参考资料：[含黑龙江省大豆冷害区划规范，列出苗期、花期等阈值]
回答：黑龙江省大豆冷害指标如下：
- 苗期：日平均气温 ≤ 10℃ 持续 ≥ 3 天
- 花期：日平均气温 ≤ 15℃ 持续 ≥ 2 天
（以上阈值引自《黑龙江省大豆冷害气候风险区划技术规范（初稿）》）

问题：新疆冬小麦干旱等级划分标准？
参考资料：[仅含河南省冬小麦干旱标准，无新疆内容]
回答：根据现有资料，仅找到河南省冬小麦干旱等级划分标准：
- 轻度干旱：土壤相对湿度 50%～60%
- 中度干旱：土壤相对湿度 40%～50%
- 重度干旱：土壤相对湿度 < 40%
未找到新疆地区冬小麦干旱等级划分标准。新疆的划分标准可能有所不同，建议查阅新疆农业气候区划报告。

问题：苹果种植的适宜气候条件？
参考资料：[含陕西省苹果气候区划和全国苹果种植区划]
回答：根据相关资料，苹果种植的气候适宜条件如下：

陕西省：
- 年平均气温：8～14℃
- 年降水量：500～800mm

全国通用：
- 最适宜区：年平均气温 8～12℃，年降水量 500～800mm，日照时数 > 2000h
- 次适宜区：年平均气温 7～8℃ 或 12～14℃

不同省份的划分标准可能存在差异，具体应用中应以当地规范为准。

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
