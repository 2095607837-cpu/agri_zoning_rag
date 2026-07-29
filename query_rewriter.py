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
TERM_MAP_FILE = BASE_DIR / "data" / "terminology_mapping.json"
SYNONYM_FILE = BASE_DIR / "data" / "synonym_dictionary.json"

_cache = OrderedDict()
CACHE_MAX = 500

# ── 同义词词典（规范术语 → 同义词列表）──
_synonym_dict: dict[str, list[str]] = {}

def _load_synonym_dict():
    global _synonym_dict
    if not SYNONYM_FILE.exists():
        return
    try:
        with open(SYNONYM_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k.startswith("_"):
                continue
            _synonym_dict[k] = v
    except Exception:
        pass


def _expand_with_synonyms(terms: list[str], max_syn: int = 3) -> list[str]:
    """对术语列表按同义词词典扩展，每个术语最多取 max_syn 个同义词。"""
    if not _synonym_dict:
        return []
    expanded = []
    for term in terms:
        if term in _synonym_dict:
            synonyms = _synonym_dict[term]
            for s in synonyms[:max_syn]:
                if s and s not in expanded and s not in terms:
                    expanded.append(s)
    return expanded

# ── 术语映射表（口语→规范术语，确定性）──
_term_map: dict[str, str] = {}   # 扁平表: 口语词 → 规范术语
_term_categories: list[dict] = []  # 分类列表, 供 prompt 引用

def _load_term_map():
    """加载 terminology_mapping.json → 扁平 dict + 分类列表。"""
    global _term_map, _term_categories
    if not TERM_MAP_FILE.exists():
        return
    try:
        with open(TERM_MAP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

    _term_categories = []
    for cat_name, mappings in data.items():
        if cat_name.startswith("_"):
            continue
        cat_entries = []
        for spoken, standard in mappings.items():
            # 跳过单字（确定性匹配用不上，prompt 中也误导）和自身映射（无增益）
            if len(spoken) <= 1 or spoken == standard:
                continue
            _term_map[spoken] = standard
            cat_entries.append({"spoken": spoken, "standard": standard})
        if cat_entries:
            _term_categories.append({"category": cat_name, "entries": cat_entries})


def _apply_terminology_map(query: str) -> list[str]:
    """确定性扫描 query 中已知口语词，返回映射后的规范术语列表（去重、最长匹配优先）。"""
    if not _term_map:
        return []
    # 按口语词长度降序排列，确保长词优先匹配
    sorted_keys = sorted(_term_map.keys(), key=len, reverse=True)
    matched_terms: list[str] = []
    matched_spans: list[tuple[int, int]] = []
    for key in sorted_keys:
        # 跳过单字（太容易误匹配）
        if len(key) <= 1:
            continue
        # 查找所有非重叠匹配
        start = 0
        while True:
            idx = query.find(key, start)
            if idx == -1:
                break
            span = (idx, idx + len(key))
            # 检查是否与已有匹配重叠
            if not any(s <= span[0] < e or s < span[1] <= e for s, e in matched_spans):
                matched_spans.append(span)
                term = _term_map[key]
                if term not in matched_terms:
                    matched_terms.append(term)
            start = idx + 1
    return matched_terms


def _build_term_map_prompt_snippet(max_entries: int = 60) -> str:
    """从映射表生成 prompt 片段，均匀覆盖所有类别。"""
    if not _term_categories:
        return ""
    n_cats = len(_term_categories)
    per_cat = max(1, max_entries // n_cats)  # 每类均匀取
    lines = []
    shown = 0
    for cat in _term_categories:
        if shown >= max_entries:
            break
        for e in cat["entries"][:per_cat]:
            if shown >= max_entries:
                break
            lines.append(f'  - "{e["spoken"]}" → {e["standard"]}')
            shown += 1
    return "\n".join(lines)


# 模块加载时初始化
_load_term_map()
_load_synonym_dict()

REWRITE_PROMPT = ChatPromptTemplate.from_messages([
    ("user", """你是一个农业气候区划领域的检索查询改写器。

## Step 1: 判断改写类型

- **none**: 问题已是标准术语+完整检索语句，直接检索。sub_queries 留空，keywords 仅抽取核心实体。适用：普通事实查询、定义查询、单实体查询、已有标准术语的"方法/评价"类查询。
- **normalize**: 问题需要术语标准化（口语→术语、简称→全称、同义词→标准名），但问题结构不变。标准化包括：①口语→术语（"光照好不好"→日照百分率/日照时数）②简称→全称（"积温"→≥10℃活动积温）③模糊→精确（"冷害"→低温冷害/冷害风险指数）④灾害否定→安全保障（"冻不坏"="不发生冻害"→安全越冬，不是越冬冻害；"不旱"="不发生干旱"→水分保障，不是干旱）⑤抽象科学概念→领域术语（"低温累积效应"即"低温冷害对产量的累积影响"→低温冷害/冷害风险指数）⑥抽象流程词→具体方法词（"工作流程"→技术流程/技术路线）⑦**口语适宜性查询→分层术语链**（如"种X选什么地方最好"=极简口语，keywords和sub_queries需覆盖不同抽象层级：动作层"X种植"→框架层"适宜性区划/气候区划"→结果层"种植适宜区/适宜种植区"，每层1-2个词，确保不同层级的chunk都能命中）。**必须将口语/同义词/抽象概念映射为知识库中的精确术语**。
- **expand**: 仅当一个问题天然需要多个独立检索才能完整回答时使用。必须满足：①同时询问多个不同对象 ②明确包含多个比较维度 ③答案分散在不同 section。普通事实查询、定义查询、单实体查询禁止 expand。

## Step 2: 执行改写

### 关键词（keywords，数组，词/短语，≤6）
- 目的：补充检索关键词，增强 BM25 召回
- 按此顺序排列：地区 → 对象（作物/灾种）→ 术语 → 限定条件（数值/时间/等级）
- 仅包含：实体、专业术语、限定条件
- **禁止**：完整句子、泛化词（农业/植物/果树/作物/栽培）、上位概念
- **必须**：将口语/同义词映射为知识库精确术语
- 若不能确定，保持原表达，不要推测

### 改写查询（rewrite_queries，数组，完整句子，≤2）
- 目的：用不同表达方式重述同一问题，增强 Dense+BM25 双通道召回
- normalize 时产出 1~2 条：将口语化/模糊表达改写为标准术语+完整句式
  - 例："种大豆需要多少积温才够？" → "大豆种植所需的≥10℃活动积温阈值是多少？"
  - 例："光照好不好用什么指标衡量？" → "农业气候资源中衡量光照条件的指标有哪些？"
- expand 时可产出 1 条，从整体视角重述（不是拆分）
- none 时留空
- **禁止**：与原始 query 逐字相同、改变问题范围

### 子查询（sub_queries，数组，完整句子，≤3）
- 目的：将复杂问题拆分为独立子问题，支持并行检索/多跳检索
- rewrite_type=expand 时必须产出。normalize 时通常留空，但口语适宜性查询例外
- 每条从不同角度切入：如"指标/方法/阈值/空间分布/影响因素/等级划分/计算公式/历史变化"
- 避免同义反复，确保命中不同 chunk
- **禁止**：与 rewrite_queries 重复、与原始 query 逐字相同

### 口语→术语映射表（normalize 时**强制查表**，不要自己猜！）
以下映射表覆盖了农业气候区划领域的常见口语词及其对应规范术语。
改写时**必须**先查下表：query 中出现左侧口语词，直接映射为右侧规范术语。
表中**没有**的口语词才允许自行推断，但要保守——不确定就保持原表达。

{term_map_snippet}

如果问题中出现了上表**没有**的口语/模糊表达，参考上表的风格自行映射为知识库术语。
不确定时保持原表达，不要臆造术语。

### 核心约束
1. **意图保持**: 改写后与原问题意图完全一致，不扩大/缩小范围
2. **信息完整**: 不丢弃地区、时间、品种、数值、条件状语（如"低风险区范围"→保留"低风险区"）
3. **噪声去除**: 删除闲聊/口头禅/重复内容
4. **歧义消除**: 补全代词和省略的上下文
5. **不过度改写**: 不改比改错好。不凭空添加"原因/措施/解决方案"
6. **知识库优先**: 优先使用知识库已有术语，不确定时保持原表达
7. **术语加权**: 对于包含口语/同义/模糊表达的问题，必须映射为知识库精确术语，提升术语加权召回
8. **灾害否定=安全保障**: "冻不坏"="冻害"+"不"=不发生冻害→安全越冬，不是越冬冻害；"不旱"="干旱"+"不"=不发生干旱→水分保障，不是干旱。**禁止**把灾害否定映射为灾害名称。

## Step 3: 自检

逐条检查：
① 是否遗漏地区/品种/时间/数值？
② 是否改变了问题范围？
③ 是否加入了用户没问的内容？
④ keywords 是否包含泛化词或完整句子？
⑤ 是否误将简单查询判为 expand？
⑥ 口语/同义词是否已映射为知识库精确术语？
⑦ 限定条件（如"低风险区""1961-1990年"）是否保留？
⑧ 否定/肯定极性是否正确？（"冻不坏"→安全越冬，不是越冬冻害）
⑨ 抽象概念是否已映射为领域术语？
⑩ rewrite_queries 是否与原始 query 表达不同但意图一致？是否误将简单查询判为 expand？

如有问题，修正后再输出。

## Few-shot

Q: 种大豆需要多少积温才够？
→ {{"rewrite_type": "normalize", "keywords": ["大豆", "≥10℃活动积温", "积温阈值"], "rewrite_queries": ["大豆种植所需的≥10℃活动积温阈值是多少？"], "sub_queries": [], "confidence": 0.95}}

Q: 种大豆选什么地方最好？
→ {{"rewrite_type": "normalize", "keywords": ["大豆", "大豆种植", "适宜性区划", "种植适宜区"], "rewrite_queries": ["大豆种植的气候适宜区如何划分？"], "sub_queries": ["大豆种植适宜性区划", "大豆适宜种植区"], "confidence": 0.88}}

Q: 农业上说的光照好不好用什么指标衡量？
→ {{"rewrite_type": "normalize", "keywords": ["日照百分率", "日照时数", "太阳辐射"], "rewrite_queries": ["农业气候资源中衡量光照条件的指标有哪些？"], "sub_queries": [], "confidence": 0.92}}

Q: 冷害风险指数低风险区范围？
→ {{"rewrite_type": "normalize", "keywords": ["冷害风险指数", "低风险区", "等级划分"], "rewrite_queries": ["冷害风险指数的低风险区如何划分？"], "sub_queries": [], "confidence": 0.85}}

Q: 黑龙江省生长季（5-9月）各气候要素在1961-1990年和1991-2020年两个时段有什么变化特征？
→ {{"rewrite_type": "normalize", "keywords": ["黑龙江", "生长季", "5-9月", "气候要素", "变化特征", "1961-1990", "1991-2020"], "rewrite_queries": [], "sub_queries": [], "confidence": 0.88}}

Q: 黑龙江省气候资源普查中，温度和无霜期天数等热量指标的空间推算采用什么方法？具体步骤是什么？
→ {{"rewrite_type": "normalize", "keywords": ["黑龙江", "气候资源普查", "温度", "无霜期", "空间推算方法"], "rewrite_queries": [], "sub_queries": [], "confidence": 0.85}}

Q: 什么是界限温度？农业气候资源普查中常用的界限温度有哪些？
→ {{"rewrite_type": "none", "keywords": ["界限温度", "农业气候资源普查"], "rewrite_queries": [], "sub_queries": [], "confidence": 0.98}}

Q: 黑龙江大豆的低温冷害和霜冻在定义、致灾机理和区划指标上有什么不同？
→ {{"rewrite_type": "expand", "keywords": ["黑龙江", "大豆", "低温冷害", "霜冻"], "rewrite_queries": ["黑龙江大豆低温冷害与霜冻的对比分析"], "sub_queries": ["大豆低温冷害定义与致灾机理", "大豆霜冻定义与致灾机理", "低温冷害与霜冻区划指标对比"], "confidence": 0.90}}

Q: 陕西省苹果品质气候区划采用什么方法进行综合评价？
→ {{"rewrite_type": "none", "keywords": ["陕西", "苹果", "品质气候区划", "综合评价"], "rewrite_queries": [], "sub_queries": [], "confidence": 0.96}}

Q: 苹果成熟期是什么时候？
→ {{"rewrite_type": "none", "keywords": ["苹果", "成熟期"], "rewrite_queries": [], "sub_queries": [], "confidence": 0.99}}

Q: 内蒙古大豆区划和陕西苹果区划在指标体系上有什么差异？
→ {{"rewrite_type": "expand", "keywords": ["内蒙古", "大豆", "陕西", "苹果", "指标体系"], "rewrite_queries": ["内蒙古大豆与陕西苹果区划指标体系对比"], "sub_queries": ["内蒙古大豆区划指标体系", "陕西苹果区划指标体系"], "confidence": 0.88}}

Q: 低温累积效应如何影响大豆产量？
→ {{"rewrite_type": "normalize", "keywords": ["大豆", "低温冷害", "冷害风险指数", "减产机制"], "rewrite_queries": ["低温冷害对大豆产量的影响机制是什么？"], "sub_queries": [], "confidence": 0.85}}

Q: 陕西苹果区划从数据收集到最终区划图输出的完整工作流程是什么？
→ {{"rewrite_type": "normalize", "keywords": ["陕西", "苹果", "气候区划", "技术流程", "技术路线"], "rewrite_queries": ["陕西苹果气候区划的技术路线和实施步骤"], "sub_queries": [], "confidence": 0.88}}

Q: 新疆冬小麦越冬期长度？
→ {{"rewrite_type": "normalize", "keywords": ["新疆", "冬小麦", "越冬期", "越冬期天数"], "rewrite_queries": ["新疆冬小麦越冬期的天数是多少？"], "sub_queries": [], "confidence": 0.90}}

用户问题：{query}

仅输出 JSON（不要输出其他内容）：
{{"rewrite_type": "none|normalize|expand", "keywords": ["kw1"], "rewrite_queries": ["rw1"], "sub_queries": ["sq1"], "confidence": 0.0-1.0}}"""),
])


def _needs_rewrite(query: str, top1_sim: float = 0.0, top2_sim: float = 0.0) -> bool:
    """多信号门控：判断是否需要 LLM 改写。

    信号优先级（任一命中 → 触发改写）：
      1. 已知口语词命中 → 直接触发（确定性最强）
      2. 检索器不确定（top1-top2 margin < 0.03）→ 触发
      3. top1 自身很低（< 0.72）→ 触发

    同时满足以下所有条件才跳过 LLM：
      - query > 12 字
      - 口语词未命中
      - margin ≥ 0.03（检索器确定）
      - top1_sim ≥ 0.72（检索质量够好）
    """
    if len(query) <= 6:
        return False

    # 信号 1：已知口语词 → 一定改写
    if _apply_terminology_map(query):
        return True

    # 信号 2：检索器不确定 → 改写
    margin = top1_sim - top2_sim
    if margin < 0.03:
        return True

    # 信号 3：top1 低于阈值 → 改写
    if top1_sim < 0.72:
        return True

    return False


def _llm_rewrite(query: str) -> dict:
    """一次 LLM 调用完成改写。"""
    prompt = REWRITE_PROMPT.format(
        query=query,
        term_map_snippet=_build_term_map_prompt_snippet(),
    )
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
        return {"rewrite_type": "none", "keywords": [], "rewrite_queries": [], "sub_queries": [], "confidence": 0.0}

    # Normalize keywords: handle both array and legacy comma-string format
    raw_kw = parsed.get("keywords", [])
    if isinstance(raw_kw, str):
        keywords = [kw.strip() for kw in raw_kw.split(",") if kw.strip()]
    else:
        keywords = [kw.strip() for kw in raw_kw if kw and kw.strip()]

    # Normalize sub_queries: handle both array and legacy \n-separated string format
    raw_sq = parsed.get("sub_queries", [])
    if isinstance(raw_sq, str):
        sub_queries = [q.strip() for q in raw_sq.split("\n") if q.strip() and q.strip() != query]
    else:
        sub_queries = [q.strip() for q in raw_sq if q and q.strip() and q.strip() != query]

    # Normalize rewrite_queries: handle both array and legacy string format
    raw_rq = parsed.get("rewrite_queries", [])
    if isinstance(raw_rq, str):
        rewrite_queries = [q.strip() for q in raw_rq.split("\n") if q.strip() and q.strip() != query]
    else:
        rewrite_queries = [q.strip() for q in raw_rq if q and q.strip() and q.strip() != query]

    rewrite_type = parsed.get("rewrite_type", "normalize")
    if rewrite_type not in ("none", "normalize", "expand"):
        rewrite_type = "normalize"

    confidence = parsed.get("confidence", 0.5)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.5

    return {
        "rewrite_type": rewrite_type,
        "keywords": keywords,
        "rewrite_queries": rewrite_queries,
        "sub_queries": sub_queries,
        "confidence": confidence,
    }


def _load_cache():
    """从文件加载改写缓存到内存 LRU。"""
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k not in _cache:
                _cache[k] = v
        return data
    except Exception:
        return {}


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


def expand_query(query: str, mode: str = "all", top1_sim: float = 0.0, top2_sim: float = 0.0) -> list[str]:
    """
    扩展查询，返回扩展后的查询列表供多路检索使用。
    mode: "keywords" | "multi_view" | "all"
    top1_sim: 原始 query 检索 top-1 相似度
    top2_sim: 原始 query 检索 top-2 相似度（用于 margin 门控）
    """
    cache_key = query.strip()
    if cache_key in _cache:
        _cache.move_to_end(cache_key)
        entry = _cache[cache_key]
    elif not _needs_rewrite(cache_key, top1_sim, top2_sim):
        # gate 不触发 LLM 改写时，确定性术语映射仍可能有产出
        mapped = _apply_terminology_map(cache_key)
        entry = {"rewrite_type": "none", "keywords": mapped, "rewrite_queries": [], "sub_queries": [], "confidence": 1.0}
        _cache[cache_key] = entry
        _cache.move_to_end(cache_key)
        _save_cache()
    else:
        entry = _llm_rewrite(cache_key)
        _cache[cache_key] = entry
        _cache.move_to_end(cache_key)
        _save_cache()
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)

    queries = [query]

    # ── Keyword 池：术语映射 + 同义词 + LLM keywords（词/短语，≤6，hard 6）──
    kw_pool = []
    if mode in ("keywords", "all"):
        mapped_terms = _apply_terminology_map(query)
        for term in mapped_terms:
            if term and term not in kw_pool:
                kw_pool.append(term)

        all_terms = list(mapped_terms) + entry.get("keywords", [])
        synonym_terms = _expand_with_synonyms(all_terms)
        for st in synonym_terms:
            if st and st not in kw_pool:
                kw_pool.append(st)

        for kw in entry.get("keywords", []):
            if kw and kw not in kw_pool:
                kw_pool.append(kw)

    MAX_KEYWORD = 6       # hard limit
    kw_pool = kw_pool[:MAX_KEYWORD]

    # ── Rewrite Query 池（完整句子改写，≤2，hard 3）──
    rw_pool = []
    if mode in ("multi_view", "all"):
        for rq in entry.get("rewrite_queries", []):
            if rq and rq not in queries and rq not in rw_pool:
                rw_pool.append(rq)

    MAX_REWRITE = 3       # hard limit
    rw_pool = rw_pool[:MAX_REWRITE]

    # ── SubQuery 池（拆分复杂问题，≤3，hard 4）──
    sq_pool = []
    if mode in ("multi_view", "all"):
        for sq in entry.get("sub_queries", []):
            if sq and sq not in queries and sq not in sq_pool:
                sq_pool.append(sq)

    MAX_SUBQUERY = 4      # hard limit
    sq_pool = sq_pool[:MAX_SUBQUERY]

    # ── 注册关键词供 BM25-only 检索 ──
    _kw_registry[cache_key] = list(kw_pool)

    # ── 合并输出：Rewrite Query → SubQuery（Keyword 不在此处，走 BM25-only）──
    extra = list(rw_pool)
    for sq in sq_pool:
        if sq not in extra:
            extra.append(sq)

    for t in extra:
        queries.append(t)

    return queries


_kw_registry: dict[str, list[str]] = {}


def get_keywords(query: str) -> list[str]:
    """返回最近一次 expand_query 为该 query 提取的关键词列表。"""
    return _kw_registry.get(query.strip(), [])
