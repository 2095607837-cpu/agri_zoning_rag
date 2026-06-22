"""
农业区划数据解析器
解析 DOCX（技术规范）/ PDF（区划报告）/ XLSX（指标数据）/ CSV（台站数据）
输出统一 chunk JSON → data/chunks.json

用法:
  python3 step1_parse.py
  DATA_SRC=/path/to/source python3 step1_parse.py  # 指定数据源目录
"""

import json
import os
import re
import csv
import hashlib
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_SRC = Path(os.environ.get("DATA_SRC", "/Users/han/大模型自研代码/农业区划算法"))
OUTPUT = BASE_DIR / "data" / "chunks.json"

# 重复目录（跳过，避免相同文件被解析两次）
SKIP_DIRS = [
    "黑龙江农业气候资源普查和大豆区划规范-算法提交",
]
# 嵌套重复：路径中包含 "05江西/05江西" 的跳过
SKIP_NESTED = re.compile(r"05江西/05江西")

# CSV 编码列表（按概率排序）
CSV_ENCODINGS = ["utf-8", "gbk", "gb2312", "gb18030", "latin-1"]


# ── DOCX 解析 ──────────────────────────────────────────

def parse_docx(filepath: str) -> list[dict]:
    """解析 DOCX 文件，提取段落文本 + 表格，按章节切分。"""
    try:
        import docx
    except ImportError:
        print(f"  [!] python-docx 未安装，跳过: {filepath}")
        return []

    chunks = []
    try:
        doc = docx.Document(filepath)
    except Exception as e:
        print(f"  [!] 无法打开 {filepath}: {e}")
        return []

    fname = Path(filepath).name
    province, crop, zoning = _infer_meta(fname)
    sections = []
    current_section = {"title": "", "paragraphs": [], "tables": []}

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = para.style.name if para.style else ""
        if "Heading" in style or "heading" in style or "标题" in style:
            if current_section["paragraphs"] or current_section["tables"]:
                sections.append(current_section)
            current_section = {"title": text, "paragraphs": [], "tables": []}
        else:
            current_section["paragraphs"].append(text)

    # 保存最后一个 section
    if current_section["paragraphs"] or current_section["tables"]:
        sections.append(current_section)

    # 提取表格
    for i, table in enumerate(doc.tables):
        table_text = _table_to_markdown(table)
        if sections:
            sections[min(i, len(sections) - 1)]["tables"].append(table_text)

    # 生成 chunks
    for si, sec in enumerate(sections):
        content_parts = []
        if sec["title"]:
            content_parts.append(f"## {sec['title']}")
        content_parts.extend(sec["paragraphs"])
        if sec["tables"]:
            content_parts.append("\n### 表格数据")
            content_parts.extend(sec["tables"])
        full_text = "\n\n".join(content_parts).strip()
        if len(full_text) < 20:
            continue

        chunk_id = f"{Path(filepath).stem}_s{si}"
        chunks.append({
            "id": chunk_id,
            "content": full_text,
            "metadata": {
                "source_type": "technical_spec",
                "source_file": fname,
                "province": province,
                "crop": crop,
                "zoning_type": zoning,
                "section_title": sec["title"],
            },
        })

    # 如果按章节切分后 chunk 太少（比如无标题的文档），按段落数切
    if len(chunks) < 2:
        return _fallback_chunk(filepath, fname, province, crop, zoning)

    return chunks


def _fallback_chunk(filepath: str, fname: str, province: str, crop: str, zoning: str) -> list[dict]:
    """无章节标题时的降级切分：每 3 段为一个 chunk。"""
    try:
        import docx
        doc = docx.Document(filepath)
    except Exception:
        return []

    all_text = []
    for para in doc.paragraphs:
        t = para.text.strip()
        if t:
            all_text.append(t)
    if not all_text:
        return []

    chunks = []
    for i in range(0, len(all_text), 3):
        chunk_text = "\n\n".join(all_text[i:i + 3])
        if len(chunk_text) < 20:
            continue
        chunks.append({
            "id": f"{Path(filepath).stem}_p{i // 3}",
            "content": chunk_text,
            "metadata": {
                "source_type": "technical_spec",
                "source_file": fname,
                "province": province,
                "crop": crop,
                "zoning_type": zoning,
            },
        })
    return chunks


