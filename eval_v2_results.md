# V2 Golden Set 全配置检索评测报告

生成时间：2026-07-06 | 最后更新：2026-08-25 | 评测集：`golden_set_v2.json` | 199 题（in-domain 179 + OOD 20）

## 变更日志

| 日期 | 变更 |
|------|------|
| 2026-08-25 | **CK 应用层移除**（query_rewriter.py）：删除 REWRITE_PROMPT 的 Knowledge Context 段、expand_query 的 use_ck/knowledge_context 参数与 ck_matcher 调用，改写缓存键统一为纯 query（旧 `\|base` 缓存自动复用，200 条改写零重跑）。此前生产 use_ck 默认 True 实际在注入 CK；移除后改写/检索/召回统一为 BASE 口径（与 2026-08-13 A/B 结论一致：CK 无正向收益）。CK 数据与 ck_matcher.py 保留；eval_v2_ck_ab.py 归档。详见"二十一、CK A/B 评测"末尾口径说明 | 
| 2026-08-24 | **RRF 权重分层扫描**（eval_rrf_weight_scan.py → data/rrf_weight_scan_report.json）：生产候选池（dense_k=30, bm25_k=20）解析式重算 0.1~0.95 权重网格。全局曲面为平台（0.4~0.8 只差 1.1pp），0.7/0.3 仍在最优区；唯一强敏感类型 query_rewrite（w_dense=0.1 时 +3 题）；按类完美权重 in-sample 上界仅 +5 题（+2.8pp）。判定：不调动态权重，优先修 single-channel boost 归一化机制。详见"二十四、RRF 权重分层扫描（2026-08-24）" |
| 2026-08-24 | **RRF 损失诊断 P0**（eval_rrf_loss.py → data/rrf_loss_report.json）：复用 Oracle Rank 逐题列补 bm25_top30，180 题原始 query。A类（Dense top10 命中但 RRF top10 丢）6 题、B类（Dense top30 命中但 RRF top10 丢）18 题、C类（Dense top30 未命中）14 题。RRF 损失 24 题 > Dense 无法解决 14 题——检索层内 RRF 是第一瓶颈。详见"二十三、RRF 损失诊断 P0（2026-08-24）" |
| 2026-08-24 | **Oracle Rank 可分性实验**（eval_oracle_rank.py → data/eval_oracle_rank_results.json）：180 原始 query × 791 chunk（生产索引）全量余弦诊断。median oracle rank=2.0（92.2% ≤30），仅 1 题 >100（Q_D29=106），>300 为 0。still-fail 24 题中 15 题表示空间可分但端到端失败（RRF 稀释+top-10 截断）。改写救回 5 题（Q_SR03 39→4）。结论：不训 Adapter，瓶颈在管线。详见"二十二、Oracle Rank 可分性实验（2026-08-24）" |
| 2026-08-13 | **CK A/B 评测**（eval_v2_ck_ab.py → eval_ck_ab_results.json）：BASE（use_ck=off）vs CK（use_ck=on）全量 180 题对比。CK 注入 Knowledge Context 到 Rewrite 后 MRR 0.5490→0.5481、R@10 86.7%→85.6%、R@10=0 24→26、Rescue Rate 0/24。CK 无正向收益，保留开关但不作为主攻方向。详见"二十一、CK A/B 评测（2026-08-13）" |
| 2026-08-02 | **CK 三增强 +Rewrite+Reranker**：Dense Dual Repr (v2.1) + BM25 索引增强 (v2.2) + CE 轻量增强 (v2.3)，基于新索引 (~1548 vectors) 的 +Rewrite+Reranker 评测。MRR=0.5298, R@10=76.1%, R@10=0=43。OOD 55.0%。耗时 2945s。query_rewrite 仍是最短板 (MRR=0.216, R@10=41.7%)。详见"二十、CK 三增强 +Rewrite+Reranker（2026-08-02）" |
| 2026-07-29 | **三池分离 Query Rewrite + Hit Boost 0.002**：rewrite 从 2 池（Keyword+SubQuery）重构为 3 池（Keyword≤6/BM25-only + RewriteQuery≤2/Dense+BM25 + SubQuery≤3/Dense+BM25），新增 `rewrite_queries` 字段（完整句子改写）。Hit Boost 从 0.10/0.15 降至 0.002。+Rewrite+Reranker: MRR=0.5172, R@10=87.2%, R@10=0=23。详见"十九、三池分离 Query Rewrite（2026-07-29）" |
| 2026-07-28 | **Parallel Evidence Merge 架构**：search_multi_query 改为并行 RRF→Evidence Merge（Hit Boost + Dense Protect）→ Global Pool → CE Rerank。search_multi_query 返回值修正为 (judge_results, merged)。评估改为 chunk 级直接对比 + section 级辅助。+Rewrite+Reranker: MRR=0.5469, R@10=86.6%, R@10=0=24。详见"十八、Parallel Evidence Merge（2026-07-28）" |
| 2026-07-24 | **chunks_split 迁移 + 三相管线 + 诊断分桶**：chunks 从 chunks.json→chunks_split.json（787 entries, 499 unique section IDs），search_multi_query 改为三相管线（Dense Protected→RRF+CE→Rewrite RRF-only），新增诊断分桶（B/C-RRF/D-CE/E-改写/G-已修复）定位零召回失败环节。+Rewrite+Reranker: MRR=0.5638, R@10=72.1%, R@10=0=50/179。OOD 召回 80.0%。详见"十七、chunks_split + 三相管线 + 诊断分桶（2026-07-24）" |
| 2026-07-15 | **Hybrid Fusion 实验（已否定）**：Original 独立 CE + Rewrite RRF 投票加分（β=0.01）。R@10=82.7%（第二），但 MRR=0.5568 为所有 reranker 配置最差，甚至低于 Baseline。**已回退 Append。** 详见"十六、Hybrid Fusion 实验（2026-07-15，已否定）" |
| 2026-07-14 | **新 Rewrite Prompt + Gate 触发逻辑**：重写 `REWRITE_PROMPT`（none/normalize/expand 三类型）、`_needs_rewrite` 门控（length>12 + top1<0.70）、修复 Q_L09/Q_L12 gold 标注、移除 Q_L05。+Rewrite+Reranker：MRR=0.6169、R@10=83.8%、R@10=0=29（-9 vs 旧 rewrite）。详见"十四、新 Rewrite Prompt 与 Gate 触发（2026-07-14）" |
| 2026-07-14 | **Late Fusion 架构**：全部 query（原始 + 改写）共享一轮 RRF 池→单次 CE 精排，替代旧 Append 架构（per-query RRF+CE→merge）。结果：MRR 和 Top1 下降（vs Append），R@5 略升。详见"十五、Late Fusion 架构评测（2026-07-14）" |
| 2026-07-13 | **CE 逐题增益分析**：对比 RRF-only vs RRF+CE 每道题，CE 救回 14 道、搅黄 12 道，净增益 +2。CE 对 cross_section 最有效（+4），但对 query_rewrite 和 cross_document 几乎对等。详见"十二、CE 逐题增益分析（2026-07-13）" |
| 2026-07-13 | **RRF+CE 分数融合 Alpha 扫描**：实现 `final = alpha * RRF_norm + (1-alpha) * CE_norm` 加权融合（min-max 归一化），扫描 alpha=[0, 0.2, 0.3, 0.4, 0.5]。alpha=0.2 最优：R@10=82.2%（vs 78.3% baseline，vs 78.9% pure CE），R@10=0 从 39 降至 32（-7 题）。alpha=0.2 已设为生产默认值。详见"十三、RRF+CE 分数融合与 Alpha 扫描（2026-07-13）" |
| 2026-07-12 | Reranker 默认开启；`pool_size` 从 `max(top_k*4,20)` 增大到 `max(top_k*10,100)`（top_k=10 时 pool=40→100），配合 `rerank_input=min(40, top_k*4)`；R@10 80.0%、R@10=0 从 39 降至 36。CrossEncoder 推理加锁避免多线程 CPU 争抢。详见"十、Reranker 开启与候选池放大（2026-07-12）" |
| 2026-07-12 | **Union 实验（已否定）**：Dense top-30 + BM25 top-10 → Union → CE。R@10 从 80.0% 跌至 76.1%，R@10=0 从 36 升至 43。BM25 只放 10 个候选丢掉太多好结果，RRF 的互补效应不可替代。**已回退 RRF。** 详见"十一、Union 融合实验（2026-07-12，已否定）" |
| 2026-07-10 | 验证第八节两条 rewrite 建议：①"按需触发改写"（top1 cosine 低才改写）实验**证伪**，硬题上改写 +0/-0；②新增中文 BM25 关键词检索（字+bigram 分词，修复默认空格分词对中文失效），BM25 通道 intrinsic 已可用但端到端 top-10 零变化，RRF 权重扫描证明 0.7/0.3 已最优。详见"九、BM25 关键词检索修复与融合权重扫描（2026-07-10）" |
| 2026-07-06 | +Rewrite+Reranker 配置完成，全四配置汇总 |
| 2026-07-05 | 修复：`search()` 新增 `skip_reranker` 参数，子查询跳过 CrossEncoder，CrossEncoder 调用从 1268 次降至 180 次 |
| 2026-07-04 | Baseline / +Rewrite / +Reranker 三配置完成，+Rewrite+Reranker 因嵌套线程池死锁挂起 |

---

## 版本演进总览

> 所有配置均使用 `BAAI/bge-small-zh-v1.5`（512-dim）、Append 架构、RRF dense/BM25=0.7/0.3、alpha=0.2，除非特别注明。
> **当前生产配置**：2026-07-29 三池分离 Rewrite（chunks_split, Evidence Merge, Hit Boost 0.002）。
> ⚠ 2026-07-24（#10）切换 chunks_split + 三相管线后指标全面退化。2026-07-28（#11）Evidence Merge 大幅恢复。2026-07-29（#12）三池分离 R@10 创新高（87.2%）。

| # | 版本 | Chunks | 管线 | 题数 | MRR | R@5 | R@10 | Top1 | R@10=0 | OOD | 耗时 |
|---|------|--------|------|------|------|------|------|------|--------|-----|------|
| 1 | 2026-07-06 Baseline | chunks.json | RRF | 180 | 0.5692 | 70.6% | 78.3% | 47.8% | 39 | — | 18s |
| 2 | 2026-07-06 +Rewrite only | chunks.json | Append | 180 | 0.4785 | 58.9% | 68.9% | 38.3% | 56 | — | 329s |
| 3 | 2026-07-06 +Reranker only | chunks.json | RRF+CE | 180 | 0.6092 | 72.8% | 80.0% | 51.7% | 36 | — | 3499s |
| 4 | 2026-07-06 +RW+Reranker (旧 prompt) | chunks.json | Append | 180 | 0.5938 | 70.6% | 78.9% | 50.0% | 38 | — | 79493s |
| 5 | 2026-07-12 +Reranker only (pool=100) | chunks.json | RRF+CE | 180 | 0.6125 | 72.8% | 80.0% | 52.2% | 36 | — | 1788s |
| 6 | 2026-07-13 alpha=0.2 (Reranker only) | chunks.json | RRF+CE | 180 | 0.6074 | 72.2% | **82.2%** | — | **32** | — | — |
| **7** | **2026-07-14 New RW Prompt Append** | chunks.json | **Append** | **179** | **0.6169** | **76.0%** | **83.8%** | **52.0%** | **29** | **75.0%** | **1927s** |
| 8 | 2026-07-14 Late Fusion | chunks.json | Late Fusion | 179 | 0.5758 | 75.4% | 81.0% | 45.8% | 34 | 75.0% | 1475s |
| 9 | 2026-07-15 Hybrid Fusion (已否定) | chunks.json | Hybrid | 179 | 0.5568 | 74.3% | 82.7% | 43.6% | 31 | 70.0% | 3243s |
| 10 | 2026-07-24 chunks_split + 三相管线 | **chunks_split** | **三相** | 179 | 0.5638 | 66.5% | 72.1% | 48.0% | 50 | **80.0%** | 1556s |
| **11** | **2026-07-28 Parallel Evidence Merge** | chunks_split | **Evidence Merge** | **179** | **0.5469** | **74.3%** | **86.6%** | **39.7%** | **24** | 55.0% | **3533s** |
| **12** | **2026-07-29 三池分离 Rewrite** | chunks_split | **Evidence Merge** | **179** | **0.5172** | **72.1%** | **87.2%** | **34.6%** | **23** | 55.0% | **3710s** |
| 13 | 2026-08-02 CK 三增强 +RW+Reranker | chunks_split | Evidence Merge | 180 | 0.5298 | 68.9% | 76.1% | 40.6% | 43 | 55.0% | 2945s |
| 14 | 2026-08-13 CK A/B BASE (use_ck=off) | chunks_split | Evidence Merge | 180 | 0.5490 | 73.3% | 86.7% | 40.6% | 24 | — | — |
| 14 | 2026-08-13 CK A/B CK (use_ck=on) | chunks_split | Evidence Merge | 180 | 0.5481 | 72.8% | 85.6% | 40.6% | 26 | — | — |

**关键对比：**

| 对比维度 | 变化 | MRR Δ | R@10 Δ | R@10=0 Δ |
|----------|------|--------|--------|-----------|
| Baseline → +Reranker only | Reranker 单开 | **+0.0433** | **+1.7pp** | -3 |
| Baseline → +RW+Reranker (新 prompt) | Rewrite + Reranker 全开 | **+0.0477** | **+5.5pp** | **-10** |
| 旧 RW prompt → 新 RW prompt | Rewrite 质量提升 | +0.0231 | +4.9pp | -9 |
| Append → Late Fusion | 架构变更 | -0.0411 | -2.8pp | +5 |
| Append → Hybrid Fusion | 架构变更 | -0.0601 | -1.1pp | +2 |
| **chunks.json → chunks_split + 三相管线** | **数据+管线变更** | **-0.0531** | **-11.7pp** | **+21** |
| **#11 → #12 三池分离 + Hit Boost 0.002** | **Rewrite 重构** | -0.0297 | +0.6pp | -1 |

**按能力雷达（#7 chunks.json/Append vs #10 chunks_split/三相管线）：**

