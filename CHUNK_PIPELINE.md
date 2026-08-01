# 农业气候区划 RAG — Chunk Pipeline 技术文档

> 完整记录从原始文档到检索结果的全链路 chunk 处理逻辑
>
> **最后更新**: 2026-07-29 (v2.0 Union + Dynamic Coverage 架构)

---

## 变更日志

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-07-29 | v2.0 | **Union + Dynamic Coverage 架构**：① Keywords 不再独立检索，拼入 Original BM25 增强关键词召回；② 检索管线从 Parallel Evidence Merge + Dense Protect 改为 Union + RRF + Evidence Voting(3档) + Dynamic Coverage Reservation(40%/20%/40%) + Global Fill → CE Rerank；③ 候选结构增加 best_channel/best_rank；④ Rewrite 上限 ≤2(hard 2)；⑤ eval 侧 Rewrite/SubQ 分路传递，各自独立配额 |
| 2026-07-29 | v1.19 | **Query Rewrite 三池分离**：expand_query 从 Keyword/SubQuery 二池改为 Keyword/RewriteQuery/SubQuery 三池分离合并；LLM Prompt 新增 `rewrite_queries` 字段（完整句子改写）；各池加硬限制：Keyword≤6, RewriteQuery≤2, SubQuery≤4 |
| 2026-07-27 | v1.18 | **Parallel Evidence Merge 架构**：多子查询从 Append 改为并行召回+Evidence Merge（去重+Hit Boost+Global Pool），Rewrite 不再被截断丢弃；Dense Protect 从全局 top-5 改为逐查询 top-2；OOD Judge 简化为 direct≥0.70 / llm 两层；移除 Context Expansion |
| 2026-07-27 | v1.17 | **评估逻辑修复**：Chunk 级评估从 section-展平-截断改为 chunk_id 直接比对 gold_chunks；新增 Section 级作为辅助指标；修复"找到但算零召回"的假阳性问题（60% 零召回题实为 CE 已命中但被 [:10] 截断挤出） |
| 2026-07-27 | v1.16 | **检索管线修复**：Dense Protected Merge 从"占位保序"改为"保送 CE 候选池"，Dense top-K 不再绕过 CE 占前 5 位，只确保进入 CE 候选池与其他候选公平竞争 |
| 2026-07-24 | v1.15 | **表格线性化回归 step1 + 行窗口拆表**：linearize_chunks.py 废弃，线性化移到 step1 解析阶段；step2 删除所有表格专用逻辑（Phase 1.5 移除），统一走 TextSplitter；拆表从"一行一 chunk"改为"≤800 字行窗口" |
| 待实施 | v1.14 | **XLSX/CSV 逐行线性化**：parse_xlsx/parse_csv 当前仅保留前 5 行，其余数据不可检索；改为每行独立线性化 chunk，可选保留统计摘要 chunk |
| 2026-07-22 | v1.13 | step1 移除表格线性化，pipe 原样保留；step2 新增 Phase 1.5 表格拆行 + 逐行线性化 |
| 2026-07-20 | v1.12 | step2 H3 子标题回退切分（>3000 字），498→753 chunks；BM25/Chroma 同源切分 |
| 2026-06-29 | v1.11 | Section Quality Filter (三层过滤) + PDF Layout 解析 Phase 1 (fitz dict 模式、布局标题检测、页眉页脚去重) + metadata.type / layout_mode |
| 2026-06-26 | v1.8 | compact header 修复，MRR 恢复到 0.8333 |

---

## 总览

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Chunk Pipeline 全流程                           │
└─────────────────────────────────────────────────────────────────────────┘

源文件 (.docx / .pdf / .xlsx / .csv)
    │
    ▼
╔═════════════════════════════════════════════════════════════════════════╗
║                      step1_parse.py  —  解析                           ║
╠═════════════════════════════════════════════════════════════════════════╣
║                                                                         ║
║  ① 文件 → Block 列表                                                   ║
║     DOCX: python-docx → 段落 + style (Heading 1/2/3)                   ║
║     PDF:  fitz dict → 逐行 + 字号/加粗/坐标                           ║
║           ├─ 页眉页脚去重 (跨页 >40%)                                  ║
║           └─ P70/P90 标题检测                                          ║
║     XLSX: openpyxl → sheet → 统计+采样                                 ║
║     CSV:  csv.DictReader → 统计+采样                                   ║
║                                                                         ║
║  ② Block → Section                                                     ║
║     Tier 1: DOCX 样式标题 (H2 切边界, H3 内联)                         ║
║     Tier 2: 正则标题 (第X章/一、/1.1/（1）...)                         ║
║     Tier 3: 固定长度 ~800 字句边界切分 (兜底)                          ║
║                                                                         ║
║  ③ Section Quality Filter (三层)                                       ║
║     L0: Drop 目录/模板注释, Merge 连续表格                             ║
║     L1: Merge 空标题/过渡句/PDF断句, Keep 完整短句                     ║
║     L2: Merge 极短无句末片段                                           ║
║                                                                         ║
║  ④ Section → Chunk                                                     ║
║     section_id = {doc_stem}_sec_{si}                                    ║
║     id         = {doc_stem}_s{si}                                       ║
║     metadata   = { section_id, heading_path, province,                 ║
║                    crop, zoning, type, layout_mode }                    ║
║     content    = section 原文 (pipe 表格原样)                           ║
║                                                                         ║
║  ⑤ 文档去重 (_dedup_documents)                                         ║
║     同(省份,作物,区划)的 DOCX/PDF → 保留内容更丰富者                    ║
║     另一份标记 excluded=True，不进入下游                                ║
║                                                                         ║
║  ⑥ 质量标记 (_tag_chunk_quality)                                       ║
║     <50 字 → quality=low                                                ║
║                                                                         ║
║  ⑦ 表格线性化 + 拆分 (_linearize_and_split_tables)                     ║
║     type=table 的 chunk → linearize() 转为自然语言                      ║
║     线性化失败 → 保留原内容                                             ║
║     线性化后 >800 字 → 按句号边界拆分子 chunk (行窗口)                  ║
║     每个子 chunk 保留上下文前缀                                          ║
║                                                                         ║
╚═════════════════════════════════════════════════════════════════════════╝
    │
    ▼
data/chunks.json  (514 条, avg 541 字, 表格线性化率 100%)
    │
    │  load_documents():
    │    - 跳过 excluded=True / quality=low
    │    - page_content = [省份 作物 区划] + 正文
    │    - 保留 section_id, heading_path 等 metadata
    │
    ▼
