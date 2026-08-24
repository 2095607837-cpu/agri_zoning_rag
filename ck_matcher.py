"""
CK Matcher — Knowledge-guided Query Understanding 的匹配模块。

CK 不参与检索。CK 只服务于 Query → Rewrite 环节：
  1. 将每条 CK 的 user_expressions + core_concept + semantic_summary 拼接为匹配文本
  2. 用 BGE-small-zh 嵌入（与检索同模型），构建 numpy 矩阵（791×512，内存级）
  3. match(query, topk=3) 返回精炼 Knowledge Context（~150-300 token）
  4. Query Rewriter 将该 context 注入 Prompt，指导关键词/改写生成

用法:
  from ck_matcher import match, build_knowledge_context
  ctx = match("大兴安岭东南麓气温怎么分析？", topk=3)
"""

import json
import os
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
CK_PATH = BASE_DIR / "data" / "chunk_knowledge.json"
EMB_CACHE_DIR = BASE_DIR / "data" / "ck_matcher"
EMB_FILE = EMB_CACHE_DIR / "ck_embeddings.npy"
IDS_FILE = EMB_CACHE_DIR / "ck_ids.json"

EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
MIN_SIM = 0.40          # 低于此相似度的 CK 视为不相关，不注入 context
MAX_CONTEXT_CHARS = 400  # Knowledge Context 总长上限（中文 ~150-300 token）

# user_expressions 按问题类型分组，每类型最多取 N 条（避免长文本稀释 embedding）
_UE_PER_TYPE = 2
_UE_MAX_TOTAL = 10

_embeddings = None
_ck_matrix = None  # type: np.ndarray | None
_ck_ids: list[str] = []
_ck_data: dict = {}


def _get_embeddings():
    global _embeddings
    if _embeddings is None:
        from langchain_huggingface import HuggingFaceEmbeddings
        _embeddings = HuggingFaceEmbeddings(
            model_name=EMBED_MODEL,
            model_kwargs={"device": "mps"},
            encode_kwargs={"normalize_embeddings": True},
        )
    return _embeddings


def _build_match_text(ck: dict) -> str:
    """拼接 CK 匹配文本：user_expressions + core_concept + semantic_summary。

    这三类字段最接近用户的提问分布（口语问法、概念词、叙事摘要）。
    technical_terms / field_value_pairs 等表格化字段不参与 embedding（噪声大、与问句分布远）。
    """
    parts = []

    ue = ck.get("user_expressions") or {}
    if isinstance(ue, dict):
        ue_items = []
        for qtype, exprs in ue.items():
            if isinstance(exprs, list):
                ue_items.extend(exprs[:_UE_PER_TYPE])
        ue_items = ue_items[:_UE_MAX_TOTAL]
        if ue_items:
            parts.append("\n".join(ue_items))
    elif isinstance(ue, list):
        if ue:
            parts.append("\n".join(ue[:_UE_MAX_TOTAL]))

    cc = ck.get("core_concept") or []
    if cc:
        parts.append("\n".join(cc))

    ss = ck.get("semantic_summary") or ""
    if ss.strip():
        parts.append(ss.strip())

    return "\n".join(parts)