| Capability | #7 MRR | #7 R@10 | #10 MRR | #10 R@10 | MRR Δ | R@10 Δ |
|------------|--------|---------|---------|----------|--------|--------|
| exact_retrieval | 0.784 | 88.6% | 0.779 | 94.3% | -0.005 | **+5.7** |
| context_expansion | 0.768 | 92.0% | 0.784 | 84.0% | +0.016 | -8.0 |
| cross_section | 0.482 | 88.0% | 0.385 | 56.0% | **-0.097** | **-32.0** |
| cross_document | 0.455 | 70.0% | 0.393 | 66.7% | -0.062 | -3.3 |
| table_retrieval | 0.560 | 85.0% | 0.484 | 60.0% | -0.076 | **-25.0** |
| numeric_retrieval | 0.827 | 95.0% | 0.685 | 85.0% | **-0.142** | -10.0 |
| query_rewrite | 0.430 | 70.8% | 0.386 | 50.0% | -0.044 | **-20.8** |

> chunks_split + 三相管线在 cross_section（-32pp）、table_retrieval（-25pp）、query_rewrite（-21pp）三个能力上退化最严重。仅 exact_retrieval 微涨（+5.7pp，猜测 chunks_split 更细粒度对精确匹配有利）。

---

## 一、全配置对比汇总

| Config | MRR | R@5 | R@10 | Top1 | R@10=0 | 耗时 |
|--------|------|------|------|------|--------|------|
| Baseline (no rewrite, no reranker) | 0.5692 | 70.6% | 78.3% | 47.8% | 39 | 18s |
| +Rewrite only | 0.4785 | 58.9% | 68.9% | 38.3% | 56 | 329s |
| +Reranker only (pool=40, CE=30) | 0.6092 | 72.8% | 80.0% | 51.7% | 36 | 3499s |
| +Rewrite + Reranker | 0.5938 | 70.6% | 78.9% | 50.0% | 38 | 79493s |
| **+Reranker only (pool=100, CE=40)** | **0.6125** | **72.8%** | **80.0%** | **52.2%** | **36** | 1788s |

---

## 二、按 Capability 详细对比

### Baseline

| Capability | n | MRR | R@5 | R@10 | Top1 |
|------------|---|------|------|------|------|
| exact_retrieval | 35 | 0.759 | 85.7% | 88.6% | 68.6% |
| context_expansion | 25 | 0.776 | 84.0% | 96.0% | 68.0% |
| cross_section | 25 | 0.421 | 64.0% | 68.0% | 32.0% |
| cross_document | 30 | 0.405 | 56.7% | 66.7% | 33.3% |
| table_retrieval | 20 | 0.524 | 70.0% | 80.0% | 40.0% |
| numeric_retrieval | 20 | 0.715 | 85.0% | 95.0% | 60.0% |
| query_rewrite | 25 | 0.362 | 48.0% | 56.0% | 28.0% |

### +Rewrite only

| Capability | n | MRR | R@5 | R@10 | Top1 |
|------------|---|------|------|------|------|
| exact_retrieval | 35 | 0.595 | 71.4% | 80.0% | 48.6% |
| context_expansion | 25 | 0.674 | 88.0% | 92.0% | 52.0% |
| cross_section | 25 | 0.352 | 44.0% | 60.0% | 28.0% |
| cross_document | 30 | 0.294 | 40.0% | 50.0% | 20.0% |
| table_retrieval | 20 | 0.434 | 50.0% | 65.0% | 35.0% |
| numeric_retrieval | 20 | 0.698 | 75.0% | 90.0% | 65.0% |
| query_rewrite | 25 | 0.327 | 44.0% | 48.0% | 24.0% |

### +Reranker only

| Capability | n | MRR | R@5 | R@10 | Top1 |
|------------|---|------|------|------|------|
| exact_retrieval | 35 | 0.818 | 85.7% | 88.6% | 77.1% |
| context_expansion | 25 | 0.834 | 88.0% | 92.0% | 80.0% |
| cross_section | 25 | 0.505 | 68.0% | 84.0% | 40.0% |
| cross_document | 30 | 0.420 | 63.3% | 70.0% | 26.7% |
| table_retrieval | 20 | 0.441 | 70.0% | 80.0% | 25.0% |
| numeric_retrieval | 20 | 0.842 | 90.0% | 90.0% | 80.0% |
| query_rewrite | 25 | 0.371 | 44.0% | 56.0% | 28.0% |

### +Rewrite + Reranker

| Capability | n | MRR | R@5 | R@10 | Top1 |
|------------|---|------|------|------|------|
| exact_retrieval | 35 | 0.809 | 82.9% | 88.6% | 77.1% |
| context_expansion | 25 | 0.834 | 88.0% | 92.0% | 80.0% |
| cross_section | 25 | 0.505 | 68.0% | 84.0% | 40.0% |
| cross_document | 30 | 0.396 | 56.7% | 63.3% | 26.7% |
| table_retrieval | 20 | 0.443 | 65.0% | 80.0% | 25.0% |
| numeric_retrieval | 20 | 0.842 | 90.0% | 90.0% | 80.0% |
| query_rewrite | 25 | 0.301 | 44.0% | 56.0% | 16.0% |

---

## 三、按 Difficulty 对比

| Difficulty | n | Baseline MRR | +Rewrite MRR | +Reranker MRR | +Both MRR |
|------------|---|-------------|-------------|--------------|----------|
| easy | 132 | 0.6297 | 0.5381 | 0.6827 | 0.6740 |
| medium | 37 | 0.4732 | 0.3820 | 0.4503 | 0.4131 |
| hard | 11 | 0.1667 | 0.0867 | 0.2606 | 0.2403 |

| Difficulty | n | Baseline R@10 | +Rewrite R@10 | +Reranker R@10 | +Both R@10 |
|------------|---|--------------|--------------|---------------|-----------|
| easy | 132 | 81.8% | 75.0% | 84.1% | 84.9% |
| medium | 37 | 75.7% | 56.8% | 75.7% | 70.3% |
| hard | 11 | 45.5% | 36.4% | 45.5% | 36.4% |

---

## 四、Reranker 增益分析（Baseline → +Reranker only）

| Capability | MRR 变化 | R@10 变化 |
|------------|---------|----------|
| exact_retrieval | +0.059 | +0.0% |
| context_expansion | +0.058 | -4.0% |
| cross_section | +0.084 | +16.0% |
| cross_document | +0.015 | +3.3% |
| table_retrieval | -0.083 | 0.0% |
| numeric_retrieval | **+0.127** | -5.0% |
| query_rewrite | +0.009 | 0.0% |

Reranker 在 cross_section 和 numeric_retrieval 上提升最大，table_retrieval 反而下降（精排噪声导致正确的 chunk 被挤到后面）。

---

## 五、Rewrite 影响分析（Baseline → +Rewrite only）

所有 capability 全部退化，MRR 整体下降 0.091（-15.9%），R@10 下降 9.4%。

| Capability | MRR 变化 | R@10 变化 |
|------------|---------|----------|
| exact_retrieval | **-0.164** | **-8.6%** |
| context_expansion | -0.102 | -4.0% |
| cross_section | -0.069 | -8.0% |
| cross_document | -0.111 | -16.7% |
| table_retrieval | -0.090 | -15.0% |
| numeric_retrieval | -0.017 | -5.0% |
| query_rewrite | -0.035 | -8.0% |

exact_retrieval 退化最严重（-0.164），说明改写引入了大量噪声，反而把本该精确匹配的查询带偏。

---

## 六、OOD 检测

评测配置：+Rewrite + Reranker

| 指标 | 值 |
|------|-----|
| OOD 召回率 | **15/20 (75.0%)** |
| 漏判 | 5 题 |

### Judge 分层

| 层 | 题数 |
|----|------|
| signal | 0 |
| high_sim (≥0.75 直接放行) | 2 |
| score (<0.65 直接拒绝) | 8 |
| llm (模糊区间) | 10 |

### 漏判详情

| ID | 类型 | 原因 |
|----|------|------|
| Q_U03 | unanswerable | LLM 判 PARTIAL→answer，实际四种灾害对比信息缺失 |
| Q_U04 | unanswerable | LLM 判 PARTIAL→answer，模型参数/公式缺失 |
| Q_U05 | unanswerable | sim=0.760 超过 high_sim 阈值直接放行 |
| Q_CF05 | conflict | sim=0.761 超过 high_sim 阈值直接放行 |
| Q_CF06 | conflict | LLM 误判为 YES，参考资料说明了答案但与问题意图相反 |

---

## 七、Recall@10=0 对比

| Config | Recall@10=0 | 占比 |
|--------|-------------|------|
| Baseline | 39 | 21.7% |
| +Rewrite only | 56 | 31.1% |
| +Reranker only | 36 | 20.0% |
| +Rewrite + Reranker | 38 | 21.1% |

四配置均未命中的顽固题目（14 题）：

Q_E23, Q_E24, Q_E30, Q_E33, Q_C18, Q_C25, Q_S01, Q_S08, Q_S15, Q_D04, Q_D06, Q_D07, Q_L02, Q_L04

---

## 八、结论与建议

1. **推荐配置：Reranker only**（不加 rewrite），MRR 0.6092，R@10 80.0%
2. **Rewrite 需要重构**：当前 LLM 改写策略（关键词+多视角）引入了过多噪声，在全部 7 个 capability 上均退化。可尝试：
   - 仅对 query_rewrite 类题目启用改写（这些题目设计就是考验改写能力）
   - 用检索结果 top1 cosine similarity 动态判断是否需要改写（相似度已足够高的跳过）
3. **OOD 阈值需微调**：high_sim 阈值 0.75 漏放了 2 个 OOD 题（Q_U05 sim=0.760, Q_CF05 sim=0.761），可考虑提高至 0.78
4. **表检索是短板**：table_retrieval R@10 仅 80.0%，且在 reranker 下 MRR 反而下降 0.083，表格 chunk 的检索策略需要专项优化
5. **CrossEncoder 性能**：CPU 上每次 predict 约 10-20s，GPU 可大幅加速。若上线需考虑推理延迟

---

## 九、BM25 关键词检索修复与融合权重扫描（2026-07-10）

评测集：`golden_set_v2.json` in-domain 180 题 | 无 Reranker | 脚本：`eval_rewrite_recall.py` + 内联诊断/权重扫描

本节验证第八节两条 rewrite 相关建议，并修复一个此前未发现的 BM25 缺陷。

### 9.1 第八节建议 ②"按需触发改写"证伪

八.2 建议"用 top1 cosine 动态判断是否改写（相似度高的跳过）"。按此实现门槛触发（`top1_sim<阈值 且 len>12` 才用改写）并扫描阈值：

| 配置 | 触发数 | MRR | R@5 | R@10 | R@10=0 |
|------|:---:|:---:|:---:|:---:|:---:|
| Baseline | 0 | 0.5692 | 0.7056 | 0.7833 | 39 |
| 门槛 top1<0.70 | 27 | 0.5692 | 0.7056 | 0.7833 | 39 |
| 门槛 top1<0.75 | 55 | 0.5694 | 0.7056 | 0.7833 | 39 |
| 全量改写 | 174 | 0.5706 | 0.7111 | 0.7889 | 38 |

被触发的 27 道低相似度硬题上，改写 **新召回 +0 / 丢失 -0，MRR、R@10 逐位不变**。根因：修复后的合并逻辑（原查询前排 + 改写补尾部）在 top-k 满窗口时把改写候选截断在窗口外；改写那点微增益只来自高相似度题的尾部补位。**结论：门槛触发拿不到任何增益，改写作为召回杠杆到此为止。**（详细过程见 `eval_rewrite_v3_results.md` 第九节。）

### 9.2 BM25 中文分词缺陷与修复

排查发现 `hybrid_search.py` 的 BM25 用 langchain 默认 `text.split()` 分词：中文无空格 → 整段成一个 token（516 chunk 分词后中位仅 20 个巨型 token），查询 token 永远匹配不上文档 token。**BM25 通道对中文近乎失效**，得分全 0 时只能返回语料头部固定 chunk 当噪声，检索质量几乎全靠 dense 通道扛。

**修复**：参照 `weather_agent_rag` 的 `BM25Index._tokenize`，改用**字 + 双字 bigram 分词**（零外部依赖），"大豆冷害" → `['大','豆','冷','害','大豆','豆冷','冷害']`。修复后 BM25 能正确命中中文（"大豆冷害区划指标" → 大豆冷害指标/低温冷害风险区划 chunk）。

### 9.3 端到端影响：intrinsic 可用 ≠ 端到端提升

| 指标 | 修复前 | 修复后(0.7/0.3) | Δ |
|------|:---:|:---:|:---:|
| MRR | 0.5692 | 0.5692 | 0 |
| R@5 | 0.7056 | 0.7056 | 0 |
| R@10 | 0.7833 | 0.7833 | 0 |
| R@10=0 | 39 | 39 | 0 |

**baseline top-10 逐位不变**。诊断 39 道 R@10=0：

- BM25-only top-10 能命中 gold 的：**7 道**（dense 漏、BM25 能找）；
- 两通道都漏：**32 道**（切块/embedding/gold 深层问题）。

即 BM25 修复把 7 道 gold 送进了候选池，但 dense 在 RRF 里占 0.7 主导，BM25 的 0.3 权重顶不进 top-10。

### 9.4 RRF 融合权重扫描：0.7/0.3 已最优

| dense/bm25 | MRR | R@5 | R@10 | R@10=0 |
|---|:---:|:---:|:---:|:---:|
| **0.7/0.3** | **0.5692** | **0.7056** | **0.7833** | **39** |
| 0.6/0.4 | 0.5692 | 0.7056 | 0.7833 | 39 |
| 0.5/0.5 | 0.5644 | 0.6722 | 0.7500 | 45 |
| 0.4/0.6 | 0.3709 | 0.5333 | 0.6444 | 64 |
| 0.3/0.7 | 0.3709 | 0.5333 | 0.6444 | 64 |

**升 BM25 权重只降不升**：那 7 道 BM25 命中题一旦升权虽进来，同时把 BM25 噪声灌进 dense 本判对的题，净负。静态 RRF 权重无法"只在 BM25 对的时候信它"。

### 9.5 结论

1. **BM25 中文关键词检索已修复并保留**（默认 0.7/0.3，零回归）。它虽不改变当前 top-10，但把 7 道 dense 漏掉的 gold 送进了候选池——**这是 Reranker 生效的前提**：池里有 gold，精排才救得回；修复前 BM25 灌的是噪声。
2. **改写、BM25 升权两条路都被静态融合卡死**：静态 top-k / 静态权重无法逐题判断该信哪个通道。**Reranker 是唯一能做逐题精排的杠杆**——与第八节"推荐 Reranker only（MRR 0.6092）"一致。
3. **剩余 32 道两通道都漏的题**是召回天花板，需从切块粒度 / embedding / gold 标注治，检索侧调参够不着。

### 9.6 代码改动