╔═════════════════════════════════════════════════════════════════════════╗
║                step2_embed.py  —  切分 + 索引                           ║
╠═════════════════════════════════════════════════════════════════════════╣
║                                                                         ║
║  ┌─ Phase 1: H3 回退切分 ──────────────────────────────────┐          ║
║  │  章节 >3000 字 → 按子标题切开，保留 heading_path        │          ║
║  └──────────────────────────────────────────────────────────┘          ║
║                         │                                               ║
║                         ▼                                               ║
║  ┌─ Phase 2: RecursiveCharacterTextSplitter ───────────────┐          ║
║  │  所有 doc 统一处理 (表格已在 step1 线性化，无特殊路径)    │          ║
║  │    ≤800 字 → keep_intact                                 │          ║
║  │    >800 字 → TextSplitter (800/chunk, 150 overlap)       │          ║
║  └──────────────────────────────────────────────────────────┘          ║
║                         │                                               ║
║                         ▼                                               ║
║  ┌─ Phase 2.5: 补回 compact header ────────────────────────┐          ║
║  │  切分后子块丢失 [省份 作物 区划] 前缀 → 补回             │          ║
║  └──────────────────────────────────────────────────────────┘          ║
║                         │                                               ║
║                         ▼                                               ║
║  ┌─ Phase 3: 分配 chunk_index / chunk_count ───────────────┐          ║
║  │  按 section_id 分组编号，用于检索时 expand_context       │          ║
║  └──────────────────────────────────────────────────────────┘          ║
║                         │                                               ║
║                         ▼                                               ║
║  build_vectorstore():                                                   ║
║    HuggingFaceEmbeddings (bge-small-zh-v1.5, 512维)                    ║
║    ChromaDB (cosine)                                                    ║
║                                                                         ║
╚═════════════════════════════════════════════════════════════════════════╝
    │
    ▼
data/chunks_split.json  (787 条, avg 374 字)  +  vectordb/  (787 vectors)
    │
    ▼
╔═════════════════════════════════════════════════════════════════════════╗
║                hybrid_search.py  —  检索 + 融合 + 评估                  ║
╠═════════════════════════════════════════════════════════════════════════╣
║                                                                         ║
║  Query: "新疆冬小麦和河南冬小麦品种有什么不同？"                         ║
║    │                                                                     ║
║    ├── [改写] expand_query → 三池产出                                    ║
║    │   ┌─ Keyword 池 (词/短语, ≤6, hard 6) ───────────────────┐          ║
║    │   │  术语映射 + 同义词扩展 + LLM keywords                  │          ║
║    │   │  → 拼入 Original BM25 query 增强关键词召回            │          ║
║    │   └──────────────────────────────────────────────────────┘          ║
║    │   ┌─ Rewrite Query 池 (完整句子, ≤2, hard 2) ───────────┐          ║
║    │   │  LLM rewrite_queries（不同表达重述同一问题）           │          ║
║    │   │  → Dense20 + BM2510 → RRF                            │          ║
║    │   └──────────────────────────────────────────────────────┘          ║
║    │   ┌─ SubQuery 池 (完整句子, ≤4, hard 4) ────────────────┐          ║
║    │   │  LLM sub_queries（拆分复杂问题为独立子问题）           │          ║
║    │   │  → Dense20 + BM2510 → RRF                            │          ║
║    │   └──────────────────────────────────────────────────────┘          ║
║    │                                                                     ║
║    ├── Phase 1: Union 并路采集 + Dict 去重 ─────────────────────        ║
║    │   Original: Dense30 + BM25(keyword增强)20 → 自然去重                ║
║    │   Rewrite:  每路 Dense20+BM2510 → 自然去重                          ║
║    │   SubQuery: 每路 Dense20+BM2510 → 自然去重                          ║
║    │   Global Merge — chunk_id 去重, 无截断                              ║
║    │                                                                     ║
║    ├── Phase 2: Evidence Voting + Retrieval Prior ─────────────        ║
║    │   Evidence: 1路→0, 2路→δ(0.002), ≥3路→2δ(0.004)                   ║
║    │   Retrieval Prior = 0.7×RRF_norm + 0.3×Evidence_norm              ║
║    │                                                                     ║
║    ├── Phase 3: Dynamic Coverage Reservation (≤50) ────────────        ║
║    │   Original=20(40%) | Rewrite=10(20%) | SubQ均分20(40%)             ║
║    │   Global Fill 补齐到 50                                             ║
║    │                                                                     ║
║    ├── Phase 4: CE Rerank — bge-reranker-v2-m3                         ║
║    │   Final = 0.8×CE_norm + 0.2×Retrieval_Prior, λ=0.1               ║
║    │   50 → Top K (默认 10)                                              ║
║    │                                                                     ║
║    ▼                                                                     ║
║  最终 10 条 → ±1 chunk 上下文扩展                                        ║
║    │                                                                     ║
║    ▼                                                                     ║
║  ┌─ 评估 ───────────────────────────────────────────────────────┐       ║
║  │  Chunk 级: chunk_id 直接比对 gold_chunks   (基础指标)         │       ║
║  │  Section 级: section_id 命中 gold section  (辅助指标)         │       ║
║  └──────────────────────────────────────────────────────────────┘       ║
║                                                                         ║
╚═════════════════════════════════════════════════════════════════════════╝
    │
    ▼
top-K 结果 → Judge (direct ≥0.70 / llm) → Generate
```

## 对比

| | v1.13 (旧) | v1.15 (新) |
|---|---|---|
| 表格线性化位置 | step2 Phase 1.5 | step1 解析阶段 |
| 表格线性化率 | 仅拆分的 32 表 | **100%** (所有 type=table) |
| table chunk 粒度 | 一行 = 1 个 chunk | ≤800 字行窗口 (多行合并) |
| embedding 信号 | 单行向量 | 多行窗口向量，上下文完整 |
| step2 表格专用代码 | ~85 行 (Phase 1.5) | 0 行 |
| chunks_split 总数 | 822 | 787 |
| avg chunk 长度 | 361 | 374 |
| raw pipe 残留 | 有 (不拆的表) | 0 |

---

## step1 管道详解：每个步骤的设计动机

### 整体目标

把农业区划的 DOCX/PDF 文档切成适合 RAG 检索的 chunk。核心矛盾：切太细（500 字以下碎片）→ 语义不完整，检索不到；切太粗（2000 字以上大段）→ 信号稀释，检索不准。目标每段 300-800 字，语义完整。

---

### 步骤 1：解析 → Block 列表

**做什么**：从 DOCX/PDF 中提取最小文本单元（段落/行），统一为 `Block` 数据结构。

**为什么**：统一 Block 层后，后续所有管道（Section 构建、质量过滤）不关心来源格式。

```
DOCX: python-docx → 段落天然带 style ("Heading 1" / "Normal")
PDF:  pymupdf fitz → 逐行提取文字 + 字号/加粗/坐标
```

Block 结构：
```python
@dataclass
class Block:
    text: str
    style: Optional[str] = None     # DOCX: "Heading 1"; PDF: None
    page: Optional[int] = None      # PDF 页码
    source: str = ""                # 源文件名
    font_size: Optional[float] = None  # PDF dict mode: 字号(pt)
    bold: bool = False                 # PDF dict mode: 是否加粗
    x0: Optional[float] = None         # PDF dict mode: 左边距
    y0: Optional[float] = None         # PDF dict mode: 上边距
    is_title: bool = False             # PDF dict mode: 布局检测为标题
