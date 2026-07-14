#!/usr/bin/env python3
"""39 道 R@10=0 漏召回错误分类诊断。

对每道 baseline top-10 未命中 gold 的题，采集信号：
  - gold chunk 是否存在于语料
  - gold 所在 section 在 dense top-100 的排名（-1=不在）
  - 在 BM25 top-100 的排名
  - 在融合结果 top-100 的排名
  - query 与 gold chunk 的直接最大余弦（gold 的"可被 embedding 看见"程度）
据此机器分桶，为深层子集导出 query+gold 原文供内容级判断。
"""
import json, sys
import numpy as np
sys.path.insert(0, ".")

gs = json.load(open("data/golden_set_v2.json"))
chunks = json.load(open("data/chunks.json"))
cid_to_chunk = {c["id"]: c for c in chunks}
qs = [q for q in gs if q["capability"] != "ood_detection"]

from hybrid_search import HybridSearcher, Reranker
s = HybridSearcher(enable_reranker=False)
emb = s.embeddings
reranker = Reranker()
reranker._load()  # 手动加载 CrossEncoder 模型

DENSE_K = 100
CE_CANDIDATE_NUM = 40  # 与 search() 中 rerank_input 一致


def gold_sections(q):
    secs = set()
    missing = []
    for cid in q["gold_chunks"]:
        c = cid_to_chunk.get(cid)
        if c:
            secs.add(c["metadata"]["section_id"])
        else:
            missing.append(cid)
    return secs, missing


def first_hit_rank(section_ids, gold_secs):
    for i, sid in enumerate(section_ids):
        if sid in gold_secs:
            return i
    return -1


def baseline_hit(q, gold_secs):
    res = s.search(q["question"], top_k=10, expand_context=True)
    return any(r["metadata"].get("section_id", "") in gold_secs for r in res)


rows = []
for q in qs:
    gold_secs, missing = gold_sections(q)
    if not gold_secs:
        rows.append({"id": q["id"], "cap": q["capability"], "cat": "标注错误",
                     "note": "所有 gold chunk id 在语料中不存在", "missing": missing})
        continue
    if baseline_hit(q, gold_secs):
        continue  # baseline 命中，非漏召回

    query = q["question"]
    # dense top-100
    dense_raw = s.vectorstore.similarity_search_with_score(query, k=DENSE_K)
    dense_secs = [d.metadata.get("section_id", "") for d, _ in dense_raw]
    dense_rank = first_hit_rank(dense_secs, gold_secs)
    # bm25 top-100
    bm = s._bm25_retriever.invoke(query)
    bm_secs = [d.metadata.get("section_id", "") for d in bm[:DENSE_K]]
    bm_rank = first_hit_rank(bm_secs, gold_secs)
    # fused top-100
    fused = s.search(query, top_k=DENSE_K, expand_context=False)
    fused_secs = [r["metadata"].get("section_id", "") for r in fused]
    fused_rank = first_hit_rank(fused_secs, gold_secs)
    # 直接最大余弦：query vs 每个 gold chunk
    qv = np.array(emb.embed_query(query))
    max_cos = 0.0
    for cid in q["gold_chunks"]:
        c = cid_to_chunk.get(cid)
        if not c:
            continue
        dv = np.array(emb.embed_documents([c["content"]])[0])
        cos = float(np.dot(qv, dv))
        max_cos = max(max_cos, cos)

    # CrossEncoder 排名：RRF top-CE_CANDIDATE_NUM 送 CrossEncoder 精排，看 gold 排第几
    ce_candidates = fused[:CE_CANDIDATE_NUM]
    ce_rank = -1
    if ce_candidates:
        ce_pairs = [(query, r["content"][:500]) for r in ce_candidates]
        ce_scores = reranker._model.predict(ce_pairs, show_progress_bar=False)
        ce_ranked = sorted(enumerate(ce_scores), key=lambda x: -x[1])
        for rank, (idx, _) in enumerate(ce_ranked):
            if ce_candidates[idx]["metadata"].get("section_id", "") in gold_secs:
                ce_rank = rank
                break

    rows.append({
        "id": q["id"], "cap": q["capability"], "difficulty": q.get("difficulty"),
        "question": query,
        "dense_rank": dense_rank, "bm_rank": bm_rank, "fused_rank": fused_rank,
        "ce_rank": ce_rank,
        "max_gold_cos": round(max_cos, 4),
        "gold_chunks": q["gold_chunks"],
    })

# 机器分桶
def classify(r):
    if r.get("cat"):
        return r["cat"]
    dr, br, fr, cer, cos = r["dense_rank"], r["bm_rank"], r["fused_rank"], r["ce_rank"], r["max_gold_cos"]
    # 优先看 CrossEncoder：进了候选池但 CE 没排进 top-10
    if 0 <= fr < CE_CANDIDATE_NUM and cer >= 10:
        return f"CE掉队(RRF{fr}→CE{cer})"
    if 0 <= cer <= 9:
        return "CE命中(但expand_context/评测未计入)"
    best = min([x for x in (dr, br) if x >= 0], default=-1)
    if (0 <= dr <= 9) or (0 <= br <= 9):
        return "融合丢失(某通道top10但融合掉)"
    if best >= 0 and best <= 40:
        return "排名偏低(pool内但10名外)"
    if cos >= 0.55:
        return "排名偏低(gold余弦高却未进pool)"
    return "深层不匹配(待内容判断)"

from collections import Counter
for r in rows:
    r["cat"] = classify(r)

cnt = Counter(r["cat"] for r in rows)
print(f"\n{'='*70}\n  39 道漏召回错误分类（机器分桶）\n{'='*70}")
print(f"  漏召回总数: {len(rows)}")
for cat, n in cnt.most_common():
    print(f"    {cat:<34s} {n}")

print(f"\n  {'ID':<8s}{'cap':<18s}{'dense':>6s}{'bm25':>6s}{'fused':>6s}{'CE':>6s}{'cos':>7s}  分类")
for r in sorted(rows, key=lambda x: x["cat"]):
    if "note" in r:
        print(f"  {r['id']:<8s}{r['cap']:<18s}{'--':>6s}{'--':>6s}{'--':>6s}{'--':>6s}{'--':>7s}  {r['cat']}")
    else:
        print(f"  {r['id']:<8s}{r['cap']:<18s}{r['dense_rank']:>6d}{r['bm_rank']:>6d}"
              f"{r['fused_rank']:>6d}{r['ce_rank']:>6d}{r['max_gold_cos']:>7.3f}  {r['cat']}")

json.dump(rows, open("diagnose_zero_recall.json", "w"), ensure_ascii=False, indent=2)
print("\n  saved: diagnose_zero_recall.json")