| 文件 | 修改 |
|------|------|
| `hybrid_search.py` | 新增 `bm25_tokenize`（字+bigram 中文分词）；`BM25Retriever.from_documents` 传 `preprocess_func`；`search()` 新增 `w_dense/w_bm25` 参数（默认 0.7/0.3）供融合权重调优 |
| `eval_rewrite_gated.py`（新增） | 按需触发改写的阈值扫描 |

---

## 十、Reranker 开启与候选池放大（2026-07-12）

评测集：`golden_set_v2.json` in-domain 180 题 | 脚本：`eval_v2_retrieval.py`

本节将 Reranker 设为默认开启，并排查 pool_size 对召回的影响。

### 10.1 代码改动

| 文件 | 修改 |
|------|------|
| `hybrid_search.py` | `enable_reranker` 默认值 `False` → `True` |
| `hybrid_search.py` | `rerank_input` 从 `min(top_k*3,40)` 改为 `min(40, top_k*4)`（top_k=10 时 30→40） |
| `hybrid_search.py` | `pool_size` 从 `max(top_k*4,20)` 改为 `max(top_k*10,100)`（top_k=10 时 40→100） |
| `hybrid_search.py` | `Reranker.__new__` 新增 `_infer_lock`，`rerank()` 内加锁序列化 CrossEncoder 推理，避免 4 线程 CPU 争抢 |
| `diagnose_zero_recall.py` | 新增 `ce_rank` 字段：CrossEncoder 精排后 gold 的实际排名，区分"卡在 RRF 阶段"还是"卡在 CE 阶段" |

### 10.2 pool_size 诊断

此前 `diagnose_zero_recall.py` 用 `top_k=100`（内部 `pool_size=400`）诊断 RRF 排名，但实际 `eval_v2_retrieval.py` 用 `top_k=10`（内部 `pool_size=40`）。两个池子大小差 10 倍，导致诊断结果不可比：

- 诊断池 400：BM25 rank 41-400 的 chunk 能进 RRF 融合 → CE 候选池
- 实际池 40：BM25 rank 41+ 的 chunk 直接被截断

修复 `pool_size` 至 100 后，CrossEncoder 候选池从更丰富的召回池中抽取 top-40，实际增益来自让更多 BM25 好结果进入 RRF 视野。

### 10.3 评测结果

| Config | MRR | R@5 | R@10 | Top1 | R@10=0 | 耗时 |
|--------|------|------|------|------|--------|------|
| Baseline | 0.5692 | 70.6% | 78.3% | 47.8% | 39 | 18s |
| **+Reranker (pool=100, CE=40)** | **0.6125** | **72.8%** | **80.0%** | **52.2%** | **36** | 1788s |

**vs Baseline 提升：**

| 指标 | Baseline | +Reranker | 变化 |
|------|----------|-----------|------|
| MRR | 0.5692 | 0.6125 | **+7.6%** |
| R@10 | 78.3% | 80.0% | **+1.7pp** |
| Top1 | 47.8% | 52.2% | **+4.4pp** |
| R@10=0 | 39 | 36 | **-3 题** |

**按 Capability（+Reranker pool=100 vs Baseline）：**

| Capability | n | Baseline R@10 | +Reranker R@10 | 变化 |
|------------|---|---------------|----------------|------|
| exact_retrieval | 35 | 88.6% | 88.6% | 0 |
| context_expansion | 25 | 96.0% | 92.0% | -4.0pp |
| cross_section | 25 | 68.0% | **84.0%** | +16.0pp |
| cross_document | 30 | 66.7% | 70.0% | +3.3pp |
| table_retrieval | 20 | 80.0% | 80.0% | 0 |
| numeric_retrieval | 20 | 95.0% | 90.0% | -5.0pp |
| query_rewrite | 25 | 56.0% | 56.0% | 0 |

**按 Difficulty：**

| Difficulty | n | Baseline R@10 | +Reranker R@10 |
|------------|---|---------------|----------------|
| easy | 132 | 81.8% | **84.9%** |
| medium | 37 | 75.7% | 75.7% |
| hard | 11 | 45.5% | 36.4% |

### 10.4 Reranker 增益与损失分析

Reranker 将 RRF 粗排结果全量替换为 CrossEncoder 精排，存在双向效应：

**增益（CrossEncoder 救回的题）：**
- cross_section 是最大赢家：R@10 从 68% 飙升到 84%（+4 题），其中 Q_S14、Q_SR01、Q_T23 等 gold 的 CE 排名达到 0-3
- 新增命中主要来自 BM25/dense 某通道排名靠前但被 RRF 融合挤出的 chunk，CE 逐对比较会重新发现它们

**损失（CE 排错方向的题）：**
- context_expansion: 96% → 92%（-1 题），numeric_retrieval: 95% → 90%（-1 题）
- 这些题 RRF 能将正确 chunk 排进 top-10，但 CE 重新打分后排到了 10 名外
- 属模型偏好差异，非代码 bug

**R@10=0 从 39 降至 36（-3 题），** 但诊断发现在 pool=100 的情况下 CE 实际把 14 道 gold 排进了 top-10（`ce_rank` 0-9），说明还有 11 道左右的"CE 命中但评测未计入"——需排查 `expand_context` 或 `sec_to_cids` 映射是否有丢 chunk 的问题。

### 10.5 结论

1. **Reranker 默认开启**：MRR +7.6%，Top1 +4.4pp，R@10 达 80%，为当前最优单配置
2. **pool_size 放大有微弱增益**：R@10 从 79.4% → 80.0%（+0.6pp），MRR 从 0.6109 → 0.6125，主要来自 BM25 好结果不再被截断
3. **CE 有双向风险**：cross_section 暴涨 +16pp，但 context_expansion/numeric 各丢 1 题。纯 CE 排序不如 CE+RRF 融合稳健
4. **剩余 36 道 R@10=0 中**：约 14 道 CE 实际排进了 top-10 但评测未计入（疑似 `expand_context` 阶段丢失），约 11 道进了 CE 候选池但排名 10-20（可调大 top_k），约 11 道两通道都漏（需从 embedding/切块治）

---

## 十一、Union 融合实验（2026-07-12，已否定）

评测集：`golden_set_v2.json` in-domain 180 题 | 脚本：`eval_v2_retrieval.py`

### 11.1 实验动机

此前诊断发现 RRF 存在"融合丢失"：某通道排名靠前（如 BM25 rank=3）但 RRF 加权后被稀释。尝试用 Dense top-30 + BM25 top-10 → Union → CrossEncoder 替代 RRF，避免跨通道分数混合。

### 11.2 代码改动

| 文件 | 修改 |
|------|------|
| `hybrid_search.py` `search()` | Dense k=30、BM25 [:10]；移除 RRF 融合逻辑，改为 Union 去重（dense 优先顺序）；Union 全部候选送 CE 精排 |

### 11.3 实验结果

| Config | MRR | R@5 | R@10 | Top1 | R@10=0 | 耗时 |
|--------|------|------|------|------|--------|------|
| RRF (pool=40, CE=30) | **0.6125** | **72.8%** | **80.0%** | 52.2% | **36** | 1788s |
| Union (D30+B10, CE all) | 0.6165 | 72.2% | 76.1% | **53.9%** | 43 | 2702s |

**比 RRF 差。** R@10 跌 3.9pp，漏召回增加 7 道。

### 11.4 按 Capability 对比

| Capability | RRF R@10 | Union R@10 | 变化 |
|------------|----------|------------|------|
| exact_retrieval | 88.6% | **91.4%** | +2.9pp |
| context_expansion | 92.0% | 88.0% | -4.0pp |
| cross_section | **84.0%** | 68.0% | **-16.0pp** |
| cross_document | 70.0% | 70.0% | 0 |
| table_retrieval | 80.0% | 80.0% | 0 |
| numeric_retrieval | 90.0% | 90.0% | 0 |
| query_rewrite | **56.0%** | 44.0% | **-12.0pp** |

### 11.5 失败原因

1. **BM25 只放 10 个候选太少**：RRF 时 BM25 top-40 全在池子里，rank=0.3 虽小但能把排名靠前的 BM25 结果推上去。Union 把 BM25 砍到 10 个，rank 11-40 的好结果直接丢弃，query_rewrite（-12pp）和 context_expansion（-4pp）首当其冲
2. **cross_section 暴跌 16pp**：跨 section 题目依赖 BM25 关键词跨度覆盖，砍 BM25 候选数直接削弱这类题
3. **RRF 的互补效应不可替代**：BM25 权重 0.3 虽低，但排名 3 的 BM25 结果乘以 0.3/(60+3) 仍能进融合 top-30；Union 无此互补，等于纯靠 dense

### 11.6 结论

**已回退 RRF。** Union 方案 MRR 和 Top1 略有提升（CrossEncoder 看到更多候选），但 R@10 显著倒退。RRF 0.7/0.3 + CE top-30 为当前最优配置。

---

## 十二、CE 逐题增益分析（2026-07-13）

评测集：`golden_set_v2.json` in-domain 180 题 | 方法：每道题同时跑 RRF-only（`enable_reranker=False`）和 RRF+CE（`enable_reranker=True`），逐题对比 R@10 结果。

### 12.1 总体增益矩阵

| | RRF hit | RRF miss |
|---|---|---|
| **CE hit** | 129（两者都对） | **14（CE 救回）** |
| **CE miss** | **12（CE 搅黄）** | 25（两者都错） |

| 指标 | 值 |
|------|-----|
| RRF only R@10 | 78.3% (141/180) |
| RRF+CE R@10 | 79.4% (143/180) |
| CE 救回 | 14 题 |
| CE 搅黄 | 12 题 |
| **CE 净增益** | **+2 题** |

### 12.2 CE 救回 14 题

| ID | Capability | 说明 |
|----|-----------|------|
| Q_S03 | cross_section | 黑龙江农业气候资源普查，多种气候要素计算方法 |
| Q_S04 | cross_section | 陕西苹果种植适宜性/产量/品质多维度区划 |
| Q_S05 | cross_section | 新疆冬小麦多维度区划与产量提升建议 |
| Q_S07 | cross_section | 内蒙古大豆多种灾害风险区划共性与差异 |
| Q_S14 | cross_section | 河南冬小麦各气象要素年代际变化规律 |
| Q_S23 | cross_section | 河南冬小麦不同生育阶段气候条件与灾害 |
| Q_D10 | cross_document | 河南冬小麦品质 vs 气科院全国大豆品质区划 |
| Q_D19 | cross_document | 内蒙古大豆灾害 vs 陕西苹果气象灾害风险区划 |
| Q_D27 | cross_document | 内蒙古 vs 黑龙江大豆种植综合区划权重差异 |
| Q_L12 | query_rewrite | "陕西苹果为啥好吃？和天气有关系吗？" |
| Q_SR01 | query_rewrite | "高温伤害在大豆区划中如何评估？" |
| Q_SR02 | query_rewrite | "水分不足对大豆种植的影响用什么指标衡量？" |
| Q_SR07 | query_rewrite | "温度适宜性隶属度在气候区划中怎么用？" |
| Q_T23 | table_retrieval | 辽宁春玉米积温和降水各等级阈值 |

**按 Capability：** cross_section 6 道、cross_document 3 道、query_rewrite 4 道、table_retrieval 1 道。cross_section 是 CE 最大受益者，CE 对跨 section 语义理解明显优于 RRF 纯向量+关键词融合。

### 12.3 CE 搅黄 12 题

RRF 已将 gold 排入 top-10，CE 重新打分后挤出。

| ID | Capability | RRF 排名 | CE 排名 | 说明 |
|----|-----------|----------|---------|------|
| Q_C25 | context_expansion | 9 | 24 | 陕西苹果品质气候区划综合评价方法 |
| Q_S08 | cross_section | 2 | 13 | 陕西苹果区划完整工作流程 |
| Q_S15 | cross_section | 8 | 15 | 新疆冬小麦南疆北疆种植适宜性差异 |
| Q_D21 | cross_document | 6 | 16 | 黑龙江 vs 新疆大豆产量区划分级标准 |
| Q_D28 | cross_document | 7 | 16 | 气科院 vs 新疆冬小麦产量区划方法 |
| Q_D31 | cross_document | 2 | 21 | 黑龙江大豆冷害 vs 霜冻风险区划权重 |
| Q_T16 | table_retrieval | 5 | 14 | 黑龙江渍涝灾害风险区划土地覆盖度和DEM |
| Q_N11 | numeric_retrieval | 4 | 16 | 陕西苹果适宜区年降水量最适范围 |
| Q_L08 | query_rewrite | 1 | 17 | "大豆怎么知道该不该打药防虫？" |
| Q_SR04 | query_rewrite | 0 | 13 | "光合有效辐射在作物产量估算中起什么作用？" |
| Q_SR06 | query_rewrite | 8 | 10 | 气候生产潜力/光温/光合潜力的层次关系 |
| Q_SR09 | query_rewrite | 4 | 12 | 承灾体暴露度和脆弱性在大豆风险区划中的定义 |

**按 Capability：** query_rewrite 4 道、cross_document 3 道、cross_section 2 道、context_expansion/table/numeric 各 1 道。query_rewrite 类最易被 CE 搅黄——改写后的口语化 query 的语义表征与原文差异大，CE 容易偏好表面更匹配的干扰项。

### 12.4 两者都错 25 题（召回天花板）

| Capability | 数量 |
|------------|:--:|
| cross_document | 7 |
| query_rewrite | 7 |
| table_retrieval | 3 |
| exact_retrieval | 4 |
| numeric_retrieval | 1 |
| cross_section | 2 |
| context_expansion | 1 |

这些题 RRF 和 CE 都拿不到，是当前检索栈的硬天花板。根因分布见第十节分析（语义鸿沟 cos<0.55 约 10 道、排名偏低约 10 道、融合丢失约 5 道）。

### 12.5 结论与建议

1. **CE 净增益仅 +2，因为救 14 搅 12 几乎对等**，核心问题是 CE 完全不信任 RRF 先验（RRF #1 能被推到 #13）
2. **保底策略可救 8-10 道搅黄题**：RRF top-3 强制保留 + CE 排后 7，预计净增益可达 +10 以上
3. **cross_section 是 CE 最大赢家（+4 净增益）**，跨 section 语义匹配是 CE 的强项
4. **query_rewrite CE 搅黄最多（4 道）**，口语化 query 不适合直接送 CE，应考虑先做 query 规范化

---

## 十三、RRF+CE 分数融合与 Alpha 扫描（2026-07-13）

评测集：`golden_set_v2.json` in-domain 180 题 | 脚本：`eval_v2_retrieval.py` | 代码：`hybrid_search.py` `search()` 新增 `alpha` 参数

### 13.1 动机

