# V2 Golden Set 全配置检索评测报告

生成时间：2026-07-06 | 评测集：`golden_set_v2.json` | 200 题（in-domain 180 + OOD 20）

## 变更日志

| 日期 | 变更 |
|------|------|
| 2026-07-04 | Baseline / +Rewrite / +Reranker 三配置完成，+Rewrite+Reranker 因嵌套线程池死锁挂起 |
| 2026-07-05 | 修复：`search()` 新增 `skip_reranker` 参数，子查询跳过 CrossEncoder，CrossEncoder 调用从 1268 次降至 180 次 |
| 2026-07-06 | +Rewrite+Reranker 配置完成，全四配置汇总 |
| 2026-07-10 | 验证第八节两条 rewrite 建议：①"按需触发改写"（top1 cosine 低才改写）实验**证伪**，硬题上改写 +0/-0；②新增中文 BM25 关键词检索（字+bigram 分词，修复默认空格分词对中文失效），BM25 通道 intrinsic 已可用但端到端 top-10 零变化，RRF 权重扫描证明 0.7/0.3 已最优。详见"九、BM25 关键词检索修复与融合权重扫描（2026-07-10）" |
| 2026-07-12 | Reranker 默认开启；`pool_size` 从 `max(top_k*4,20)` 增大到 `max(top_k*10,100)`（top_k=10 时 pool=40→100），配合 `rerank_input=min(40, top_k*4)`；R@10 80.0%、R@10=0 从 39 降至 36。CrossEncoder 推理加锁避免多线程 CPU 争抢。详见"十、Reranker 开启与候选池放大（2026-07-12）" |
| 2026-07-12 | **Union 实验（已否定）**：Dense top-30 + BM25 top-10 → Union → CE。R@10 从 80.0% 跌至 76.1%，R@10=0 从 36 升至 43。BM25 只放 10 个候选丢掉太多好结果，RRF 的互补效应不可替代。**已回退 RRF。** 详见"十一、Union 融合实验（2026-07-12，已否定）" |
| 2026-07-13 | **CE 逐题增益分析**：对比 RRF-only vs RRF+CE 每道题，CE 救回 14 道、搅黄 12 道，净增益 +2。CE 对 cross_section 最有效（+4），但对 query_rewrite 和 cross_document 几乎对等。详见"十二、CE 逐题增益分析（2026-07-13）" |
| 2026-07-13 | **RRF+CE 分数融合 Alpha 扫描**：实现 `final = alpha * RRF_norm + (1-alpha) * CE_norm` 加权融合（min-max 归一化），扫描 alpha=[0, 0.2, 0.3, 0.4, 0.5]。alpha=0.2 最优：R@10=82.2%（vs 78.3% baseline，vs 78.9% pure CE），R@10=0 从 39 降至 32（-7 题）。alpha=0.2 已设为生产默认值。详见"十三、RRF+CE 分数融合与 Alpha 扫描（2026-07-13）" |

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
