# Golden Rewrite 数据集构建规范

## 一、数据集用途

Golden Rewrite 数据集用于评估 Query Rewriter（查询改写器）在农业气候区划 RAG 场景下的改写质量。数据集定义了每条原始问题的"参考答案"，包括：

- **Keywords**：评估术语提取质量（BM25 + Embedding 召回锚点）
- **Canonical Queries**：评估改写查询的语义覆盖度（多表达式 Embedding Recall）
- **Reference Sub-queries**：评估多角度检索能力（不同 Chunk 命中率）

---

## 二、JSON Schema

```json
{
  "id": "Q_L01",
  "question": "种大豆需要多少积温才够？",
  "rewrite_type": "normalize",
  "rewrite_eval": {
    "required_terms": {
      "must_have": ["大豆"],
      "core_concept": ["活动积温"],
      "precision_term": ["≥10℃"]
    },
    "important_terms": [],
    "optional_terms": []
  },
  "canonical_queries": [
    "大豆≥10℃活动积温",
    "大豆种植积温要求",
    "大豆≥10℃活动积温阈值"
  ],
  "reference_sub_queries": [
    "大豆≥10℃活动积温阈值标准是多少",
    "大豆种植积温条件要求",
    "大豆积温适宜性区划指标"
  ]
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 唯一标识，Q_L 为 normalize 类，Q_SR 为 expand 类 |
| `question` | string | 原始用户问题 |
| `rewrite_type` | string | `"normalize"`（口语→术语）或 `"expand"`（同义→标准） |
| `rewrite_eval` | object | 术语评估标准（见第三节） |
| `canonical_queries` | array | 同一信息需求的多种等价表述（3-4条） |
| `reference_sub_queries` | array | 不同检索角度的参考查询（2-3条，可为空） |

---

## 三、Keywords 标注规范（★★★★★ 最重要）

### 3.1 核心原则

**Keywords = 召回锚点，不是知识点。**

- 标注的是"BM25/Embedding 靠什么词找到正确 Chunk"，不是"这个问题的答案包含哪些概念"
- 每条 keyword 必须能在知识库 Chunk 中**实际找到**，禁止专家脑补
- 优先用知识库高频术语，无法确认时保留用户原始表达，不造新词

### 3.2 五级分层（权重从高到低）

```
must_have       (0.45) — 召回锚点：领域实体，没有它基本找错文档
core_concept    (0.30) — 核心概念：概念正确即可，不要求字面完全一致
precision_term   (0.10) — 精确术语：理想状态下应包含的精确指标名
important_terms  (0.10) — 重要补充：有助于缩小召回范围的补充词
optional_terms   (0.05) — 可选加分：锦上添花
```

### 3.3 各层填入标准

**must_have**：实体/领域锚点。通常是作物名、指数缩写等。缺失=严重扣分。

- 例：`"大豆"` `"冬小麦"` `"陕西苹果"` `"干热风"` `"大豆食心虫"`
- 没有明确实体锚点的题可以留空：`"must_have": []`

**core_concept**：核心概念区。写这个题在讨论什么概念，允许多词覆盖 KB 不同用词风格。

- 例：Q_L10 橘子怕什么天气 → `"低温冻害", "极端最低气温", "高温热害"`（两个词分别对应不同文档的用词风格）
- 再例：Q_L01 种大豆需要多少积温 → `"活动积温"`（不是 `"积温指标"`——那是知识点描述，不是召回词）

**precision_term**：精确加分。最具体的那一层，命中了说明改写质量高。

- 例：`"≥10℃"` `"灌浆期"` `"生育期"` `"Penman-Monteith"`

**important_terms**：重要但不强制。通常是交叉领域的补充词。

- 例：`"坡度"` `"CWDI"` `"防灾减灾能力"` `"虫害防治"`

**optional_terms**：锦上添花。不放心可以多写，但宁缺毋滥。

- 例：`"黄土高原"` `"糖分"` `"时间尺度"`

### 3.4 常见错误

| 错误写法 | 问题 | 正确写法 |
|---|---|---|
| `"积温指标"` | 知识点描述，不是召回锚点 | `"活动积温"` `"≥10℃活动积温"` |
| `"适宜性区划"` | 太泛，匹配面太宽 | `"大豆"` + `"适宜性区划"` 组合 |
| `"蛋白质变性"` | KB 中不存在 | 不加，或用 KB 已有等价术语 |
| `"光照指标"` | KB 中不存在，是脑补的泛化词 | `"日照时数"` `"日照百分率"` |
| 每个 layer 只放 1 个词 | KB 多文档用词不同时多放几个 | `"低温冻害", "极端最低气温", "高温热害"` |

### 3.5 数量建议

| 层级 | 建议数量 | 说明 |
|---|---|---|
| must_have | 0-1 | 有就写，没有就留空 |
| core_concept | 1-3 | 覆盖 KB 不同文档的用词变体 |
| precision_term | 1-2 | 最精确的那个（些） |
| important_terms | 0-2 | 有用就写 |
| optional_terms | 0-2 | 不确定就不写 |
| **总计** | **3-7** | 不是越多越好 |

---

## 四、Canonical Queries 标注规范

### 4.1 核心原则

**同一信息需求的不同表达方式**，用于评估 Embedding Recall 是否受单一句式限制。

### 4.2 撰写标准

- 数量：2-4 条
- 每条应该是**等价但词序/用词不同**的表述
- 帮助验证：如果 KB 以其中任何一条去检索，都能命中正确文档

### 4.3 示例

```json
// 原问题：种大豆需要多少积温才够？
"canonical_queries": [
  "大豆≥10℃活动积温",          // 最精确的表述
  "大豆种植积温要求",            // 换个说法
  "大豆活动积温指标",            // 再换个说法
  "大豆≥10℃活动积温阈值"        // 加上"阈值"
]
```

### 4.4 常见错误

- 三个 query 语义几乎一样（如 "ET0定义" "ET0是什么" "ET0概念"）→ 没有增加召回多样性
- 把不同角度当成等价表述（那是 sub_query 的职责）

---

## 五、Reference Sub-queries 标注规范

### 5.1 核心原则

**Sub-query ≠ 知识点标题。Sub-query = 拿去检索能命中不同 Chunk 的查询。**

### 5.2 三条铁律

1. **覆盖不同 Chunk**：三个 sub-query 应该命中 KB 中不同的文档/章节
2. **不能互相包含**：语义上不要重叠，否则 Embedding 几乎一样，没有增量价值
3. **不强制 3 条**：如果确实不需要多角度检索（如"苹果成熟期是什么时候？"），允许 1-2 条甚至 0 条

### 5.3 类型配比

每个问题的 sub-queries 尽量覆盖以下三种类型：

| 类型 | 示例 | 命中 Chunk 类型 |
|---|---|---|
| **定义类** | "水分亏缺指数CWDI是什么" | 术语定义、概念解释 |
| **方法类** | "CWDI作物需水量计算公式" | 公式、算法、计算步骤 |
| **应用类** | "大豆干旱风险区划致灾因子评估" | 区划图、评估报告、案例分析 |

### 5.4 正确 vs 错误

```
错误（知识点标题，不是检索查询）:
  "高温热害导致大豆蛋白质变性机理及产量影响"

