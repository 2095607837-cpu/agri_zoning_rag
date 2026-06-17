"""
Query Rewriter（LangChain 实现）

统一 prompt 完成 HyDE + 关键词 + 多视角改写，LRU 缓存。

用法:
  from query_rewriter import expand_query
  queries = expand_query("黑龙江大豆冷害区划用什么指标", mode="all")
"""

import json
from collections import OrderedDict
from langchain_core.prompts import ChatPromptTemplate
from llm_client import call_llm

_cache = OrderedDict()
CACHE_MAX = 500

REWRITE_PROMPT = ChatPromptTemplate.from_messages([
    ("user", """你是一个农业气候区划领域的检索专家。用户提出一个问题，请同时完成以下三项任务，输出一个 JSON：

1. **hypothesis**: 假设你是农业区划百科全书，用 80 字以内写一段相关知识解释
2. **keywords**: 提取问题中的农业区划专业术语和关键概念，2-5 个，用逗号分隔
3. **sub_queries**: 将问题改写成 2-3 个不同角度的检索查询

注意：
- hypothesis 用学术化的语气写，像百科条目
- keywords 优先提取专业术语（如"冷害区划""隶属度函数""积温""干旱风险指数"），不要提取泛化词
- sub_queries 每个一行，从不同角度切入（如指标选择、计算方法、阈值设定）

用户问题：{query}

严格输出以下 JSON（不要输出其他内容）：
{{"hypothesis": "...", "keywords": "kw1, kw2, kw3", "sub_queries": "sq1\\nsq2\\nsq3"}}"""),
])


def _llm_rewrite(query: str) -> dict:
    """一次 LLM 调用完成改写。"""
    prompt = REWRITE_PROMPT.format(query=query)
    prompt_str = prompt.to_string() if hasattr(prompt, 'to_string') else str(prompt)
    try:
        resp = call_llm(
            [{"role": "user", "content": prompt_str}],
            temperature=0.3,
            stream=False,
        )
        start = resp.find("{")
        end = resp.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(resp[start:end])
        else:
            raise ValueError("No JSON in response")
    except Exception:
        return {"hypothesis": "", "keywords": "", "sub_queries": ""}

    return {
        "hypothesis": parsed.get("hypothesis", "").strip(),
        "keywords": [kw.strip() for kw in parsed.get("keywords", "").split(",") if kw.strip()],
        "sub_queries": [q.strip() for q in parsed.get("sub_queries", "").split("\n") if q.strip() and q.strip() != query],
    }


def expand_query(query: str, mode: str = "all") -> list[str]:
    """
    扩展查询，返回扩展后的查询列表供多路检索使用。
    mode: "hyde" | "keywords" | "multi_view" | "all"
    """
    cache_key = query.strip()
    if cache_key in _cache:
        _cache.move_to_end(cache_key)
        entry = _cache[cache_key]
    else:
        entry = _llm_rewrite(cache_key)
        _cache[cache_key] = entry
        _cache.move_to_end(cache_key)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)

    queries = [query]

    if mode in ("hyde", "all"):
        h = entry.get("hypothesis", "")
        if h and h != query:
            queries.append(h)

    if mode in ("keywords", "all"):
        for kw in entry.get("keywords", []):
            if kw and kw not in queries:
                queries.append(kw)

    if mode in ("multi_view", "all"):
        for sq in entry.get("sub_queries", []):
            if sq and sq not in queries:
                queries.append(sq)

    return queries