def _table_to_markdown(table) -> str:
    """将 python-docx Table 转为 Markdown 表格文本。"""
    rows = []
    for row in table.rows:
        cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
        rows.append("| " + " | ".join(cells) + " |")
    if not rows:
        return ""
    # 插入表头分隔线
    if len(rows) > 1:
        header_sep = "|" + "|".join(["---"] * len(rows[0].split("|"))) + "|"
        rows.insert(1, header_sep)
    return "\n".join(rows)


# ── PDF 解析 ───────────────────────────────────────────

def parse_pdf(filepath: str) -> list[dict]:
    """解析 PDF 报告，按页切分为 chunks。"""
    try:
        import fitz  # pymupdf
    except ImportError:
        print(f"  [!] pymupdf 未安装，跳过: {filepath}")
        return []

    chunks = []
    fname = Path(filepath).name
    province, crop, zoning = _infer_meta(fname)

    try:
        doc = fitz.open(filepath)
    except Exception as e:
        print(f"  [!] 无法打开 PDF {filepath}: {e}")
        return []

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text().strip()
        if len(text) < 30:
            continue
        # 每页作为一个 chunk，长页再切开
        if len(text) > 1200:
            parts = _split_long_text(text, 600)
            for pi, part in enumerate(parts):
                chunks.append({
                    "id": f"{Path(filepath).stem}_p{page_num}c{pi}",
                    "content": part,
                    "metadata": {
                        "source_type": "report",
                        "source_file": fname,
                        "province": province,
                        "crop": crop,
                        "zoning_type": zoning,
                        "page": page_num + 1,
                    },
                })
        else:
            chunks.append({
                "id": f"{Path(filepath).stem}_p{page_num}",
                "content": text,
                "metadata": {
                    "source_type": "report",
                    "source_file": fname,
                    "province": province,
                    "crop": crop,
                    "zoning_type": zoning,
                    "page": page_num + 1,
                },
            })
    doc.close()
    return chunks


# ── XLSX 解析 ──────────────────────────────────────────

def parse_xlsx(filepath: str) -> list[dict]:
    """解析 XLSX 文件，每个 sheet 转为结构化文本描述。"""
    try:
        import openpyxl
    except ImportError:
        print(f"  [!] openpyxl 未安装，跳过: {filepath}")
        return []

    chunks = []
    fname = Path(filepath).name
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

        # 表头
        headers = [str(h) if h else "" for h in rows[0]]
        data_rows = rows[1:]

        # 过滤空行
        data_rows = [r for r in data_rows if any(c is not None for c in r)]

        if not data_rows:
            continue

        # 转为文本描述：表头 + 统计 + 示例行
        text_parts = [
            f"表格: {sheet_name}",
            f"列名: {', '.join(h for h in headers if h)}",
            f"数据行数: {len(data_rows)}",
            "",
        ]

        # 数值列的统计
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

        # 前 5 行示例
        text_parts.append("\n前5行数据示例:")
        for ri, row in enumerate(data_rows[:5]):
            row_str = " | ".join(str(c) if c is not None else "" for c in row)
            text_parts.append(f"  行{ri + 2}: {row_str}")

        full_text = "\n".join(text_parts)
        chunks.append({
            "id": f"{Path(filepath).stem}_{sheet_name}",
            "content": full_text,
            "metadata": {
                "source_type": "data_table",
                "source_file": fname,
                "province": province,
                "crop": crop,
                "zoning_type": zoning,
                "sheet_name": sheet_name,
            },
        })

    wb.close()
    return chunks


# ── CSV 解析 ───────────────────────────────────────────

