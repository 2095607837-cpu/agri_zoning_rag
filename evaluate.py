"""
Golden Set 评测脚本

对 RAG 系统进行多维度评测：

  A. 检索层指标（无需 LLM）:
     - MRR / Recall@K / Precision@K（基于 source_chunk_id 匹配）
     - 语义相关率：检索结果与 golden answer 的语义余弦相似度
     - 真实向量相似度（从 Chroma 获取）
     - 按维度细分（省份/作物/区划类型/难度/问题类型）

  B. OOD 检测（无需 LLM）:
     - Judge Layer 1-2 的拒答能力

  C. 全管道评测（需 LLM API Key）:
     - 忠实率 / 答案正确率 / OOD 检测准确率

用法:
  python3 evaluate.py                    # 检索层 + OOD 评测
  python3 evaluate.py --full             # 全管道评测（需要 LLM API Key）
"""

import json
import os
import sys
import time
import argparse
from pathlib import Path
from collections import defaultdict
from typing import Any, Optional

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
GOLDEN_PATH = BASE_DIR / "data" / "golden_set.json"


def cosine_sim(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))


# ── Helpers ──────────────────────────────────────────

def _normalize_name(s: str) -> str:
    """去除括号/空格差异，用于文件名匹配。"""
    return s.replace("(", "").replace(")", "").replace("（", "").replace("）", "").replace(" ", "").lower()


def _has_source_match(source_id: str, src_file: str) -> bool:
    """检查 source_chunk_id 是否与 retrieved source_file 匹配。"""
    if not source_id:
        return False
    return _normalize_name(source_id) in _normalize_name(src_file)


# ── RAG Evaluator ────────────────────────────────────

