# V2 Golden Set 全配置检索评测报告

生成时间：2026-07-06 | 评测集：`golden_set_v2.json` | 200 题（in-domain 180 + OOD 20）

## 变更日志

| 日期 | 变更 |
|------|------|
| 2026-07-04 | Baseline / +Rewrite / +Reranker 三配置完成，+Rewrite+Reranker 因嵌套线程池死锁挂起 |
| 2026-07-05 | 修复：`search()` 新增 `skip_reranker` 参数，子查询跳过 CrossEncoder，CrossEncoder 调用从 1268 次降至 180 次 |
| 2026-07-06 | +Rewrite+Reranker 配置完成，全四配置汇总 |

---

## 一、全配置对比汇总

| Config | MRR | R@5 | R@10 | Top1 | R@10=0 | 耗时 |
|--------|------|------|------|------|--------|------|
| Baseline (no rewrite, no reranker) | 0.5692 | 70.6% | 78.3% | 47.8% | 39 | 18s |
| +Rewrite only | 0.4785 | 58.9% | 68.9% | 38.3% | 56 | 329s |
| +Reranker only | **0.6092** | **72.8%** | **80.0%** | **51.7%** | **36** | 3499s |
| +Rewrite + Reranker | 0.5938 | 70.6% | 78.9% | 50.0% | 38 | 79493s |

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
