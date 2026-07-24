"""多环节漏召回诊断框架
架构：
  Layer 1 (数据质量): gold chunk 是否在语料中
  Layer 2 (检索层):   Dense top-100 / BM25 top-100 / RRF fused rank
  Layer 3 (精排层):   CE rank vs RRF rank — CE 是救回还是搅黄？
  Layer 4 (合并层):   Rewrite 是否触发？触发后有否命中？
  Layer 5 (内容层):   Query-Gold 直接余弦 + 内容级诊断

对每道 R@10=0 题逐层采集信号，机器分桶，导出可操作的修改方向。
"""
import json, sys, numpy as np
from collections import Counter, defaultdict
sys.path.insert(0, ".")

with open("data/golden_set_v2.json") as f: gs = json.load(f)
with open("data/chunks_split.json") as f: chunks = json.load(f)
with open("data/rewrite_cache.json") as f: rw_cache = json.load(f)

cid_to_chunk = {c["id"]: c for c in chunks}
sec_to_cids = {}
for c in chunks:
    sec_to_cids.setdefault(c["metadata"]["section_id"], []).append(c["id"])

from hybrid_search import HybridSearcher
from query_rewriter import _needs_rewrite

print("加载模型...", flush=True)
searcher = HybridSearcher(enable_reranker=True)
emb = searcher.embeddings

indomain = [q for q in gs if q["capability"] != "ood_detection"]

# 改写缓存
rewrite_map = {}
for q in indomain:
    entry = rw_cache.get(q["question"], {})
    rewrite_map[q["question"]] = entry.get("sub_queries", [])

# ── Step 1: 识别 Append CE 下的 R@10=0 ──
print("Step 1: 识别 Append CE 漏召题...", flush=True)
zero_rows = []
for q in indomain:
    query = q["question"]
    gold = set(q["gold_chunks"])
    extra = rewrite_map.get(query, [])
    _, results = searcher.search_multi_query(
        query, top_k=10, expand_context=True, extra_queries=extra)
    
    # 收集所有命中的 chunk id（section expand 后）
    retrieved = []
    for r in results:
        sid = r["metadata"].get("section_id", "")
        retrieved.extend(sec_to_cids.get(sid, []))
    
    # 同时也记录 section-level 命中（不 expand）
    retrieved_raw = []
    for r, _ in [(r, None) for r in results]:
        pass
    
    if not any(rid in gold for rid in retrieved[:10]):
        zero_rows.append({
            "id": q["id"], "query": query,
            "capability": q["capability"],
            "difficulty": q.get("difficulty", ""),
            "gold_cids": q["gold_chunks"],
            "extra": extra,
            "top1_sim": results[0].get("dense_similarity", results[0].get("similarity", 0)) if results else 0,
        })

print(f"Append CE R@10=0: {len(zero_rows)} 题\n", flush=True)

