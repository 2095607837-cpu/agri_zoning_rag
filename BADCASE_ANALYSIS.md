# Badcase 分析与优化记录

## 变更日志

| 日期 | 变更 | 影响 |
|------|------|------|
| 07-22 | 发现 step2_embed section 边界丢失问题 | Q_L09 等题诊断误报的根因 |
| 07-21 | chunks 新增 topic + entities 字段 | 为 metadata 过滤检索做准备 |
| 07-20 | Dense Protected Merge（K=5）| C-RRF融合 25→16，修复 8 题 |
| 07-20 | CE 候选池 RRF top-30 ∪ Dense top-5 | 理论保护，实测无增量 |
| 07-20 | CE 长度归一化 λ=0.1 | 抵消 CE 长文本偏好 bias |
| 07-20 | bge-small → bge-large（1024维）| cross_document 提升，单文档噪声增加 |
| 07-20 | BM25/Chroma 同源切分（chunks_split.json）| 修复 BM25 用旧 chunk 问题 |
| 07-20 | H3 子标题回退切分（>3000 字章节）| 498→753 chunks |
| 07-17 | 同义词词典集成到 query rewriter | BM25 命中率提升 |
| 07-17 | 表格线性化 | 20 题中 18 题可召回 |

---

## 一、检索管线现状

```
Query → [改写层] → [Dense Protected Merge] → [RRF+CE] → [Rewrite补充] → top-K
```

- **改写层**：LLM 改写 + 术语映射 + 同义词词典 → extra_queries
- **Dense Protected Merge**：Dense top-5 不参与 RRF，直接保序 → RRF+CE 去重追加 → Rewrite 子查询 RRF-only 去重追加
- **RRF 融合**：w_dense=0.7, w_bm25=0.3, K=60, single-channel boost 补偿
- **CE 精排**：候选池 RRF top-30 ∪ Dense top-5, 长度归一化 λ=0.1, α=0.2

---

## 二、Badcase 分桶框架

| 层 | 环节 | 典型问题 |
|----|------|---------|
| A-数据层 | 标注错误 | gold chunk 不在语料/文档不匹配 |
| B-检索层 | Embedding 语义鸿沟 | 双通道 top-100 无 gold |
| C-RRF融合 | 融合信号不足 | Dense/BM25 命中但 RRF 排名 ≥10 |
| D-CE精排 | 精排搅黄 | RRF 进候选池，CE 推出 top-10 |
| E-改写层 | 改写未触发/未命中 | gate 拦截或改写与 gold 不匹配 |
| G-已修复 | Dense Protected Merge 修复 | Dense 强但曾被 BM25 拖累 |

### 标注规则（cross_document）

- **L1**: gold chunk 所属文档与 query 指定匹配
- **L2**: gold chunk 内容能直接回答 query 核心问点（常见错误：只过 L1 不过 L2）

---

## 三、30 题分桶演进

| 类别 | 旧 | 新（Dense Protected Merge） |
|------|----|---------------------------|
| A-数据层 | 0 | 0 |
| B-检索层 | 4 | 5 |
| C-RRF融合 | 25 | 16 |
| D-CE精排 | 1 | 1 |
| E-改写层 | 0 | 0 |
| G-已修复 | — | 8 |

---

## 四、逐题分析

### 已修复 ✅

<details>
<summary>Q_D25 / Q_D29 — Gold 标注错误（修 gold）</summary>

**Q_D25**: 河南省和新疆冬小麦的生育期长度和关键发育阶段的气候条件有什么差异？
- 旧标注：河南 s4（烂场雨）+ 新疆 s9（干热风）← 气象灾害，非生育期
- 新标注：河南 s30（生长发育时期表）+ 新疆 s9（种植适宜区越冬期 120~130d）
- 影响：标注错误导致的假阴性，不是检索失败

**Q_D29**: 陕西苹果品质区划和新疆冬小麦品质区划在气候因子选择和评价方法上有什么异同？
- 旧标注：陕西 s17 ✓（品质指标）+ 新疆 s8 ✗（政策建议，非品质区划）
- 新标注：陕西 s17 + 新疆 t11（粗蛋白回归方程 R²=0.072 P=0.000）

</details>

<details>
<summary>Q_D31 / Q_E30 / Q_D30 / Q_C18 / Q_L07 / Q_D21 / Q_S08 / Q_S04 — Dense Protected Merge 修复（8 题）</summary>

保护 Dense top-5 不参与 RRF 后，以下 8 题 gold 进入 CE top-10：