正确（检索查询，能命中不同 Chunk）:
  "高温热害指数定义及评估框架"         → 定义类
  "大豆高温热害指数计算方法及计算公式"  → 方法类
  "大豆开花期高温热害等级划分标准"     → 应用类
```

### 5.5 验证方法

写完后用脚本验证：每条 sub-query 是否能命中 KB 中**不同 heading** 的 Chunk，且至少命中 2 个 gold term。

---

## 六、rewrite_type 判定

| 类型 | 适用场景 | 示例 |
|---|---|---|
| `normalize` | 口语/白话 → 专业术语 | "光照好不好"→"日照百分率" |
| `expand` | 学术同义 → 标准指标名 | "高温伤害"→"高温热害指数" |

判定标准：看用户是否知道专业术语。
- 知道概念但用词不规范 → `expand`
- 完全用大白话描述 → `normalize`

---

## 七、标注流程

### Step 1: 理解问题
- 用户在问什么？核心意图是什么？

### Step 2: 搜索知识库
- 在 chunks.json 中搜索相关术语
- 确认哪些术语 KB 中**实际存在**，以及出现频次
- 记录 KB 中不同文档的用词变体

### Step 3: 标注 Keywords
- 先写 must_have（有实体锚点就写，没有就空）
- 再写 core_concept（覆盖 KB 不同用词风格）
- 最后写 precision_term / important / optional（按需）
- **每写一个词，确认 KB 中存在**

### Step 4: 写 Canonical Queries
- 同一个意思换 2-4 种说法
- 确保每种说法都能独立命中正确文档

### Step 5: 写 Sub-queries
- 思考：这个问题的知识在 KB 中分布在哪些不同类型的 Chunk 里？
- 每个 sub-query 瞄准一个类型（定义/方法/应用）
- 检查语义是否重叠，重叠就合并或删除

### Step 6: 验证
- 全量 KB 验证脚本：所有 must_have + core_concept + precision_term + important + optional **必须**在 KB 中存在
- Sub-query 命中验证：每条 sub-query 应命中 ≥2 个 gold term 的 KB chunk

---

## 八、反面教材速查

| 问题 | 症状 | 修复 |
|---|---|---|
| Keywords 脑补 | 写了 KB 里没有的术语 | 搜索 KB 确认，找不到就删 |
| Keywords 太泛 | `"积温指标"` `"光照指标"` 这种自己造的描述性短语 | 换成 KB 里的具体术语 |
| Sub-query = 知识点 | "蛋白质变性机理及产量影响" | 改成检索式："大豆高温热害指数计算方法" |
| Sub-query 互相包含 | "苹果品质评价方法" "苹果品质评价指标" "苹果品质区划指标体系" | 只保留角度最不同的一条，其余换成别的方向 |
| Sub-query 全方法类 | 三条都是"计算方法""评估方法" | 加一条定义类（"XX是什么"）和一条应用类（"XX区划应用"） |
| 硬凑 3 条 | 明明不需要角度分散的题也写 3 条 | 该 1 条就 1 条，该 2 条就 2 条 |
| Canonical 等价 | 三条 query 语义完全一样仅换了词序 | 用真正不同的表述方式 |

---

## 九、评估权重速查

```
加权召回 = 0.45 × must_have命中率
         + 0.30 × core_concept命中率
         + 0.10 × precision_term命中率
         + 0.10 × important_terms命中率
         + 0.05 × optional_terms命中率
