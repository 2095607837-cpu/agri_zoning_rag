"""
农业区划数据解析器
解析 DOCX（技术规范）/ PDF（区划报告）/ XLSX（指标数据）/ CSV（台站数据）
输出统一 chunk JSON → data/chunks.json

统一架构: Parser → List[Block] → Heading Detect → Section → Chunk

用法:
  python3 step1_parse.py
  DATA_SRC=/path/to/source python3 step1_parse.py  # 指定数据源目录
"""

import json
import os
import re
import csv
import hashlib
from dataclasses import dataclass
from typing import Optional
from pathlib import Path
from collections import defaultdict

BASE_DIR = Path(__file__).resolve().parent
DATA_SRC = Path(os.environ.get("DATA_SRC", "/Users/han/大模型自研代码/农业区划算法"))
OUTPUT = BASE_DIR / "data" / "chunks.json"

SKIP_DIRS = [
    "黑龙江农业气候资源普查和大豆区划规范-算法提交",
]
SKIP_NESTED = re.compile(r"05江西/05江西")
CSV_ENCODINGS = ["utf-8", "gbk", "gb2312", "gb18030", "latin-1"]


# ═══════════════════════════════════════════════════════════
#  Unified Block & Heading Pipeline
# ═══════════════════════════════════════════════════════════

@dataclass
class Block:
    """统一文本块，Parser 无关。DOCX 带 style，PDF style 为 None。"""
    text: str
    style: Optional[str] = None    # DOCX: "Heading 1"/"Heading 2"; PDF: None
    page: Optional[int] = None     # PDF 页码
    source: str = ""               # 源文件名（用于调试）


# 多 Regex 标题模式列表（按优先级从高到低）
_TIER2_PATTERNS = [
    (re.compile(r'^第[一二三四五六七八九十\d]+章[\s　]'),                                         'chapter'),   # 第6章 / 第一章
    (re.compile(r'^第[一二三四五六七八九十\d]+\s+章'),                                            'chapter_sp'), # 第6 章（数字章间有空格）
    (re.compile(r'^第[一二三四五六七八九十\d]+节[\s　]'),                                         'section'),   # 第2节
    (re.compile(r'^[一二三四五六七八九十]+[、．.\s]'),                                          'cn_num'),     # 一、二、
    (re.compile(r'^（[一二三四五六七八九十]+）'),                                               'cn_paren'),   # （一）（二）
    (re.compile(r'^\d+\.\d+\.\d+(?:\.\d+)?[\s　]'),                                            'num_dot3'),   # 5.2.4 / 2.3.1.1
    (re.compile(r'^\d+\.\d+[\s　]'),                                                            'num_dot2'),   # 5.2 / 6.1
    (re.compile(r'^\d+[\.\、][\s　]'),                                                          'num_dot1'),   # 1. / 3、
    (re.compile(r'^（\d+）'),                                                                    'digit_paren'),# （1）（2）
    (re.compile(r'^[\(（]\d+[\)）][\s　]'),                                                     'digit_paren2'),# (1) / 1)
    (re.compile(r'^附录[一二三四五六七八九十A-Z]?'),                                            'appendix'),   # 附录A / 附录一
    (re.compile(r'^[A-Z]\.\s'),                                                                  'letter_dot'), # A. / B.
]


def _is_heading(text: str) -> bool:
    """多 Regex 列表逐个匹配，任意命中即为标题行。"""
    text = text.strip()
    if not text:
        return False
    for pattern, _ in _TIER2_PATTERNS:
        if pattern.match(text):
            return True
    return False


def _heading_level_from_style(style: Optional[str]) -> int:
    """从段落样式名提取标题级别，非标题返回 0。"""
    if not style:
        return 0
    m = re.search(r'(?:heading|标题|TOC)\s*(\d+)', style, re.IGNORECASE)
    if m:
        return int(m.group(1))
    if re.search(r'heading|标题', style, re.IGNORECASE):
        return 1
    return 0


# ── Section Builder ────────────────────────────────────

def _build_sections(blocks: list[Block]) -> list[dict]:
    """统一 Section 构建管道。
    Tier 1: 样式标题（DOCX 有 style 的 block）
    Tier 2: 多 Regex 标题识别（所有 block）
    Tier 3: 固定长度切分（兜底）
    """
    # Tier 1: 尝试样式标题
    style_sections = _build_style_sections(blocks)
    if len(style_sections) >= 2:
        return style_sections

    # Tier 2: 多 Regex 标题识别
    regex_sections = _build_regex_sections(blocks)
    if len(regex_sections) >= 2:
        return regex_sections

    # Tier 3: 固定长度切分（~800 字，句边界对齐）
    return _build_length_sections(blocks)


