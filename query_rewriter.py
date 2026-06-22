"""
Query Rewriter（LangChain 实现）

统一 prompt 完成关键词提取 + 多视角改写，LRU 缓存。

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
    ("user", """提取农业气候区划问题的关键词，并改写为多角度检索查询。

关键词：优先专业术语（"冷害区划""积温""干旱风险指数"），2-5个，逗号分隔
改写查询：从不同角度切入（指标选择、计算方法、阈值设定），2-3个，换行分隔

用户问题：{query}

仅输出 JSON：{{"keywords": "kw1, kw2", "sub_queries": "sq1\\nsq2"}}"""),
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
        return {"keywords": "", "sub_queries": ""}

    return {
        "keywords": [kw.strip() for kw in parsed.get("keywords", "").split(",") if kw.strip()],
        "sub_queries": [q.strip() for q in parsed.get("sub_queries", "").split("\n") if q.strip() and q.strip() != query],
    }


def expand_query(query: str, mode: str = "all") -> list[str]:
    """
    扩展查询，返回扩展后的查询列表供多路检索使用。
    mode: "keywords" | "multi_view" | "all"
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

    if mode in ("keywords", "all"):
        for kw in entry.get("keywords", []):
            if kw and kw not in queries:
                queries.append(kw)

    if mode in ("multi_view", "all"):
        for sq in entry.get("sub_queries", []):
            if sq and sq not in queries:
                queries.append(sq)

    return queries