```

命中判定：子串匹配（term 在 keyword 中 或 keyword 在 term 中），容错大小写。

---

## 十、配套验证脚本

```python
import json

# 1) 全量 KB 术语验证
with open('data/chunks.json') as f:
    chunks = json.load(f)
all_text = ' '.join(c.get('content', '') for c in chunks)

with open('data/golden_rewrite.json') as f:
    golden = json.load(f)

for item in golden:
    for layer in ['must_have', 'core_concept', 'precision_term']:
        for term in item['rewrite_eval']['required_terms'].get(layer, []):
            assert term in all_text, f"{item['id']} {layer} '{term}' NOT IN KB"

# 2) Sub-query Chunk 命中验证
for item in golden:
    gold_terms = []
    req = item['rewrite_eval']['required_terms']
    for layer in ['must_have', 'core_concept', 'precision_term']:
        gold_terms.extend(req.get(layer, []))
    gold_terms.extend(item['rewrite_eval'].get('important_terms', []))
    gold_terms.extend(item['rewrite_eval'].get('optional_terms', []))
    
    for sq in item.get('reference_sub_queries', []):
        best = max(
            sum(1 for t in gold_terms if t in c.get('content', ''))
            for c in chunks
        )
        if best < 2:
            print(f"WARNING: {item['id']} sub_query '{sq[:50]}...' best={best}")
```