def _build_style_sections(blocks: list[Block]) -> list[dict]:
    """Tier 1: 基于 DOCX 样式标题构建 Section。"""
    h1_title = ""
    sections = []
    current = {"heading_path": [], "blocks": []}

    for b in blocks:
        level = _heading_level_from_style(b.style)
        text = b.text

        if level == 1:
            h1_title = text
        elif level == 2:
            if current["blocks"]:
                sections.append(current)
            path = [h1_title, text] if h1_title else [text]
            current = {"heading_path": path, "blocks": []}
        elif level >= 3:
            prefix = "#" * min(level, 4)
            current["blocks"].append(f"{prefix} {text}")
        else:
            current["blocks"].append(text)

    if current["blocks"]:
        sections.append(current)
    return sections


def _build_regex_sections(blocks: list[Block]) -> list[dict]:
    """Tier 2: 多 Regex 匹配标题行构建 Section。"""
    sections = []
    current = {"heading_path": [], "blocks": []}

    for b in blocks:
        text = b.text.strip()
        if not text:
            continue
        if _is_heading(text):
            if current["blocks"]:
                sections.append(current)
            current = {"heading_path": [text], "blocks": []}
        else:
            current["blocks"].append(text)

    if current["blocks"]:
        sections.append(current)
    return sections


def _build_length_sections(blocks: list[Block]) -> list[dict]:
    """Tier 3: 固定长度切分（兜底）。"""
    all_text = "\n".join(b.text for b in blocks if b.text.strip())
    parts = _split_by_sentence(all_text, 800)
    return [{"heading_path": [""], "blocks": [p]} for p in parts]


def _split_by_sentence(text: str, max_chars: int = 800) -> list[str]:
    """按句边界切分，每段 ≤ max_chars。"""
    sentences = re.split(r'(?<=[。！？\n])\s*', text)
    chunks = []
    current = ""
    for s in sentences:
        if not s.strip():
            continue
        if len(current) + len(s) > max_chars and current:
            chunks.append(current.strip())
            current = s
        else:
            current += s
    if current.strip():
        chunks.append(current.strip())
    return chunks or [text]


# ── Chunk 生成 ─────────────────────────────────────────

def _sections_to_chunks(sections: list[dict], doc_stem: str, fname: str,
                         province: str, crop: str, zoning: str,
                         source_type: str = "technical_spec") -> list[dict]:
    """将 Section 列表转为最终 chunk 列表。每个 Section 生成一个 chunk。"""
    chunks = []
    for si, sec in enumerate(sections):
        full_text = "\n\n".join(sec.get("blocks", [])).strip()
        if len(full_text) < 20:
            continue

        heading_path = sec.get("heading_path") or [doc_stem]
        section_id = f"{doc_stem}_sec_{si}"
        chunks.append({
            "id": f"{doc_stem}_s{si}",
            "content": full_text,
            "metadata": {
                "doc_id": doc_stem,
                "section_id": section_id,
                "heading_path": heading_path,
                "heading_level": len(heading_path),
                "source_type": source_type,
                "source_file": fname,
                "province": province,
                "crop": crop,
                "zoning_type": zoning,
                "section_title": heading_path[-1] if heading_path else "",
            },
        })
    return chunks


# ═══════════════════════════════════════════════════════════
#  DOCX Parser
# ═══════════════════════════════════════════════════════════

def parse_docx(filepath: str) -> list[dict]:
    """解析 DOCX：提取段落 → Block 列表 → 统一 Section 管道。"""
    try:
        import docx
    except ImportError:
        print(f"  [!] python-docx 未安装，跳过: {filepath}")
        return []

    try:
        doc = docx.Document(filepath)
    except Exception as e:
        print(f"  [!] 无法打开 {filepath}: {e}")
        return []

    fname = Path(filepath).name
    doc_stem = Path(filepath).stem
    province, crop, zoning = _infer_meta(fname)

    # 提取段落 → Block 列表
    blocks = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = para.style.name if para.style else ""
        blocks.append(Block(text=text, style=style, source=fname))

    # 统一 Section 管道
    sections = _build_sections(blocks)
    chunks = _sections_to_chunks(sections, doc_stem, fname, province, crop, zoning,
                                  source_type="technical_spec")

    # 分配表格到最近的 section
    table_chunks = _extract_tables(doc, doc_stem, fname, province, crop, zoning)
    chunks.extend(table_chunks)

    return chunks