第十二节分析揭示 pure CE（alpha=0）存在"搅黄"问题：CE 完全不信任 RRF 先验，RRF #1 能被推到 #13。本节实现 RRF+CE 分数融合——将两者的归一化分数加权组合，让 RRF 先验约束 CE 的精排方向，减少 CE 独立判断时的过偏。

### 13.2 实现

```
final = alpha * RRF_norm + (1-alpha) * CE_norm
```

- RRF 分数和 CE 分数各自 min-max 归一化到 [0,1]
- alpha=0 → pure CE；alpha=1 → pure RRF
- 在 `search()` 方法中替代原有的 pure CE 排序逻辑

### 13.3 Alpha 扫描结果

| alpha | RRF/CE 权重 | MRR | R@5 | R@10 | R@10=0 |
|-------|-------------|------|------|------|--------|
| 0.0 | 0.0/1.0 (pure CE) | 0.6075 | 72.2% | 78.9% | 38 |
| **0.2** | **0.2/0.8** | **0.6074** | **72.2%** | **82.2%** | **32** |
| 0.3 | 0.3/0.7 | 0.6055 | 71.1% | 80.6% | 35 |
| 0.4 | 0.4/0.6 | 0.5995 | 72.8% | 81.1% | 34 |
| 0.5 | 0.5/0.5 | 0.6083 | 71.7% | 81.7% | 33 |
| 1.0 | 1.0/0.0 (RRF only) | 0.5692 | 70.6% | 78.3% | 39 |

**alpha=0.2 是最优配置：**

| 指标 | RRF only | Pure CE (α=0) | α=0.2 | 提升(vs RRF) | 提升(vs pure CE) |
|------|----------|---------------|-------|-------------|-----------------|
| MRR | 0.5692 | 0.6075 | 0.6074 | **+6.7%** | ~0 |
| R@10 | 78.3% | 78.9% | **82.2%** | **+3.9pp** | **+3.3pp** |
| R@10=0 | 39 | 38 | **32** | **-7 题** | **-6 题** |

### 13.4 分析

**alpha=0.2 为何最优？**

- 20% RRF 先验权重提供"锚定"效应：RRF 排很靠前的结果即使 CE 不偏好也不会被完全推翻
- 80% CE 权重保留了精排的语义匹配能力（cross_section 等跨 section 题仍能受益）
- alpha≥0.3 时 RRF 先验过强，压制 CE 的语义能力，导致 CE 救不回来 Dense/BM25 漏掉的题

**vs 第十二节 pure CE 净增益+2：**

第十二节 pure CE 净增益仅 +2（救 14 搅 12），根因就是"CE 不信任 RRF"。alpha=0.2 保留 20% RRF 先验后，预计搅黄数从 12 降至 5 以下，净增益提升至约 +12。

**R@10=0 从 39→32：**

7 道原来两通道都漏或排名偏低的题被 alpha=0.2 的融合策略救回。这些题 RRF 虽未排进 top-10，但给了 gold 一定的 RRF 分数（rank 11-30），乘以 0.2 后再加 CE 0.8 就够进 top-10。

### 13.5 代码改动

| 文件 | 修改 |
|------|------|
| `hybrid_search.py` `search()` | 新增 `alpha` 参数（默认 0.2），实现 RRF+CE min-max 归一化加权融合 |
| `hybrid_search.py` | 移除旧的 `Reranker.rerank()` 调用路径，改为 inline CE scoring + 融合排序 |

### 13.6 结论

1. **alpha=0.2 为生产默认值**：R@10 从 78.3% 升至 82.2%，漏召回从 39 降至 32
2. **分数融合优于纯 CE**：加入少量 RRF 先验（20%）能有效抑制 CE 的过偏，同时保留精排能力
3. **不推荐 alpha>0.3**：RRF 先验过强时 MRR 开始下降，CE 的语义匹配优势被压制
4. **后续方向**：RRF top-3 强制保留策略可与分数融合叠加，预计进一步降低搅黄至接近 0

---

## 十四、新 Rewrite Prompt 与 Gate 触发（2026-07-14）

### 14.1 变更内容

| 变更 | 说明 |
|------|------|
| `REWRITE_PROMPT` 重写 | 明确 none/normalize/expand 三种输出类型，新增 few-shot 示例和术语映射表 |
| `_needs_rewrite` 门控 | 两个条件均满足才触发：`len(query) > 12` 且 `top1_sim < 0.70` |
| Gold 标注修复 | Q_L09、Q_L12 gold chunks 修正；Q_L05 因标注不可靠移除 |
| 评测集 | In-domain 从 180→179 题（移除 Q_L05） |

**架构**：旧 Append 架构（原始 query → RRF+CE，子查询 → RRF-only skip_reranker，merge 去重），alpha=0.2。

### 14.2 整体指标

```
题目数: 179  耗时: 1927.0s  改写触发: 158/199 (79.4%)

  MRR:             0.6169
  Recall@5:        0.7598 (76.0%)
  Recall@10:       0.8380 (83.8%)
  Top-1 命中率:    0.5196 (52.0%)
  平均命中 chunk:  1.2 / 1.6
  Recall@10=0:     29/179
```

### 14.3 按 Capability

| Capability | n | MRR | R@5 | R@10 | Top1 |
|------------|----|------|------|------|------|
| exact_retrieval | 35 | 0.784 | 0.886 | 0.886 | 0.714 |
| context_expansion | 25 | 0.768 | 0.920 | 0.920 | 0.680 |
| cross_section | 25 | 0.482 | 0.680 | 0.880 | 0.360 |
| cross_document | 30 | 0.455 | 0.600 | 0.700 | 0.333 |
| table_retrieval | 20 | 0.560 | 0.800 | 0.850 | 0.400 |
| numeric_retrieval | 20 | 0.827 | 0.900 | 0.950 | 0.800 |
| query_rewrite | 24 | 0.430 | 0.542 | 0.708 | 0.333 |

### 14.4 按 Difficulty

| Difficulty | n | MRR | R@5 | R@10 |
|------------|----|------|------|------|
| easy | 131 | 0.6800 | 0.8244 | 0.8702 |
| medium | 37 | 0.5020 | 0.6757 | 0.8378 |
| hard | 11 | 0.2516 | 0.2727 | 0.4545 |

### 14.5 R@10=0 漏召回（29 题）

```
Q_E23 (exact_retrieval, easy): 新疆的地理位置有什么特点？东西和南北跨度有多大？
Q_E24 (exact_retrieval, easy): 新疆冬小麦区划报告中列举了哪些主栽品种和示范品种？
Q_E30 (exact_retrieval, easy): 辽宁省春玉米干旱灾害区划基于什么核心指标？
Q_E33 (exact_retrieval, easy): 新疆冬小麦产量划分标准分为几个等级？
Q_C18 (context_expansion, easy): 新疆冬小麦品质性状与气候因子之间的关系模型是如何建立的？
Q_C25 (context_expansion, easy): 陕西省苹果品质气候区划采用什么方法进行综合评价？
Q_S04 (cross_section, easy): 陕西苹果气候区划报告中种植适宜性、产量和品质区划分别从哪些维度进行？
Q_S15 (cross_section, easy): 新疆冬小麦区划中南疆和北疆的冬小麦种植适宜性有什么差异？
Q_D04 (cross_document, hard): 内蒙古大豆区划和陕西苹果区划在指标体系构建和空间分析方法上有什么共性和差异？
Q_D05 (cross_document, hard): 黑龙江省区划报告中大豆不同生育期的活动积温空间分布有什么特征？
```

### 14.6 OOD 检测

```
OOD 召回率: 15/20 (75.0%)  漏判: 5
分层: signal=0 high_sim=3 score=8 llm=9
```

5 个漏判题与之前一致（Q_U03、Q_U04、Q_U05、Q_CF05、Q_CF06），OOD 检测器未变。

### 14.7 vs 旧 Rewrite Prompt（第一节表格）

| 指标 | 旧 +RW+Reranker | 新 +RW+Reranker | Δ |
|------|-----------------|-----------------|----|
| MRR | 0.5938 | 0.6169 | **+0.0231** |
| R@5 | 70.6% | 76.0% | **+5.4%** |
| R@10 | 78.9% | 83.8% | **+4.9%** |
| Top1 | 50.0% | 52.0% | **+2.0%** |
| R@10=0 | 38 | 29 | **-9** |

### 14.8 结论

1. **新 Rewrite Prompt 全面提升**：MRR +0.023，R@10 +4.9pp，漏召回 -9 题
2. **Gate 门控有效**：79.4% 触发改写（vs 旧 100% 无差别改写），改写更精准
3. **numeric_retrieval 提升最明显**：MRR 0.827（+0.156 vs baseline），受益于术语标准化
4. **cross_document 仍是最短板**：R@10=70%，改写对此能力的帮助有限

---

## 十五、Late Fusion 架构评测（2026-07-14）

### 15.1 架构变更

| 维度 | 旧 Append | 新 Late Fusion |
|------|-----------|----------------|
| 候选池 | per-query 独立 RRF | 全部 query 共享一个 RRF 池 |
| CE 精排 | 原始 query 过 CE，子查询跳过 | 统一 top-40 过一次 CE |
| 合并时机 | per-query RRF+CE 后 append 去重 | RRF 打分时就合并（多 query 累加 RRF） |
| 子查询信号 | 子查询只在 RRF 轮参与，不进 CE | 子查询与原始 query 在 RRF 中同台竞争，共享 CE |

**动机**：避免子查询召回的高价值 chunk 因 skip_reranker 而缺少 CE 信号，导致 cross_document/cross_section 等能力受限。

**配置**：alpha=0.2，pool_size=40，rerank_input=min(top_k*3, 40)=30。

### 15.2 整体指标

```
题目数: 179  耗时: 1475.0s  改写触发: 154/199 (77.4%)

  MRR:             0.5758
  Recall@5:        0.7542 (75.4%)
  Recall@10:       0.8101 (81.0%)
  Top-1 命中率:    0.4581 (45.8%)
  平均命中 chunk:  1.1 / 1.6
  Recall@10=0:     34/179
```

### 15.3 按 Capability

| Capability | n | MRR | R@5 | R@10 | Top1 |
|------------|----|------|------|------|------|
| exact_retrieval | 35 | 0.745 | 0.886 | 0.886 | 0.629 |
| context_expansion | 25 | 0.683 | 0.880 | 0.920 | 0.560 |
| cross_section | 25 | 0.446 | 0.640 | 0.720 | 0.320 |
| cross_document | 30 | 0.514 | 0.667 | 0.667 | 0.433 |
| table_retrieval | 20 | 0.437 | 0.800 | 0.900 | 0.250 |
| numeric_retrieval | 20 | 0.671 | 0.900 | 0.900 | 0.500 |
| query_rewrite | 24 | 0.466 | 0.500 | 0.708 | 0.417 |

### 15.4 按 Difficulty

| Difficulty | n | MRR | R@5 | R@10 |
|------------|----|------|------|------|
| easy | 131 | 0.6191 | 0.8092 | 0.8550 |
| medium | 37 | 0.5060 | 0.6757 | 0.7838 |
| hard | 11 | 0.2955 | 0.3636 | 0.3636 |

### 15.5 R@10=0 漏召回（34 题）

```
Q_E23 (exact_retrieval, easy): 新疆的地理位置有什么特点？东西和南北跨度有多大？
Q_E24 (exact_retrieval, easy): 新疆冬小麦区划报告中列举了哪些主栽品种和示范品种？
Q_E30 (exact_retrieval, easy): 辽宁省春玉米干旱灾害区划基于什么核心指标？
Q_E33 (exact_retrieval, easy): 新疆冬小麦产量划分标准分为几个等级？
Q_C18 (context_expansion, easy): 新疆冬小麦品质性状与气候因子之间的关系模型是如何建立的？
Q_C25 (context_expansion, easy): 陕西省苹果品质气候区划采用什么方法进行综合评价？
Q_S01 (cross_section, medium): 内蒙古大豆种植气候区划中选用了哪些区划指标？各指标的分级阈值是多少？
Q_S04 (cross_section, easy): 陕西苹果气候区划报告中种植适宜性、产量和品质区划分别从哪些维度进行？
Q_S05 (cross_section, easy): 新疆冬小麦气候区划从哪几个维度进行？产量提升建议与区划结果有什么关联？
Q_S07 (cross_section, easy): 内蒙古大豆多种灾害（干旱、霜冻、食心虫）风险区划在指标体系和方法上有什么共性和差异？
```

### 15.6 OOD 检测

```
OOD 召回率: 15/20 (75.0%)  漏判: 5
分层: signal=0 high_sim=3 score=5 llm=12
```

漏判题与第十四节一致（Q_U03、Q_U04、Q_U05、Q_CF05、Q_CF06）。

### 15.7 全配置对比（含 Late Fusion）

| Config | MRR | R@5 | R@10 | Top1 | R@10=0 | 架构 |
|--------|------|------|------|------|--------|------|
| Baseline | 0.5692 | 70.6% | 78.3% | 47.8% | 39 | — |
| +Rewrite only | 0.4785 | 58.9% | 68.9% | 38.3% | 56 | Append |
| +Reranker only | 0.6092 | 72.8% | 80.0% | 51.7% | 36 | — |
| **+RW+Reranker (新 prompt, Append)** | **0.6169** | **76.0%** | **83.8%** | **52.0%** | **29** | Append |
| +RW+Reranker (Late Fusion) | 0.5758 | 75.4% | 81.0% | 45.8% | 34 | Late Fusion |

### 15.8 Late Fusion vs Append 逐能力对比

| Capability | Append MRR | LF MRR | Δ MRR | Append R@10 | LF R@10 | Δ R@10 |
|------------|-----------|--------|-------|-------------|---------|--------|
| exact_retrieval | 0.784 | 0.745 | -0.039 | 0.886 | 0.886 | 0 |
| context_expansion | 0.768 | 0.683 | -0.085 | 0.920 | 0.920 | 0 |
| cross_section | 0.482 | 0.446 | -0.036 | 0.880 | 0.720 | **-0.160** |
| cross_document | 0.455 | 0.514 | **+0.059** | 0.700 | 0.667 | -0.033 |
| table_retrieval | 0.560 | 0.437 | -0.123 | 0.850 | 0.900 | +0.050 |
| numeric_retrieval | 0.827 | 0.671 | -0.156 | 0.950 | 0.900 | -0.050 |
| query_rewrite | 0.430 | 0.466 | +0.036 | 0.708 | 0.708 | 0 |

### 15.9 分析

**Late Fusion 为什么整体变差？**