```

---

### 步骤 2：Section 构建

**做什么**：把平面的 Block 列表变成有边界的 Section。每个 Section = 一个标题 + 若干正文 Block。

**为什么**：RAG 检索需要"围绕一个主题的完整段落"。Section 是最小的主题单元——同一节讲同一件事（如"1.4.2 大豆主产区积温特征"），不会被跨主题切碎。

#### DOCX：样式标题切分

```
H1 "黑龙江省大豆冷害气候风险区划"  → 记录文档标题，不切 Section
H2 "一、数据来源"                  → 新 Section 边界
  H3 "1.1 气象数据"                → 内联为 ### 1.1 气象数据，不切新 Section
  正文段落                          → 全部归属此 Section
```

只用 H2 切边界、H3 保留在正文内。因为 H3 通常是 H2 的子主题，切成独立 chunk 反而丢失父级语境。

二层降级：无 Heading style → 正则标题匹配（`第X章` `一、` `1.1` 等模式）→ 仍失败则固定长度 ~800 字按句边界切分（Tier 3 兜底）。

#### PDF：fitz dict 模式（布局感知）

```
fitz page.get_text("dict") → 提取每行文字 + 字号/加粗/坐标
    ↓
P70/P90 双阈值标题检测（_detect_titles_by_layout）:
  字号 ≥ P90（章节级，例 14pt）  → +0.5 分
  字号 ≥ P70（小节级，例 10pt）  → +0.3 分
  加粗（flags bit 4 = 1）        → +0.1 分
  文字长度 < 30 字                → +0.2 分
  总分 > 0.6                     → is_title = True，切新 Section
```

**为什么 PDF 不能只用正则标题**：dict 模式下每行一个 Block，正则 `^\d+[\.\、]` 会把正文中的 `1. 海拔高度` 误判为标题，导致 386 个 section（text 模式只有 162 个）。布局信息（字号）比正则更可靠。

**页眉页脚去重**（`_dedup_headers_footers`）：
```
每页按 y0 排序 → 取首 2 行 / 尾 2 行为候选
  → 候选项跨页出现 > 40% → 标记为噪声哈希
  → 仅移除处于页眉/页脚位置的匹配 Block
```

`"内蒙古自治区农业气候区划报告"` 每页顶部都有，频率 > 40% → 识别为页眉，剔除。正文中的同名文本不受影响。

**Fallback**：dict 模式失败（加密 PDF、扫描件）→ 回退 `page.get_text()` + 正则标题。

---

### 步骤 3：Section Quality Filter（三层过滤）

**做什么**：Section 构建后的原始产物有大量"名义上的 section"——很多只是跨页碎片、空标题、目录残渣、表格行。单遍 while 循环处理，维护 result 列表。

**为什么**：这些碎片直接输出 → 检索返回"目录见第15页"或孤立的 `| 大豆 | 100 |` 表格行，严重影响 RAG 质量。

#### Layer 0：结构识别 → Drop 或 Group-Merge

先识别明显不需要的内容和需要特殊合并的结构：

| 规则 | 判定条件 | 动作 | 为什么 |
|------|---------|------|--------|
| **L0a: 模板注释** | 含"不用删除""标红内容为示例""仅供参考" | Drop | DOCX 模板红字提示，不是实际内容 |
| **L0b: TOC 目录** | 匹配 `... 1` / `tab tab 页号` / `### 3.2.1 标题 页号`，多行时 >40% 命中 | Drop | "第三章见第15页"对检索无意义 |
| **L0c: 连续表格** | 同 pipe 数的连续 table section（`|...|...|` 结构相同） | Merge 为一个 | PDF 每行表格行被切成独立 section，孤行无意义 |

#### Layer 1：语义判断 → Merge 或 Keep

判断短 section 是"独立完整内容"还是"残缺片段"：

| 规则 | 判定条件 | 动作 | 为什么 |
|------|---------|------|--------|
| **L1a: 空标题** | 正文 < 50 字 | Merge 到下一个 | "第二章 数据来源"后没正文，应和下面内容合并 |
| **L1b: 过渡句** | < 120 字 + 含"包括以下""如下""分别为"或以`：`结尾 | Merge 到下一个 | "区划指标主要包括以下几个方面："不承载信息，只是引出下文 |
| **L1c: 完整句** | < 200 字且以`。！？`结尾 | **Keep** | "大豆种植面积占全区耕地面积的 35%。"虽短但语义完整，删掉丢信息。农业文档短定义句常见 |
| **L1d: PDF 断句** | 50-250 字 + 不以`。`结尾 + 含领域词 + 下一 section 是续接 | Merge 到下一个 | 跨页截断："呼伦贝尔境内海拔在 800 米以下，锡林郭勒"(页末) + "盟大部分地区..."(下页) |

**L1c 为什么放在 L1a/L1b 之后**：先清理垃圾，再判断保留。否则 `"计算方法如下。"`（32 字，以`。`结尾）会被 L1c 保留——它确实是完整句，但语义上应和后面的公式合并。

#### Layer 2：长度兜底 → Merge

L1 没覆盖到的极短片段，最后一道防线：

| 规则 | 判定条件 | 动作 | 为什么 |
|------|---------|------|--------|
| **L2: 极短片段** | < 80 字 + 不以`。`结尾 + 非定义 + 非列表 | Merge 到下一个 | "土壤有机质含量较高，土壤酸碱度适中，适宜大豆"（45 字，无句号）明显被截断 |

**保护条件**：
- 含 `是指|即|公式|定义` → 不合并（"活动积温是指日平均气温稳定通过 10℃期间的积温总和。"是完整定义）
- 列表项（`1.` `一、` `-`）→ 不合并（列表项可能独立承载信息）

**领域词表**（`_DOMAIN_PAT`，用于 L1d 断句识别）：
- 农业气象：区划|风险|作物|指标|气候|农业|气象|品种|种植|产量|品质|灾害|温度|降水|干旱|冷害|渍涝|霜冻|病虫害|土壤|光照|积温|日照
- 地形地理：海拔|高原|地形|地势|地貌|丘陵|平原|山地|盆地|谷地|高程|纬度|经度

**过滤效果（内蒙古区划报告示例）**：
```
[Section 过滤] 386 -> 50 | 合并180
           [表格=24, PDF断句=46, 过渡句=3, 空标题=76, 短片段=29, 保留短完整句=4]
```
- 386 个原始 section → 50 个最终 chunk（保留 13%）
- 空标题 76 个 + PDF 断句 46 个占合并的 68%
- 保留 4 个短完整句："大豆种植面积占全区 35%。"类独立语义句