def _extract_tables(doc, doc_stem: str, fname: str,
                    province: str, crop: str, zoning: str) -> list[dict]:
    """提取 DOCX 表格为独立 chunk。"""
    chunks = []
    for i, table in enumerate(doc.tables):
        table_text = _table_to_markdown(table)
        if len(table_text) < 30:
            continue
        section_id = f"{doc_stem}_table_{i}"
        chunks.append({
            "id": f"{doc_stem}_t{i}",
            "content": table_text,
            "metadata": {
                "doc_id": doc_stem,
                "section_id": section_id,
                "heading_path": [f"表格{i+1}"],
                "heading_level": 1,
                "source_type": "technical_spec",
                "source_file": fname,
                "province": province,
                "crop": crop,
                "zoning_type": zoning,
                "section_title": f"表格{i+1}",
            },
        })
    return chunks


def _table_to_markdown(table) -> str:
    rows = []
    for row in table.rows:
        cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
        rows.append("| " + " | ".join(cells) + " |")
    if not rows:
        return ""
    if len(rows) > 1:
        header_sep = "|" + "|".join(["---"] * len(rows[0].split("|"))) + "|"
        rows.insert(1, header_sep)
    return "\n".join(rows)


# ═══════════════════════════════════════════════════════════
#  PDF Parser (全文拼接 + 多 Regex Heading)
# ═══════════════════════════════════════════════════════════

def parse_pdf(filepath: str) -> list[dict]:
    """解析 PDF：全文拼接 → 段落切分 → Block 列表 → 统一 Section 管道。"""
    try:
        import fitz
    except ImportError:
        print(f"  [!] pymupdf 未安装，跳过: {filepath}")
        return []

    fname = Path(filepath).name
    doc_stem = Path(filepath).stem
    province, crop, zoning = _infer_meta(fname)

    try:
        doc = fitz.open(filepath)
    except Exception as e:
        print(f"  [!] 无法打开 PDF {filepath}: {e}")
        return []

    # Step 1: 提取所有页面文本 → 跳过封面和目录页 → 拼接全文
    all_text_parts = []
    toc_pages = set()

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text().strip()
        if len(text) < 50:
            continue
        # 检测目录页：包含大量 "....." 或罗马数字
        if _is_toc_page(text):
            toc_pages.add(page_num)
            continue
        all_text_parts.append(text)

    doc.close()

    if not all_text_parts:
        return []

    full_text = "\n".join(all_text_parts)

    # Step 2: 清洗后按行切分 → 每行独立检测标题
    full_text = _clean_pdf_text(full_text)
    blocks = _lines_to_blocks(full_text, fname)

    # Step 4: 统一 Section 管道
    sections = _build_sections(blocks)
    return _sections_to_chunks(sections, doc_stem, fname, province, crop, zoning,
                                source_type="report")


def _is_toc_page(text: str) -> bool:
    """检测是否为目录页。"""
    lines = text.split("\n")
    dot_lines = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 目录行特征: 标题 ... 页码 或 罗马数字章节头
        if "...." in line or "……" in line:
            dot_lines += 1
        if re.match(r'^[IVX]+\s*$', line):
            return True
    # 超过 40% 的行含省略号 → 目录页
    return dot_lines > len(lines) * 0.4 if lines else False


def _clean_pdf_text(text: str) -> str:
    """清洗 PDF 全文：去页眉页脚，保留段落结构（不合并行内换行）。"""
    # 移除罗马数字独立行（页眉页脚）
    text = re.sub(r'\n[IVX]+\n', '\n', text)
    # 移除纯数字独立行（页码）
    text = re.sub(r'\n\d{1,3}\n', '\n', text)
    # 压缩三行以上空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def _lines_to_blocks(text: str, source: str) -> list[Block]:
    """将文本按段落边界转为 Block 列表，标题行独立，正文行合并。
    策略：
      1. 按双换行切段落
      2. 段落内按单换行拆行
      3. 标题行 → 独立 Block
      4. 连续非标题行 → 合并为一个 Block
    """
    blocks = []
    paragraphs = text.split("\n\n")

    for para in paragraphs:
        lines = para.strip().split("\n")
        if not lines:
            continue

        body_buf = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                body_buf.append("")
                continue

            if _is_heading(stripped):
                # 先刷出积攒的正文
                if body_buf:
                    merged = "".join(body_buf).strip()
                    if len(merged) >= 20:
                        blocks.append(Block(text=merged, source=source))
                    body_buf = []
                # 标题独立成块
                blocks.append(Block(text=stripped, source=source))
            else:
                body_buf.append(stripped)

        if body_buf:
            merged = "".join(body_buf).strip()
            if len(merged) >= 20:
                blocks.append(Block(text=merged, source=source))

    return blocks