1. **多 query RRF 累加引入噪声**：改写查询的 BM25/Dense 结果与原始 query 共享 RRF 池，改写查询召回的 chunk 获得额外 RRF 加分，挤占了原始 query 的高质量候选。原始 query 本身的 CE 评分是最可靠的信号，Late Fusion 稀释了这一优势。

2. **单次 CE 覆盖不足**：旧架构原始 query 过 CE 时面对的是 RRF 精选后的 top-30，干扰少。Late Fusion 的 RRF top-40 混合了多个 query 的结果，CE 需要从更嘈杂的候选池中挑选，精度下降。

3. **cross_section R@10 暴跌 -16pp**：cross_section 需要跨 section 对比，改写查询容易引入不相关 section，在 RRF 累加后排名上升，挤掉了正确答案。

4. **cross_document MRR 微涨 +0.059**：Late Fusion 的设计动机（让改写 query 的 CE 信号参与排序）对 cross_document 产生了预期的正面效果，但幅度有限，且 R@10 反而 -0.033。

**结论**：

- Late Fusion 在 R@5 上持平（75.4% vs 76.0%），但 MRR 和 Top1 显著下降
- **不推荐 Late Fusion**：Append 架构中原始 query 独占 CE 精排信号的优势不容忽视
- **建议回退 Append 架构**，将新 rewrite prompt（第十四节）作为生产配置
- cross_document 的改善方向应探索其他策略（如 query 分解后独立检索+LLM 汇总），而非改变融合架构

### 15.10 代码改动

| 文件 | 修改 |
|------|------|
| `hybrid_search.py` | 新增 `_chunk_key()` chunk_id 去重 helper |
| `hybrid_search.py` | 新增 `_expand_results()` 上下文扩展 helper |
| `hybrid_search.py` | 新增 `_rrf_ce_fusion()` RRF+CE 融合 helper |
| `hybrid_search.py` `search_multi_query()` | 完全重写为 Late Fusion |
| `hybrid_search.py` `search()` | 重构使用三个 helper，减少 ~85 行 |
| `hybrid_search.py` `_init()` | BM25 metadata 补全 chunk_id + chunk_index |

---

## 十六、Hybrid Fusion 实验（2026-07-15，已否定）

### 16.1 架构

用户提出的两级 Hybrid Fusion：

```
Level 1:  Original → RRF → Top30 → CE → Scored Candidates
          Rewrite → RRF → Top20 → chunk keys (投票)

Level 2:  Score Fusion
          Final = CE_Score + β × RewriteVote  (β=0.01)
          Rewrite 只加分不推翻，Original 没有的候选可补入
```

**设计意图**：Rewrite 作为轻量投票信号，不进入 CE 管线，避免噪声干扰 original 的精确排序。

### 16.2 整体指标

```
题目数: 179  耗时: 3242.9s  改写触发: 154/199 (77.4%)

  MRR:             0.5568
  Recall@5:        0.7430 (74.3%)
  Recall@10:       0.8268 (82.7%)
  Top-1 命中率:    0.4358 (43.6%)
  平均命中 chunk:  1.1 / 1.6
  Recall@10=0:     31/179
```

### 16.3 按 Capability

| Capability | n | MRR | R@5 | R@10 | Top1 |
|------------|----|------|------|------|------|
| exact_retrieval | 35 | 0.715 | 0.886 | 0.886 | 0.600 |
| context_expansion | 25 | 0.693 | 0.920 | 0.920 | 0.560 |
| cross_section | 25 | 0.477 | 0.680 | 0.840 | 0.360 |
| cross_document | 30 | 0.423 | 0.600 | 0.700 | 0.300 |
| table_retrieval | 20 | 0.397 | 0.800 | 0.900 | 0.200 |
| numeric_retrieval | 20 | 0.733 | 0.900 | 0.900 | 0.600 |
| query_rewrite | 24 | 0.421 | 0.417 | 0.667 | 0.375 |

### 16.4 按 Difficulty

| Difficulty | n | MRR | R@5 | R@10 |
|------------|----|------|------|------|
| easy | 131 | 0.6189 | 0.8092 | 0.8702 |
| medium | 37 | 0.4284 | 0.6486 | 0.7838 |
| hard | 11 | 0.2487 | 0.2727 | 0.4545 |

### 16.5 OOD 检测

```
OOD 召回率: 14/20 (70.0%)  漏判: 6
分层: signal=0 high_sim=3 score=9 llm=8
```

新增漏判 Q_CF02，其他 5 题与之前一致。

### 16.6 全架构对比

| Config | MRR | R@5 | R@10 | Top1 | R@10=0 | 耗时 |
|--------|------|------|------|------|--------|------|
| Baseline | 0.5692 | 70.6% | 78.3% | 47.8% | 39 | 18s |
| +Rewrite only | 0.4785 | 58.9% | 68.9% | 38.3% | 56 | 329s |
| +Reranker only | 0.6092 | 72.8% | 80.0% | 51.7% | 36 | 3499s |
| **+RW+Reranker (Append)** | **0.6169** | **76.0%** | **83.8%** | **52.0%** | **29** | 1927s |
| +RW+Reranker (Late Fusion) | 0.5758 | 75.4% | 81.0% | 45.8% | 34 | 1475s |
| +RW+Reranker (Hybrid Fusion) | 0.5568 | 74.3% | 82.7% | 43.6% | 31 | 3243s |

### 16.7 vs Append 逐能力 Δ

| Capability | Append MRR | HF MRR | Δ MRR | Append R@10 | HF R@10 | Δ R@10 |
|------------|-----------|--------|-------|-------------|---------|--------|
| exact_retrieval | 0.784 | 0.715 | -0.069 | 0.886 | 0.886 | 0 |
| context_expansion | 0.768 | 0.693 | -0.075 | 0.920 | 0.920 | 0 |
| cross_section | 0.482 | 0.477 | -0.005 | 0.880 | 0.840 | -0.040 |
| cross_document | 0.455 | 0.423 | -0.032 | 0.700 | 0.700 | 0 |
| table_retrieval | 0.560 | 0.397 | **-0.163** | 0.850 | 0.900 | +0.050 |
| numeric_retrieval | 0.827 | 0.733 | -0.094 | 0.950 | 0.900 | -0.050 |
| query_rewrite | 0.430 | 0.421 | -0.009 | 0.708 | 0.667 | -0.041 |

### 16.8 分析

**Hybrid Fusion 为什么失败？**

1. **MRR 0.5568 为所有 reranker 配置最差**，甚至低于 Baseline（0.5692）。说明加了 Rewrite 反而比不用 Rewrite 差。

2. **Medium 难度暴跌**：MRR 从 0.5020（Append）降到 0.4284（-7.4pp）。中等难度题需要改写来补全上下文，但 Hybrid Fusion 的 bonus-only 机制无法有效利用改写信号。

3. **table_retrieval MRR -0.163**：表格类问题受影响最大。Rewrite-only 候选以 base_score 补入 top-k，挤占了 original CE 排序中本应在的表格 chunk。

4. **β=0.01 太小**：3 票才 +0.03，不足以弥补排序差距。但如果增大 β，会违背"不推翻 original"的设计原则，退化为噪声。

5. **rewrite-only 候选占位**：base_score 补入的 rewrite-only chunk 质量不可靠（无 CE 信号），占用了 top-k 位置。

6. **耗时 3243s**：每个 rewrite query 独立做 Chroma+BM25，无共享，比 Append 慢 68%。

### 16.9 结论

1. **Hybrid Fusion 不推荐**：所有指标（除 R@10）均为最差，Medium 难度严重退化
2. **Append 架构仍是最优**：Original 独占 CE 管线 + Rewrite skip_reranker 追加，简单有效
3. **核心洞察**：Rewriter 的 RRF 信号质量远不如 Original 的 CE 信号，无论用 Late Fusion 还是 Hybrid Fusion 混合，都会稀释 Original CE 的精度优势
4. **已回退 Append**，作为生产配置

---

## 十七、chunks_split + 三相管线 + 诊断分桶（2026-07-24）

评测集：`golden_set_v2.json` 199 题（in-domain 179 + OOD 20） | 脚本：`eval_v2_full.py`

### 17.1 变更内容

| 变更 | 说明 |
|------|------|
| Chunks 源 | `chunks.json` → `chunks_split.json`（787 entries, 499 unique section IDs, 89 duplicate IDs with chunk_index） |
| 检索管线 | `search_multi_query()` 从旧 Append 改为三相管线：Dense Protected (Phase 1) → RRF+CE (Phase 2) → Rewrite RRF-only (Phase 3) |
| 诊断分桶 | 新增 `eval_diagnostic.py` DiagnosticAnalyzer，逐题追溯 gold chunk 在管线中的排名变化 |
| Gold 标注 | 5 个 EXCLUDED case 修复（PDF→DOCX 等价 chunk），282 个 gold_chunk 全部在 chunks_split.json 中存在 |
| 进度输出 | 每 20 题输出进度、耗时、预估剩余时间 |

**配置**：+Rewrite + Reranker（Append 架构），alpha=0.2, pool_size=max(top_k*4,20), top_k=10, expand_context=True。

### 17.2 整体指标

```
题目数: 179 (In-domain)  耗时: 1556s (~26 min)

  MRR:             0.5638
  Recall@5:        0.6648 (66.5%)
  Recall@10:       0.7207 (72.1%)
  Top-1 命中率:    0.4804 (48.0%)
  平均命中 chunk:  1.6 / 1.6
  Recall@10=0:     50/179 (27.9%)
```

### 17.3 按 Capability

| Capability | n | MRR | R@5 | R@10 | Top1 |
|------------|----|------|------|------|------|
| exact_retrieval | 35 | 0.779 | 0.914 | 0.943 | 0.686 |
| context_expansion | 25 | 0.784 | 0.840 | 0.840 | 0.720 |
| cross_section | 25 | 0.385 | 0.560 | 0.560 | 0.280 |
| cross_document | 30 | 0.393 | 0.500 | 0.667 | 0.300 |
| table_retrieval | 20 | 0.484 | 0.600 | 0.600 | 0.400 |
| numeric_retrieval | 20 | 0.685 | 0.750 | 0.850 | 0.600 |
| query_rewrite | 24 | 0.386 | 0.417 | 0.500 | 0.333 |

### 17.4 按 Difficulty

| Difficulty | n | MRR | R@5 | R@10 |
|------------|----|------|------|------|
| easy | 131 | 0.624 | 0.718 | 0.756 |
| medium | 37 | 0.442 | 0.541 | 0.622 |
| hard | 11 | 0.258 | 0.455 | 0.636 |

### 17.5 OOD 检测

| 指标 | 值 |
|------|-----|
| OOD 召回率 | 16/20 (80.0%) |
| 漏判 | 4 |
| Judge 分层 | signal=0, high_sim=2, score=9, llm=9 |

vs 第十四节（75.0%, 5 漏判）：OOD 提升 5pp，漏判减少 1 题。

### 17.6 诊断分桶 — 50 题零召回定位

诊断分析针对 +Rewrite+Reranker 配置下的 50 道 R@10=0 题目，逐题追溯 gold chunk 在 Dense→BM25→RRF→CE 管线中的排名变化。

#### 环节汇总

| 环节 | 题数 | 占比 | 建议 |
|------|------|------|------|
| B-检索层-低余弦断连 | 2 | 4% | 优化 query 改写（口语→术语）、增大 Dense pool、优化 chunk 切分 |
| C-RRF融合-单通道强但被稀释 | 8 | 16% | 提高 Dense 权重 (0.7→0.85)、增大 RRF pool |
| C-RRF融合-排名偏低 | 3 | 6% | 扩大候选池或调 RRF 权重 |
| C-RRF融合-排名靠后 | 9 | 18% | 检索信号不足 — 提升检索质量是根本 |
| D-CE精排-严重搅黄 | 4 | 8% | 增加 alpha (0.2→0.3)、扩大 CE 候选池 (30→50) |
| G-已修复 | 24 | 48% | 诊断独立检索时复现，主 eval 与诊断搜索参数差异导致 |

#### B-检索层-低余弦断连（2 题）

| ID | Capability | Difficulty | Query | cos | 原因 |
|----|-----------|------------|-------|-----|------|
| Q_L11 | query_rewrite | easy | 种大豆选什么地方最好？ | 0.507 | 语义鸿沟，口语→术语映射失败 |
| Q_D29 | cross_document | hard | 陕西苹果品质区划和新疆冬小麦品质区划... | 0.650 | 双通道 top100 均无 gold |

#### C-RRF融合-单通道强但被稀释（8 题）

Dense 或 BM25 单通道 top10 内有 gold，但 RRF 融合后被另一通道噪声稀释至 top10 外。

| ID | Capability | Difficulty | D Rank | B Rank | RRF Rank | CE Rank |
|----|-----------|------------|--------|--------|----------|---------|
| Q_C25 | context_expansion | easy | 6 | -1 | 16 | 12 |
| Q_D26 | cross_document | medium | 6 | 9 | 11 | 18 |
| Q_T09 | table_retrieval | easy | 7 | -1 | 14 | 11 |
| Q_E13 | exact_retrieval | easy | 7 | 2 | 13 | 10 |
| Q_N11 | numeric_retrieval | easy | 9 | -1 | 23 | 12 |
| Q_D10 | cross_document | medium | 28 | 6 | 27 | 12 |
| Q_L08 | query_rewrite | medium | 9 | 1 | 14 | 16 |
| Q_D20 | cross_document | medium | 27 | 1 | 20 | 13 |

#### C-RRF融合-排名偏低（3 题）

Gold 在 RRF pool 内（top 30），但排名偏低无法进入 CE 候选。

| ID | Capability | Difficulty | RRF Rank |
|----|-----------|------------|-----------|
| Q_S01 | cross_section | medium | 20 |
| Q_D05 | cross_document | hard | 28 |
| Q_E33 | exact_retrieval | easy | 15 |

#### C-RRF融合-排名靠后（9 题）

Gold 在 RRF pool 外（>30），CE 未获得 gold。

| ID | Capability | Difficulty | RRF Rank |
|----|-----------|------------|-----------|
| Q_D11 | cross_document | medium | 50 |
| Q_T28 | table_retrieval | easy | 50 |
| Q_SR01 | query_rewrite | medium | 37 |
| Q_N13 | numeric_retrieval | easy | 39 |
| Q_SR03 | query_rewrite | medium | 49 |
| Q_L01 | query_rewrite | medium | 67 |
| Q_D04 | cross_document | hard | 69 |
| Q_L07 | query_rewrite | easy | 41 |
| Q_L02 | query_rewrite | easy | 47 |

#### D-CE精排-严重搅黄（4 题）

Gold 在 RRF top10 内，但 CE 精排后排名被推后至 top10 外。