**全局效果（v1.11, 516 chunks）**：完整 62% + 可接受 23% + 碎片 10%（主要是跨页公式和表格）。低质量标记仅 3 个。

---

## 核心改动总览

| 维度 | 旧方案 | 新方案 |
|------|--------|--------|
| **DOCX 切分** | 遇 Heading 就切，H1/H2/H3 平级 | H1 文档标题，H2 章节边界，H3+ 内联正文 |
| **层级信息** | 仅 section_title，父子关系丢失 | heading_path 完整谱系：`["黑龙江大豆区划", "区划指标", "冷害指数"]` |
| **page_content** | `[省份 作物 区划]` header + 正文 | `[省份 作物 区划]` compact header + 正文；heading_path 仅保留在 metadata（v1.9 精简） |
| **大章节处理** | 无中间层，直接切 1000 字子块 | >3000 字先按 H3 回退切分，保留 path，再走 800/150 |
| **切分参数** | chunk_size=1000, overlap=200 | chunk_size=800, overlap=150 |
| **parent-child** | parent_content = 完整章节全文 | 按 section_id + chunk_index 同 section ±1 窗口扩展 |
| **上下文扩展** | 命中子块 → 展开全部父文档（可能万字） | 命中子块 → 同 section 内前1+当前+后1 chunks |
| **BM25 数据源** | 从 raw chunks.json 构建（step1 原始大小） | 从 vectordb 构建（与 Dense 共享同一套子块） |
| **表格处理** | step1 整表线性化为自然语言 | step1 表格线性化 + 行窗口拆分，step2 统一 TextSplitter |
| **降级策略** | 无标题 → 每 3 段一个 chunk | 无 style 标题 → 正则识别 → 仍失败 → 固定长度 ~800 字/句边界 |
| **metadata 字段** | province, crop, zoning, section_title | + doc_id, section_id, heading_path, heading_level, chunk_index, chunk_count |

---

## 第1层：step1_parse.py — 三层切分策略

### 旧方案

```python
# 所有级别标题一视同仁
if "Heading" in style or "heading" in style or "标题" in style:
    sections.append(current_section)  # H1/H2/H3 全部平级切
    current_section = {"title": text, "paragraphs": [], "tables": []}
```

```
Heading 1  黑龙江省大豆冷害区划    → Chunk A (空壳，丢弃)
Heading 2  一、数据来源            → Chunk B (几乎空的)
Heading 3  1.1 气象数据 + 正文     → Chunk C (仅有正文，丢失父级语境)
Heading 3  1.2 产量数据 + 正文     → Chunk D (同上)
```

问题：H3 成为独立 chunk 后，不知道父级是"一、数据来源"，更不知道自己在"黑龙江大豆冷害区划"这个大主题下。

### 新方案：三层递进

```
Tier 1: 样式标题切分
  H1 → 记录文档标题，不切 chunk
  H2 → 章节边界，触发新 section
  H3+ → 内联为 ### / #### markdown，保留在所属 H2 章节内

Tier 2: 正则标题降级（< 2 sections 时触发）
  匹配模式：一、二、三、  |  第一章  |  （一）  |  1. 2.
  所有匹配项视为 H2 级别

Tier 3: 固定长度兜底（Tier 2 也失败时）
  max_chars = 800
  按句边界（。！？\n）切分，不硬截断
  heading_path = [doc_stem]
```

```
Tier 3 示例（某无标题 DOCX 全文 2400 字）:

原始文本:
  "黑龙江省农业气候资源普查空间范围覆盖省、市、县（区）三级行政区。
   时间范围包含年度农业气候资源和不同作物全生育期的农业气候资源。
   空间范围：全省、各地市及下辖县。时间范围：1961-1990年、1991-2020年两个标准气候期。
   普查内容主要包括以下几个方面：（1）光照资源...（2）热量资源...（3）水分资源...
   ..."  (2400 字)

↓ _split_by_sentence(text, max_chars=800)

Chunk 0 (780字): "黑龙江省农业气候资源普查空间范围覆盖省..."
Chunk 1 (810字): "（2）热量资源。包括年平均气温、各月平均气温..."
Chunk 2 (790字): "（5）基础地理信息。① 新疆、伊犁州..."

每个 chunk 的 metadata:
{
    "doc_id": "新疆农业气候资源普查清单",
    "section_id": "新疆农业气候资源普查清单_sec_0",
    "heading_path": ["新疆农业气候资源普查清单"],
    "heading_level": 1,
}
```

### 三层触发条件与阈值

| 层级 | 触发条件 | 切分方式 | 阈值 |
|------|---------|---------|------|
| Tier 1 | 文档含 Heading style | H2 为章节边界，H3+ 内联 | — |
| Tier 2 | Tier 1 产出 < 2 sections | 正则匹配标题行视为 H2 | — |
| Tier 3 | Tier 2 产出 < 2 sections | 按句边界固定长度切分 | **800 字** |

三层实际触发情况（本次 898 chunk 测试）：

| 文档类别 | 数量 | 触发层级 |
|---------|------|---------|
| 有 style 标题的 DOCX | ~10 个 | Tier 1 |
| 无 style 但有编号标题 | 0 个（本次数据未出现） | Tier 2 |
| 完全无标题文档 | 0 个（本次数据未出现） | Tier 3 |

```
以二级标题作为章节边界：
  H2 "一、数据来源与处理" ──→ Section A
    H3 "1.1 气象数据" ──→ 内联为 ### 1.1 气象数据
    H3 "1.2 产量数据" ──→ 内联为 ### 1.2 产量数据
    正文段落 ──→ 全部归属 Section A

  H2 "二、区划指标与方法" ──→ Section B
    H3 "2.1 冷害指数" ──→ 内联为 ### 2.1 冷害指数
    H3 "2.2 阈值确定" ──→ 内联为 ### 2.2 阈值确定
    正文段落 ──→ 全部归属 Section B
```

### Heading Path 设计

每个 chunk 携带完整层级谱系：

```python
# chunk from Section A
metadata = {
    "doc_id": "黑龙江大豆冷害区划技术规范",
    "section_id": "黑龙江大豆冷害区划技术规范_sec_1",
    "heading_path": ["黑龙江省大豆冷害气候风险区划", "一、数据来源与处理"],
    "heading_level": 2,
}

# chunk from Section B
metadata = {
    "section_id": "黑龙江大豆冷害区划技术规范_sec_2",
    "heading_path": ["黑龙江省大豆冷害气候风险区划", "二、区划指标与方法"],
    "heading_level": 2,
}
```

### 无标题文档处理对比

| | 旧方案 | 新方案 |
|---|---|---|
| 无 heading style | 每 3 段打包一个 chunk | 先尝试正则识别标题 (Tier 2) |
| 正则也匹配不到 | — | 固定长度 800 字，句边界对齐 (Tier 3) |
| heading_path | 无 | `[doc_stem]`，不丢文档归属 |
| 切分函数 | — | `_split_by_sentence(text, max_chars=800)` |