# ═══════════════════════════════════════════════════════════
#  XLSX Parser（不变）
# ═══════════════════════════════════════════════════════════

def parse_xlsx(filepath: str) -> list[dict]:
    try:
        import openpyxl
    except ImportError:
        print(f"  [!] openpyxl 未安装，跳过: {filepath}")
        return []

    chunks = []
    fname = Path(filepath).name
    doc_stem = Path(filepath).stem
    province, crop, zoning = _infer_meta(fname)

    try:
        wb = openpyxl.load_workbook(filepath, data_only=True)
    except Exception as e:
        print(f"  [!] 无法打开 XLSX {filepath}: {e}")
        return []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue

        headers = [str(h) if h else "" for h in rows[0]]
        data_rows = rows[1:]
        data_rows = [r for r in data_rows if any(c is not None for c in r)]

        if not data_rows:
            continue

        text_parts = [
            f"表格: {sheet_name}",
            f"列名: {', '.join(h for h in headers if h)}",
            f"数据行数: {len(data_rows)}",
            "",
        ]

        numeric_cols = {}
        for ci, h in enumerate(headers):
            vals = []
            for r in data_rows:
                v = r[ci] if ci < len(r) else None
                if isinstance(v, (int, float)):
                    vals.append(v)
            if vals:
                numeric_cols[h] = vals

        if numeric_cols:
            text_parts.append("数值列统计:")
            for col_name, vals in numeric_cols.items():
                text_parts.append(
                    f"  {col_name}: 最小值={min(vals):.4f}, 最大值={max(vals):.4f}, "
                    f"平均值={sum(vals)/len(vals):.4f}"
                )

        text_parts.append("\n前5行数据示例:")
        for ri, row in enumerate(data_rows[:5]):
            row_str = " | ".join(str(c) if c is not None else "" for c in row)
            text_parts.append(f"  行{ri + 2}: {row_str}")

        full_text = "\n".join(text_parts)
        section_id = f"{doc_stem}_{sheet_name}"
        chunks.append({
            "id": f"{doc_stem}_{sheet_name}",
            "content": full_text,
            "metadata": {
                "doc_id": doc_stem,
                "section_id": section_id,
                "heading_path": [sheet_name],
                "heading_level": 1,
                "source_type": "data_table",
                "source_file": fname,
                "province": province,
                "crop": crop,
                "zoning_type": zoning,
                "sheet_name": sheet_name,
                "section_title": sheet_name,
            },
        })

    wb.close()
    return chunks


# ═══════════════════════════════════════════════════════════
#  CSV Parser（不变）
# ═══════════════════════════════════════════════════════════

def parse_csv(filepath: str) -> list[dict]:
    fname = Path(filepath).name
    doc_stem = Path(filepath).stem
    province, crop, zoning = _infer_meta(fname)

    rows = None
    for enc in CSV_ENCODINGS:
        try:
            with open(filepath, "r", encoding=enc) as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
        except Exception:
            break

    if rows is None:
        print(f"  [!] 无法解码 CSV {filepath}，已尝试编码: {CSV_ENCODINGS}")
        return []

    if not rows:
        return []

    headers = list(rows[0].keys())
    field_info = "\n".join([f"- {h}" for h in headers])

    numeric_stats = []
    for h in headers:
        try:
            vals = [float(r[h]) for r in rows if r[h] and r[h].strip()]
            if vals:
                numeric_stats.append(
                    f"  {h}: 范围 [{min(vals):.2f}, {max(vals):.2f}], "
                    f"均值 {sum(vals)/len(vals):.2f}, 共 {len(vals)} 条"
                )
        except ValueError:
            pass

    text_parts = [
        f"CSV 数据文件: {fname}",
        f"总行数: {len(rows)}",
        f"\n字段列表:\n{field_info}",
    ]
    if numeric_stats:
        text_parts.append(f"\n数值字段统计:")
        text_parts.extend(numeric_stats)

    text_parts.append(f"\n前5行数据示例:")
    for ri, row in enumerate(rows[:5]):
        items = [f"{k}={v}" for k, v in row.items()]
        text_parts.append(f"  [{ri + 1}] {', '.join(items)}")

    return [{
        "id": doc_stem,
        "content": "\n".join(text_parts),
        "metadata": {
            "doc_id": doc_stem,
            "section_id": doc_stem,
            "heading_path": [fname],
            "heading_level": 1,
            "source_type": "data_table",
            "source_file": fname,
            "province": province,
            "crop": crop,
            "zoning_type": zoning,
            "section_title": fname,
        },
    }]


