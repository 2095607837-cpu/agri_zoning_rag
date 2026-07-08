# Golden Set V2 评测集分析报告

生成时间：2026-07-03 | 文件：`data/golden_set_v2.json` | 200 题 | Valid JSON

---

## 变更日志

| 日期 | 变更 |
|------|------|
| 2026-07-02 | 初始版本，200 题全部生成完毕 |
| 2026-07-03 | Table_retrieval 纯度修复：12 道指向 `_s` 的题删除，新增 12 道纯 `_t` 题（含跨 section/跨 doc 补齐）；删除 2 道真重复；generator_eval 补齐 |

---

## 一、总体概览

| 指标 | 数值 |
|------|------|
| 题目总数 | **200** |
| 知识库 section 总数 | 516 |
| 知识库 chunk 总数 | 516 |
| Gold section 引用数 | 152（29.5%）|
| 覆盖文档数 | 24 / 41（59%）|
| 必填字段完整性 | 100%（无缺失）|
| Chunk ID 有效性 | 100%（0 个无效引用）|
| ID 唯一性 | 200/200 |
| Table_retrieval `_t` chunk 纯度 | 20/20（100%）|

---

## 二、Capability 分布

| Capability | 实际 | 目标 | 说明 |
|------------|------|------|------|
| exact_retrieval | 35 | 35 | 单 section 精确定位，定义/事实/方法 ✓ |
| context_expansion | 25 | 25 | 相邻 section 上下文扩展 ✓ |
| cross_section | 25 | 25 | 同文档跨 section 综合 ✓ |
| cross_document | 30 | 30 | 跨文档对比/关联 ✓ |
| table_retrieval | 20 | 20 | 独立表格（`_t` chunk）数据提取 ✓ |
| numeric_retrieval | 20 | 20 | 数值/阈值提取 ✓ |
| query_rewrite | 25 | 25 | 口语→术语 或 同义词映射 ✓ |
| ood_detection | 20 | 20 | 拒答/冲突检测 ✓ |
| **合计** | **200** | **200** | |

---

## 三、Difficulty 分布

| Difficulty | 数量 | 占比 |
|------------|------|------|
| easy | 152 | 76% |
| medium | 37 | 18% |
| hard | 11 | 6% |

### 按 Capability 细分

| Capability | easy | medium | hard |
|------------|------|--------|------|
| exact_retrieval | 35 | 0 | 0 |
| context_expansion | 24 | 1 | 0 |
| cross_section | 21 | 4 | 0 |
| cross_document | 0 | 19 | 11 |
| table_retrieval | 15 | 5 | 0 |
| numeric_retrieval | 20 | 0 | 0 |
| query_rewrite | 17 | 8 | 0 |
| ood_detection | 20 | 0 | 0 |

---

## 四、Query Type 分布

| Query Type | 数量 | 占比 |
|------------|------|------|
| fact | 61 | 30% |
| comparison | 46 | 23% |
| method | 32 | 16% |
| table_lookup | 20 | 10% |
| definition | 17 | 8% |
| threshold | 12 | 6% |
| summary | 5 | 2% |
| workflow | 3 | 2% |
| calculation | 2 | 1% |
| process | 2 | 1% |

---

## 五、Features 分布

| Feature | 数量 | 说明 |
|---------|------|------|
| numeric | 78 | |
| multi_hop | 30 | |
| table | 29 | |
| rewrite | 25 | |
| （无 features） | 77 | |

---

## 六、Table Retrieval 专项

- 题目数：**20**，全部指向 `_t` chunk（100% 纯度）
- 覆盖文档：**12** 个