| ID | Capability | Difficulty | RRF Rank | CE Rank | Δ |
|----|-----------|------------|----------|---------|---|
| Q_S05 | cross_section | easy | 7 | 14 | +7 |
| Q_S15 | cross_section | easy | 5 | 12 | +7 |
| Q_D28 | cross_document | hard | 4 | 18 | +14 |
| Q_C08 | context_expansion | easy | 5 | 13 | +8 |

#### G-已修复（24 题）

诊断独立检索时这 24 题 gold 进入 CE top-10（通过 R@10），但主 eval 报告为 R@10=0。原因：
1. **HNSH 非确定性**：ChromaDB HNSW 图搜索每次返回略有不同
2. **参数差异**：诊断 top_k=30（pool_size=120）vs 主 eval top_k=10（pool_size=40）
3. **expand_context 差异**：主 eval 开启上下文扩展，诊断 CE 搜索关闭

| ID | Capability | Difficulty | CE Rank |
|----|-----------|------------|---------|
| Q_S04 | cross_section | easy | 3 |
| Q_T31 | table_retrieval | easy | 2 |
| Q_C07 | context_expansion | easy | 1 |
| Q_S22 | cross_section | easy | 8 |
| Q_N14 | numeric_retrieval | easy | 6 |
| Q_T23 | table_retrieval | easy | 4 |
| Q_T20 | table_retrieval | easy | 8 |
| Q_D06 | cross_document | medium | 1 |
| Q_T29 | table_retrieval | easy | 6 |
| Q_T16 | table_retrieval | medium | 4 |
| Q_SR09 | query_rewrite | easy | 4 |
| Q_D16 | cross_document | medium | 8 |
| Q_S07 | cross_section | easy | 9 |
| Q_S26 | cross_section | medium | 6 |
| Q_T24 | table_retrieval | easy | 7 |
| Q_S23 | cross_section | easy | 6 |
| Q_S14 | cross_section | easy | 9 |
| Q_C23 | context_expansion | easy | 7 |
| Q_S03 | cross_section | easy | 8 |
| Q_L12 | query_rewrite | easy | 5 |
| Q_SR02 | query_rewrite | easy | 9 |
| Q_SR06 | query_rewrite | easy | 7 |
| Q_S08 | cross_section | easy | 3 |
| Q_SR13 | query_rewrite | medium | 4 |

### 17.7 vs 第十四节 Append（chunks.json + 旧 Append 管线）

| 指标 | 十四节 (chunks.json, 旧管线) | 本节 (chunks_split, 三相管线) | Δ |
|------|----------------------------|------------------------------|----|
| MRR | 0.6169 | 0.5638 | **-0.0531** |
| R@5 | 76.0% | 66.5% | **-9.5pp** |
| R@10 | 83.8% | 72.1% | **-11.7pp** |
| Top1 | 52.0% | 48.0% | **-4.0pp** |
| R@10=0 | 29 | 50 | **+21** |
| OOD 召回 | 75.0% | 80.0% | **+5.0pp** |

chunks_split + 三相管线相比旧 chunks.json + Append 管线全面退化，R@10 下降近 12pp。需排查是 chunks_split 切分变细导致 embedding 信号稀释，还是三相管线 Dense Protected Merge 阶段存在问题。OOD 检测反升 5pp（可能 top1_sim 整体降低后 high_sim 漏判减少）。

### 17.8 代码改动

| 文件 | 修改 |
|------|------|
| `hybrid_search.py` | `search_multi_query()` 改为三相管线（Dense Protected→RRF+CE→Rewrite RRF-only） |
| `data/golden_set_v2.json` | 5 个 EXCLUDED case 修复（PDF→DOCX 等价 chunk），Q_L09/Q_L12 gold 修正 |
| `eval_v2_full.py` | 数据源 → chunks_split.json，新增诊断分桶、进度输出 |
| `eval_diagnostic.py`（新增） | DiagnosticAnalyzer：逐题追溯 gold 在 Dense→BM25→RRF→CE 管线排名 |
| `query_rewriter.py` | `_load_cache()` 修复返回值 bug（曾返回 None） |
| `eval_v2_retrieval.py`, `eval_v2_rerank_rewrite.py`, `eval_rewrite_recall.py`, `eval_rewrite_gated.py`, `diagnose_zero_recall.py`, `eval_30_quick.py` | chunks.json → chunks_split.json |

### 17.9 已知限制

1. **HNSW 非确定性**：ChromaDB 默认 HNSW 索引每次检索结果不完全一致
2. **诊断与主 eval 结果不一致**：诊断独立检索（top_k=30, expand_context=False），与主 eval（top_k=10, expand_context=True）参数不同。24 题 G-已修复即此差异导致。应改为诊断复用主 eval 搜索结果
3. **Embedding 语义鸿沟**：口语化问法与定义类文本 embedding 相似度低，是 query_rewrite 类别零召回主因
4. **Cross-document 仍是短板**：MRR=0.393, R@10=66.7%，跨文档语义关联检索能力不足

### 17.10 后续建议

1. **排查 chunks_split + 三相管线退化根因**：#10 在所有指标上显著劣于 #7（同模型），需逐环节对比旧 Append vs 新三相管线，定位退化环节
2. **修复诊断复用**：DiagnosticAnalyzer 应接收主 eval 的 search_multi_query 结果，而非独立检索
3. **RRF 权重调整**：8 题单通道强但被稀释 → 提高 Dense 权重（0.7→0.85）或动态权重
4. **CE 候选池扩大**：4 题 CE 搅黄 → 增大 CE 候选池（30→50）、提高 alpha（0.2→0.3）

---

## 十八、Parallel Evidence Merge（2026-07-28）

评测集：`golden_set_v2.json` 199 题（in-domain 179 + OOD 20） | 脚本：`eval_v2_full.py`

### 18.1 变更内容

| 变更 | 说明 |
|------|------|
| 检索架构 | `search_multi_query` 从 Append 改为 Parallel Evidence Merge：Original + SubQueries 并行 Dense+BM25→RRF → 去重 + Hit Boost + Dense Protect → Global Pool → CE Rerank |
| 返回值修正 | `search_multi_query` 返回 `(judge_results, merged)` — judge_results 为原始查询独立 CE 结果（供 OOD Judge），merged 为 Evidence Merge 合并结果（供生成/评估） |
| 评估逻辑 | 从 section 展开展平改为 chunk 级直接 ID 对比（primary）+ section 级辅助 |
| `_rrf_retrieve` 提取 | 单查询 Dense+BM25→RRF 融合逻辑提取为独立方法 |
| `search()` 重构 | 复用 `_rrf_retrieve` + `_rrf_ce_fusion` |
| OOD Judge | 4 层简化为 2 层：direct（top1_sim ≥ 0.70） / llm |

**配置**：+Rewrite + Reranker，alpha=0.2, lambda_length=0.1, expand_context=False（eval），orig_dense_k=30, orig_bm25_k=20, subq_dense_k=20, subq_bm25_k=10。

**检索流程**：

```
Query + SubQueries (并行)
  │
  ├─ Original:  Dense k=30  + BM25 k=20  → RRF (K=60, 0.7/0.3)
  ├─ SubQ_1:    Dense k=20  + BM25 k=10  → RRF
  ├─ SubQ_2:    Dense k=20  + BM25 k=10  → RRF
  └─ ...
  │
  ├─ Evidence Merge: max(RRF) 去重 + 来源追踪
  ├─ Hit Boost: 2 查询命中 +0.10, 3+ 查询命中 +0.15
  ├─ Per-query Dense Protect: 每个查询 Dense top-2 保送
  │
  ├─ Global Pool: RRF top-40 ∪ Dense Protect → cap 50
  ├─ CE Rerank (CrossEncoder bge-reranker-v2-m3, 使用 Original Query)
  ├─ Alpha Fusion: final = 0.2 × RRF_norm + 0.8 × CE_norm
  └─ Output: top_k = 10
```

### 18.2 整体指标

```
题目数: 179 (In-domain)  耗时: 3532.8s (~59 min)

  ── Chunk 级（基础指标）──
  MRR:             0.5469
  Recall@5:        0.7430 (74.3%)
  Recall@10:       0.8659 (86.6%)
  Top-1 命中率:    0.3966 (39.7%)
  平均命中 chunk:  1.1 / 1.6
  Recall@10=0:     24/179 (13.4%)

  ── Section 级（辅助指标）──
  MRR:             0.5469
  Recall@5:        0.7430 (74.3%)
  Recall@10:       0.8659 (86.6%)
```

### 18.3 按 Capability (Chunk 级)

| Capability | n | MRR | R@5 | R@10 | Top1 |
|------------|----|------|------|------|------|
| exact_retrieval | 35 | 0.719 | 0.914 | 0.971 | 0.543 |
| context_expansion | 25 | 0.660 | 0.840 | 0.960 | 0.560 |
| cross_section | 25 | 0.424 | 0.800 | 0.880 | 0.240 |
| cross_document | 30 | 0.418 | 0.633 | 0.733 | 0.267 |
| table_retrieval | 20 | 0.468 | 0.650 | 0.950 | 0.250 |
| numeric_retrieval | 20 | 0.800 | 0.900 | 0.900 | 0.700 |
| query_rewrite | 24 | 0.322 | 0.417 | 0.667 | 0.208 |

### 18.4 按 Difficulty (Chunk 级)

| Difficulty | n | MRR | R@5 | R@10 |
|------------|----|------|------|------|
| easy | 131 | 0.601 | 0.809 | 0.908 |
| medium | 37 | 0.414 | 0.595 | 0.811 |
| hard | 11 | 0.352 | 0.455 | 0.545 |

### 18.5 OOD 检测 (20 题)

| 指标 | 值 |
|------|-----|
| OOD 召回率 | 11/20 (55.0%) |
| 漏判 | 9 |
| Judge 分层 | direct=7, llm=13 |

9 题漏判中 7 题被 direct 放行（top1_sim ≥ 0.70），0.70 阈值偏低。

### 18.6 诊断分桶（24 题零召回）

| 环节 | 题数 | 占比 | 建议 |
|------|------|------|------|
| C-RRF融合-单通道强但被稀释 | 17 | 70.8% | 提高单通道权重或抑制噪声 |
| D-CE精排-搅黄 | 4 | 16.7% | 增加 alpha、扩大 CE 候选池 |
| B-检索层-低余弦断连 | 2 | 8.3% | 优化 query 改写（口语→术语） |
| E-改写层-命中但未进top10 | 1 | 4.2% | 优化 merge 策略 |

### 18.7 vs 三相管线（第十七节）