def parse_csv(filepath: str) -> list[dict]:
    """解析 CSV 文件，转为结构化文本。支持多编码自动检测。"""
    fname = Path(filepath).name
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

    # 数值列统计
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
        "id": Path(filepath).stem,
        "content": "\n".join(text_parts),
        "metadata": {
            "source_type": "data_table",
            "source_file": fname,
            "province": province,
            "crop": crop,
            "zoning_type": zoning,
        },
    }]


# ── 元数据推断 ─────────────────────────────────────────

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
    """从文件名推断省份、作物、区划类型。"""
    province = ""
    crop = ""
    zoning = ""

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


# ── 工具函数 ───────────────────────────────────────────

def _split_long_text(text: str, chunk_size: int) -> list[str]:
    """按段落边界切开长文本。"""
    paragraphs = text.split("\n")
    parts = []
    current = ""
    for p in paragraphs:
        if len(current) + len(p) > chunk_size and current:
            parts.append(current.strip())
            current = p
        else:
            current += "\n" + p if current else p
    if current.strip():
        parts.append(current.strip())
    return parts


# ── 质量标记 & 去重 ─────────────────────────────────────

def _tag_chunk_quality(chunks: list[dict]) -> list[dict]:
    """标记每个 chunk 的质量等级，用于 step2 选择性 embed。"""
    import re

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
    """文档级去重：同一省份+作物+区划类型的 DOCX/PDF 对，保留内容多、质量好的。"""
    from collections import defaultdict

    # 按 (province, crop, zoning_type) 分组
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
                # 计算文档级总内容量
                docx_total = sum(len(chunks[i]['content']) for i in docx_idxs)
                pdf_total = sum(len(chunks[i]['content']) for i in pdf_idxs)
                ratio = max(docx_total, pdf_total) / min(docx_total, pdf_total)

                # 内容量差距 > 1.15x → 保留多的
                if ratio > 1.15:
                    if docx_total > pdf_total:
                        excluded.update(pdf_idxs)
                        print(f"  文档去重: 保留 DOCX ({docx_total}字) 排除 PDF ({pdf_total}字) [{group_key}]")
                    else:
                        excluded.update(docx_idxs)
                        print(f"  文档去重: 保留 PDF ({pdf_total}字) 排除 DOCX ({docx_total}字) [{group_key}]")
                else:
                    # 内容量相当(<1.15x) → 都保留，不做文档级去重
                    print(f"  文档去重: 内容量相当(ratio={ratio:.2f}) 都保留 [{group_key}]")

    if excluded:
        for i in excluded:
            chunks[i]['excluded'] = True
        print(f"\n  文档去重: 排除 {len(excluded)} 个 chunk")

    return chunks


# ── 主流程 ─────────────────────────────────────────────

def parse_all(src_dir: str) -> list[dict]:
    """遍历源目录，解析所有支持的文件，返回统一 chunk 列表。"""
    all_chunks = []
    src_path = Path(src_dir)

    for f in sorted(src_path.rglob("*")):
        if not f.is_file():
            continue

        # 跳过重复目录
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

    # 去重
    # 第1轮：按 id 去重
    seen_ids = set()
    id_deduped = []
    for c in raw_chunks:
        if c["id"] not in seen_ids:
            seen_ids.add(c["id"])
            id_deduped.append(c)

    # 第2轮：按内容哈希去重（前200字符的 MD5）
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

    # 文档级去重（DOCX/PDF 对）
    deduped = _dedup_documents(deduped)

    # Chunk 质量标记
    deduped = _tag_chunk_quality(deduped)

    # 统计
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

    # 平均长度
    avg_len = sum(len(c["content"]) for c in deduped) / len(deduped) if deduped else 0
    print(f"  平均 chunk 长度: {avg_len:.0f} 字符")

    # 保存
    os.makedirs(OUTPUT.parent, exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(deduped, f, ensure_ascii=False, indent=2)

    print(f"\n  已保存到: {OUTPUT}")


if __name__ == "__main__":
    main()