| 文档 | 题数 |
|------|------|
| 辽宁省生态气象和卫星遥感中心-算法说明文档 | 3 |
| 黑龙江省大豆冷害气候风险区划技术规范（初稿） | 2 |
| 黑龙江省大豆种植气候区划技术规范(初稿）1120 | 2 |
| 黑龙江省大豆产量气候区划技术规范(初稿) | 2 |
| 黑龙江省大豆渍涝气候风险区划技术规范（初稿） | 2 |
| 2025河南小麦品质_病害区划算法 | 2 |
| 新疆冬小麦气候区划报告 | 2 |
| 黑龙江省大豆病虫害（食心虫）气候风险区划技术规范(初稿)  | 1 |
| 气科院-农业气候区划指标算法说明文档-王旗 | 1 |
| 黑龙江省大豆品质气候区划技术规范(初稿) | 1 |
| 黑龙江省大豆霜冻气候风险区划技术规范（初稿） | 1 |
| 农业气候区划指标算法说明文档（新疆农业气象台-算法说明文档-郑新倩） | 1 |

---

## 七、OOD / Query Rewrite 子类型

### OOD Detection（20 题）

| 子类型 | 数量 | 设计意图 |
|--------|------|------|
| outside_kb | 8 | 知识库完全不相关内容 |
| unanswerable | 6 | 知识库有相关内容但无法具体回答 |
| conflict | 6 | 知识库内不同文档给出矛盾信息 |

### Query Rewrite（25 题）

| 子类型 | 数量 | 示例 |
|--------|------|------|
| semantic | 13 | 近义概念映射（如"高温伤害"→热害指数） |
| lexical | 12 | 口语化表达→专业术语（如"光照好不好"→日照百分率） |

---

## 八、文档覆盖度

### 引用频次 Top 15

| 文档 | 引用 |
|------|------|
| D_P_R_150000_001-内蒙古区划报告 | 32 |
| D_P_R_410000_001-农业气候区划报告-河南省 | 29 |
| D_P_R_610000_001-陕西苹果气候区划报告 | 22 |
| 黑龙江农业气候资源普查技术规范2025 | 18 |
| 新疆冬小麦气候区划报告 | 15 |
| 黑龙江省大豆冷害气候风险区划技术规范（初稿） | 15 |
| 辽宁省生态气象和卫星遥感中心-算法说明文档 | 10 |
| 气科院-农业气候区划指标算法说明文档-王旗 | 8 |
| D_P_R_230000_001-农业气候区划报告 | 8 |
| 黑龙江省大豆渍涝气候风险区划技术规范（初稿） | 7 |
| D_P_R_360000_001-江西柑橘气候区划报告 | 6 |
| 黑龙江省大豆霜冻气候风险区划技术规范（初稿） | 6 |
| 黑龙江省大豆种植气候区划技术规范(初稿）1120 | 6 |
| 黑龙江农业生产普查技术规范2025 | 4 |
| 黑龙江省大豆病虫害（食心虫）气候风险区划技术规范(初稿)  | 4 |

### 文档类型分布

| 类型 | 文档数 | 说明 |
|------|--------|------|
| 省级区划报告 | 7 | 内蒙古、河南、陕西、新疆(×2)、黑龙江、江西 |
| 灾种风险区划规范 | 6 | 冷害、霜冻、干旱、渍涝、食心虫、产量 |
| 普查技术规范 | 3 | 黑龙江气候、黑龙江生产、新疆气候 |
| 算法说明文档 | 4 | 气科院、陕西、新疆、辽宁 |
| 种植/品质区划规范 | 2 | 黑龙江种植、黑龙江品质 |
| 普查清单/统计 | 2 | 新疆清单、河南/内蒙古统计 |
| 附件 | 2 | 河南省病害区划、干旱晚霜冻连阴雨区划 |

---

## 九、字段完整性

| 字段 | 非空率 |
|------|--------|
| gold_chunks | 180/200（90%，OOD 20题为设计空值）|
| must_include | 200/200（100%）|
| must_not_include | 200/200（100%）|
| expected_citation | 180/200（90%，OOD 20题为设计空值）|
| optional_retrieve | 162/200（81%）|
| negative_sections | 123/200（62%）|

---

## 十、Answer 质量

| 指标 | 数值 |
|------|------|
| 平均长度（in-domain） | 98 字 |
| 最短 | 6 字 |
| 最长 | 303 字 |

### 按 Capability 平均长度

| Capability | 平均长度 | 特点 |
|------------|------|------|
| exact_retrieval | 80 字 | |
| context_expansion | 94 字 | |
| cross_section | 145 字 | |
| cross_document | 161 字 | |
| table_retrieval | 91 字 | |
| numeric_retrieval | 14 字 | |
| query_rewrite | 79 字 | |
| ood_detection | 63 字 | 拒答类，需说明为什么不回答 |

---

## 十一、Section 复用分析

- 仅被 1 题引用：75 个 section
- 被 2+ 题引用：77 个 section
- 最多引用：8 题（同一 section 多角度出题）

### Top 5 复用 Section

| Section | 题数 | 涉及 Capability |
|---------|------|----------------|
| D_P_R_150000_001-内蒙古区划报告_s11 | 8 | cross_document, cross_section, numeric_retrieval, query_rewrite |
| D_P_R_610000_001-陕西苹果气候区划报告_s10 | 7 | cross_document, cross_section, exact_retrieval, numeric_retrieval, query_rewrite |
| D_P_R_150000_001-内蒙古区划报告_s21 | 7 | context_expansion, cross_document, cross_section, numeric_retrieval, query_rewrite |
| 新疆冬小麦气候区划报告_s9 | 5 | cross_document, cross_section, numeric_retrieval, query_rewrite |
| 新疆冬小麦气候区划报告_s2 | 4 | cross_document, exact_retrieval, query_rewrite |

---

## 十二、近重复分析

同 capability 内 gold_chunks Jaccard=100%：**2 对**

| 题目对 | Capability | 判断 |
|--------|------------|------|
| Q_N05 vs Q_N06 | numeric_retrieval | 同一 section 不同角度，retriever 相同但 generator 不同，保留 |
| Q_L01 vs Q_L11 | query_rewrite | 同一 section 不同角度，retriever 相同但 generator 不同，保留 |

均为同一 section 从不同角度出题（如最低/最高值、口语/术语改写），属于健康复用。

---

## 十三、综合评价

### 优势

1. **Table_retrieval 100% 纯 `_t`**：20 题全部指向独立表格 chunk，评测结果可独立归因
2. **Capability 分布精准**：8 类 capability 全部达到目标值，偏差为 0
3. **Section 级引用稳定**：以 section 为评测单元，不随 chunk 切分策略变化而失效
4. **Chunk ID 零错误**：全部 gold_chunks 在 chunks.json 中验证通过
5. **Generator eval 全覆盖**：must_include/must_not_include 100% 完整
6. **OOD 三维度完整**：outside_kb + unanswerable + conflict
7. **Query Rewrite 双向覆盖**：lexical（口语化）+ semantic（专业术语映射）
8. **0 真重复**：所有 Jaccard=100% 对均为同 section 不同题型，属于合理设计

### 待改进

1. **Section 覆盖率 29.5%**：152/516，364 个 section 零覆盖，新增题目可优先填充
2. **17 个文档零覆盖**：主要是附件、清单、算法统计表等低信息密度文档，部分仍有出题价值
3. **Difficulty 偏 easy（76%）**：主要由单 section 单文档规则驱动，如需更均衡可调整评分阈值