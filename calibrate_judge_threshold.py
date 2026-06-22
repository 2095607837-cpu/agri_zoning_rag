"""
计算 Judge 高置信度跳过阈值。

对 200 题 golden set 做检索，统计 top1 相似度分布，
找出最优阈值：在 OOD 误放可控的前提下最大化 LLM 跳过率。
"""

import json, os, numpy as np
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from hybrid_search import HybridSearcher

searcher = HybridSearcher()

with open(BASE_DIR / "data" / "golden_set.json") as f:
    golden = json.load(f)

in_sims = []
ood_sims = []

print(f"正在检索 {len(golden)} 题...\n")
for i, g in enumerate(golden):
    query = g["question"]
    is_ood = g["question_type"] == "OOD"
    results = searcher.search(query, top_k=5)
    top1_sim = results[0]["similarity"] if results else 0

    if is_ood:
        ood_sims.append(top1_sim)
    else:
        in_sims.append(top1_sim)

    if (i + 1) % 20 == 0:
        print(f"  进度: {i+1}/{len(golden)}")

# ── 分布统计 ──
print(f"\n{'='*60}")
print(f"  Top1 余弦相似度分布")
print(f"{'='*60}")
print(f"\n  In-domain ({len(in_sims)} 题):")
print(f"    mean={np.mean(in_sims):.4f}  median={np.median(in_sims):.4f}")
print(f"    min={np.min(in_sims):.4f}   max={np.max(in_sims):.4f}")
print(f"    std={np.std(in_sims):.4f}")
print(f"    p5={np.percentile(in_sims, 5):.4f}   p10={np.percentile(in_sims, 10):.4f}")
print(f"    p25={np.percentile(in_sims, 25):.4f}  p75={np.percentile(in_sims, 75):.4f}")
print(f"    p90={np.percentile(in_sims, 90):.4f}  p95={np.percentile(in_sims, 95):.4f}")

print(f"\n  OOD ({len(ood_sims)} 题):")
print(f"    mean={np.mean(ood_sims):.4f}  median={np.median(ood_sims):.4f}")
print(f"    min={np.min(ood_sims):.4f}   max={np.max(ood_sims):.4f}")
print(f"    std={np.std(ood_sims):.4f}")
print(f"    p75={np.percentile(ood_sims, 75):.4f}  p90={np.percentile(ood_sims, 90):.4f}")
print(f"    p95={np.percentile(ood_sims, 95):.4f}  p99={np.percentile(ood_sims, 99):.4f}")

# ── 阈值扫描 ──
print(f"\n{'='*60}")
print(f"  阈值扫描：sim >= 阈值 → 跳过 LLM，直接判 answer")
print(f"{'='*60}")
print(f"  {'阈值':<8s} {'跳过':>5s} {'占比':>7s} {'OOD误放':>8s} {'准确率':>8s}")
print(f"  {'-'*45}")

total_in = len(in_sims)
total_ood = len(ood_sims)
total = len(in_sims) + len(ood_sims)

for threshold in np.arange(0.40, 0.85, 0.025):
    threshold = round(threshold, 3)
    in_above = sum(1 for s in in_sims if s >= threshold)
    ood_above = sum(1 for s in ood_sims if s >= threshold)
    skipped = in_above + ood_above
    accuracy = (total - ood_above) / total
    bar = "█" * (in_above // 5) if in_above else ""
    print(f"  {threshold:<8.3f} {skipped:>4d}  {skipped/total:>6.1%}  {ood_above:>6d}  {accuracy:>7.2%}  {bar}")

# ── 分位数分析 ──
print(f"\n{'='*60}")
print(f"  关键分位点")
print(f"{'='*60}")

print(f"\n  OOD sim 上限:")
for p in [75, 80, 85, 90, 95, 99, 100]:
    v = np.percentile(ood_sims, p)
    print(f"    p{p:>2d}: {v:.4f}")

print(f"\n  In-domain sim 下限:")
for p in [1, 5, 10, 15, 20, 25]:
    v = np.percentile(in_sims, p)
    print(f"    p{p:>2d}: {v:.4f}")

# ── 推荐 ──
print(f"\n{'='*60}")
print(f"  推荐决策")
print(f"{'='*60}")

# 找到 OOD p99 值 —— 保守阈值，99% 的 OOD 都在此值以下
ood_p99 = np.percentile(ood_sims, 99)
ood_p95 = np.percentile(ood_sims, 95)
ood_max = np.max(ood_sims)

# In-domain p5 —— 最差 5% 的 in-domain 在此值以下
in_p5 = np.percentile(in_sims, 5)
in_p10 = np.percentile(in_sims, 10)
in_min = np.min(in_sims)

print(f"\n  OOD p95: {ood_p95:.4f}  (95% OOD sim ≤ 此值)")
print(f"  OOD p99: {ood_p99:.4f}  (99% OOD sim ≤ 此值)")
print(f"  OOD max: {ood_max:.4f}")
print(f"  In-domain p5: {in_p5:.4f}  (5% In-domain sim ≤ 此值)")
print(f"  In-domain min: {in_min:.4f}")

# 如果 OOD max < in_p5，说明两组完全可分
if ood_max < in_p5:
    t = (ood_max + in_p5) / 2
    print(f"\n  OOD 和 In-domain 完全可分 → 推荐阈值 = ({ood_max:.3f} + {in_p5:.3f}) / 2 = {t:.3f}")
elif ood_p95 < in_p10:
    t = round((ood_p95 + in_p10) / 2, 3)
    print(f"\n  95% OOD 低于 in-domain p10 → 推荐阈值 = ({ood_p95:.3f} + {in_p10:.3f}) / 2 = {t:.3f}")
else:
    # 有重叠区间，权衡
    print(f"\n  OOD 和 In-domain 有重叠，需要权衡。")
    for t in [0.60, 0.65, 0.70, 0.75]:
        ood_fp = sum(1 for s in ood_sims if s >= t)
        in_skip = sum(1 for s in in_sims if s >= t)
        print(f"    阈值={t:.2f}: 跳过{in_skip}题 in-domain, 误放{ood_fp}题 OOD")