# ── Step 2: 逐环节信号采集 ──
print("Step 2: 逐环节信号采集...", flush=True)
for r in zero_rows:
    query = r["query"]
    gold_cids = r["gold_cids"]
    
    # Layer 1: 数据质量
    gold_contents = []
    missing = []
    for cid in gold_cids:
        c = cid_to_chunk.get(cid)
        if c:
            gold_contents.append(c)
        else:
            missing.append(cid)
    r["gold_missing"] = missing
    r["gold_in_corpus"] = len(gold_contents) > 0
    if not gold_contents:
        r["cat_L1"] = "标注错误:gold不存在"
        continue
    
    gold_sections = {c["metadata"]["section_id"] for c in gold_contents}
    r["gold_sections"] = list(gold_sections)
    
    # Layer 2: 检索层信号 — Dense/BM25/RRF
    dense_raw = searcher.vectorstore.similarity_search_with_score(query, k=100)
    dense_sids = [doc.metadata.get("section_id", "") for doc, _ in dense_raw]
    r["dense_rank"] = next((i for i, s in enumerate(dense_sids) if s in gold_sections), -1)
    
    bm25_docs = searcher._bm25_retriever.invoke(query)[:100]
    bm25_sids = [d.metadata.get("section_id", "") for d in bm25_docs]
    r["bm25_rank"] = next((i for i, s in enumerate(bm25_sids) if s in gold_sections), -1)
    
    # RRF fused rank
    RRF_K = 60; rrf = {}
    for rank, (doc, _) in enumerate(dense_raw):
        sid = doc.metadata.get("section_id", "")
        rrf[sid] = rrf.get(sid, 0) + 0.7 / (RRF_K + rank)
    for rank, doc in enumerate(bm25_docs):
        sid = doc.metadata.get("section_id", "")
        rrf[sid] = rrf.get(sid, 0) + 0.3 / (RRF_K + rank)
    rrf_sorted = sorted(rrf.items(), key=lambda x: -x[1])
    r["rrf_rank"] = next((i for i, (s, _) in enumerate(rrf_sorted) if s in gold_sections), -1)
    r["rrf_score"] = next((v for s, v in rrf_sorted if s in gold_sections), 0)
    
    # RRF top-30 (CE 输入) 中 gold 是否在
    r["gold_in_ce_input"] = r["rrf_rank"] >= 0 and r["rrf_rank"] < 30
    
    # Layer 3: CE 精排层 — 实际 Append CE top-10 中 gold section 排名
    extra = r["extra"]
    _, ce_results = searcher.search_multi_query(
        query, top_k=30, expand_context=False, extra_queries=extra)
    ce_sids = [res["metadata"].get("section_id", "") for res in ce_results]
    r["ce_rank"] = next((i for i, s in enumerate(ce_sids) if s in gold_sections), -1)
    
    # CE rank vs RRF rank
    if r["rrf_rank"] >= 0 and r["ce_rank"] >= 0:
        r["ce_delta"] = r["ce_rank"] - r["rrf_rank"]  # +CE变差, -CE救回
    else:
        r["ce_delta"] = None
    
    # Layer 4: 合并层 — Rewrite 信号
    r["has_rewrite"] = len(extra) > 0
    r["rewrite_count"] = len(extra)
    if extra:
        # Rewrite 是否命中了 gold section？
        rw_hit = False
        for rq in extra:
            rq_results = searcher.search(rq, top_k=20, expand_context=False, skip_reranker=True)
            rq_sids = [rr["metadata"].get("section_id", "") for rr in rq_results]
            if any(s in gold_sections for s in rq_sids):
                rw_hit = True
                break
        r["rewrite_hit_gold"] = rw_hit
    else:
        r["rewrite_hit_gold"] = False
    
    # Gate 信号
    initial = searcher.search(query, top_k=2, expand_context=True)
    t1 = initial[0].get("similarity", 0) if len(initial) > 0 else 0
    r["gate_top1_sim"] = t1
    r["gate_would_trigger"] = _needs_rewrite(query, t1) if len(query) > 12 else False
    
    # Layer 5: 内容层
    query_emb = np.array(emb.embed_query(query))
    cos_sims = []
    for gc in gold_contents[:5]:
        doc_emb = np.array(emb.embed_documents([gc["content"]])[0])
        sim = float(np.dot(query_emb, doc_emb)
                    / (np.linalg.norm(query_emb) * np.linalg.norm(doc_emb) + 1e-8))
        cos_sims.append(sim)
    r["max_gold_cos"] = max(cos_sims) if cos_sims else 0
    r["avg_gold_cos"] = np.mean(cos_sims) if cos_sims else 0
    
    # Top-1 实际内容与 gold 内容
    if ce_results:
        r["top1_content_preview"] = ce_results[0]["content"][:80]
        r["top1_source"] = ce_results[0]["metadata"].get("source_file", "")[-50:]
    if gold_contents:
        r["gold_content_preview"] = gold_contents[0]["content"][:80]
        r["gold_source"] = gold_contents[0]["metadata"].get("source_file", "")[-50:]

    print(f"  {r['id']} D={r['dense_rank']:>4d} B={r['bm25_rank']:>4d} "
          f"RRF={r['rrf_rank']:>4d} CE={r['ce_rank']:>4d} "
          f"cos={r['max_gold_cos']:.3f} rw={r['has_rewrite']} rw_hit={r['rewrite_hit_gold']}",
          flush=True)

# ── Step 3: 机器分桶（逐环节定位） ──
print(f"\n{'='*80}")
print("Step 3: 机器分桶 — 逐环节定位")
print(f"{'='*80}")