| 指标 | 三相管线 (#10) | Evidence Merge (#11) | Δ |
|------|---------------|---------------------|----|
| MRR | 0.5638 | 0.5469 | -0.0169 |
| R@5 | 66.5% | 74.3% | **+7.8pp** |
| R@10 | 72.1% | 86.6% | **+14.5pp** |
| Top1 | 48.0% | 39.7% | -8.3pp |
| R@10=0 | 50 | 24 | **-26 题** |
| OOD 召回 | 80.0% | 55.0% | -25.0pp |

**分析**：
- **R@10 大幅提升 +14.5pp**：Evidence Merge 的 Hit Boost + Dense Protect 机制将子查询的高质量结果有效并入候选池，零召回从 50 降至 24
- **MRR 微降 -0.017**：合并后的排序中，第一位命中率下降（Top1 39.7% vs 48.0%），说明 Evidence Merge 更擅长"召回"而非"精排第一位"
- **OOD 下降 -25pp**：三相管线 expand_context=True 时 similarity 更高，Evidence Merge eval 用 expand_context=False 导致 direct 阈值更容易被触发。需要重新校准 0.70 阈值
- **query_rewrite 仍是最大短板**：MRR=0.322, R@10=66.7%

### 18.8 最大瓶颈：C-RRF融合-单通道强但被稀释（17/24 = 70.8%）

17 道零召回的 gold chunk 在 BM25 或 Dense 单通道 top10 内，但 RRF 融合后被另一通道噪声稀释。典型：
- BM25 单通道命中但 Dense 噪声大 → RRF 排名跌出 top10
- Dense 单通道命中但 BM25 灌入干扰 chunk → 稀释 Dense 信号

### 18.9 代码改动

| 文件 | 修改 |
|------|------|
| `hybrid_search.py` | 新增 `_rrf_retrieve()` 单查询 Dense+BM25→RRF 方法 |
| `hybrid_search.py` | `search_multi_query()` 重写为 Parallel Evidence Merge |
| `hybrid_search.py` | `search()` 重构复用 `_rrf_retrieve` + `_rrf_ce_fusion` |
| `hybrid_search.py` | `_rrf_ce_fusion` 新增 `lambda_length` 长度归一化 |
| `hybrid_search.py` | `search_multi_query` 返回 `(judge_results, merged)` |
| `eval_v2_full.py` | `run_one` chunk 级直接 ID 对比 + section 级辅助 |
| `eval_v2_full.py` | `run_one` 调用侧 `_, results = search_multi_query(...)` |
| `judge.py` | OOD Judge 从 4 层简化为 2 层（direct ≥0.70 / llm） |
| `CHUNK_PIPELINE.md` | 更新为 Parallel Evidence Merge 架构文档 |

### 18.10 后续建议

1. **RRF 权重调优**：70.8% 零召回是单通道被稀释，尝试提高 Dense 权重（0.7→0.85）或动态权重
2. **OOD 阈值重校准**：Evidence Merge + expand_context=False 下 OOD 仅 55%，需扫描 direct 阈值
3. **query_rewrite 专项优化**：口语→术语映射是最弱环节，考虑加入同义词词典或 fine-tuned embedding
4. **子查询质量过滤**：部分改写查询引入噪声，可考虑对子查询做质量评分后再决定是否加入 Evidence Merge

---

## 十九、三池分离 Query Rewrite + Hit Boost 0.002（2026-07-29）

### 19.1 变更概要

| 变更 | 旧 | 新 |
|------|----|----|
| Rewrite 池结构 | Keyword + SubQuery 2池 | Keyword + RewriteQuery + SubQuery 3池 |
| Rewrite Queries | 无此字段 | 新增，完整句子改写（≤2，hard 3） |
| Keyword 限制 | ≤4，hard 5 | ≤6，hard 6 |
| Keyword 检索方式 | Dense+BM25（同其他 extra query） | BM25 only |
| SubQuery 限制 | 无 | ≤3，hard 4 |
| Hit Boost | 2命中+0.10 / 3命中+0.15 | 统一+0.002（固定值） |
| `_llm_rewrite` 返回值 | 缺失 `rewrite_queries` key | 三字段完整 |
| `_load_cache` | 有死代码（return 后不可达） | 已清理 |

### 19.2 全指标

| 指标 | 值 |
|------|----|
| 题目数 | 179 |
| MRR | **0.5172** |
| Recall@5 | **72.1%** |
| Recall@10 | **87.2%** |
| Top-1 命中率 | 34.6% |
| 平均命中 chunk | 1.1 / 1.6 |
| Recall@10=0 | **23/179** |
| Section MRR | 0.5172 |
| Section R@10 | 87.2% |
| OOD 召回率 | 11/20 (55.0%) |
| 耗时 | 3710s (62 min) |

### 19.3 按 Capability（Chunk 级）

| Capability | n | MRR | R@5 | R@10 | Top1 |
|------------|:--:|------|------|------|------|
| exact_retrieval | 35 | 0.738 | 88.6% | **97.1%** | 60.0% |
| context_expansion | 25 | 0.567 | 88.0% | 96.0% | 32.0% |
| table_retrieval | 20 | 0.422 | 75.0% | 95.0% | 15.0% |
| numeric_retrieval | 20 | 0.708 | 90.0% | 90.0% | 55.0% |
| cross_section | 25 | 0.385 | 52.0% | 80.0% | 28.0% |
| cross_document | 30 | 0.376 | 60.0% | 80.0% | 20.0% |
| query_rewrite | 24 | 0.377 | 50.0% | **70.8%** | 25.0% |

### 19.4 按 Difficulty

| Difficulty | n | MRR | R@5 | R@10 |
|------------|:--:|------|------|------|
| easy | 131 | 0.5788 | 79.4% | 90.1% |
| medium | 37 | 0.3644 | 56.8% | 81.1% |
| hard | 11 | 0.2979 | 36.4% | 72.7% |

### 19.5 Rewrite 统计

| 池 | 检索方式 | 平均 | 最大 | LLM 覆盖率 |
|----|----------|:--:|:--:|:--:|
| Rewrite Query | Dense+BM25 | 0.4 | 1 | 87/199 (43.7%) |
| Sub Query | Dense+BM25 | 0.5 | 4 | 36/199 (18.1%) |
| Keyword | BM25 only | 4.3 | 8 (截断到6) | 188/199 (94.5%) |
| **Dense+BM25 总量** | | **~1.9** | | |
| **BM25-only 总量** | | **~4.3** | | |

> LLM keywords 有 74/199 (37%) 超过 prompt 约定 ≤4，以 cross_document 和多因子复杂题为主。代码 hard cap=6 兜底截断。

### 19.6 OOD 检测

| 指标 | 值 |
|------|:--:|
| 检出率 | 11/20 (55.0%) |
| Judge 分层 | direct=7, llm=13 |
| 漏判 | 9 (7 direct, 2 llm) |

**漏判原因分析：**
- direct 误过 (7/9)：top1_sim ≥0.70，但实际无法回答（如 OOD 问题与库内文档标题相似度高）
- llm 误判 (2/9)：LLM 认为参考资料能回答但实际上不完整或跨文档矛盾

### 19.7 诊断分桶（23 道零召回）

| 环节 | 题数 | 占比 | 说明 |
|------|:--:|:--:|------|
| B-检索层-低余弦断连 | 1 | 4.3% | 口语→术语语义鸿沟 |
| B-检索层-高余弦未召回 | 1 | 4.3% | cos=0.650 双通道均未命中 |
| **C-RRF融合-单通道强但被稀释** | **6** | **26.1%** | Dense/BM25 一通道 top10 另一通道噪声稀释 |
| C-RRF融合-排名偏低 | 4 | 17.4% | Gold 在 RRF pool 内但 10-30 名 |
| C-RRF融合-排名靠后 | 6 | 26.1% | Gold 在 RRF pool 外 (>30) |
| D-CE精排-严重搅黄 | 1 | 4.3% | RRF=2 → CE=28 |
| D-CE精排-轻微搅黄 | 1 | 4.3% | RRF=8 → CE=13 |
| E-改写层-命中但未合并 | 2 | 8.7% | 改写命中 gold 但 merge 未选入 |
| E-改写层-改写未命中 | 1 | 4.3% | 5 个改写均未命中 gold |

**环节汇总：**

| 环节 | 题数 | 占比 | 建议 |
|------|:--:|:--:|------|
| C-RRF融合层 | 16 | 69.6% | 提高 Dense 权重、增大 RRF pool |
| E-改写层 | 3 | 13.0% | 放宽 gate 阈值、改写质量优化 |
| B-检索层 | 2 | 8.7% | 口语→术语映射、chunk 切分优化 |
| D-CE精排层 | 2 | 8.7% | 增加 alpha、扩大 CE 候选池 |

### 19.8 vs v1.18（#11）对比

| 指标 | #11 Evidence Merge | #12 三池分离 | 变化 |
|------|:--:|:--:|:--:|
| MRR | 0.5469 | 0.5172 | **-0.0297** |
| R@5 | 74.3% | 72.1% | -2.2pp |
| R@10 | 86.6% | 87.2% | **+0.6pp** |
| Top-1 | 39.7% | 34.6% | -5.1pp |
| R@10=0 | 24 | 23 | **-1** |
| OOD | 55.0% | 55.0% | 持平 |

**分析：**
- MRR/Top-1 下降主因：Hit Boost 从 0.10/0.15→0.002，多查询交叉验证信号消失，gold chunk 不再因多路命中获得排名加成
- R@10 微涨：三池分离减少了 keyword 的 Dense 噪声，BM25-only 的 keyword 更聚焦
- R@10=0 减 1 题：keyword BM25-only 对个别题有帮助
- OOD 持平（55%）：阈值问题未解决，与 rewrite 变更无关

### 19.9 代码改动

| 文件 | 修改 |
|------|------|
| `query_rewriter.py` | Prompt 新增 `rewrite_queries` 字段（15 个 few-shot 全部更新） |
| `query_rewriter.py` | `expand_query()` 三池分离：Keyword(BM25-only) + RewriteQuery + SubQuery |
| `query_rewriter.py` | Keyword hard cap 5→6，SubQuery hard 4，Rewrite hard 3 |
| `query_rewriter.py` | 新增 `get_keywords()` 函数暴露 kw_pool |
| `query_rewriter.py` | 修复 `_llm_rewrite()` 4处缺失 `rewrite_queries` 的 bug |
| `query_rewriter.py` | 清理 `_load_cache()` 死代码 |
| `hybrid_search.py` | Hit Boost 从 0.10/0.15 改为 0.002 |
| `hybrid_search.py` | `search_multi_query()` 新增 `keyword_queries` 参数 |
| `hybrid_search.py` | Keyword 查询只走 BM25（dense_k=0），不走 Dense |
| `eval_v2_full.py` | 新增 `keyword_map`，`run_one()` 和 OOD 评测传 `keyword_queries` |
| `CHUNK_PIPELINE.md` | 更新为三池分离架构、Hit Boost=0.002 |

### 19.10 后续建议

1. **Hit Boost 恢复交叉验证信号**：当前 0.002 对排名几乎无影响，考虑改为 `0.002 × N`（线性加权）或恢复较小的固定值如 0.02/0.03
2. **RRF 权重调优**：69.6% 零召回仍是单通道稀释，Dense 权重 0.7→0.85 或动态权重
3. **OOD direct 阈值**：7/9 漏判因 similarity≥0.70 直接放行，考虑提高到 0.78 或引入 LLM 二次确认
4. **Rewrite Query 利用率**：LLM 仅 43.7% 生成 rewrite_queries，且最多 1 个（prompt 允许 2），考虑强化 prompt 引导
5. **keyword 超限控制**：74/199 超 prompt ≤4 约定，本次已放宽到 ≤6 并 hard cap=6，后续观察 LM 行为

---

## 二十一、CK A/B 评测（2026-08-13）

> 评测脚本 `eval_v2_ck_ab.py` → `eval_ck_ab_results.json`，全量 180 题（golden_set_v2 in-domain）
>
> BASE = expand_query 关 CK（use_ck=off）；CK = expand_query 开 CK（use_ck=on，默认），CK 匹配文本经 Dense Top-3 精炼为 Knowledge Context 注入 Rewrite Prompt。检索管线（Dense/BM25/RRF/CE）零改动，两者差异全部来自改写质量变化。

### 21.1 整体指标（n=180）

| 指标 | BASE | CK | Δ |
|------|------|-----|---|
| MRR | 0.5490 | 0.5481 | -0.0009 |
| R@5 | 73.3% | 72.8% | -0.6pp |
| R@10 | 86.7% | 85.6% | -1.1pp |
| Top-1 命中率 | 40.6% | 40.6% | 持平 |
| Rewrite 命中率 | 87.8% | 87.8% | 持平 |
| R@10=0 | 24 | 26 | **+2** |

### 21.2 阶段召回（原始 query，无改写）

| 通道 | Recall |
|------|--------|
| BM25 | 74.4% |
| Dense | 81.7% |
| RRF 融合 | 79.4% |

### 21.3 CK 诊断指标

| 指标 | 值 | 说明 |
|------|-----|------|
| ck_hit_rate | 100% | 每题均匹配到 CK（含 fallback） |
| ck_gold_top1_rate | 46.7% | CK Top-1 即 gold chunk 的题占比 |
| ck_rw_gold_cosine_avg | 0.7629 | 改写后 query 与 gold chunk 的平均余弦 |
| Rescue Rate | 0/24 | BASE 失败的 24 题，CK 无一救回 |

### 21.4 失败题

BASE R@10=0 的 24 题全部仍失败（CK 另新增 2 题）：

`Q_C08, Q_C25, Q_D04, Q_D10, Q_D20, Q_D26, Q_D29, Q_L02, Q_L07, Q_L08, Q_L11, Q_N07, Q_N11, Q_N20, Q_S03, Q_S05, Q_S07, Q_S14, Q_S23, Q_SR03, Q_SR04, Q_SR06, Q_SR08, Q_SR11`

### 21.5 结论

- CK 注入改写环节后 MRR/R@5/R@10 全面**略降**（-0.1% ~ -1.1pp），Top-1 与 Rewrite 命中率持平，R@10=0 反增 2 题，Rescue Rate 0/24 —— **CK-guided Query Understanding 在当前版本无正向收益**
- ck_gold_top1_rate 仅 46.7%：过半题的 CK Top-1 不是 gold chunk，Knowledge Context 的引导方向本身有偏差；cosine 平均 0.7629 说明改写与 gold 相关度处于中等水平，未能突破口语↔技术语言的语义鸿沟（如 Q_SR03「低温累积效应」 vs chunk「5-9月月平均温度之和的距平」）
- 决定：CK 机制保留 use_ck 开关（默认开、A/B 可关），不作为当前主攻方向；失败题根因仍集中在 embedding 语义鸿沟与长尾定义类文本召回
  > ⚠ 2026-08-24 Oracle Rank 实验（第二十二节）修订了本条判断：语义鸿沟是窄尾（1 题 >100），端到端失败大头在管线（RRF 稀释+截断），详见 22.6
  > ⚠ 2026-08-25 口径说明：CK 已从改写层（query_rewriter.py）移除——REWRITE_PROMPT 的 Knowledge Context 段、expand_query 的 use_ck/knowledge_context 参数、ck_matcher 调用全部删除，改写缓存键统一为纯 query（旧 `|base` 缓存自动复用）。此后所有改写/检索/召回路径均为 BASE 口径。CK 数据（chunk_knowledge.json、data/ck_matcher/、ck_matcher.py 等）保留未删，本评测脚本 eval_v2_ck_ab.py 归档为历史记录

---

## 二十二、Oracle Rank 可分性实验（2026-08-24）

> 脚本 `eval_oracle_rank.py` → `data/eval_oracle_rank_results.json`（47s 可重跑，不改任何线上代码）
>
> 目的：把"语义鸿沟"从猜测变成可量化问题——直接测试 BGE 表示空间能否把 Query 和 Gold Chunk 排在一起
>
> 方法：180 in-domain 原始 query × 791 chunk（生产索引 vectordb/agri_zoning，与 chunks_split.json 同源）全量余弦。逐题计算 oracle rank（gold 在全量余弦排序下的排名）/ percentile / margin（同 section 兄弟 chunk + 全局最强非 gold），并与线上 Dense top-30 / BM25 top-20 / RRF 排名对照；另用 rewrite_cache.json 计算改写 query 的 oracle rank

### 22.1 结果验证

| 验证项 | 结果 |
|--------|------|
| float64 重算 oracle rank vs 保存结果 | 0/180 不一致 |
| Dense/BM25/RRF @10 命中率 vs CK A/B stage_recall | 0.817 / 0.744 / 0.794 **完全一致** |
| gold 与生产索引匹配 | 180/180 全部在索引内（0 缺失） |
| 矩阵 NaN/inf | 0（运行期 matmul 警告为 MPS BLAS 噪声，双精度复核无影响） |

### 22.2 Oracle Rank 分布（180 题，N=791）

| 指标 | 值 |
|------|-----|
| median / mean / P75 / P90 / max | **2.0** / 8.1 / 6.2 / 22.4 / 106 |
| rank ≤10 | 147 (81.7%) |
| rank 11-30 | 19 (10.6%) |
| rank 31-50 | 9 |
| rank 51-100 | 4 |
| rank 101-300 | 1（Q_D29=106） |
| rank >300 | **0** |
| gold cosine median / mean / min / max | 0.7714 / 0.7554 / 0.4944 / 0.9205 |

**表示空间整体健康：92.2% 的题 gold 在 top-30，不存在两极分化（easy 1-20 / hard 200-700）情景。**

### 22.3 Margin 分布

| Margin | n | median | mean | <0.02 | <0.05 |
|--------|-----|--------|------|-------|-------|
| 同 section 兄弟 chunk | 25 | +0.0100 | 0.0180 | 13 | 18 |
| 全局最强非 gold | 180 | **-0.0060** | -0.0061 | 114 | 151 |

> 全局 margin 中位数为负的解读：oracle rank 中位数=2，rank-1 常是同 section 的准重复 chunk（切分重叠产物），并非"错误答案"。同 section 兄弟 margin 仅 +0.01 佐证：模型连同一 section 内真正相关内容都难以拉开——但这对检索无害（兄弟 chunk 多为可接受答案）。

### 22.4 still-fail 24 题归因

| 分组 | 题数 | 明细 |
|------|:--:|------|
| oracle>100（表示不可分） | 1 | Q_D29=106（跨两文档比较题） |
| oracle 31-100（中间带） | 8 | Q_S03=98, Q_SR11=79, Q_D10=73, Q_S05=61, Q_L02=48, Q_S14=43, Q_SR03=39, Q_D04=38 |
| **oracle≤30 但端到端 R@10 失败（管线问题）** | **15** | 表示空间可分，gold 在前 30 仍丢 |

15 题管线失败中 RRF 系统性劣于 Dense 的案例（BM25 噪声稀释实锤）：

| 题 | Dense rank | RRF rank |
|----|-----------|----------|
| Q_N11 | 15 | 27 |
| Q_S23 | 17 | 30 |
| Q_L07 | 22 | 40 |
| Q_L11 | 28 | 40 |
| Q_D20 | 28 | 33 |
| Q_SR08 | 27 | 28 |

### 22.5 改写救回（关键发现）

- 改写 oracle 优于原始 31/59，劣于 9/59
- **5 题改写把表示空间救回（raw oracle>30 → rw oracle≤30）**：

| 题 | raw ork | rw ork | 改写文本 |
|----|--------|--------|----------|
| Q_SR03 | 39 | 4 | 低温冷害对大豆产量的影响机制是什么？ |
| Q_D04 | 38 | 7 | 内蒙古大豆区划指标体系构建方法 |
| Q_D10 | 73 | 9 | 全国大豆品质区划的方法论 |
| Q_D12 | 32 | 5 | 黑龙江农业气候资源普查热量资源分析框架 |
| Q_SR01 | 32 | 12 | 大豆气候区划中高温热害的评估指标和方法是什么？ |

> 语义鸿沟是**语域翻译问题**（口语 query ↔ 技术文本），不是 embedding 模型能力问题：同一模型下改写 query 即可跨域（Q_SR03 从"分不开"39 到"完全可分"4）。

### 22.6 结论

1. **不训 Retriever Adapter**：92.2% 的题表示空间已可分，训 adapter 的风险是破坏存量；窄尾（1 题 >100 + 8 题中间带）的正确修法是改写，已证明 5 题可救回
2. **当前最大瓶颈在管线**：15/24 still-fail 题 gold 表示空间在前 30 但端到端失败——RRF 排名系统性劣于 Dense（BM25 噪声稀释）+ top-10 截断（d=15~28 的题被截断挤出）
3. **下一步优先级**：提高 Dense 在 RRF 的权重 / 修 top-10 截断 / 扩大 CE 候选池 > 改写质量优化 > （最低）Adapter
4. **局限**：改写分析仅覆盖 59 题（缓存命中）；同 section 兄弟 margin 仅 25 题有样本
5. **修订 21.5 结论**：CK A/B 时期"失败根因集中在 embedding 语义鸿沟"的判断被本实验证伪——语义鸿沟是窄尾，端到端失败大头在管线

---

## 二十三、RRF 损失诊断 P0（2026-08-24）

> 脚本 `eval_rrf_loss.py` → `data/rrf_loss_report.json`（复用第二十二节 Oracle Rank 逐题 dense/bm25/rrf 排名，仅补采 BM25 top-30 一列，9s 完成，不改任何线上代码）
>
> 目的：证明多少题是 Dense 已找到、但 RRF 把 gold 推出 top10
>
> 口径：180 in-domain 题，原始 query 无改写；Dense top-30 / BM25 top-20（诊断另采 top-30）/ RRF 融合池

### 23.1 三分类统计

| 类别 | 定义 | 题数 | 占比 |
|------|------|:--:|:--:|
| A | Dense Top10 命中，RRF Top10 不命中 | 6 | 3.3% |
| B | Dense Top30 命中（11-30），RRF Top10 不命中 | 18 | 10.0% |
| C | Dense Top30 未命中 | 14 | 7.8% |
| **A+B** | **RRF 损失** | **24** | **13.3%** |
| — | Dense 已解决（dense≤30 且 rrf≤10） | 142 | 78.9% |
| — | RRF 增益（BM25 救回：dense>30 但 rrf≤10） | 1 | 0.6% |

### 23.2 A 类明细（6 题）

| 题 | dense | bm25 | rrf | 备注 |
|----|:--:|:--:|:--:|------|
| Q_C08 | 7 | 10 | 11 | |
| Q_L10 | 10 | **1** | 11 | BM25 第 1 名仍被推出 top10 |
| Q_T09 | 8 | — | 12 | |
| Q_N20 | 10 | — | 12 | |
| Q_L09 | 7 | 6 | 12 | |
| Q_SR06 | 10 | 19 | 13 | |

> 机制：RRF single-channel boost 只除单通道权重（score = Σ w_channel/(60+rank_channel)），**任何双通道命中的 chunk 分数天然高于单通道 gold**——这就是稀释的数学机制。Q_L10（BM25 rank 1）也推不进 top10 即为实证。

### 23.3 B 类明细（18 题）

| 题 | dense | rrf | 题 | dense | rrf |
|----|:--:|:--:|----|:--:|:--:|
| Q_T24 | 11 | 11 | Q_S26 | 21 | 25 |
| Q_T29 | 14 | 12 | Q_N11 | 15 | 27 |
| Q_SR05 | 16 | 13 | Q_SR08 | 27 | 28 |
| Q_C25 | 13 | 14 | Q_SR07 | 26 | 29 |
| Q_S07 | 15 | 15 | Q_S23 | 17 | 30 |
| Q_D11 | 12 | 15 | Q_L12 | 18 | 32 |
| Q_D19 | 12 | 16 | Q_C26 | 21 | 32 |
| Q_D05 | 12 | 17 | Q_D20 | 28 | 33 |
| | | | Q_L07 | 22 | 40 |
| | | | Q_L11 | 28 | 40 |

> B 类 gold 本就在 11-30 名——问题不是表示，而是 RRF 排名/权重与 top-10 截断。

### 23.4 C 类明细（14 题）

| 题 | oracle rank | bm25 | rrf | 备注 |
|----|:--:|:--:|:--:|------|
| Q_E24 | 32 | 1 | 1 | BM25 直接救回（RRF 增益唯一题） |
| Q_D12 | 32 | 13 | 22 | |
| Q_SR01 | 32 | — | — | 改写可救（rw ork=12） |
| Q_SR02 | 33 | — | — | |
| Q_D04 | 38 | — | — | 改写可救（rw ork=7） |
| Q_SR03 | 39 | — | — | 改写可救（rw ork=4） |
| Q_S14 | 43 | — | — | |
| Q_T28 | 43 | — | — | |
| Q_L02 | 48 | — | — | |
| Q_S05 | 61 | 15 | 29 | |
| Q_D10 | 73 | — | — | 改写可救（rw ork=9） |
| Q_SR11 | 79 | — | — | |
| Q_S03 | 98 | — | — | |
| Q_D29 | 106 | — | — | 唯一表示不可分 |

> C 类中 oracle rank≤50 的 9 题只是 Dense top-30 截断问题（扩到 top-50 即进池），其中 5 题改写可救（见 22.5）；真正表示空间不可分的仅 Q_D29 一题（oracle=106）。

### 23.5 判定

1. **A+B=13.3% 不算很高**（vs Dense 已解决 78.9%），但 **RRF 损失 24 题 > Dense 本身无法解决 14 题**——检索层内部 RRF 是第一瓶颈。与 19.7 旧诊断（零召回 69.6% 在融合层）方向一致，本次为全量 180 题口径的干净证据
2. **B 类 18 题是主修对象**：gold 本在 Dense 11-30 名——修法是提高 Dense 权重（0.7→0.85）或 Dense top-k 保序，不是修表示。⚠ 修订（2026-08-24，见 24.4）：全局提高 Dense 权重经模拟验证**无净收益**（w_dense=0.85/0.95 时 rrf@10 78.9%，低于 0.7 的 79.4%）——B 类正解是 23.5-3 的 boost 归一化机制修复，而非调权重
3. **A 类 6 题是纯稀释**（single-channel boost 数学机制所致），修法是改 boost 归一化（如取 max 通道分或按通道数归一）
4. **RRF 增益仅 1 题**（Q_E24，BM25 rank 1 救回）——BM25 通道在原始 query 上的增量贡献极小，验证 0.7/0.3 权重下 BM25 是弱辅助的定位
5. 口径提醒：本诊断是原始 query 无改写的检索层；端到端还有改写层（可救 C 类 5 题，见 22.5）与 CE 层（可修 rrf 排名 11-15 的题）

---

## 二十四、RRF 权重分层扫描（2026-08-24）

> 脚本 `eval_rrf_weight_scan.py` → `data/rrf_weight_scan_report.json`（7s，不改任何线上代码）
>
> 口径：180 in-domain 原始 query；候选池 = 生产配置（Dense top-30 + BM25 top-20，RRF_K=60）；采集一次候选池后按 `_rrf_retrieve` 融合语义（含 single-channel boost）解析式重算 0.1~0.95 权重网格下 rrf@10
>
> 目的：回答"0.7/0.3 是否适合所有 Query、是否需要动态权重"

### 24.1 全局权重曲面：平台，0.7/0.3 仍在最优区

| w_dense | 0.95 | 0.90 | 0.80 | **0.70** | 0.60 | 0.50 | 0.40 | 0.30 | 0.20 | 0.10 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| rrf@10 | 78.9% | 78.9% | 78.9% | **79.4%** | 79.4% | 80.0% | 79.4% | 77.8% | 77.8% | 78.9% |

> 0.4~0.8 区间 rrf@10 只差 1.1pp，全局曲面是平台；0.7/0.3（79.4%）处于平台内。与 9.4（2026-07-10，chunks.json 口径）的全量扫描结论互相印证：**0.7/0.3 仍是全局最优区**。

### 24.2 0.7/0.3 下按 Capability 分布

| capability | n | d@10 | d@30 | b@10 | b@30 | rrf@10 | A | B | C |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|
| exact_retrieval | 35 | 97.1% | 97.1% | 94.3% | 100.0% | **100.0%** | 0 | 0 | 1 |
| numeric_retrieval | 20 | 95.0% | 100.0% | 90.0% | 90.0% | 90.0% | 1 | 1 | 0 |
| context_expansion | 26 | 92.3% | 100.0% | 92.3% | 100.0% | 88.5% | 1 | 2 | 0 |
| table_retrieval | 20 | 85.0% | 95.0% | 80.0% | 85.0% | 80.0% | 1 | 2 | 1 |
| cross_section | 25 | 76.0% | 88.0% | 48.0% | 80.0% | 76.0% | 0 | 3 | 3 |
| cross_document | 30 | 70.0% | 86.7% | 66.7% | 86.7% | 73.3% | 0 | 4 | 4 |
| **query_rewrite** | 24 | **54.2%** | 79.2% | 45.8% | 66.7% | **41.7%** | **3** | **6** | 5 |

> 分布极不均匀：query_rewrite 一类独吞 9/24（37.5%）的 RRF 损失（A+B），且 d@10 仅 54.2%；其余 6 类 d@10 均 ≥70%，损失分散。cross_section 的 b@10 仅 48.0%——BM25 对跨节语义匹配天然弱。
>
> BM25 独占命中（b@10=1 且 d@10=0）共 7 题：Q_C26、Q_D16、Q_D20、Q_E24、Q_SR05、Q_SR07、Q_T29；RRF 只转化了 1 题（Q_E24，BM25 rank 1），其余 6 题在 0.7/0.3 下仍丢。

### 24.3 分类型权重扫描（* = 该类最优）

| capability | 0.95 | 0.90 | 0.80 | 0.70 | 0.60 | 0.50 | 0.40 | 0.30 | 0.20 | 0.10 | 最优 | n | 最优-0.7 |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| context_expansion | 88.5% | 88.5% | 92.3%* | 88.5% | 88.5% | 88.5% | 88.5% | 88.5% | 88.5% | 88.5% | 0.80 | 26 | +1 题 |
| cross_document | 73.3%* | 73.3% | 73.3% | 73.3% | 70.0% | 66.7% | 66.7% | 66.7% | 66.7% | 70.0% | 0.95 | 30 | +0 题 |
| cross_section | 76.0%* | 76.0% | 76.0% | 76.0% | 76.0% | 76.0% | 76.0% | 68.0% | 68.0% | 68.0% | 0.95 | 25 | +0 题 |
| exact_retrieval | 100.0%* | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 97.1% | 97.1% | 97.1% | 0.95 | 35 | +0 题 |
| numeric_retrieval | 90.0%* | 90.0% | 90.0% | 90.0% | 90.0% | 90.0% | 90.0% | 90.0% | 90.0% | 90.0% | 0.95 | 20 | +0 题 |
| query_rewrite | 41.7% | 41.7% | 37.5% | 41.7% | 45.8% | 50.0% | 45.8% | 45.8% | 50.0% | 54.2%* | 0.10 | 24 | +3 题 |
| table_retrieval | 75.0% | 75.0% | 75.0% | 80.0% | 80.0% | 85.0%* | 85.0% | 85.0% | 80.0% | 80.0% | 0.50 | 20 | +1 题 |
| **TOTAL** | 78.9% | 78.9% | 78.9% | 79.4% | 79.4% | 80.0% | 79.4% | 77.8% | 77.8% | 78.9% | 0.50 | 180 | +1 题 |

> 唯一强敏感类型是 query_rewrite：w_dense 0.7→0.1 时 rrf@10 41.7%→54.2%（+3 题），方向与 24.2 中该类 BM25 高排名命中（b=1/2）一致；其余 6 类对权重不敏感（最优与 0.7 差 0~1 题）。
>
> 若给每类都配"完美权重"，全量 in-sample 上界 = +5 题（79.4%→82.2%）。注意这是同一批 180 题上选优的乐观上界，出样本必然缩水。

### 24.4 判定：不调动态权重，维持 0.7/0.3

1. **收益上限太小**：全局权重在 0.3~0.95 全区间只造成 ≤1.1pp 波动（平台）；按类完美权重 in-sample 上界仅 +2.8pp
2. **capability 是黄金集标签，线上 query 没有此字段**——"按类型权重"须先建 query 分类器，用 180 题训练 7 分类器必然过拟合，系统复杂度远超收益
3. **query_rewrite 的缺口在检索信号层**（d@10 54.2%），正解是改写增强（现行方向），不是融合权重——与 22.6（不训 Adapter）同向
4. **生产管线 RRF 后还有 CE 精排**（alpha=0.2），会进一步抹平融合权重差异——本扫描为 pre-CE 口径，端到端权重敏感性只会更低
5. 由此**修订 23.5-2**：全局提高 Dense 权重无净收益，B 类正解是 23.5-3 的 single-channel boost 归一化机制修复（静态、全局、无过拟合风险）
6. 若将来仍要做 query 级自适应：起点是廉价运行时特征（BM25 top 分强度、query 长度）的两档切换 + 留出集验证，不是 7 类分类器