# ═══════════════════════════════════════════════════════════
#  元数据推断（不变）
# ═══════════════════════════════════════════════════════════

PROVINCE_MAP = {
    "内蒙古": "内蒙古", "黑龙江": "黑龙江", "河南": "河南",
    "江西": "江西", "陕西": "陕西", "新疆": "新疆",
    "辽宁": "辽宁", "气科院": "全国",
}

CROP_MAP = {
    "大豆": "大豆", "小麦": "冬小麦", "冬小麦": "冬小麦",
    "柑橘": "柑橘", "苹果": "苹果",
    "猕猴桃": "猕猴桃", "桃": "桃", "梨": "梨",
    "品质": None, "病害": None, "区划": None,
}

ZONING_MAP = {
    "产量": "产量区划", "品质": "品质区划", "种植": "种植区划",
    "干旱": "干旱风险区划", "冷害": "冷害风险区划",
    "渍涝": "渍涝风险区划", "霜冻": "霜冻风险区划",
    "病虫害": "病虫害风险区划", "食心虫": "病虫害风险区划",
    "气候区划": "气候区划", "气候风险": "气候风险区划",
    "算法": "算法说明", "技术规范": "技术规范",
    "普查": "资源普查", "指标": "区划指标",
}


def _infer_meta(filename: str):
    province, crop, zoning = "", "", ""
    for key, val in PROVINCE_MAP.items():
        if key in filename:
            province = val
            break
    for key, val in CROP_MAP.items():
        if key in filename:
            crop = val or crop
            break
    for key, val in ZONING_MAP.items():
        if key in filename:
            zoning = val
            break
    if not zoning:
        zoning = "农业气候区划"
    return province, crop, zoning


# ═══════════════════════════════════════════════════════════
#  质量标记 & 去重（不变）
# ═══════════════════════════════════════════════════════════

def _tag_chunk_quality(chunks: list[dict]) -> list[dict]:
    low_patterns = [
        (r'^[#\s]*(目\s*录|Contents|Table of Contents)', '目录'),
        (r'^[#\s]*(参考文献|References|参考书目)', '参考文献'),
    ]
    for c in chunks:
        content = c['content'].strip()
        reasons = []
        if len(content) < 50:
            reasons.append('极短(<50字)')
        for pat, label in low_patterns:
            if re.match(pat, content):
                reasons.append(label)
        c['quality'] = 'low' if reasons else 'high'
        c['quality_reason'] = '; '.join(reasons) if reasons else ''
    low_count = sum(1 for c in chunks if c['quality'] == 'low')
    print(f"\n  质量标记: high={len(chunks)-low_count} low={low_count}")
    return chunks


def _dedup_documents(chunks: list[dict]) -> list[dict]:
    groups = defaultdict(lambda: {'docx': defaultdict(list), 'pdf': defaultdict(list)})
    for i, c in enumerate(chunks):
        meta = c['metadata']
        key = (meta.get('province', ''), meta.get('crop', ''), meta.get('zoning_type', ''))
        src = meta.get('source_file', '')
        if src.endswith('.docx'):
            groups[key]['docx'][src].append(i)
        elif src.endswith('.pdf'):
            groups[key]['pdf'][src].append(i)

    excluded = set()
    for group_key, data in groups.items():
        if not data['docx'] or not data['pdf']:
            continue
        for docx_src, docx_idxs in data['docx'].items():
            for pdf_src, pdf_idxs in data['pdf'].items():
                docx_total = sum(len(chunks[i]['content']) for i in docx_idxs)
                pdf_total = sum(len(chunks[i]['content']) for i in pdf_idxs)
                ratio = max(docx_total, pdf_total) / min(docx_total, pdf_total)
                if ratio > 1.15:
                    if docx_total > pdf_total:
                        excluded.update(pdf_idxs)
                        print(f"  文档去重: 保留 DOCX ({docx_total}字) 排除 PDF ({pdf_total}字) [{group_key}]")
                    else:
                        excluded.update(docx_idxs)
                        print(f"  文档去重: 保留 PDF ({pdf_total}字) 排除 DOCX ({docx_total}字) [{group_key}]")
                else:
                    print(f"  文档去重: 内容量相当(ratio={ratio:.2f}) 都保留 [{group_key}]")

    if excluded:
        for i in excluded:
            chunks[i]['excluded'] = True
        print(f"\n  文档去重: 排除 {len(excluded)} 个 chunk")
    return chunks