### 产出

`data/chunks.json`

| 指标 | 旧值 | 新值 (v1.11) |
|------|------|------|
| 总 chunk 数 | 1177 | 516 |
| 有效 chunk (排除 excluded + low) | 1060 | 513 |
| 平均 chunk 长度 | ~490 字符 | 547 字符 |
| heading_level 1 / 2 | 无此字段 | 分布见 section 统计 |
| metadata.type | 无 | text / heading / table |
| metadata.layout_mode | 无 | dict / docx_style / xlsx / csv |

---

### Section Quality Filter（新增：v1.11）

Section 构建后、chunk 生成前插入三道过滤，沿 sections 列表单遍 while 循环处理。

**三层优先级架构**：

```
Layer 0: 结构识别 → Drop（丢弃）或 Group-Merge（表格合并）
  ├── L0a: 模板注释 ("不用删除"/"标红内容为示例") → Drop
  ├── L0b: TOC 目录 (省略号+页码 / tab+页码 / DOCX ###标记) → Drop
  └── L0c: 表格结构 (|...| 或 >60% 行为数字) → 连续表格合并为一个

Layer 1: 语义判断 → Merge（向后合并）或 Keep（保留）
  ├── L1a: 空标题 (<50字正文) → Merge 到下一个 section
  ├── L1b: 过渡句 (<120字 + 过渡关键词或冒号结尾) → Merge
  ├── L1c: 完整句 (<200字且以。！？结尾) → Keep（短但语义完整）
  └── L1d: PDF 断句补全 (50-250字 + 无句末标点 + 含领域词 + 下一 section 续接) → Merge

Layer 2: 长度兜底
  └── L2: 极短片段 (<80字 + 无句末标点 + 非定义/非列表) → Merge
```

**领域词表（`_DOMAIN_PAT`）**：
农业气象类：区划|风险|作物|指标|气候|农业|气象|品种|种植|产量|品质|灾害|温度|降水|干旱|冷害|渍涝|霜冻|病虫害|土壤|光照|积温|日照
地形地理类：海拔|高原|地形|地势|地貌|丘陵|平原|山地|盆地|谷地|高程|纬度|经度

**过渡关键词**（`TRANSITION_PATTERNS`）：
"包括以下", "主要包括", "如下", "具体如下", "分别为", "分为", "以下几个方面", "以下方面", "主要措施", "对策建议"

**过滤效果（内蒙古 PDF 示例）**：
```
[Section 过滤] 386 -> 50 | 合并180
           [表格=24, PDF断句=46, 过渡句=3, 空标题=76, 短片段=29, 保留短完整句=4]
```

**全局统计（516 chunks）**：
- 完整段落: 62%，可接受: 23%，碎片: 10%（主要是跨页断句和公式/小表格）
- 低质量标记: 仅 3 个（空表格壳）

---

### PDF Layout 解析 Phase 1（新增：v1.11）

将 PDF 解析从纯文本模式升级为排版感知模式。

**Block 字段扩展**：
```python
@dataclass
class Block:
    text: str
    font_size: Optional[float] = None  # 字号(pt)
    bold: bool = False                 # 是否加粗
    x0: Optional[float] = None         # 左边距
    y0: Optional[float] = None         # 上边距
    is_title: bool = False             # 布局检测为标题
```

**双模解析流程**（`parse_pdf`）：
```
fitz.open(doc)
  │
  ├── try dict 模式 (_extract_layout_blocks)
  │     page.get_text("dict") → 逐 blocks/lines/spans 解析
  │     每个 line → 一个 Block（合并同 line 内 spans）
  │     ↓
  │     按 y0 排序 + 首尾位置去重页眉页脚 (_dedup_headers_footers)
  │     ↓
  │     全局 P70/P90 双阈值标题检测 (_detect_titles_by_layout)
  │     ↓
  │     layout_mode = "dict"
  │
  └── except → fallback text 模式
        page.get_text() → _lines_to_blocks() → 正则标题
        layout_mode = "text_fallback"
```

**标题检测评分规则**（`_detect_titles_by_layout`）：
| 信号 | 条件 | 得分 |
|------|------|------|
| 字号 | font_size >= P90（章节级） | +0.5 |
| 字号 | font_size >= P70（小节级） | +0.3 |
| 加粗 | flags bit 4 = 1 | +0.1 |
| 长度 | len(text) < 30 | +0.2 |

score > 0.6 → `is_title = True`。dict 模式下仅用 `is_title` 切 section（关闭正则标题，避免 `_is_heading()` 误匹配列表项/数字编号）。

**字号分位数示例（内蒙古 PDF）**：
```
3387 blocks, P50=9.0pt, P70=10.0pt, P90=14.0pt
727 个 block 得分 > 0.6 → is_title
```

**页眉页脚去重**（`_dedup_headers_footers`）：
- 每页按 y0 排序，取前 2 / 后 2 个 Block 为候选
- 候选项在 >40% 页面出现 → 标记为噪声哈希
- 仅移除处于页眉/页脚**位置**的匹配 Block（保留正文中同名文本）
- 效果：新疆冬小麦报告移除 54 行，新疆普查技术规范移除 12 行

**dict 模式覆盖率**：
9 个已处理 PDF 100% 成功使用 dict 模式，0 回退到 text 模式。

**新增 metadata 字段**（`_sections_to_chunks`）：
| 字段 | 值 | 说明 |
|------|-----|------|
| `metadata.type` | `"heading"` / `"text"` / `"table"` | section 内容类型 |
| `metadata.layout_mode` | `"dict"` / `"text_fallback"` / `"docx_style"` / `"xlsx"` / `"csv"` | 解析模式来源 |

**PDF Section 匹配适配**：
原 `_build_regex_sections` 同时用 `_is_heading()` 正则 + `is_title` 布局信号。发现 dict 模式每行一个 Block，`_is_heading()` 的 `num_dot1` / `cn_num` 等模式会误匹配列表项（如 "1. "），导致 386 sections（text 模式下仅 162）。修复：dict 模式（有布局信息时）仅信任 `is_title`，关闭正则标题。

---

## 第2层：step2_embed.py — 切分 + 向量化

### page_content 组装

**旧方案**：文件名推断的三元组 header
```python
header = f"[{province} {crop} {zoning_type}] "
page_content = header + c["content"]
# "[黑龙江 大豆 冷害风险区划] ## 一、数据来源\n\n正文..."
```

**v1.7 方案**：heading_path 替代 header（**已弃用 — 见 v1.8**）
```python
heading_path = m.get("heading_path", [])
path_str = " > ".join(heading_path) if heading_path else ""
page_content = path_str + "\n\n" + c["content"]
# "黑龙江省大豆冷害气候风险区划 > 一、数据来源与处理\n\n正文..."
```

