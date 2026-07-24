#!/usr/bin/env python3
"""快速评测 30 题零召回 — 清除缓存后重新改写+检索+诊断（并行）"""
import json, time, os
from concurrent.futures import ThreadPoolExecutor, as_completed

os.system("rm -f data/rewrite_cache.json")
print("[eval] 清除 rewrite_cache.json", flush=True)

with open("data/golden_set_v2.json") as f:
    gs = json.load(f)
with open("data/chunks_split.json") as f:
    chunks = json.load(f)

# BADCASE_ANALYSIS.md 标记的不可约 badcase: Q_E23, Q_E24, Q_L09, Q_L02
target_ids = {'Q_E30','Q_E33','Q_C18','Q_C25','Q_S01','Q_S04','Q_S08','Q_S15',
              'Q_D04','Q_D05','Q_D11','Q_D20','Q_D21','Q_D25','Q_D29','Q_D30',
              'Q_T16','Q_N11','Q_N13','Q_L01','Q_L07','Q_L08','Q_L11',
              'Q_SR03','Q_T28','Q_D31',
              'Q_E23','Q_E24','Q_L09','Q_L02'}
indomain_qs = [q for q in gs if q["id"] in target_ids]
print(f"[eval] 共 {len(indomain_qs)} 题", flush=True)

from query_rewriter import expand_query, _save_cache
from hybrid_search import HybridSearcher
from eval_diagnostic import DiagnosticAnalyzer

_searcher = HybridSearcher(enable_reranker=True)
rewrite_map = {}

# Phase 1: 并行生成改写
print("[eval] 生成改写（并行）...", flush=True)
def gen_rewrite(q):
    initial = _searcher.search(q["question"], top_k=2, expand_context=True)
    t1 = initial[0].get("similarity", 0) if len(initial) > 0 else 0
    t2 = initial[1].get("similarity", 0) if len(initial) > 1 else 0
    expanded = expand_query(q["question"], mode="all", top1_sim=t1, top2_sim=t2)
    extra = expanded[1:] if len(expanded) > 1 else []
    return q["question"], extra

with ThreadPoolExecutor(max_workers=4) as ex:
    futures = {ex.submit(gen_rewrite, q): q["id"] for q in indomain_qs}
    done = 0
    for f in as_completed(futures):
        q_text, extra = f.result()
        rewrite_map[q_text] = extra
        done += 1
        if done % 10 == 0:
            print(f"  改写进度: {done}/{len(indomain_qs)}", flush=True)

_save_cache()
n_rw = sum(1 for v in rewrite_map.values() if v)
print(f"[eval] Rewrite: {n_rw}/{len(indomain_qs)} 题有改写", flush=True)

# Phase 2: 诊断（DiagnosticAnalyzer 内部串行但逐题独立，用并行加速）
print("[eval] 运行诊断...", flush=True)
analyzer = DiagnosticAnalyzer(_searcher, chunks, rewrite_map)
diag_rows = analyzer.analyze(indomain_qs)
analyzer.print_report(diag_rows)

with open("diagnose_30_retest.json", "w", encoding="utf-8") as f:
    json.dump(diag_rows, f, ensure_ascii=False, indent=2)
print(f"\n  saved: diagnose_30_retest.json")

# 快速统计
from collections import Counter
cats = Counter(r['category'] for r in diag_rows)
print(f"\n=== 分桶对比（旧→新）===")
print(f"  {'类别':<32s} {'旧':>5s} {'新':>5s}")
old_map = {'B':4, 'C':25, 'D':1}
for c, label in [('A','A-数据层'),('B','B-检索层'),('C','C-RRF融合'),('D','D-CE精排'),('E','E-改写层'),('F','F-其他')]:
    old_n = old_map.get(c, 0)
    new_n = sum(v for k,v in cats.items() if k.startswith(c+'-'))
    arrow = ''
    if new_n < old_n: arrow = ' ↓'
    elif new_n > old_n: arrow = ' ↑'
    print(f"  {label:<32s} {old_n:>5d} {new_n:>5d}{arrow}")