def _load(force_rebuild: bool = False) -> None:
    """加载 CK + 嵌入矩阵（磁盘缓存优先，懒加载 embedding 模型）。"""
    global _ck_matrix, _ck_ids, _ck_data

    if _ck_matrix is not None:
        return

    with open(CK_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    _ck_data = {k: v for k, v in raw.items() if not k.startswith("_")}
    # 跳过匹配文本为空的 CK（零向量 normalize 除零 → NaN）
    _ck_ids = [cid for cid, ck in _ck_data.items()
               if _build_match_text(ck).strip()]

    if (not force_rebuild and EMB_FILE.exists() and IDS_FILE.exists()
            and json.loads(IDS_FILE.read_text(encoding="utf-8")) == _ck_ids):
        _ck_matrix = np.load(EMB_FILE)
        return

    texts = [_build_match_text(_ck_data[cid]) for cid in _ck_ids]
    emb = _get_embeddings()
    matrix = np.array(emb.embed_documents(texts), dtype=np.float32)

    EMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    np.save(EMB_FILE, matrix)
    with open(IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(_ck_ids, f, ensure_ascii=False)

    _ck_matrix = matrix


def _refine_context(ck: dict, max_chars: int = MAX_CONTEXT_CHARS) -> str:
    """从单个 CK 抽取精炼 Knowledge Context。

    只保留对 Rewrite 最有价值的字段：
      core_concept / technical_terms / evaluation_method / semantic_summary（截断）
    """
    lines = []

    cc = ck.get("core_concept") or []
    if cc:
        lines.append(f"概念: {'、'.join(cc[:3])}")

    tt = ck.get("technical_terms") or []
    if tt:
        lines.append(f"术语: {'、'.join(tt[:4])}")

    em = ck.get("evaluation_method") or []
    if em:
        lines.append(f"方法: {'、'.join(em[:3])}")

    ss = (ck.get("semantic_summary") or "").strip()
    if ss:
        if len(ss) > 80:
            ss = ss[:80] + "…"
        lines.append(f"摘要: {ss}")

    ctx = "\n".join(lines)
    if len(ctx) > max_chars:
        ctx = ctx[:max_chars] + "…"
    return ctx


def match(query: str, topk: int = 3) -> list[dict]:
    """Dense 检索 CK：返回 Top-K CK 候选（含相似度 + 精炼 context + 溯源）。

    Returns:
      [{"chunk_id": ..., "sim": 0.91,
        "match_text": "user_expressions + core_concept + semantic_summary 拼接全文",
        "match_fields": ["user_expressions", "core_concept", "semantic_summary"],
        "context": "概念: ...\n术语: ...\n方法: ...\n摘要: ..."}, ...]
      仅返回 sim ≥ MIN_SIM 的候选（可能少于 topk）。
      chunk_id 即 source chunk id（CK 与 chunk 一一对应）。
    """
    _load()
    q_emb = np.array(_get_embeddings().embed_query(query), dtype=np.float32)
    # 逐元素乘求和（避开 macOS Accelerate gemm 对归一化矩阵的误报警告）
    sims = (_ck_matrix * q_emb).sum(axis=1)
    top_idx = np.argsort(sims)[::-1][:topk]

    results = []
    for i in top_idx:
        sim = float(sims[i])
        if sim < MIN_SIM:
            break
        cid = _ck_ids[i]
        ck = _ck_data[cid]
        results.append({
            "chunk_id": cid,
            "sim": sim,
            "match_text": _build_match_text(ck),
            "match_fields": ["user_expressions", "core_concept", "semantic_summary"],
            "context": _refine_context(ck),
        })
    return results


def context_from_matches(matches: list[dict]) -> str:
    """从 match() 结果构建 Prompt 注入用的 Knowledge Context 文本块。

    供并行评测复用：主线程先 match() 一次，避免多线程调用 embedding 模型。
    """
    if not matches:
        return ""
    blocks = []
    for i, m in enumerate(matches, 1):
        blocks.append(f"[CK{i} (sim={m['sim']:.2f})]\n{m['context']}")
    ctx = "\n\n".join(blocks)
    if len(ctx) > MAX_CONTEXT_CHARS:
        ctx = ctx[:MAX_CONTEXT_CHARS] + "…"
    return ctx


def build_knowledge_context(query: str, topk: int = 3) -> str:
    """生成 Prompt 注入用的 Knowledge Context 文本块。

    topk 个候选按 [CK1 (sim=0.91)] ... 分段拼接，总长控制在 ~150-300 token。
    """
    return context_from_matches(match(query, topk=topk))


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "大兴安岭东南麓气温怎么分析？"
    print(f"Query: {q}\n")
    for m in match(q, topk=3):
        print(f"  [{m['chunk_id']}] sim={m['sim']:.3f}")
        print(f"  {m['context']}\n")
    print("=" * 60)
    print("Knowledge Context (Prompt 注入):")
    print(build_knowledge_context(q))
