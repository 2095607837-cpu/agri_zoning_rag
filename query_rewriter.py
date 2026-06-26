"""
Query Rewriter（LangChain 实现）

统一 prompt 完成关键词提取 + 多视角改写，LRU 缓存 + 文件持久化。
评测问题集的改写结果会保存到 data/rewrite_cache.json，下次直接加载，省 LLM 调用。

用法:
  from query_rewriter import expand_query, precompute_rewrites
  queries = expand_query("黑龙江大豆冷害区划用什么指标", mode="all")
"""

import json
import os
from collections import OrderedDict
from pathlib import Path
from langchain_core.prompts import ChatPromptTemplate
from llm_client import call_llm

BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = BASE_DIR / "data" / "rewrite_cache.json"

_cache = OrderedDict()
CACHE_MAX = 500

REWRITE_PROMPT = ChatPromptTemplate.from_messages([
    ("user", """提取农业气候区划问题的关键词，并改写为多角度检索查询。

关键词：优先专业术语（"冷害区划""积温""干旱风险指数"），2-5个，逗号分隔
改写查询：从不同角度切入（指标选择、计算方法、阈值设定），2-3个，换行分隔

用户问题：{query}

仅输出 JSON：{{"keywords": "kw1, kw2", "sub_queries": "sq1\\nsq2"}}"""),
])


REWRITE_TRIGGERS = ["为什么", "怎么", "如何", "什么原因", "区别", "比较", "影响", "关系", "差异", "不同", "优缺点"]


def _needs_rewrite(query: str) -> bool:
    """规则判断是否需要 LLM 改写（无 LLM 调用）。"""
    if len(query) <= 12:
        return False
    if any(w in query for w in REWRITE_TRIGGERS):
        return True
    return False


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


def _load_cache():
    """从文件加载改写缓存到内存 LRU。"""
    if not CACHE_FILE.exists():
        return
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k not in _cache:
                _cache[k] = v
        # LRU: 按加载顺序排列
        for k in data:
            if k in _cache:
                _cache.move_to_end(k)
    except Exception:
        pass


def _save_cache():
    """将内存 LRU 缓存持久化到文件。"""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(dict(_cache), f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def precompute_rewrites(queries: list[str], mode: str = "all"):
    """预计算一组 query 的改写结果并持久化。用于评测前批量预热。"""
    for q in queries:
        expand_query(q, mode=mode)
    _save_cache()
    print(f"[rewriter] 已缓存 {len(_cache)} 条改写结果 → {CACHE_FILE}")


def expand_query(query: str, mode: str = "all") -> list[str]:
    """
    扩展查询，返回扩展后的查询列表供多路检索使用。
    mode: "keywords" | "multi_view" | "all"
    """
    cache_key = query.strip()
    if cache_key in _cache:
        _cache.move_to_end(cache_key)
        entry = _cache[cache_key]
    elif not _needs_rewrite(cache_key):
        # 简单查询无需改写，缓存空结果避免重复判断
        entry = {"keywords": "", "sub_queries": ""}
        _cache[cache_key] = entry
        _cache.move_to_end(cache_key)
        _save_cache()
    else:
        entry = _llm_rewrite(cache_key)
        _cache[cache_key] = entry
        _cache.move_to_end(cache_key)
        _save_cache()  # 新结果即时持久化
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


# 模块导入时自动加载持久化缓存
_load_cache()
