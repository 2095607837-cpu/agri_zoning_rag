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
from concurrent.futures import ThreadPoolExecutor, as_completed
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


# ── 分层抽样 ──────────────────────────────────────────

def _stratified_sample(golden: list[dict], limit: int, seed: int = 42) -> list[dict]:
    """分层抽样：优先保证 OOD、难度、省份、问题类型的覆盖面。

    层级优先级：
      1. OOD — 上限 10% quota，下限 2 题
      2. 难度 (easy/medium/hard) — 每层 ≥1 题，按原比例分配
      3. 省份 — 每省 ≥1 题（如果 quota 够）
      4. 问题类型 — 每种 ≥1 题（如果 quota 够）
      5. 剩余按原分布随机填充

    与 self.golden[:limit] 对比：
      - 旧方案：取前 N 题 → 严重偏向前几个省份和 easy 题
      - 新方案：各维度均衡覆盖，seed 固定可复现
    """
    import random
    rng = random.Random(seed)

    if limit is None or limit >= len(golden):
        return list(golden)

    ood = [g for g in golden if g.get("question_type") == "OOD"]
    indomain = [g for g in golden if g.get("question_type") != "OOD"]

    sampled = []
    pool = list(indomain)  # 剩余候选池

    # ── Layer 1: OOD ──
    ood_n = max(2, min(len(ood), limit // 10))
    sampled.extend(rng.sample(ood, min(ood_n, len(ood))))
    quota = limit - len(sampled)

    # ── Layer 2: 难度分层 ──
    from collections import defaultdict
    diff_groups = defaultdict(list)
    for g in pool:
        diff_groups[g.get("difficulty", "medium")].append(g)

    diff_n = {}
    for diff in ["hard", "medium", "easy"]:
        items = diff_groups.get(diff, [])
        if items:
            # 保证每层 ≥1，权重按 √(层大小) 缓和极端分布
            diff_n[diff] = max(1, int(quota * (len(items) ** 0.5) /
                                       sum(len(v) ** 0.5 for v in diff_groups.values())))

    # 确保总和 ≤ quota
    while sum(diff_n.values()) > quota:
        largest = max(diff_n, key=diff_n.get)
        if diff_n[largest] > 1:
            diff_n[largest] -= 1

    for diff, n in diff_n.items():
        items = diff_groups.get(diff, [])
        chosen = rng.sample(items, min(n, len(items)))
        sampled.extend(chosen)
        for g in chosen:
            pool.remove(g)

    # ── Layer 3: 省份补位 ──
    quota = limit - len(sampled)
    if quota > 0:
        prov_groups = defaultdict(list)
        for g in pool:
            prov_groups[g.get("province", "?")].append(g)

        missing_provs = [p for p in prov_groups if p not in
                         {g.get("province") for g in sampled}]
        for prov in missing_provs:
            if quota <= 0:
                break
            items = prov_groups[prov]
            g = rng.choice(items)
            sampled.append(g)
            pool.remove(g)
            quota -= 1

    # ── Layer 4: 问题类型补位 ──
    if quota > 0:
        type_groups = defaultdict(list)
        for g in pool:
            type_groups[g.get("question_type", "?")].append(g)

        missing_types = [t for t in type_groups if t not in
                         {g.get("question_type") for g in sampled}]
        for qt in missing_types:
            if quota <= 0:
                break
            items = type_groups[qt]
            g = rng.choice(items)
            sampled.append(g)
            pool.remove(g)
            quota -= 1

    # ── Layer 5: 随机填满 ──
    quota = limit - len(sampled)
    if quota > 0:
        rng.shuffle(pool)
        sampled.extend(pool[:quota])

    rng.shuffle(sampled)
    return sampled


# ── RAG Evaluator ────────────────────────────────────

class RAGEvaluator:
    def __init__(self, golden_path=GOLDEN_PATH, enable_reranker: bool = False,
                 enable_rewrite: bool = False, max_workers: int = 4):
        with open(golden_path, "r", encoding="utf-8") as f:
            self.golden = json.load(f)
        self._searcher = None
        self._enable_reranker = enable_reranker
        self._enable_rewrite = enable_rewrite
        self._max_workers = max_workers

    @property
    def searcher(self):
        if self._searcher is None:
            from hybrid_search import HybridSearcher
            self._searcher = HybridSearcher(enable_reranker=self._enable_reranker)
        return self._searcher

    # ── A+B. 检索层 + OOD 检测（单次遍历）────────────────

    _FIELD_NAMES = ("province", "crop", "zoning_type", "question_type", "difficulty")

    def _process_one_query(self, args: tuple) -> dict:
        """处理单个 query：检索 + 去重 + Judge + source match。线程安全。"""
        from judge import judge
        from query_rewriter import expand_query

        idx, g, top_k = args
        query = g["question"]
        source_id = g.get("source_chunk_id", "")
        is_ood = g["question_type"] == "OOD"
        is_source_question = bool(source_id)

        result = {
            "idx": idx,
            "is_ood": is_ood,
            "golden_fields": {f: g.get(f, "unknown") for f in self._FIELD_NAMES},
        }

        # 原始 query 检索 — 用于 Judge OOD 判定（不受改写干扰）
        judge_results_raw = self.searcher.search(query, top_k=top_k, expand_context=True)

        if self._enable_rewrite:
            search_queries = expand_query(query, mode="multi_view")
            extra_queries = [sq for sq in search_queries if sq != query]
            all_results_list = list(judge_results_raw)
            if extra_queries:
                # 改写子查询并发检索
                with ThreadPoolExecutor(max_workers=min(len(extra_queries), 4)) as ex:
                    futures = {ex.submit(self.searcher.search, sq, top_k, True): sq for sq in extra_queries}
                    for f in as_completed(futures):
                        all_results_list.extend(f.result())
        else:
            all_results_list = judge_results_raw

        # 去重：按 content 前 80 字符，保留最高相似度
        seen = {}
        for r in all_results_list:
            key = r["content"][:80]
            sim = r.get("similarity", 0)
            if key not in seen or sim > seen[key].get("similarity", 0):
                seen[key] = r
        results = sorted(seen.values(), key=lambda r: r.get("similarity", 0), reverse=True)[:top_k]
        judge_results = judge_results_raw[:top_k]

        result["retrieved_texts"] = (idx, [(pos, r["content"]) for pos, r in enumerate(results)])
        result["top1_sim"] = results[0].get("similarity", 0) if results else 0

        # Source Match
        hits = [False] * len(results)
        for i, r in enumerate(results):
            if _has_source_match(source_id, r.get("metadata", {}).get("source_file", "")):
                hits[i] = True

        if is_source_question:
            rr = 0.0
            for i, h in enumerate(hits):
                if h:
                    rr = 1.0 / (i + 1)
                    break
            result["source_metrics"] = {
                "rr": rr,
                "recall": {1: any(hits[:1]), 3: any(hits[:3]), 5: any(hits[:5])},
                "precision": {1: sum(hits[:1]) / 1, 3: sum(hits[:3]) / 3, 5: sum(hits[:5]) / 5},
            }

        # Judge（用原始 query 检索结果，避免改写干扰）
        j = judge(query, judge_results)
        result["rejected"] = j["decision"] == "reject"
        result["judge_method"] = j["method"]

        return result

    def eval_retrieval(self, top_k: int = 5, limit: Optional[int] = None) -> dict:
        """
        检索层评测 + OOD 检测，一次遍历完成。

        两个层面:
          1. Source Match: 基于 source_chunk_id 严格匹配
          2. Semantic Relevance: 检索结果与 golden answer 的语义余弦相似度（batch embedding）
        同时采集 OOD 检测指标（复用同一次检索结果）。
        """
        from judge import judge

        samples = _stratified_sample(self.golden, limit) if limit else self.golden
        n = len(samples)
        has_source = sum(1 for g in samples if g.get("source_chunk_id"))
        is_ood_count = sum(1 for g in samples if g["question_type"] == "OOD")
        is_indomain = n - is_ood_count
        print(f"\n[检索+OOD评测] {n} 题 (含 source_chunk_id={has_source}, OOD={is_ood_count}, In-domain={is_indomain})")
        print(f"  top_k={top_k}\n")

        embeddings = self.searcher.embeddings
        golden_texts = [g["answer"] for g in samples]
        golden_embs = np.array(embeddings.embed_documents(golden_texts))

        # ── 检索累积器 ──
        mrr_source = 0.0
        recall_k = {1: 0, 3: 0, 5: 0}
        precision_k = {1: 0, 3: 0, 5: 0}
        chroma_sims = []
        retrieval_ndcg = []
        relevance_scores = [0.0] * n
        per_field = {
            "province": defaultdict(lambda: {"count": 0, "mrr": 0.0, "relevance": 0.0, "hits": 0}),
            "crop": defaultdict(lambda: {"count": 0, "mrr": 0.0, "relevance": 0.0, "hits": 0}),
            "zoning_type": defaultdict(lambda: {"count": 0, "mrr": 0.0, "relevance": 0.0, "hits": 0}),
            "question_type": defaultdict(lambda: {"count": 0, "mrr": 0.0, "relevance": 0.0, "hits": 0}),
            "difficulty": defaultdict(lambda: {"count": 0, "mrr": 0.0, "relevance": 0.0, "hits": 0}),
        }

        # ── OOD 累积器 ──
        ood_detected = ood_missed = 0
        indomain_kept = indomain_rejected = 0
        layer1 = layer2 = layer3 = layer4 = 0
        ood_sims = []
        indomain_sims = []

        # ── 延迟 embedding：先收集所有检索结果文本，最后 batch embed ──
        retrieval_records = []  # [(query_idx, pos, text)]

        # ── 并行检索+Judge：主查询 6 路并发 + 改写子查询并行 ──
        workers = min(self._max_workers, n)
        print(f"  并行度: {workers} workers")
        args_list = [(idx, g, top_k) for idx, g in enumerate(samples)]

        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(self._process_one_query, args): args[0] for args in args_list}
            for f in as_completed(futures):
                r = f.result()
                idx = r["idx"]
                completed += 1
                if completed % 10 == 0 or completed == n:
                    print(f"  [{completed}/{n}] 已完成...")

                # ── 收集检索文本 ──
                q_idx, texts = r["retrieved_texts"]
                for pos, text in texts:
                    retrieval_records.append((q_idx, pos, text))

                # ── Chroma 相似度 ──
                chroma_sims.append(r["top1_sim"])

                # ── Source Match 累加 ──
                if r.get("source_metrics"):
                    sm = r["source_metrics"]
                    mrr_source += sm["rr"]
                    for k in [1, 3, 5]:
                        if sm["recall"][k]:
                            recall_k[k] += 1
                        precision_k[k] += sm["precision"][k]
                    for f_name in per_field:
                        per_field[f_name][r["golden_fields"][f_name]]["mrr"] += sm["rr"]
                        per_field[f_name][r["golden_fields"][f_name]]["hits"] += 1

                # ── OOD 检测累加 ──
                is_ood = r["is_ood"]
                top1_sim = r["top1_sim"]
                if is_ood:
                    ood_sims.append(top1_sim)
                    if r["rejected"]:
                        ood_detected += 1
                    else:
                        ood_missed += 1
                else:
                    indomain_sims.append(top1_sim)
                    if r["rejected"]:
                        indomain_rejected += 1
                    else:
                        indomain_kept += 1

                # ── Judge Layer 分布 ──
                layer_map = {"signal": "layer1", "high_sim": "layer2", "score": "layer3", "llm": "layer4"}
                layer_name = layer_map.get(r["judge_method"], "unknown")
                if layer_name == "layer1": layer1 += 1
                elif layer_name == "layer2": layer2 += 1
                elif layer_name == "layer3": layer3 += 1
                else: layer4 += 1

                # ── 分维度 count（relevance 稍后 batch 算完再填）──
                for f_name in per_field:
                    per_field[f_name][r["golden_fields"][f_name]]["count"] += 1

        # ── Batch embed 所有检索结果，一次性计算语义相关度 ──
        print(f"  正在批量计算语义相关度 ({len(retrieval_records)} 条文本)...")
        all_texts = [r[2] for r in retrieval_records]
        all_ret_embs = np.array(embeddings.embed_documents(all_texts))

        # 按 query 分组计算 relevance
        query_rel_groups = defaultdict(list)  # query_idx → [(pos, rel)]
        for (query_idx, pos, _), ret_emb in zip(retrieval_records, all_ret_embs):
            rel = cosine_sim(golden_embs[query_idx], ret_emb)
            query_rel_groups[query_idx].append((pos, rel))

        for idx in range(n):
            rels_for_q = query_rel_groups.get(idx, [])
            if rels_for_q:
                rel_values = [r[1] for r in sorted(rels_for_q, key=lambda x: -x[1])]
                max_rel = max(rel_values)
                relevance_scores[idx] = max_rel
                # NDCG
                dcg = sum((2**r - 1) / np.log2(i + 2) for i, r in enumerate(rel_values[:top_k]))
                ideal_rels = sorted(rel_values[:top_k], reverse=True)
                idcg = sum((2**r - 1) / np.log2(i + 2) for i, r in enumerate(ideal_rels))
                retrieval_ndcg.append(dcg / idcg if idcg > 0 else 0.0)
            else:
                relevance_scores[idx] = 0.0
                retrieval_ndcg.append(0.0)

        # 回填分维度 relevance
        for idx, g in enumerate(samples):
            max_rel = relevance_scores[idx]
            for field_name in per_field:
                val = g.get(field_name, "unknown")
                per_field[field_name][val]["relevance"] += max_rel

        # ── 汇总检索指标 ──
        n_source = sum(1 for g in samples if g.get("source_chunk_id"))

        ret_metrics = {
            "total": n,
            "with_source_id": n_source,
            "top_k": top_k,
            "mrr_source": mrr_source / n_source if n_source > 0 else 0,
            "recall": {k: recall_k[k] / n_source if n_source > 0 else 0 for k in [1, 3, 5]},
            "precision": {k: precision_k[k] / n_source if n_source > 0 else 0 for k in [1, 3, 5]},
            "avg_relevance": float(np.mean(relevance_scores)),
            "pct_relevant_06": sum(1 for s in relevance_scores if s > 0.6) / n,
            "pct_relevant_07": sum(1 for s in relevance_scores if s > 0.7) / n,
            "avg_ndcg": float(np.mean(retrieval_ndcg)),
            "indomain_avg_relevance": float(np.mean([relevance_scores[i] for i, g in enumerate(samples) if g["question_type"] != "OOD"])),
            "indomain_pct_relevant_06": sum(1 for i, g in enumerate(samples) if g["question_type"] != "OOD" and relevance_scores[i] > 0.6) / is_indomain if is_indomain > 0 else 0,
            "avg_chroma_top1_sim": float(np.mean(chroma_sims)),
            "per_field": per_field,
            "detail": [],
        }

        for field_name in per_field:
            for cat in per_field[field_name].values():
                c = cat["count"]
                if c > 0:
                    cat["relevance"] /= c
                    cat["mrr"] = cat["mrr"] / cat["hits"] if cat["hits"] > 0 else 0

        # ── 汇总 OOD 指标 ──
        ood_metrics = {
            "total": n, "n_ood": is_ood_count, "n_indomain": is_indomain,
            "ood_detected": ood_detected, "ood_missed": ood_missed,
            "indomain_kept": indomain_kept, "indomain_rejected": indomain_rejected,
            "ood_recall": ood_detected / is_ood_count if is_ood_count > 0 else 1.0,
            "indomain_pass_rate": indomain_kept / is_indomain if is_indomain > 0 else 1.0,
            "accuracy": (ood_detected + indomain_kept) / n,
            "avg_ood_sim": float(np.mean(ood_sims)) if ood_sims else 0,
            "avg_indomain_sim": float(np.mean(indomain_sims)) if indomain_sims else 0,
            "min_ood_sim": float(min(ood_sims)) if ood_sims else 0,
            "max_ood_sim": float(max(ood_sims)) if ood_sims else 0,
            "layer1": layer1, "layer2": layer2, "layer3": layer3, "layer4": layer4,
        }

        return ret_metrics, ood_metrics

    # ── C. 忠实率 + 正确率评测（需 LLM）─────────────────────

    def eval_generation_faithfulness(self, limit: Optional[int] = None) -> dict:
        """
        全管道评测（抽样）：retrieve + judge + generate → 忠实率 & 正确率。
        """
        from llm_client import call_llm
        from judge import judge
        from generator import generate, build_context

        indomain = [g for g in self.golden if g["question_type"] != "OOD"]
        if limit:
            samples = _stratified_sample(indomain, limit)
        else:
            samples = list(indomain)

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

                print(f"  [{idx+1}/{n}] query={g['id']}  [生成={metrics['generated']} 拒答={metrics['rejected']}]")

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
    print(f"  Layer 分布: signal(L1)={ood['layer1']}, high_sim(L2)={ood['layer2']}, score(L3)={ood['layer3']}, llm(L4)={ood['layer4']}")

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
    if ood.get("avg_ood_sim", 0) < 0.55:
        print(f"  4. OOD 余弦相似度偏低 (avg={ood['avg_ood_sim']:.4f})，向量空间分离良好。")
        print(f"     OOD 查询与知识库距离远，Judge score 层可有效拦截。")
    else:
        print(f"  4. OOD 余弦相似度偏高 (avg={ood['avg_ood_sim']:.4f})，存在话题漂移。")
        print(f"     OOD 查询在向量空间中仍有近邻内容，需依赖 LLM 层细判。")


# ── 主函数 ──────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Golden Set 评测")
    parser.add_argument("--full", action="store_true", help="全管道评测（需要 LLM）")
    parser.add_argument("--reranker", action="store_true", help="启用 CrossEncoder Reranker 精排")
    parser.add_argument("--rewrite", action="store_true", help="启用查询改写（需要 LLM）")
    parser.add_argument("--precompute-rewrites", action="store_true", help="预计算所有评测问题的改写结果并缓存到文件")
    parser.add_argument("--limit", type=int, default=None, help="限制评测数量")
    parser.add_argument("--top-k", type=int, default=5, help="检索返回数量")
    parser.add_argument("--workers", type=int, default=4, help="并发 worker 数（评测加速）")
    parser.add_argument("--output", type=str, default=None, help="保存结果 JSON")
    args = parser.parse_args()

    if not os.path.exists(GOLDEN_PATH):
        print("请先运行 python3 generate_golden_set.py")
        sys.exit(1)

    if args.precompute_rewrites:
        from query_rewriter import precompute_rewrites
        with open(GOLDEN_PATH, "r", encoding="utf-8") as f:
            golden = json.load(f)
        queries = list({g["question"] for g in golden})
        print(f"[precompute] 预计算 {len(queries)} 个唯一问题的改写结果...")
        precompute_rewrites(queries)
        print("[precompute] 完成。可直接运行 python3 evaluate.py --rewrite 进行评测。")
        sys.exit(0)

    evaluator = RAGEvaluator(enable_reranker=args.reranker, enable_rewrite=args.rewrite,
                             max_workers=args.workers)

    # A+B. 检索层 + OOD 检测（单次遍历）
    ret, ood = evaluator.eval_retrieval(top_k=args.top_k, limit=args.limit)

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