**v1.8 方案**：compact header + heading_path 共存
```python
compact = f"[{province} {crop} {zoning_type}] "  # 三元组关键词锚点
path_str = " > ".join(heading_path) if heading_path else ""
page_content = compact + path_str + "\n\n" + c["content"]
# "[黑龙江 大豆 冷害风险区划] 黑龙江省大豆冷害气候风险区划 > 一、数据来源与处理\n\n正文..."
```

**v1.9 方案（当前）**：compact header only，heading_path 仅保留在 metadata
```python
compact = _compact_header(m)   # f"[{province} {crop} {zoning_type}] "
page_content = compact + c["content"] if compact else c["content"]
# "[黑龙江 大豆 冷害风险区划] 表2-1 陕西富士系苹果种植气候适宜性区划指标\n\n..."
```

**演进说明**：

v1.7 用 heading_path 替代三元组 header 后 MRR -11.2%（0.4958→0.4402）。根因：BGE embedding 对所有 token 平均池化，heading_path 的冗余词（"技术规范""初稿""3.3区化方法"）稀释了核心关键词的信号权重。PDF 文档 heading_path 仅为文件名（如 `D_P_R_150000_001-内蒙古区划报告`），信号更弱。

v1.8 修复：compact header 提供高密度关键词锚点 + heading_path 提供层级语义，MRR 恢复到 0.8333。

v1.9 精简：将 heading_path 从 page_content 移除，仅保留在 metadata 中。原因：heading_path 在 page_content 中的增量收益有限（compact header 已提供省份/作物/区划信号），且对表格型 chunk 会进一步稀释 compact header 的信号密度。当前 page_content = `[省份 作物 区划]` + 正文，heading_path 仅参与 metadata 驱动的 section 级上下文扩展。

### H3 回退切分（新增）

章节 >3000 字的，在进入 Recursive splitter 之前先按 H3 子标题切开：

```
原始 Section: heading_path = ["黑龙江", "区划指标"]
  content = 5000字，内含 H3 "2.1 冷害指数" / "2.2 阈值" / "2.3 分级标准"

↓ _split_by_h3()

Sub 1: heading_path = ["黑龙江", "区划指标", "2.1 冷害指数"]  (~1500字)
Sub 2: heading_path = ["黑龙江", "区划指标", "2.2 阈值确定"]  (~1800字)
Sub 3: heading_path = ["黑龙江", "区划指标", "2.3 分级标准"]  (~1700字)
```

每个子章节保持完整 heading_path 谱系，H3 标题不丢失。

### RecursiveCharacterTextSplitter 参数

| 参数 | 旧值 | 新值 | 说明 |
|------|------|------|------|
| chunk_size | 1000 | 800 | 中文技术文档更适合 ~800 字检索窗口 |
| chunk_overlap | 200 | 150 | 减少冗余，检索精度更高 |
| separators | 不变 | `["\n\n", "\n", "。", "；", "，", " ", ""]` | 句边界优先 |

### 表格线性化 + 行窗口拆分（step1，v1.15）

表格线性化从 step2 移回 step1 解析阶段，在去重和质量标记之后执行。所有 type=table 的 chunk 调用 `linearize()` 转为自然语言，>800 字的按句号边界拆分为行窗口子 chunk。

```
v1.13 (旧) : parse → pipe 原样保留 → step2 Phase 1.5 → 拆分判断 → 逐行线性化 (一行一 chunk)
v1.15 (新) : parse → pipe 原样保留 → 去重/质量标记 → linearize() → ≤800字行窗口拆分
```

**线性化效果示例**：

```
输入 pipe:
| 区划因子 | 适宜 | 最适宜 | 不适宜 |
| X1(℃)   | 8.5~12.5 | 9.5~11.5 | ≤2.9 |
| X2(mm)   | 501~800 | 600~750 | ≥1000 |

输出自然语言:
陕西苹果种植气候区划中，该表格为区划指标体系，列出X1、X2等2项因子的适宜、最适宜、不适宜分级阈值。X1℃：适宜 8.5至12.5，最适宜 9.5至11.5，不适宜 ≤2.9。X2mm：适宜 501至800，最适宜 600至750，不适宜 ≥1000。
```

**行窗口拆分**：线性化后 >800 字 → 以句号为分隔符，按 800 字窗口分组多行。每个子 chunk 保留上下文前缀。当前仅 1 表触发拆分（陕西苹果区划报告 s1，拆为 2 块）。

**linearize_chunks.py 废弃**：该脚本是 v1.13 架构下的独立后处理工具，v1.15 后不再使用。

### 切分决策

```
N 条 step1 chunk (已线性化表格)
    │
    ├── Phase 1: >3000 字 → H3 回退切分 (+116 子章节)
    │
    ├── Phase 2: RecursiveCharacterTextSplitter (所有 doc 统一)
    │     ├── ≤800 字 → 免切分
    │     └── >800 字 → 切分为 800 字子块
    │
    ├── Phase 2.5: 子块补回 compact header
    │
    └── Phase 3: 按 section_id 分组，分配 chunk_index / chunk_count
```

### Parent-Child → Section 上下文

**旧方案**：parent_content 存完整父文档

```python
d.metadata["parent_content"] = d.page_content  # 可能是 5000~10000 字
# 检索命中 → expand_parent → 返回完整万字章节（大量冗余）
```

**新方案**：chunk_index + chunk_count 替代 parent_content

```python
d.metadata["chunk_index"] = 0   # 第 0 / 3 块
d.metadata["chunk_count"] = 3
# 检索命中 → expand_context → 取同 section 的前后 ±1 chunks（固定窗口）
```

### 产出

ChromaDB `vectordb/`

| 指标 | v1.13 | v1.15 |
|------|------|------|
| 向量总数 | 822 | **787** |
| 免切分 | 606 | 525 |
| 被切分源 | 75 | 90 |
| 切分后子块 | 216 | 262 |
| H3 回退切分 | +116 子章节 | +116 子章节 |
| 表格线性化率 | 部分 (仅拆分的表) | **100%** |
| raw pipe 残留 | 有 | 0 |
| 平均 chunk 长度 | 361 字符 | 374 字符 |

---

## 第3层：BM25 索引 + Section 索引 (hybrid_search.py)

### 数据源

BM25 与 Chroma Dense 使用**同一套 chunks_split.json**（787 条），通过 `_load_chunks()` 并行加载：

```python
# Dense: ChromaDB 向量检索
self._vectorstore = Chroma(collection_name="agri_zoning", ...)

# BM25: 从 chunks_split.json 构建 (与 Dense 同源)
self._bm25_retriever = BM25Retriever.from_documents(
    bm25_docs, preprocess_func=bm25_tokenize
)
self._bm25_retriever.k = 20  # 实际检索返回数
```

### BM25 中文分词器