class RAGEvaluator:
    def __init__(self, golden_path=GOLDEN_PATH, enable_reranker: bool = False):
        with open(golden_path, "r", encoding="utf-8") as f:
            self.golden = json.load(f)
        self._searcher = None
        self._enable_reranker = enable_reranker

    @property
    def searcher(self):
        if self._searcher is None:
            from hybrid_search import HybridSearcher
            self._searcher = HybridSearcher(enable_reranker=self._enable_reranker)
        return self._searcher

    # ── A. 检索层评测 ────────────────────────────────

    def eval_retrieval(self, top_k: int = 5, limit: Optional[int] = None) -> dict:
        """
        检索层评测。

        两个层面:
          1. Source Match: 基于 source_chunk_id 严格匹配（51 题有标注）
          2. Semantic Relevance: 检索结果与 golden answer 的语义余弦相似度（全部 200 题）
        """
        samples = self.golden[:limit] if limit else self.golden
        n = len(samples)
        has_source = sum(1 for g in samples if g.get("source_chunk_id"))
        is_ood_count = sum(1 for g in samples if g["question_type"] == "OOD")
        is_indomain = n - is_ood_count
        print(f"\n[检索层评测] {n} 题 (含 source_chunk_id={has_source}, OOD={is_ood_count}, In-domain={is_indomain})")
        print(f"  top_k={top_k}\n")

        # 复用 HybridSearcher 的 HuggingFaceEmbeddings，避免重复加载模型
        embeddings = self.searcher.embeddings
        golden_texts = [g["answer"] for g in samples]
        golden_embs = embeddings.embed_documents(golden_texts)
        golden_embs = np.array(golden_embs)

        # 累积器
        mrr_source = 0.0        # MRR based on source_chunk_id match
        recall_k = {1: 0, 3: 0, 5: 0}
        precision_k = {1: 0, 3: 0, 5: 0}
        relevance_scores = []   # max cosine-sim per query
        retrieval_ndcg = []     # NDCG using golden-answer similarity as relevance
        chroma_sims = []        # real Chroma similarity scores (top1)

        # 分维度统计: field_name -> value -> {count, mrr, relevance, hits}
        per_field = {
            "province": defaultdict(lambda: {"count": 0, "mrr": 0.0, "relevance": 0.0, "hits": 0}),
            "crop": defaultdict(lambda: {"count": 0, "mrr": 0.0, "relevance": 0.0, "hits": 0}),
            "zoning_type": defaultdict(lambda: {"count": 0, "mrr": 0.0, "relevance": 0.0, "hits": 0}),
            "question_type": defaultdict(lambda: {"count": 0, "mrr": 0.0, "relevance": 0.0, "hits": 0}),
            "difficulty": defaultdict(lambda: {"count": 0, "mrr": 0.0, "relevance": 0.0, "hits": 0}),
        }

        for idx, g in enumerate(samples):
            query, golden_ans = g["question"], g["answer"]
            source_id = g.get("source_chunk_id", "")
            is_ood = g["question_type"] == "OOD"
            is_source_question = bool(source_id)

            # ── 检索 ──
            results = self.searcher.search(query, top_k=top_k, expand_parent=True)
            retrieved_texts = [r["content"] for r in results]

            # ── Source Match（仅对 51 题有效）──
            hits = []
            for r in results:
                hit = _has_source_match(source_id, r.get("metadata", {}).get("source_file", ""))
                hits.append(hit)

            # ── 语义相关度 (retrieved vs golden answer) ──
            if retrieved_texts:
                ret_embs = np.array(embeddings.embed_documents(retrieved_texts))
                rels = [cosine_sim(golden_embs[idx], re) for re in ret_embs]
                max_rel = max(rels)
            else:
                rels = [0.0] * top_k
                max_rel = 0.0

            relevance_scores.append(max_rel)

            # ── NDCG (用 golden-sim 作为 relevance gain) ──
            dcg = sum((2**r - 1) / np.log2(i + 2) for i, r in enumerate(rels))
            ideal_rels = sorted(rels, reverse=True)
            idcg = sum((2**r - 1) / np.log2(i + 2) for i, r in enumerate(ideal_rels))
            ndcg = dcg / idcg if idcg > 0 else 0.0
            retrieval_ndcg.append(ndcg)

            # ── Chroma 真实相似度 ──
            if results:
                chroma_sims.append(results[0].get("similarity", 0))
            else:
                chroma_sims.append(0)

            # ── Source MRR/Recall/Precision（仅算有 source_chunk_id 的题）──
            if is_source_question:
                rr = 0.0
                for i, h in enumerate(hits):
                    if h:
                        rr = 1.0 / (i + 1)
                        break
                mrr_source += rr
                for k in [1, 3, 5]:
                    if any(hits[:k]):
                        recall_k[k] += 1
                    precision_k[k] += sum(hits[:k]) / min(k, len(hits))

            # ── 分维度 ──
            for field_name in per_field:
                val = g.get(field_name, "unknown")
                cat = per_field[field_name][val]
                cat["count"] += 1
                cat["relevance"] += max_rel
                if is_source_question:
                    rr_for_cat = 0.0
                    any_hit = False
                    for i, h in enumerate(hits):
                        if h:
                            rr_for_cat = 1.0 / (i + 1)
                            any_hit = True
                            break
                    cat["mrr"] += rr_for_cat
                    if any_hit:
                        cat["hits"] += 1

            if (idx + 1) % 50 == 0:
                print(f"  进度: {idx + 1}/{n}")

        # ── 汇总 ──
        n_source = sum(1 for g in samples if g.get("source_chunk_id"))

        metrics = {
            "total": n,
            "with_source_id": n_source,
            "top_k": top_k,
            # Source-match metrics (仅 51 题)
            "mrr_source": mrr_source / n_source if n_source > 0 else 0,
            "recall": {k: recall_k[k] / n_source if n_source > 0 else 0 for k in [1, 3, 5]},
            "precision": {k: precision_k[k] / n_source if n_source > 0 else 0 for k in [1, 3, 5]},
            # Semantic relevance (全部 200 题)
            "avg_relevance": float(np.mean(relevance_scores)),
            "pct_relevant_06": sum(1 for s in relevance_scores if s > 0.6) / n,
            "pct_relevant_07": sum(1 for s in relevance_scores if s > 0.7) / n,
            "avg_ndcg": float(np.mean(retrieval_ndcg)),
            # In-domain only relevance
            "indomain_avg_relevance": float(np.mean([relevance_scores[i] for i, g in enumerate(samples) if g["question_type"] != "OOD"])),
            "indomain_pct_relevant_06": sum(1 for i, g in enumerate(samples) if g["question_type"] != "OOD" and relevance_scores[i] > 0.6) / is_indomain if is_indomain > 0 else 0,
            # Chroma real sim
            "avg_chroma_top1_sim": float(np.mean(chroma_sims)),
            "per_field": per_field,
            "detail": [],
        }

        # 分维度归一化
        for field_name in per_field:
            for cat in per_field[field_name].values():
                c = cat["count"]
                if c > 0:
                    cat["relevance"] /= c
                    source_qs_in_cat = sum(1 for g in samples
                                           if g.get(field_name) == list(per_field[field_name].keys())[list(per_field[field_name].values()).index(cat)]
                                           and g.get("source_chunk_id"))
                    # simpler: just divide by total count in category
                    cat["mrr"] = cat["mrr"] / cat["hits"] if cat["hits"] > 0 else 0

        return metrics

    # ── B. OOD 检测评测 ────────────────────────────────

    def eval_judge_ood(self, limit: Optional[int] = None) -> dict:
        """评测 Judge 三层 OOD 检测。"""
        samples = self.golden[:limit] if limit else self.golden
        n_ood = sum(1 for g in samples if g["question_type"] == "OOD")
        n_indomain = sum(1 for g in samples if g["question_type"] != "OOD")
        print(f"\n[Judge OOD 检测] {len(samples)} 题 (OOD={n_ood}, In-domain={n_indomain})")

        from judge import judge

        # 复用 HybridSearcher 的 Chroma 实例
        vectorstore = self.searcher.vectorstore

        result = {
            "ood_detected": 0, "ood_missed": 0,
            "indomain_kept": 0, "indomain_rejected": 0,
            "layer1": 0, "layer2": 0, "layer3": 0,
            "ood_sims": [],
            "indomain_sims": [],
            "details": [],
        }

        for g in samples:
            query = g["question"]
            is_ood = g["question_type"] == "OOD"

            # 用 Chroma 直接搜获取真实分数
            vec_results = vectorstore.similarity_search_with_score(query, k=3)
            real_top1_sim = vec_results[0][1] if vec_results else 0

            # 也用 hybrid search 走一遍
            results = self.searcher.search(query, top_k=3)
            j = judge(query, results)
            rejected = j["decision"] == "reject"

            if is_ood:
                result["ood_sims"].append(real_top1_sim)
                if rejected:
                    result["ood_detected"] += 1
                else:
                    result["ood_missed"] += 1
            else:
                result["indomain_sims"].append(real_top1_sim)
                if rejected:
                    result["indomain_rejected"] += 1
                else:
                    result["indomain_kept"] += 1

            if j["method"] == "signal":
                result["layer1"] += 1
            elif j["method"] == "score":
                result["layer2"] += 1
            else:
                result["layer3"] += 1

        total = len(samples)
        result.update({
            "total": total,
            "n_ood": n_ood, "n_indomain": n_indomain,
            "ood_recall": result["ood_detected"] / n_ood if n_ood > 0 else 1.0,
            "indomain_pass_rate": result["indomain_kept"] / n_indomain if n_indomain > 0 else 1.0,
            "accuracy": (result["ood_detected"] + result["indomain_kept"]) / total,
            "avg_ood_sim": float(np.mean(result["ood_sims"])) if result["ood_sims"] else 0,
            "avg_indomain_sim": float(np.mean(result["indomain_sims"])) if result["indomain_sims"] else 0,
            "min_ood_sim": float(min(result["ood_sims"])) if result["ood_sims"] else 0,
            "max_ood_sim": float(max(result["ood_sims"])) if result["ood_sims"] else 0,
        })

        return result

    # ── C. 忠实率 + 正确率评测（需 LLM）─────────────────────

    def eval_generation_faithfulness(self, limit: Optional[int] = None) -> dict:
        """
        全管道评测（抽样）：retrieve + judge + generate → 忠实率 & 正确率。
        """
        from llm_client import call_llm
        from judge import judge
        from generator import generate, build_context

        samples = [g for g in self.golden if g["question_type"] != "OOD"]
        if limit:
            import random
            random.seed(42)
            easy = [g for g in samples if g["difficulty"] == "easy"]
            med = [g for g in samples if g["difficulty"] == "medium"]
            hard = [g for g in samples if g["difficulty"] == "hard"]
            n_per = max(limit // 3, 3)
            sampled = (random.sample(easy, min(n_per, len(easy))) +
                       random.sample(med, min(n_per, len(med))) +
                       random.sample(hard, min(n_per, len(hard))))
            samples = sampled[:limit]

        n = len(samples)
        print(f"\n[忠实率+正确率评测] {n} 条 In-domain 题 (分层抽样)")

        FAITH_PROMPT = """评估生成答案是否忠实于参考资料。\n\n## 参考资料\n{context}\n\n## 问题\n{query}\n\n## 生成答案\n{answer}\n\n评分: 3=完全忠实 2=基本忠实 1=部分编造 0=完全编造\n输出 JSON: {{\"score\": 0-3, \"reason\": \"...\"}}"""

        CORRECT_PROMPT = """评估生成答案与参考答案的一致性。\n\n## 参考答案\n{golden}\n\n## 生成答案\n{answer}\n\n评分: 3=一致 2=基本一致(细节差异) 1=部分错误 0=完全错误\n输出 JSON: {{\"score\": 0-3, \"reason\": \"...\"}}"""

        metrics = {
            "total": n, "generated": 0, "rejected": 0, "errors": 0,
            "faith_scores": [], "correct_scores": [], "elapsed_ms": [],
            "judge_reject_in_domain": 0,
        }

        for idx, g in enumerate(samples):
            query = g["question"]
            try:
                t0 = time.time()
                results = self.searcher.search(query, top_k=5)
                j = judge(query, results)

                if j["decision"] == "reject":
                    gen_answer = "[拒答] " + j.get("reason", "")
                    metrics["rejected"] += 1
                    # 对 in-domain 被拒答做标记
                    from judge import judge as _j
                else:
                    gen_answer = generate(query, results, temperature=0.3)
                    metrics["generated"] += 1

                elapsed = (time.time() - t0) * 1000
                metrics["elapsed_ms"].append(elapsed)

                # 只对非拒答的做忠实率/正确率评估
                if j["decision"] != "reject":
                    ctx = build_context(results)
                    fp = FAITH_PROMPT.format(context=ctx[:2000], query=query, answer=gen_answer[:800])
                    try:
                        resp = call_llm([{"role": "user", "content": fp}], temperature=0.1, stream=False)
                        s = resp.find("{"); e = resp.rfind("}") + 1
                        faith = json.loads(resp[s:e]).get("score", -1) if s >= 0 else -1
                    except:
                        faith = -1
                    metrics["faith_scores"].append(faith)

                    cp = CORRECT_PROMPT.format(golden=g["answer"][:800], answer=gen_answer[:800])
                    try:
                        resp = call_llm([{"role": "user", "content": cp}], temperature=0.1, stream=False)
                        s = resp.find("{"); e = resp.rfind("}") + 1
                        correct = json.loads(resp[s:e]).get("score", -1) if s >= 0 else -1
                    except:
                        correct = -1
                    metrics["correct_scores"].append(correct)
                else:
                    metrics["faith_scores"].append(-1)
                    metrics["correct_scores"].append(-1)

                if (idx + 1) % 10 == 0:
                    print(f"  进度: {idx+1}/{n}  [生成={metrics['generated']} 拒答={metrics['rejected']}]")

            except Exception as ex:
                metrics["errors"] += 1
                metrics["faith_scores"].append(-1)
                metrics["correct_scores"].append(-1)
                print(f"  [{g['id']}] ERROR: {ex}")

        valid_f = [s for s in metrics["faith_scores"] if s >= 0]
        valid_c = [s for s in metrics["correct_scores"] if s >= 0]

        metrics.update({
            "avg_faithfulness": float(np.mean(valid_f)) if valid_f else 0,
            "pct_faith_3": sum(1 for s in valid_f if s == 3) / len(valid_f) * 100 if valid_f else 0,
            "pct_faith_2": sum(1 for s in valid_f if s == 2) / len(valid_f) * 100 if valid_f else 0,
            "pct_faith_bad": sum(1 for s in valid_f if s <= 1) / len(valid_f) * 100 if valid_f else 0,
            "avg_correctness": float(np.mean(valid_c)) if valid_c else 0,
            "pct_correct_3": sum(1 for s in valid_c if s == 3) / len(valid_c) * 100 if valid_c else 0,
            "pct_correct_2": sum(1 for s in valid_c if s == 2) / len(valid_c) * 100 if valid_c else 0,
            "pct_correct_bad": sum(1 for s in valid_c if s <= 1) / len(valid_c) * 100 if valid_c else 0,
            "avg_latency_ms": float(np.mean(metrics["elapsed_ms"])) if metrics["elapsed_ms"] else 0,
            "reject_rate": metrics["rejected"] / n * 100,
        })
        return metrics


# ── 报告输出 ──────────────────────────────────────────

def print_full_report(gen: dict):
    print(f"\n  {'─' * 55}")
    print(f"  📝 忠实率 & 正确率 (In-domain 生成评测)")
    print(f"  {'─' * 55}")
    print(f"  抽样数:         {gen['total']}")
    print(f"  成功生成:       {gen['generated']}")
    print(f"  被拒答:         {gen['rejected']} ({gen['reject_rate']:.1f}%)")
    print(f"  错误:           {gen['errors']}")
    print(f"  平均耗时:       {gen['avg_latency_ms']:.0f}ms")
    print(f"")
    print(f"  📊 忠实率 (Faithfulness):")
    print(f"    平均分:       {gen['avg_faithfulness']:.2f} / 3.0")
    print(f"    3分(完全忠实): {gen['pct_faith_3']:.1f}%")
    print(f"    2分(基本忠实): {gen['pct_faith_2']:.1f}%")
    print(f"    ≤1分(编造):   {gen['pct_faith_bad']:.1f}%")
    print(f"")
    print(f"  📊 答案正确率 (Correctness vs Golden):")
    print(f"    平均分:       {gen['avg_correctness']:.2f} / 3.0")
    print(f"    3分(一致):    {gen['pct_correct_3']:.1f}%")
    print(f"    2分(基本一致): {gen['pct_correct_2']:.1f}%")
    print(f"    ≤1分(错误):   {gen['pct_correct_bad']:.1f}%")

    # 综合指标
    faithful = gen['pct_faith_3'] + gen['pct_faith_2']
    correct = gen['pct_correct_3'] + gen['pct_correct_2']
    print(f"")
    print(f"  🎯 综合:")
    print(f"    忠实率 (≥2分): {faithful:.1f}%")
    print(f"    正确率 (≥2分): {correct:.1f}%")


def print_report(ret: dict, ood: dict):
    print(f"\n{'=' * 65}")
    print(f"  RAG Golden Set 评测报告")
    print(f"{'=' * 65}")
    print(f"  总题数: {ret['total']}  (含 source_chunk_id: {ret['with_source_id']})")
    print(f"  Top-K:   {ret['top_k']}")

    # ── 1. 检索指标 ──
    print(f"\n  {'─' * 55}")
    print(f"  📊 检索指标 (Source Match, n={ret['with_source_id']} 题有标注)")
    print(f"  {'─' * 55}")
    print(f"  MRR:                            {ret['mrr_source']:.4f}")
    for k in [1, 3, 5]:
        print(f"  Recall@{k}:                       {ret['recall'][k]:.4f} ({ret['recall'][k]*100:.1f}%)")
    for k in [1, 3, 5]:
        print(f"  Precision@{k}:                    {ret['precision'][k]:.4f} ({ret['precision'][k]*100:.1f}%)")

    print(f"\n  {'─' * 55}")
    print(f"  📊 语义相关度 (全部 {ret['total']} 题)")
    print(f"  {'─' * 55}")
    print(f"  平均最大语义相似度:               {ret['avg_relevance']:.4f}")
    print(f"  相关率 (sim>0.6):                 {ret['pct_relevant_06']:.4f} ({ret['pct_relevant_06']*100:.1f}%)")
    print(f"  高相关率 (sim>0.7):               {ret['pct_relevant_07']:.4f} ({ret['pct_relevant_07']*100:.1f}%)")
    print(f"  平均 NDCG@5:                      {ret['avg_ndcg']:.4f}")
    print(f"  In-domain 平均相关度:             {ret['indomain_avg_relevance']:.4f}")
    print(f"  In-domain 相关率(sim>0.6):        {ret['indomain_pct_relevant_06']:.4f} ({ret['indomain_pct_relevant_06']*100:.1f}%)")

    # ── 2. OOD 检测 ──
    print(f"\n  {'─' * 55}")
    print(f"  🛡️  OOD 检测 (Judge Layer 1-2)")
    print(f"  {'─' * 55}")
    print(f"  OOD 题数:      {ood['n_ood']}")
    print(f"  In-domain 题数: {ood['n_indomain']}")
    print(f"  OOD 召回率:     {ood['ood_recall']:.4f} ({ood['ood_recall']*100:.1f}%)")
    print(f"  In-domain 通过: {ood['indomain_pass_rate']:.4f} ({ood['indomain_pass_rate']*100:.1f}%)")
    print(f"  总准确率:       {ood['accuracy']:.4f} ({ood['accuracy']*100:.1f}%)")
    print(f"  误拒 (in-domain→reject): {ood['indomain_rejected']}")
    print(f"  漏判 (OOD→pass):         {ood['ood_missed']}")
    print(f"\n  OOD 真实 Chroma 相似度: avg={ood['avg_ood_sim']:.4f} min={ood['min_ood_sim']:.4f} max={ood['max_ood_sim']:.4f}")
    print(f"  In-domain 真实相似度:    avg={ood['avg_indomain_sim']:.4f}")
    print(f"  Layer 分布: signal={ood['layer1']}, score={ood['layer2']}, llm={ood['layer3']}")

    # ── 3. 按维度细分 ──
    for field_name, title in [
        ("province", "省份"),
        ("difficulty", "难度"),
        ("question_type", "问题类型"),
        ("zoning_type", "区划类型"),
        ("crop", "作物"),
    ]:
        print(f"\n  {'─' * 55}")
        print(f"  📂 按{title}")
        print(f"  {'─' * 55}")
        data = ret["per_field"][field_name]
        for cat, v in sorted(data.items(), key=lambda x: -x[1]["count"]):
            if v["count"] >= 2:
                rel_pct = f"{v['relevance']*100:.1f}%"
                print(f"  {cat:<14s} n={v['count']:>3d}  相关度={rel_pct:>6s}  MRR={v['mrr']:.3f}  Hits={v['hits']}")

    # ── 4. 问题诊断 ──
    print(f"\n  {'─' * 55}")
    print(f"  ⚠️  诊断与建议")
    print(f"  {'─' * 55}")

    # Judge 的 rank-based scoring 问题
    if ood["ood_recall"] < 0.3:
        print(f"  1. OOD 检测率低: HybridSearcher 使用 rank-based 相似度 (1/rank)")
        print(f"     而非真实余弦距离，导致所有查询 top1 sim=1.0 均超过 0.46 阈值。")
        print(f"     建议: hybrid_search.py 中改回 Chroma 真实 similarity_with_score,")
        print(f"     或对 BM25 结果使用归一化 IDF 分数。")

    if ret["mrr_source"] < 0.3:
        print(f"  2. Source MRR 偏低 ({ret['mrr_source']:.3f}): 仅 {ret['with_source_id']} 题有 source_id 标注。")
        print(f"     建议: 补充 golden set 中的 source_chunk_id 字段以支持更精确的评测。")

    if ret["indomain_pct_relevant_06"] < 0.85:
        print(f"  3. In-domain 相关率 {ret['indomain_pct_relevant_06']*100:.0f}% 有提升空间。")
        print(f"     建议: 调优 EnsembleRetriever 权重, 或增大 top_k 配合 Reranker 使用。")

    # OOD 真实 sim 分析
    if ood.get("avg_ood_sim", 0) > 1.5:
        print(f"  4. OOD 查询的 Chroma 真实距离偏低 (avg={ood['avg_ood_sim']:.2f})。")
        print(f"     可设 distance > 1.5 作为 Layer 2 reject 阈值。")
    else:
        print(f"  4. OOD 查询 Chrome 距离: avg={ood['avg_ood_sim']:.2f}。")
        print(f"     说明 OOD 查询在向量空间中能找到'近邻'内容（话题漂移）。")


# ── 主函数 ──────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Golden Set 评测")
    parser.add_argument("--full", action="store_true", help="全管道评测（需要 LLM）")
    parser.add_argument("--reranker", action="store_true", help="启用 CrossEncoder Reranker 精排")
    parser.add_argument("--limit", type=int, default=None, help="限制评测数量")
    parser.add_argument("--top-k", type=int, default=5, help="检索返回数量")
    parser.add_argument("--output", type=str, default=None, help="保存结果 JSON")
    args = parser.parse_args()

    if not os.path.exists(GOLDEN_PATH):
        print("请先运行 python3 generate_golden_set.py")
        sys.exit(1)

    evaluator = RAGEvaluator(enable_reranker=args.reranker)

    # A. 检索层评测
    ret = evaluator.eval_retrieval(top_k=args.top_k, limit=args.limit)

    # B. OOD 检测
    ood = evaluator.eval_judge_ood(limit=args.limit)

    # C. 打印报告
    print_report(ret, ood)

    # D. 全管道评测 — 忠实率 + 答案正确率
    if args.full:
        has_key = bool(os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"))
        if not has_key:
            print("\n⚠️  全管道评测需要 LLM API Key")
            sys.exit(1)
        gen_metrics = evaluator.eval_generation_faithfulness(limit=args.limit)
        print_full_report(gen_metrics)

    # 保存
    if args.output:
        out = {
            "retrieval": {
                "total": ret["total"],
                "with_source_id": ret["with_source_id"],
                "mrr_source": ret["mrr_source"],
                "recall": ret["recall"],
                "precision": ret["precision"],
                "avg_relevance": ret["avg_relevance"],
                "pct_relevant_06": ret["pct_relevant_06"],
                "avg_ndcg": ret["avg_ndcg"],
                "indomain_avg_relevance": ret["indomain_avg_relevance"],
                "indomain_pct_relevant_06": ret["indomain_pct_relevant_06"],
                "avg_chroma_top1_sim": ret["avg_chroma_top1_sim"],
            },
            "ood_detection": {
                "n_ood": ood["n_ood"],
                "n_indomain": ood["n_indomain"],
                "ood_recall": ood["ood_recall"],
                "indomain_pass_rate": ood["indomain_pass_rate"],
                "accuracy": ood["accuracy"],
                "avg_ood_sim": ood["avg_ood_sim"],
                "avg_indomain_sim": ood["avg_indomain_sim"],
            },
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\n结果已保存至: {args.output}")


if __name__ == "__main__":
    main()