def classify(r):
    """逐层定位失败环节"""
    if not r.get("gold_in_corpus"):
        return ("A-数据层", "gold chunk 不在语料中")
    
    dr, br, rr, cr = r["dense_rank"], r["bm25_rank"], r["rrf_rank"], r["ce_rank"]
    cos = r["max_gold_cos"]
    has_rw = r["has_rewrite"]
    rw_hit = r.get("rewrite_hit_gold", False)
    ce_delta = r.get("ce_delta")
    
    # A: 检索层问题
    if dr == -1 and br == -1:
        if cos > 0.65:
            return ("B-检索层-高余弦未召回", f"cos={cos:.3f} 但双通道top100无gold → embedding/分词与gold表达方式不匹配")
        elif cos > 0.55:
            return ("B-检索层-中余弦未召回", f"cos={cos:.3f} 双通道均弱 → 需query改写或chunk切分优化")
        else:
            return ("B-检索层-低余弦断连", f"cos={cos:.3f} query-gold语义鸿沟 → 口语化/间接问法需术语映射")
    
    # B: RRF 融合层问题
    if rr >= 10 or rr == -1:
        if (0 <= dr <= 9) or (0 <= br <= 9):
            return ("C-RRF融合-单通道强但被稀释", f"D={dr} B={br} 某通道top10但RRF={rr} → 提高该通道权重或降低另一通道噪声")
        elif rr < 30:
            return ("C-RRF融合-排名偏低", f"RRF={rr} 在pool内但未进CE top30 → 候选池偏小或RRF权重不当")
        else:
            return ("C-RRF融合-排名靠后", f"RRF={rr} pool外 → 检索层信号太弱")
    
    # C: CE 精排层问题
    if rr < 30 and cr >= 10:
        if ce_delta is not None and ce_delta > 5:
            return ("D-CE精排-严重搅黄", f"RRF={rr}→CE={cr} CE把gold从RRF rank{rr}推到{cr} → CE不认可RRF判断")
        elif ce_delta is not None and ce_delta > 0:
            return ("D-CE精排-轻微搅黄", f"RRF={rr}→CE={cr} CE轻微后移 → 调整alpha增加RRF先验")
        else:
            return ("D-CE精排-未选入top10", f"RRF={rr}在CE输入但CE rank={cr} → CE偏好其他候选")
    
    # D: 改写层问题
    if not has_rw:
        gate_trigger = r.get("gate_would_trigger", False)
        if gate_trigger and len(r["query"]) > 12:
            return ("E-改写层-gate误阻拦", f"gate条件满足但未触发改写 → 检查rewrite_map缓存/capability特殊处理")
        else:
            return ("E-改写层-未触发", f"query长度={len(r['query'])} top1_sim={r.get('gate_top1_sim',0):.3f} → gate合理拦下或需放宽阈值")
    else:
        if not rw_hit:
            return ("E-改写层-改写未命中", f"有{len(r['extra'])}个改写但都未命中gold → 改写方向不对/改写质量差")
        else:
            return ("E-改写层-改写命中但未进入最终top10", f"改写命中了gold但最终排序没进 → merge策略问题")
    
    return ("F-其他", "需人工检查")

for r in zero_rows:
    cat, detail = classify(r)
    r["category"] = cat
    r["detail"] = detail

cnt = Counter(r.get("category", "?") for r in zero_rows)
for cat, n in cnt.most_common():
    print(f"\n{'─'*60}")
    print(f"  {cat} ({n} 题)")
    print(f"{'─'*60}")
    subset = [r for r in zero_rows if r.get("category") == cat]
    for r in sorted(subset, key=lambda x: x["max_gold_cos"], reverse=True):
        ce_d = r.get("ce_delta")
        ce_str = f"Δ={ce_d:+d}" if ce_d is not None else "Δ=N/A"
        print(f"  {r['id']:<8s} {r['capability']:<20s} {r['difficulty']:<6s} "
              f"cos={r['max_gold_cos']:.3f} D={r['dense_rank']:>4d} B={r['bm25_rank']:>4d} "
              f"RRF={r['rrf_rank']:>4d} CE={r['ce_rank']:>4d} {ce_str} "
              f"rw={'Y' if r['has_rewrite'] else 'N'}")
        print(f"         query: {r['query'][:70]}")
        print(f"         {r['detail']}")

# ── Step 4: 汇总与修改建议 ──
print(f"\n{'='*80}")
print("Step 4: 按环节汇总 → 可操作修改建议")
print(f"{'='*80}")

suggestions = {
    "A-数据层": "修复 gold 标注，重新验证 chunk 切分",
    "B-检索层": "优化 query 改写（口语→术语映射）、增加 Dense pool_size、BM25 分词方案改进",
    "C-RRF融合": "提高 Dense 权重（0.7→0.85）、增大 RRF pool（20→40）",
    "D-CE精排": "增加 alpha（0.2→0.3）增加 RRF 先验占比、扩大 CE 候选池（30→50）",
    "E-改写层": "放宽 gate 阈值（0.70→0.75）、cross_document/query_rewrite 强制触发改写",
}

total = len(zero_rows)
for cat, n in cnt.most_common():
    pct = n / total * 100
    print(f"  {cat:<30s} {n:>2d} 题 ({pct:>4.1f}%) → {suggestions.get(cat[:2], '')}")

json.dump(zero_rows, open("diagnose_zero_recall.json", "w"), ensure_ascii=False, indent=2)
print(f"\nsaved: diagnose_zero_recall.json ({total} 题)")