```python
_TECH_TOKEN_RE = re.compile(
    r'[A-Z]{2,}\d*'           # FCV, GIS, AHP, ET0, DEM
    r'|[Δ∇∂αβγ][A-Z]?\d*'    # ΔT5-9
    r'|[≥≤<>]\d+[℃°C%]?'     # ≥10℃, ≤5500
    r'|\d+~?\d*[℃°C%hm²]'    # 10℃, 30%, hm²
)

def bm25_tokenize(text):
    1. 专业词正则提取 → tech_tokens
    2. 单字 + 双字 bigram (中文无空格分词) → tokens
    3. 专业词去重追加
    return tokens
```

默认 `text.split()` 对中文完全失效（无空格）。单字+bigram 保证中文切分；正则保留专业术语不被拆散。

### 两个索引对比

| | Chroma (Dense) | BM25 |
|---|---|---|
| **数据源** | chunks_split.json (787 条向量化) | chunks_split.json (787 条) |
| **返回数** | pool_size=40 | k=20 (≤ pool_size) |
| **匹配方式** | 余弦相似度 (cosine, 512-dim) | TF-IDF 关键词精确匹配 |
| **元数据** | heading_path, section_id, chunk_index, chunk_count | 相同 |

### Section 索引

初始化时构建，供上下文扩展和评估使用：

```python
self._section_index = {
    "doc1_sec_0": [
        {"content": "...", "chunk_index": 0, "metadata": {...}},
        {"content": "...", "chunk_index": 1, "metadata": {...}},
    ],
}
```

---

## 第4层：检索管线详解 (hybrid_search.py)

### 整体架构：Union + Evidence Voting + Dynamic Coverage → CE Rerank

```
Query: "新疆冬小麦和河南冬小麦品种有什么不同？"
  │
  ├── [改写] expand_query → 三池产出
  │     Keywords (≤6):    "新疆", "冬小麦", "河南", "品种", "主栽品种"
  │     Rewrite  (≤2):    "新疆冬小麦与河南冬小麦品种对比"
  │     SubQuery (≤4):    "新疆冬小麦主栽品种有哪些", "河南冬小麦主栽品种有哪些"
  │
  ├── Phase 1: 并路采集 + Dict 去重 ─────────────────────────
  │
  │   ① Original (qid=0)
  │      Dense Top30 + BM25(Query+Keywords) Top20
  │      → 每路内部 RRF → dict 自然去重
  │
  │   ② Rewrite × N (qid=1..N, N≤2)   每路: Dense20+BM2510
  │   ③ SubQuery × M (qid=N+1.., M≤4)  每路: Dense20+BM2510
  │
  │   Global Merge — chunk_id 去重, 无截断
  │   每条候选:
  │   { chunk_id, text, sources: ["Original-Dense","SubQ2-BM25"],
  │     query_hits: {0,3}, dense_rank, bm25_rank,
  │     best_channel: "Dense", best_rank: 3,
  │     rrf_prior, cosine_sim }
  │
  ├── Phase 2: Evidence Voting + Retrieval Prior ────────────
  │
  │   Evidence (δ=0.002):
  │     len(query_hits)=1  →  0
  │                   =2  →  δ    (0.002)
  │                   ≥3  →  2δ   (0.004)
  │
  │   Retrieval Prior = 0.7 × RRF_norm + 0.3 × Evidence_norm
  │   → 按 Retrieval Prior 降序排列
  │
  ├── Phase 3: Dynamic Coverage Reservation (≤50) ───────────
  │
  │   ┌──────────┬────────┬──────────────────────────┐
  │   │ 来源      │  占比   │ 配额                     │
  │   ├──────────┼────────┼──────────────────────────┤
  │   │ Original  │  40%   │ 20 = 50×40%             │
  │   │ Rewrite   │  20%   │ 10 (N个共享)             │
  │   │ SubQuery  │  40%   │ 20 (M个均分)             │
  │   │ 候补      │   —    │ Global Fill 补齐到 50    │
  │   └──────────┴────────┴──────────────────────────┘
  │
  │   SubQ 均分: 2个→[10,10]  3个→[7,7,6]  4个→[5,5,5,5]
  │   优先级: Original → SubQ₁ → SubQ₂ → ... → Rewrite → 候补
  │   候补: 未入配额的候选按 Retrieval Prior 竞争剩余名额
  │
  ├── Phase 4: CE Rerank + Prior 融合 ──────────────────────
  │
  │   候选池 ≤50 → CrossEncoder (bge-reranker-v2-m3)
  │   Query = Original Query (仅用原始查询文本)
  │   CE 长度归一化: raw - 0.1×log(len(content))
  │
  │   Final = 0.8 × CE_norm + 0.2 × Retrieval_Prior
  │   (Retrieval_Prior 已在 [0,1], 不二次归一化)
  │
  ├── Top K (默认 10)
  │
  └── Context Expansion: ±1 chunk (同 section)
```

### 数量流追踪

```
Query: "新疆冬小麦和河南冬小麦品种有什么不同？"
  │
  ├── [改写] Rewrite ×1 + SubQuery ×2 + Keyword ×5
  │
  ├── Phase 1 并路采集 ─────────────────────────────────────
  │   Original:            Dense 30 + BM25(keyword增强) 20 → 自然去重
  │   Rewrite ×1:          Dense 20 + BM25 10              → 自然去重
  │   SubQuery ×2:         每路 Dense 20 + BM25 10         → 自然去重
  │   Keywords:            不独立检索, 拼入 Original BM25
  │
  ├── Global Merge: chunk_id 去重, 无截断
  │
  ├── Phase 2: Evidence Voting + Retrieval Prior 排序
  │
  ├── Phase 3: Dynamic Coverage → 50
  │     Original=20, Rewrite=10, SubQ: 2个→[10,10]
  │     候补补齐剩余 → 50
  │
  ├── Phase 4: CE Rerank: 50 → Top 10
  │
  ▼
最终 10 条
```

| 环节 | 数量 | 说明 |
|------|------|------|
| Original Dense | **30** | 原始查询语义最准，最大召回 |
| Original BM25 | **20** | Keywords 拼入 query 增强关键词召回 |
| Rewrite Dense | **20** | ≤2 路 |
| Rewrite BM25 | **10** | ≤2 路 |
| SubQ Dense | **20** | ≤4 路 |
| SubQ BM25 | **10** | ≤4 路 |
| Global Merge | **自然去重** | 无截断, 无人工上限 |
| CE 候选池 | **≤50** | Dynamic Coverage + Global Fill |
| CE 输出 | **10** | top_k |
| 最终返回 | **10** | ±1 chunk 上下文扩展 |

### 多子查询融合策略演进