| ID | capability | D→CE | cos | 原先问题 |
|----|-----------|-------|-----|---------|
| Q_D31 | cross_document | D=0→CE=0 | 0.816 | Dense 强但被 BM25(B=7)拖累 |
| Q_E30 | exact_retrieval | D=0→CE=0 | 0.727 | 改写结果挤占 Dense 排名 |
| Q_D30 | cross_document | D=0→CE=0 | 0.652 | 同上 |
| Q_C18 | context_expansion | D=0→CE=0 | 0.628 | 同上 |
| Q_L07 | query_rewrite | D=2→CE=2 | 0.667 | Dense-only，single-channel boost 拖累 |
| Q_D21 | cross_document | D=1→CE=1 | 0.565 | Dense 强但 BM25(B=12)噪声稀释 |
| Q_S08 | cross_section | D=5→CE=9 | 0.605 | Dense-only，single-channel boost 不够 |
| Q_S04 | cross_section | D=11→CE=9 | 0.762 | bge-large 后 Dense 变弱但仍被保护 |

</details>

---

### 待修复 ⚠️（B-检索层 5 题 + 重点题）

<details>
<summary>Q_E23 — 语义鸿沟：地理位置问法 ↔ 坐标数据</summary>

- **指标**: cos=0.619 D=-1 B=-1 | **chunk topic**: `地理概况` | **entities**: `[西北边陲, 欧亚大陆, 东经, 北纬]`
- **问题**: "地理位置特点、东西南北跨度" ↔ "东经96°23′～73°40′，北纬34°25′～49°10′，东西长2200km" — 无共享关键词
- **修改方向**: 检索时 topic 匹配 + entity 子串 BM25 命中

</details>

<details>
<summary>Q_E24 — 字段值失联：字段名（主栽品种） ↔ 字段值（新冬18号）</summary>

- **指标**: cos=0.590 D=-1 B=-1 | **chunk topic**: `品种资源` | **entities**: `[新冬18号, 新冬22号, 新冬53号, 新冬52号, 九圣D1508]`
- **问题**: Embedding 学的是语义相似度，不是数据库字段关系。"主栽品种"和品种名称之间的从属关系被当普通词共现
- **修改方向**: query 侧识别"主栽品种"→ topic 匹配 `品种资源` → entity BM25 命中

</details>

<details>
<summary>Q_D11 — 过长元问题 vs 事实数据</summary>

- **指标**: cos=0.604 D=-1 B=-1
- **问题**: "指标体系有哪些异同"（元问题）↔ 积温公式+区划阈值表（事实数据），跨文档语义分散

</details>

<details>
<summary>Q_L09 — 术语极性反转 + Section 边界丢失</summary>

- **指标**: cos=0.558 D=-1 B=-1 | **chunk topic**: `越冬条件` | **entities**: `[越冬期, 积雪覆盖, 积雪深度, 安全越冬, 极端最低气温]`
- **子问题 1 — 术语极性**: "冻不坏"（否定，问安全条件）→ 当前映射为"越冬冻害"（灾害概念），应为"安全越冬"
- **子问题 2 — Section 边界**: chunks.json sec_9（越冬内容 251 字符）→ step2_embed 被合并到 sec_5 chunk#2（756 字符，前半段病虫害），embedding 被污染，section_id 不匹配导致诊断永久 D=-1 B=-1

</details>

<details>
<summary>Q_L11 — 极简口语 → 指标阈值表语义鸿沟</summary>

- **指标**: cos=0.487 D=-1 B=-1 | **chunk topic**: `区划指标` | **entities**: `[稳定≥10℃活动积温, 5～9 月降水, 平均气温, 多元回归]`
- **问题**: 6 字口语 "种大豆选什么地方最好" → 改写 "大豆种植适宜性区划" cos 从 0.501→0.621(+0.12)，仍不够
- **query 侧扩展**: "选什么地方最好" → 注入 "最适宜区, 活动积温, 降水量" → BM25 命中 chunk entities

</details>

<details>
<summary>Q_L02 — 不可约：口语指标问法 ↔ 定义性文本</summary>

- **指标**: cos=0.467 D=88 | **状态**: 已知 Embedding 上限
- **问题**: 改写 "日照百分率、日照时数" 精准正确，但 gold 写的是 "实际日照时间与可能日照时间之比" — 词汇重叠为零，Embedding 无法跨过

</details>

<details>
<summary>Q_SR03 — 问机制 vs 给公式的意图错配</summary>

