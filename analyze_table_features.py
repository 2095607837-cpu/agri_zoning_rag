#!/usr/bin/env python3
"""表格特征统计：验证多特征打分方案的可行性（先看数据，再定规则）。

1. 打印 Q_L07 gold chunk 的逐项特征
2. 全量 chunk 上统计各特征覆盖率
3. 估算新规则的翻转量：多少 type=text 会改判为 table（假阳性风险）
"""
import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
chunks = json.load(open(BASE / "data" / "chunks.json"))

HEADER_KW = re.compile(
    r"指标|项目|等级|单位|地区|分区|品种|类型|区域|"
    r"播期|成熟期|积温|降水|气温|土壤|海拔|面积|产量"
)
UNIT_SYM = re.compile(r"℃|%|mm|km|kg|亩|≥|≤|＞|＜|>|<")


def features(content: str) -> dict:
    lines = [l.strip() for l in content.split("\n") if l.strip()]
    n = len(lines)
    if n == 0:
        return {}
    pipe_count = content.count("|")
    digit_lines = sum(1 for l in lines if re.search(r"\b\d+\.?\d*\b", l))
    short_lines = sum(1 for l in lines if len(l) <= 30)
    # 列切分：连续2+空格 或 tab
    col_counts = []
    for l in lines:
        cols = re.split(r"\s{2,}|\t", l)
        cols = [c for c in cols if c.strip()]
        if len(cols) >= 2:
            col_counts.append(len(cols))
    stable_columns = False
    col_split_ratio = len(col_counts) / n
    if col_counts and col_split_ratio >= 0.7:
        from collections import Counter
        most = Counter(col_counts).most_common(1)[0][1]
        stable_columns = most / len(col_counts) >= 0.7
    header_kw_count = len(HEADER_KW.findall(content))
    unit_count = len(UNIT_SYM.findall(content))
    return {
        "n_lines": n,
        "pipe_count": pipe_count,
        "numeric_ratio": round(digit_lines / n, 3),
        "short_line_ratio": round(short_lines / n, 3),
        "col_split_ratio": round(col_split_ratio, 3),
        "stable_columns": stable_columns,
        "header_kw_count": header_kw_count,
        "unit_count": unit_count,
    }


def score(f: dict) -> int:
    if not f:
        return 0
    if f["pipe_count"] >= 3:
        return 99  # 硬通过
    s = 0
    if f["numeric_ratio"] >= 0.55:
        s += 2
    if f["header_kw_count"] >= 2:
        s += 2
    if f["short_line_ratio"] >= 0.5:
        s += 1
    if f["stable_columns"]:
        s += 1
    if f["unit_count"] >= 2:
        s += 1
    return s


# ── 1. Q_L07 gold ──
GOLD_ID = "D_P_R_610000_001-陕西苹果气候区划报告_s10"
gold = next(c for c in chunks if c["id"] == GOLD_ID)
f = features(gold["content"])
print("=" * 70)
print(f"Q_L07 gold: {GOLD_ID}  type={gold['metadata'].get('type', '?')}")
print("=" * 70)
for k, v in f.items():
    print(f"  {k:<20s} {v}")
print(f"  → score = {score(f)} (阈值 4)")
print()
print("  内容前 500 字:")
print("  " + gold["content"][:500].replace("\n", "\n  "))

# ── 2. 全量统计 ──
print()
print("=" * 70)
print("全量 chunk 特征覆盖率")
print("=" * 70)
by_type = {"table": [], "text": []}
for c in chunks:
    t = c["metadata"].get("type", "text")
    by_type.setdefault(t, []).append(c)

for t, cs in by_type.items():
    if not cs:
        continue
    n = len(cs)
    fs = [(c, features(c["content"])) for c in cs]
    fs = [(c, f) for c, f in fs if f]
    n_pipe = sum(1 for _, f in fs if f["pipe_count"] >= 3)
    n_num = sum(1 for _, f in fs if f["numeric_ratio"] >= 0.55)
    n_kw = sum(1 for _, f in fs if f["header_kw_count"] >= 2)
    n_short = sum(1 for _, f in fs if f["short_line_ratio"] >= 0.5)
    n_col = sum(1 for _, f in fs if f["stable_columns"])
    n_unit = sum(1 for _, f in fs if f["unit_count"] >= 2)
    print(f"\n[type={t}] {n} chunks")
    print(f"  pipe>=3          {n_pipe:>5d} ({n_pipe/n*100:.1f}%)")
    print(f"  numeric>=0.55    {n_num:>5d} ({n_num/n*100:.1f}%)")
    print(f"  header_kw>=2     {n_kw:>5d} ({n_kw/n*100:.1f}%)")
    print(f"  short_line>=0.5  {n_short:>5d} ({n_short/n*100:.1f}%)")
    print(f"  stable_columns   {n_col:>5d} ({n_col/n*100:.1f}%)")
    print(f"  unit>=2          {n_unit:>5d} ({n_unit/n*100:.1f}%)")

# ── 3. 翻转量：text → table ──
print()
print("=" * 70)
print("翻转估算：type=text 且新规则 score>=4（无 pipe，纯打分过线）")
print("=" * 70)
flipped = []
for c in by_type.get("text", []):
    f = features(c["content"])
    if f and f["pipe_count"] < 3 and score(f) >= 4:
        flipped.append((c, f, score(f)))
print(f"共 {len(flipped)} 个 text chunk 会改判为 table\n")
for c, f, s in flipped[:15]:
    print(f"  {c['id'][:60]:<62s} score={s}")
    print(f"    num={f['numeric_ratio']} kw={f['header_kw_count']} short={f['short_line_ratio']} col={f['stable_columns']} unit={f['unit_count']}")
    first = c["content"].strip().split("\n")[0][:70]
    print(f"    首行: {first}")
if len(flipped) > 15:
    print(f"  ... 及其余 {len(flipped)-15} 个")