# ═══════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════

def parse_all(src_dir: str) -> list[dict]:
    all_chunks = []
    src_path = Path(src_dir)

    for f in sorted(src_path.rglob("*")):
        if not f.is_file():
            continue

        fstr = str(f)
        skip = False
        for sd in SKIP_DIRS:
            if sd in fstr:
                skip = True
                break
        if SKIP_NESTED.search(fstr):
            skip = True
        if skip:
            continue

        suffix = f.suffix.lower()
        fname = f.name

        if suffix == ".docx":
            print(f"  [DOCX] {fname}")
            chunks = parse_docx(str(f))
        elif suffix == ".pdf":
            size_mb = f.stat().st_size / 1024 / 1024
            if size_mb > 50:
                print(f"  [PDF] {fname} ({size_mb:.0f}MB) — 跳过（过大）")
                continue
            print(f"  [PDF] {fname} ({size_mb:.1f}MB)")
            chunks = parse_pdf(str(f))
        elif suffix == ".xlsx":
            print(f"  [XLSX] {fname}")
            chunks = parse_xlsx(str(f))
        elif suffix == ".csv":
            print(f"  [CSV] {fname}")
            chunks = parse_csv(str(f))
        else:
            continue

        all_chunks.extend(chunks)
        if chunks:
            print(f"         → {len(chunks)} chunks")

    return all_chunks


def main():
    print(f"数据源: {DATA_SRC}")
    print(f"输出: {OUTPUT}")
    print()

    raw_chunks = parse_all(str(DATA_SRC))

    # 第1轮：按 id 去重
    seen_ids = set()
    id_deduped = []
    for c in raw_chunks:
        if c["id"] not in seen_ids:
            seen_ids.add(c["id"])
            id_deduped.append(c)

    # 第2轮：按内容哈希去重
    seen_hashes = set()
    deduped = []
    for c in id_deduped:
        content_hash = hashlib.md5(c["content"][:200].encode()).hexdigest()
        if content_hash not in seen_hashes:
            seen_hashes.add(content_hash)
            c["content_hash"] = content_hash
            deduped.append(c)

    dup_count = len(raw_chunks) - len(id_deduped)
    content_dup = len(id_deduped) - len(deduped)
    if dup_count:
        print(f"\n  已去除 {dup_count} 个 ID 重复 chunk")
    if content_dup:
        print(f"  已去除 {content_dup} 个内容重复 chunk")

    deduped = _dedup_documents(deduped)
    deduped = _tag_chunk_quality(deduped)

    source_types = {}
    provinces = {}
    for c in deduped:
        st = c["metadata"].get("source_type", "unknown")
        source_types[st] = source_types.get(st, 0) + 1
        p = c["metadata"].get("province", "unknown")
        provinces[p] = provinces.get(p, 0) + 1

    print(f"\n{'=' * 60}")
    print(f"  解析完成")
    print(f"{'=' * 60}")
    print(f"  总 chunks: {len(deduped)}")
    print(f"  按类型: {json.dumps(source_types, ensure_ascii=False)}")
    print(f"  按省份: {json.dumps(provinces, ensure_ascii=False)}")

    avg_len = sum(len(c["content"]) for c in deduped) / len(deduped) if deduped else 0
    print(f"  平均 chunk 长度: {avg_len:.0f} 字符")

    os.makedirs(OUTPUT.parent, exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(deduped, f, ensure_ascii=False, indent=2)

    print(f"\n  已保存到: {OUTPUT}")


if __name__ == "__main__":
    main()
