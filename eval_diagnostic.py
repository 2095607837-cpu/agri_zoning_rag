"""漏召回多环节诊断模块。

分析框架（逐环节信号采集→机器分桶→定位修改环节）：

  Layer 1 (数据层):  gold chunk 是否在语料中
  Layer 2 (检索层):  Dense top-100 / BM25 top-100 / RRF fused rank
  Layer 3 (精排层):  CE rank vs RRF rank — CE 救回还是搅黄？
  Layer 4 (改写层):  Rewrite 是否触发？改写是否命中 gold？
  Layer 5 (内容层):  Query-Gold 直接余弦，内容级语义鸿沟

机器分桶:
  A-数据层:       标注错误
  B-检索层:       高/中/低余弦未召回（按 cos 三分）
  C-RRF融合:      单通道强但被稀释 / 排名偏低 / 排名靠后
  D-CE精排:       严重搅黄 / 轻微搅黄 / 未选入
  E-改写层:       gate误拦 / 未触发 / 改写未命中 / 改写命中但未进
  F-其他

用法:
  from eval_diagnostic import DiagnosticAnalyzer
  analyzer = DiagnosticAnalyzer(searcher, chunks, rewrite_map)
  report = analyzer.analyze(zero_recall_questions)
  analyzer.print_report(report)
"""

import json
import numpy as np
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed


class DiagnosticAnalyzer:
    def __init__(self, searcher, chunks: list, rewrite_map: dict[str, list[str]]):
        self.searcher = searcher
        self.emb = searcher.embeddings
        self.rewrite_map = rewrite_map
        self.cid_to_chunk = {}
        for c in chunks:
            cid = c["id"]
            if cid not in self.cid_to_chunk or c.get("metadata", {}).get("chunk_index", 0) == 0:
                self.cid_to_chunk[cid] = c
        self.sec_to_cids = {}
        for c in chunks:
            self.sec_to_cids.setdefault(c["metadata"]["section_id"], []).append(c["id"])

    def analyze(self, zero_qs: list[dict], workers: int = 4) -> list[dict]:
        """对 R@10=0 的题逐环节采集信号，返回诊断结果列表。"""
        rows = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(self._diagnose_one, q): q["id"] for q in zero_qs}
            for f in as_completed(futures):
                rows.append(f.result())
        return rows

    def _diagnose_one(self, q: dict) -> dict:
        query = q["question"]
        gold_cids = q["gold_chunks"]
        extra = self.rewrite_map.get(query, [])

        row = {
            "id": q["id"], "query": query,
            "capability": q.get("capability", ""),
            "difficulty": q.get("difficulty", ""),
        }

        # ── Layer 1: 数据层 ──
        gold_contents = [self.cid_to_chunk[cid] for cid in gold_cids if cid in self.cid_to_chunk]
        missing = [cid for cid in gold_cids if cid not in self.cid_to_chunk]
        row["gold_missing"] = missing
        if not gold_contents:
            row["category"] = "A-数据层"
            row["detail"] = f"标注错误: {len(missing)} gold chunk 不在语料中"
            row["max_gold_cos"] = 0
            return row

        gold_sections = {c["metadata"]["section_id"] for c in gold_contents}
        row["gold_sections"] = list(gold_sections)

        # ── Layer 5: 内容层（先算，供后续判断） ──
        qv = np.array(self.emb.embed_query(query))
        cos_sims = []
        for gc in gold_contents[:5]:
            dv = np.array(self.emb.embed_documents([gc["content"]])[0])
            sim = float(np.dot(qv, dv) / (np.linalg.norm(qv) * np.linalg.norm(dv) + 1e-8))
            cos_sims.append(sim)
        max_cos = max(cos_sims) if cos_sims else 0
        row["max_gold_cos"] = round(max_cos, 4)
        row["avg_gold_cos"] = round(np.mean(cos_sims), 4) if cos_sims else 0

        # ── Layer 2: 检索层 ──
        dense_raw = self.searcher.vectorstore.similarity_search_with_score(query, k=100)
        dense_sids = [doc.metadata.get("section_id", "") for doc, _ in dense_raw]
        row["dense_rank"] = next((i for i, s in enumerate(dense_sids) if s in gold_sections), -1)

        bm25_docs = self.searcher._bm25_retriever.invoke(query)[:100]
        bm25_sids = [d.metadata.get("section_id", "") for d in bm25_docs]
        row["bm25_rank"] = next((i for i, s in enumerate(bm25_sids) if s in gold_sections), -1)

        # RRF fused rank (with single-channel boost)
        RRF_K = 60; rrf = {}
        dense_ranked_sids = []  # Dense top-N section_id 排序（CE 保送用）
        dense_sids = set()
        bm25_sids = set()
        for rank, (doc, _) in enumerate(dense_raw):
            sid = doc.metadata.get("section_id", "")
            dense_ranked_sids.append(sid)
            dense_sids.add(sid)
            rrf[sid] = rrf.get(sid, 0) + 0.7 / (RRF_K + rank)
        for rank, doc in enumerate(bm25_docs):
            sid = doc.metadata.get("section_id", "")
            bm25_sids.add(sid)
            rrf[sid] = rrf.get(sid, 0) + 0.3 / (RRF_K + rank)
        # Single-channel boost
        for sid in list(rrf.keys()):
            in_dense = sid in dense_sids
            in_bm25 = sid in bm25_sids
            if in_dense and not in_bm25:
                rrf[sid] = rrf[sid] / 0.7
            elif in_bm25 and not in_dense:
                rrf[sid] = rrf[sid] / 0.3
        rrf_sorted = sorted(rrf.items(), key=lambda x: -x[1])
        row["rrf_rank"] = next((i for i, (s, _) in enumerate(rrf_sorted) if s in gold_sections), -1)
        # CE 候选池 = RRF top-30 ∪ Dense top-5
        in_rrf30 = row["rrf_rank"] >= 0 and row["rrf_rank"] < 30
        dense_top5 = dense_ranked_sids[:5]
        in_dense5 = any(s in gold_sections for s in dense_top5)
        row["gold_in_ce_input"] = in_rrf30 or in_dense5

        # ── Layer 3: 精排层（Append CE） ──
        _, ce_results = self.searcher.search_multi_query(
            query, top_k=30, expand_context=False, extra_queries=extra)
        ce_sids = [r["metadata"].get("section_id", "") for r in ce_results]
        row["ce_rank"] = next((i for i, s in enumerate(ce_sids) if s in gold_sections), -1)
        if row["rrf_rank"] >= 0 and row["ce_rank"] >= 0:
            row["ce_delta"] = row["ce_rank"] - row["rrf_rank"]
        else:
            row["ce_delta"] = None
        row["top1_sim"] = ce_results[0].get("dense_similarity", ce_results[0].get("similarity", 0)) if ce_results else 0

        # ── Layer 4: 改写层 ──
        row["has_rewrite"] = len(extra) > 0
        row["rewrite_count"] = len(extra)
        if extra:
            rw_hit = False
            for rq in extra:
                rq_results = self.searcher.search(rq, top_k=20, expand_context=False, skip_reranker=True)
                rq_sids = [rr["metadata"].get("section_id", "") for rr in rq_results]
                if any(s in gold_sections for s in rq_sids):
                    rw_hit = True; break
            row["rewrite_hit_gold"] = rw_hit
        else:
            row["rewrite_hit_gold"] = False

        # ── 机器分桶 ──
        row["category"], row["detail"] = self._classify(row)
        return row

    def _classify(self, r: dict) -> tuple[str, str]:
        """按逐层信号定位失败环节。"""
        dr, br, rr, cr = r["dense_rank"], r["bm25_rank"], r["rrf_rank"], r["ce_rank"]
        cos = r["max_gold_cos"]
        has_rw = r["has_rewrite"]

        if r.get("gold_missing") and not any(self.cid_to_chunk.get(cid) for cid in r.get("gold_cids", [])):
            return ("A-数据层", "gold chunk 不在语料中 → 修复标注")

        if dr == -1 and br == -1:
            if cos > 0.65:
                return ("B-检索层-高余弦未召回", f"cos={cos:.3f} 双通道top100无gold → embedding/chunk切分问题")
            elif cos > 0.55:
                return ("B-检索层-中余弦未召回", f"cos={cos:.3f} 双通道弱 → 需query改写或chunk优化")
            else:
                return ("B-检索层-低余弦断连", f"cos={cos:.3f} 语义鸿沟 → 口语→术语映射")

        # 最终 CE 结果已命中 top-10 → 成功，Dense Protected Merge 已修复
        if cr >= 0 and cr < 10:
            return ("G-已修复", f"gold 进入 CE top-10 (rank={cr})")

        if rr >= 10 or rr == -1:
            if (0 <= dr <= 9) or (0 <= br <= 9):
                ch = "Dense" if 0 <= dr <= 9 else "BM25"
                return ("C-RRF融合-单通道强但被稀释", f"{ch} top10 D={dr} B={br} RRF={rr} → 提高{ch}权重或抑制另一通道噪声")
            elif rr < 30:
                return ("C-RRF融合-排名偏低", f"RRF={rr} pool内 → 扩大候选池或调RRF权重")
            else:
                return ("C-RRF融合-排名靠后", f"RRF={rr} pool外 → 检索信号不足")

        if rr < 30 and cr >= 10:
            d = r.get("ce_delta")
            if d is not None and d > 5:
                return ("D-CE精排-严重搅黄", f"RRF={rr}→CE={cr} CE推后{d}位 → 增加alpha/扩大CE候选池")
            elif d is not None and d > 0:
                return ("D-CE精排-轻微搅黄", f"RRF={rr}→CE={cr} 微调alpha可救")
            else:
                return ("D-CE精排-未选入top10", f"RRF={rr}在CE输入但CE={cr} → CE偏好偏差")

        if not has_rw:
            return ("E-改写层-未触发", f"gate未触发 → 考虑放宽阈值或capability强制触发")
        else:
            if not r.get("rewrite_hit_gold"):
                return ("E-改写层-改写未命中", f"{r['rewrite_count']}个改写未命中gold → 改写质量/方向问题")
            else:
                return ("E-改写层-改写命中但未进top10", "改写命中但merge未选入 → merge策略问题")

        return ("F-其他", "需人工检查")

    def print_report(self, rows: list[dict]):
        """打印诊断报告。"""
        n = len(rows)
        print(f"\n{'='*80}")
        print(f"  漏召回多环节诊断 ({n} 题)")
        print(f"{'='*80}")

        # 分组打印
        groups = {}
        for r in rows:
            cat = r.get("category", "?")
            groups.setdefault(cat, []).append(r)

        cat_order = sorted(groups.keys())

        for cat in cat_order:
            subset = groups[cat]
            print(f"\n{'─'*70}")
            print(f"  {cat} ({len(subset)} 题)")
            print(f"{'─'*70}")
            for r in sorted(subset, key=lambda x: x["max_gold_cos"], reverse=True):
                ce_d = r.get("ce_delta")
                ce_str = f"Δ={ce_d:+d}" if ce_d is not None else "N/A"
                print(f"  {r['id']:<8s} {r['capability']:<20s} {r['difficulty']:<6s} "
                      f"cos={r['max_gold_cos']:.3f} D={r['dense_rank']:>4d} B={r['bm25_rank']:>4d} "
                      f"RRF={r['rrf_rank']:>4d} CE={r['ce_rank']:>4d} {ce_str} "
                      f"rw={'Y' if r.get('has_rewrite') else 'N'}")
                print(f"         query: {r['query'][:75]}")
                print(f"         → {r.get('detail', '')}")

        # ── 环节汇总 ──
        print(f"\n{'='*80}")
        print(f"  环节汇总 → 修改建议")
        print(f"{'='*80}")

        suggestions = {
            "A-数据层": "修复 gold 标注",
            "B-检索层": "优化 query 改写（口语→术语）、增大 Dense pool、优化 chunk 切分",
            "C-RRF融合": "提高 Dense 权重 (0.7→0.85)、增大 RRF pool",
            "D-CE精排": "增加 alpha (0.2→0.3)、扩大 CE 候选池 (30→50)",
            "E-改写层": "放宽 gate 阈值 (0.70→0.75)、cross_document/query_rewrite 强制触发",
            "F-其他": "人工检查",
            "G-已修复": "Dense Protected Merge 已修复，无需额外改动",
        }

        layer_cnt = Counter()
        for r in rows:
            layer = r.get("category", "?")[0]  # A/B/C/D/E/F
            layer_cnt[layer] += 1

        for layer in "ABCDEF":
            if layer in layer_cnt:
                n_layer = layer_cnt[layer]
                # find matching categories
                matching = [c for c in cat_order if c.startswith(layer + "-")]
                label = matching[0] if matching else layer
                print(f"  {label:<30s} {n_layer:>2d} 题 ({n_layer/n*100:>4.1f}%) → {suggestions.get(f'{layer}-数据层', suggestions.get(f'{layer}-检索层', suggestions.get(f'{layer}-RRF融合', suggestions.get(f'{layer}-CE精排', suggestions.get(f'{layer}-改写层', suggestions.get(f'{layer}-其他', ''))))))}")