- **指标**: cos=0.575 D=16 RRF=27 CE=19
- **问题**: "低温累积效应如何影响产量"（因果解释）↔ "ΣTi 为 5-9 月各月月平均温度之和"（公式步骤）

</details>

<details>
<summary>Q_L01 — 数值表信号稀释：问一个数 ↔ 给一张表</summary>

- **指标**: cos=0.619 D=76
- **问题**: "种大豆需要多少积温够"（问一个阈值）↔ 3因子×4等级密集数值表（200+ 字），信号被稀释
- **修改方向**: 复合表按因子拆分为独立 chunk

</details>

<details>
<summary>Q_L08 — Dense+BM25 双强但 RRF+CE 双层损失</summary>

- **指标**: cos=0.594 D=3 B=0 RRF=10 CE=13 | **状态**: protect_k 3→5 已改待验证
- **问题**: BM25 rank 1-9 噪声在 RRF 中叠加，gold 从 Dense 第 3→RRF 第 10→CE 第 13
- K=3 时 D=3（第 4 位）刚好卡在保护窗口外

</details>

---

### 其余 C-RRF融合 / D-CE精排（汇总表）

| ID | capability | cos | D | RRF | CE | 问题 |
|----|-----------|-----|---|-----|-----|------|
| Q_C25 | context_expansion | 0.751 | 10 | 17 | 10 | Dense 在保护边界 |
| Q_S01 | cross_section | 0.670 | 12 | 16 | 14 | Dense 不够强 |
| Q_S15 | cross_section | 0.622 | 45 | 18 | 11 | CE 改善但不够 |
| Q_D20 | cross_document | 0.499 | 20 | 14 | 10 | BM25 好 Dense 弱 |
| Q_D25 | cross_document | 0.674 | 77 | 46 | -1 | Dense 视野边缘 |
| Q_D05 | cross_document | 0.669 | 79 | 74 | -1 | 同上 |
| Q_D29 | cross_document | 0.636 | 32 | 30 | -1 | 同上 |
| Q_T28 | table_retrieval | 0.594 | 27 | 38 | -1 | 同上 |
| Q_E33 | exact_retrieval | 0.593 | 89 | 35 | 13 | CE 大幅改善 |
| Q_D04 | cross_document | 0.589 | 52 | 47 | -1 | Dense 视野边缘 |
| Q_N13 | numeric_retrieval | 0.581 | 65 | 30 | -1 | 同上 |
| Q_N11 | numeric_retrieval | 0.554 | 21 | 33 | -1 | 同上 |
| Q_L02 | query_rewrite | 0.467 | 88 | 83 | -1 | 不可约（见上）|
| Q_T16 | table_retrieval | 0.333 | 4 | 8 | 11 | CE 轻微搅黄 |

---

## 五、优化策略

### 已实施 ✅

1. **Dense Protected Merge**（K=5）— 修复 8 题
2. **CE 长度归一化**（λ=0.1）
3. **CE 候选池 Dense top-5 保送**
4. **bge-large** 替换 bge-small
5. **表格线性化** — 18/20 表格题可召回
6. **同义词词典** — 30+ 组
7. **chunk topic + entities 标注**（step1b_tag_chunks.py）
8. **H3 子标题回退切分** — 498→753 chunks
9. **BM25/Chroma 同源切分** — chunks_split.json

### 待实施（按优先级）⚠️

1. **修复 section 边界丢失** — step2_embed 禁止跨 section_id 合并
2. **protect_k: 3→5 验证** — Q_L08
3. **topic/entities 检索过滤** — query 侧匹配 chunk metadata，目标 B 层 5 题
4. **query 侧隐式术语扩展** — "冻不坏"→"安全越冬"，"选什么地方"→"最适宜区+活动积温"
5. **复合表按因子拆分** — Q_L01/L11/N11/N13
6. **表格 chunk 加 caption 元描述**
7. **术语映射极性感知** — "冻不坏"→"安全越冬"（非"越冬冻害"）

---

## 六、附录

### A. 不可约 Badcase

| ID | 原因 |
|----|------|
| Q_E23 | 地理位置问法 ↔ 坐标数据，无共享关键词 |
| Q_E24 | 字段名↔字段值从属关系断裂 |
| Q_L02 | 口语指标问法 ↔ 定义性文本，词汇重叠为零 |

### B. 评测命令

```bash
python3 eval_30_quick.py

python3 -c "
import json; from collections import Counter
with open('diagnose_30_retest.json') as f: rows = json.load(f)
cats = Counter(r['category'] for r in rows)
for c, n in cats.most_common(): print(f'{c}: {n}')
"
```