```
v1.18 Append:
  Original(CE) → [0..9] | Rewrite1(RRF) → [10..19] | Rewrite2(RRF) → [20..29]
  → [:10] 截断 → 只有 Original, Rewrite 全部白算

v1.19 Evidence Merge:
  Original(CE) ─┐
  SubQ1(RRF)   ─┼── Evidence Merge (去重/Hit Boost) → Global Pool 30~50
  SubQ2(RRF)   ─┘                                        │
                                                    CE Rerank → Top 10

v2.0 Union + Dynamic Coverage (当前):
  各路 Dense+BM25→RRF → Union 自然去重 (无截断)
    → Retrieval Prior (70%RRF + 30%Evidence) 排序
    → Dynamic Coverage: Original 40% + Rewrite 20% + SubQ 40%
    → Global Fill 补齐 50 → CE Rerank → Top 10
  Keywords 不再独立检索, 拼入 Original BM25 增强
```

### 关键参数总表

| 参数 | 值 | 说明 |
|------|-----|------|
| Original Dense | **30** | 原始查询语义最准 |
| Original BM25 | **20** | Keyword 增强 |
| SubQ Dense (Rewrite/SubQ) | **20** | |
| SubQ BM25 (Rewrite/SubQ) | **10** | |
| RRF_K | **60** | RRF 平滑常数 |
| w_dense / w_bm25 | **0.7 / 0.3** | RRF 通道权重 |
| Evidence δ | **0.002** | 2路=δ, ≥3路=2δ |
| Retrieval Prior | **0.7×RRF + 0.3×Ev** | Phase 2 融合 |
| CE 候选池 (MAX_POOL) | **50** | Dynamic Coverage 截断 |
| Coverage 配比 | **40%/20%/40%** | Orig=20, RW=10, SubQ均分20 |
| alpha (CE fusion) | **0.2** | 20% Prior + 80% CE |
| lambda_length | **0.1** | CE 长度归一化 |
| CE Query | **Original Query** | 最终要回答的是原始问题 |

### 改写触发逻辑

```
query → search(top_k=2) → top1_sim, top2_sim
  → expand_query(mode="all", top1_sim, top2_sim)
    → 内置 gate: length≤6 或 (top1_sim≥0.72 且 margin≥0.03 且无口语词) → 跳过 LLM
    → 否则 → LLM 改写 → 三池产出
```

### expand_query 三池输出

```
┌─ Keyword 池 ────────────────────────────────────────────────┐
│ 目的: 拼入 Original BM25 query，增强关键词召回               │
│ 来源: 术语映射 + 同义词扩展 + LLM keywords                     │
│ 形式: 词/短语                                                │
│ 限制: ≤6 (hard 6)                                           │
│ 检索: 不独立检索，拼入 Original 的 BM25 query 文本            │
└──────────────────────────────────────────────────────────────┘

┌─ Rewrite Query 池 ──────────────────────────────────────────┐
│ 目的: 用不同表达重述同一问题，增强 Dense+BM25 双通道             │
│ 来源: LLM rewrite_queries                                    │
│ 形式: 完整句子                                                │
│ 限制: ≤2 (hard 2)                                           │
│ 检索: 每路 Dense20 + BM2510 → RRF                            │
└──────────────────────────────────────────────────────────────┘

┌─ SubQuery 池 ───────────────────────────────────────────────┐
│ 目的: 拆分复杂问题为独立子问题，支持并行/多跳检索                │
│ 来源: LLM sub_queries                                        │
│ 形式: 完整句子                                                │
│ 限制: ≤4 (hard 4)                                           │
│ 检索: 每路 Dense20 + BM2510 → RRF                            │
└──────────────────────────────────────────────────────────────┘
```

| 池 | 来源 | 形式 | 检索方式 | Target | Hard Limit |
|----|------|------|----------|:--:|:--:|
| Keyword | 术语映射 + 同义词 + LLM keywords | 词/短语 | 拼入 Original BM25 | ≤6 | **6** |
| Rewrite Query | LLM rewrite_queries | 完整句子 | Dense20+BM2510→RRF | ≤2 | **2** |
| SubQuery | LLM sub_queries | 完整句子 | Dense20+BM2510→RRF | ≤4 | **4** |

### BM25 中文分词

```
bm25_tokenize(text):
  1. 专业词正则提取: FCV, GIS, ΔT5-9, ≥10℃, 5-9月, 10℃, 30% 等
  2. 单字 + bigram (中文无空格分词)
  3. 专业词去重追加
  → langChain BM25Retriever 使用此分词器 (preprocess_func)
```

### RRF Single-Channel Boost

```python
# 仅单通道命中的 key，补偿缺失通道的权重
for key in rrf_scores:
    if in_dense and not in_bm25:
        rrf_scores[key] /= 0.7   # 升权：Dense 独有
    elif in_bm25 and not in_dense:
        rrf_scores[key] /= 0.3   # 升权：BM25 独有
```

防止某些结果因为只在一个通道出现而被 RRF 过度惩罚。

### 评估指标 (eval_v2_full.py)

```
Chunk 级 (基础):
  chunk_ids = [result.metadata.chunk_id for result in results[:10]]
  recall_10 = any(cid in gold_chunks for cid in chunk_ids[:10])
  → 直接比对检索结果的 chunk_id 是否命中标注的 gold_chunk_id

Section 级 (辅助):
  section_ids = [result.metadata.section_id for result in results[:10]]
  sec_recall_10 = any(sid in gold_sections for sid in section_ids[:10])
  → 命中 gold chunk 所在 section 即算成功 (比 chunk 级宽松)
```

### OOD Judge

```
top1_sim ≥ 0.70 → direct → Answer
top1_sim < 0.70 → LLM 判定 → Answer / Reject
```

### Similarity 字段说明

| 字段 | 含义 | 用途 |
|------|------|------|
| `similarity` | 排序分（有 reranker 时为 rerank_score，无时为 RRF score 或余弦相似度） | 返回排序 |
| `dense_similarity` | Chroma 真实余弦相似度 | Judge OOD 判定 |

---

## 数值总结

| 阶段 | 产物 | v1.11 | v1.13 | v1.15 |
|------|------|------|------|------|
| step1 解析 | chunks.json 总条数 | 516 | 513 | 514 |
| step1 表格 | 线性化 | — | — (在 step2) | 85/245 (35%) |
| step1 表格 | 拆分 (≤800字行窗口) | — | — | 1 表→2 块 |
| step2 H3 回退 | 大章节切分 | +62 | +116 | +116 |
| step2 免切分 | ≤800字 | 818 | 606 | 525 |
| step2 切分 | >800字 → 子块 | 83→235 | 75→216 | 90→262 |
| step2 表格专用代码 | Phase 1.5 | — | ~85 行 | 0 |
| step2 总计 | vectordb 向量 | 1061 | 822 | 787 |
| 表格线性化率 | final chunks | — | 部分 | **100%** |
| raw pipe 残留 | final chunks | — | 有 | 0 |
| 平均 chunk 长度 | — | 547 | 361 | 374 |

---

> **关联文件**: step1_parse.py, step2_embed.py, hybrid_search.py, evaluate.py, rag_pipeline.py
