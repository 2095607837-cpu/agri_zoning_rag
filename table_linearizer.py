"""
Markdown / PDF 表格 → 自然语言线性化。

将 pipe table 转为可被 BM25 和 Dense 检索的自然语言句子，
保留实体-数值之间的语义关联。

不可线性化时返回 None（调用方回退到原 content）。
"""

import re
from typing import Optional

_FORMULA_CELL = re.compile(r'^[（(]?\d+[）)]?$')  # 纯编号 "(1)" "（2）" "3"
_SEPARATOR_ROW = re.compile(r'^[-: |]+$')
_IS_NUMERIC_CELL = re.compile(r'^[\d.≤≥<>＜＞−-]')  # 首字符为数字/比较符 → 值而非标签
_TABLE_CAPTION = re.compile(r'^表\s*[0-9一二三四五六七八九十]+([-.．][0-9]+)*')
_HAS_DIGIT = re.compile(r'\d')
_IS_LABEL_LINE = re.compile(r'^[^0-9≤≥<>＜＞−\-]')  # 不以数字/符号开头 → 标签行

# ── 表格元描述 caption ─────────────────────────────────────
_ZONE_KW = ("适宜", "次适宜", "最适宜", "不适宜", "等级", "区划等级", "分级", "风险等级")
_WEIGHT_KW = ("权重", "系数")


def _detect_table_schema(headers: list[str], data_rows: list[list[str]]) -> dict:
    """分析表格结构，返回类型、关键指标名、分级体系名。"""
    all_text = "".join(headers) + "".join(c for r in data_rows for c in r)

    # 类型判定
    if any(kw in all_text for kw in _ZONE_KW):
        t = "zone"
    elif any(kw in all_text for kw in _WEIGHT_KW) or any(
        r[0] and _IS_NUMERIC_CELL.match(_clean_value(r[0])) for r in data_rows
    ):
        t = "weight"
    else:
        t = "general"

    # 指标名：从第一列提取（非空、非数值、非编号、<30字）
    indicators = []
    for r in data_rows:
        if not r[0]:
            continue
        label = _clean_value(r[0])
        if not label:
            continue
        if len(label) > 30 or _IS_NUMERIC_CELL.match(label) or _FORMULA_CELL.match(label):
            continue
        if label not in indicators:
            indicators.append(label)
        if len(indicators) >= 4:
            break

    # 列头模式兜底：第一列全是数值/编号 → 从列头提取指标名
    if not indicators:
        for h in headers:
            h_clean = _clean_value(h)
            if not h_clean:
                continue
            if len(h_clean) > 30 or _IS_NUMERIC_CELL.match(h_clean) or _FORMULA_CELL.match(h_clean):
                continue
            if h_clean not in indicators:
                indicators.append(h_clean)
            if len(indicators) >= 4:
                break

    # 分级名：从列头提取（zone 表才有意义）
    zones = []
    if t == "zone":
        for h in headers[1:]:
            h_clean = _clean_value(h)
            if h_clean and len(h_clean) < 10 and not _FORMULA_CELL.match(h_clean):
                zones.append(h_clean)
                if len(zones) >= 4:
                    break

    return {"type": t, "indicators": indicators, "zones": zones}


def _build_caption(schema: dict) -> str:
    """根据表结构生成一句话元描述。返回空字符串表示不生成。"""
    indicators = schema.get("indicators", [])
    if not indicators:
        return ""

    n = len(indicators)
    ind_str = "、".join(indicators)
    t = schema["type"]

    if t == "zone":
        zones = schema.get("zones", [])
        if zones:
            zone_str = "、".join(zones)
            return f"该表格为区划指标体系，列出{ind_str}等{n}项因子的{zone_str}分级阈值"
        else:
            return f"该表格为区划指标体系，列出{ind_str}等{n}项因子的分级标准"
    elif t == "weight":
        return f"该表格为权重分配，列出{ind_str}等{n}个维度的权重系数"
    else:
        return f"该表格列出{ind_str}等{n}项指标的具体数值"


def _build_context(metadata: dict) -> str:
    """从 metadata 构建表格上下文前缀。"""
    province = metadata.get("province", "")
    crop = metadata.get("crop", "")
    zoning = metadata.get("zoning_type", "")
    title = metadata.get("section_title", "")

    parts = []
    if province and province not in ("全国",):
        parts.append(province)
    if crop:
        parts.append(crop)
    if zoning:
        parts.append(zoning)
    context = "".join(parts) if parts else ""

    if title and not re.match(r'^表格\d+$', title) and title not in context:
        if context:
            context += f"，{title}"
        else:
            context = title

    return context


def _clean_value(v: str) -> str:
    """清洗单元格值：统一数字、过滤占位符。返回空字符串表示应跳过。"""
    v = v.strip()
    # 占位符/空标记 → 视为空
    if not v or v in ("——", "—", "---", "--", "…", "...", "/", "无"):
        return ""
    v = v.replace("０", "0").replace("１", "1").replace("２", "2").replace("３", "3")
    v = v.replace("４", "4").replace("５", "5").replace("６", "6").replace("７", "7")
    v = v.replace("８", "8").replace("９", "9")
    # [0～0.27) → 0至0.27；保留内部逗号/分号等有意义标点
    v = re.sub(r'[\[（(]([^\]）)]+)[\]）)]', r'\1', v)
    v = v.replace("～", "至").replace("~", "至")
    return v


def _parse_pipe_table(content: str) -> Optional[tuple[list[str], list[list[str]]]]:
    """解析 markdown pipe 表格，返回 (headers, data_rows)。失败返回 None。"""
    lines = content.strip().split("\n")
    pipe_lines = [l for l in lines if l.startswith("|")]

    if len(pipe_lines) < 2:
        return None

    parsed = []
    for pl in pipe_lines:
        cells = [c.strip() for c in pl[1:].rstrip("|").split("|")]
        parsed.append(cells)

    max_cols = max(len(r) for r in parsed) if parsed else 0
    if max_cols < 2:
        return None

    parsed = [r + [""] * (max_cols - len(r)) for r in parsed]

    header_rows = []
    data_rows = []
    in_data = False

    for row in parsed:
        joined = "".join(row)
        if _SEPARATOR_ROW.match(joined):
            in_data = True
            continue
        if not in_data:
            header_rows.append(row)
        else:
            data_rows.append(row)

    if not header_rows:
        return None

    data_rows = [r for r in data_rows if any(c for c in r)]
    if not data_rows:
        return None

    # 多级表头：用最后一行（通常是最细粒度的子列名）
    if len(header_rows) >= 2:
        header_rows = [header_rows[-1]]

    # 检测第一个数据行是否为子表头（内容短、首列匹配表头首列）
    if (data_rows and header_rows and
        header_rows[0][0] and data_rows[0][0] and
        header_rows[0][0] == data_rows[0][0] and
        all(len(c) < 20 for c in data_rows[0] if c)):
        header_rows = [data_rows[0]]
        data_rows = data_rows[1:]

    headers = header_rows[0]
    return headers, data_rows


def linearize(content: str, metadata: dict) -> Optional[str]:
    """尝试将表格线性化。先试 pipe 表格，再试 PDF 无 pipe 表格。"""
    result = _parse_pipe_table(content)
    if result is not None:
        return _linearize_parsed(result, metadata)
    return _linearize_pdf_table(content, metadata)


def _linearize_parsed(result: tuple, metadata: dict) -> Optional[str]:
    """对已解析的 pipe 表格做线性化。"""
    headers, data_rows = result
    n_cols = len(headers)
    n_rows = len(data_rows)

    # 跳过：过宽的表
    if n_cols > 10:
        return None

    # 跳过：空表头
    if all(not h for h in headers):
        return None

    # 跳过：公式编号表（大部分单元格是纯编号）
    formula_count = sum(
        1 for r in data_rows for c in r
        if c and _FORMULA_CELL.match(c)
    )
    if formula_count >= max(1, n_rows * 0.4):
        return None

    # 跳过：单元格内容已是长文本描述（>60字），不需要线性化
    long_cells = sum(1 for r in data_rows for c in r if len(c) > 60)
    if long_cells >= n_rows * 0.5:
        return None

    # 跳过：纯空白模板表（数据行每行有效内容 < 2 个非空单元格）
    if all(sum(1 for c in r if c) < 2 for r in data_rows):
        return None

    ctx = _build_context(metadata)
    schema = _detect_table_schema(headers, data_rows)
    caption = _build_caption(schema)

    if n_cols == 2:
        result = _linearize_2col(headers, data_rows, ctx)
    else:
        result = _linearize_multicol(headers, data_rows, ctx)

    if result and caption:
        # 在上下文前缀之后、数据内容之前插入 caption
        prefix = f"{ctx}中，" if ctx else ""
        if result.startswith(prefix):
            result = prefix + caption + "。" + result[len(prefix):]
        else:
            result = caption + "。" + result

    return result


def _linearize_2col(headers: list[str], data_rows: list[list[str]], ctx: str) -> str:
    """2 列表格线性化。"""
    h0, h1 = headers[0], headers[1]

    # 单行 key-value
    if len(data_rows) == 1:
        label = _clean_value(data_rows[0][0])
        value = _clean_value(data_rows[0][1]) if len(data_rows[0]) > 1 else ""
        if label and value:
            prefix = f"{ctx}中，" if ctx else ""
            if h1:
                return f"{prefix}{label}的{h1}为{value}"
            else:
                return f"{prefix}{label}为{value}"

    # 多行
    items = []
    for r in data_rows:
        label = _clean_value(r[0]) if r[0] else ""
        value = _clean_value(r[1]) if len(r) > 1 else ""
        if not label:
            continue
        if value:
            if h1:
                items.append(f"{label}的{h1}为{value}")
            else:
                items.append(f"{label}为{value}")
        else:
            items.append(label)

    if not items:
        return None

    prefix = f"{ctx}中，" if ctx else ""

    # 检测是否分区/等级表
    is_zone = any(
        any(kw in _clean_value(r[0]) for kw in ("区", "等级", "风险"))
        for r in data_rows if r and r[0]
    )
    if is_zone and h0:
        return f"{prefix}{h0}：{'；'.join(items)}"

    return f"{prefix}{'；'.join(items)}"


def _linearize_multicol(headers: list[str], data_rows: list[list[str]], ctx: str) -> str:
    """3+ 列表格线性化。"""
    prefix = f"{ctx}中，" if ctx else ""
    n_cols = len(headers)

    # 判断：第一列是否为标签列（行头模式）还是数据列（列头模式）
    # 行头模式：第一列有非空标签名，且第一列非空的比例 > 50%
    col0_nonempty = sum(1 for r in data_rows if r[0]) / max(len(data_rows), 1)
    row_header_mode = col0_nonempty > 0.5

    # 单数据行 + 首列非标签（空或数值）→ 表头当标签，数据行当值
    first_cell_is_value = (
        len(data_rows) == 1 and data_rows[0][0] and
        _IS_NUMERIC_CELL.match(_clean_value(data_rows[0][0]))
    )
    if len(data_rows) == 1 and (not row_header_mode or first_cell_is_value):
        parts = []
        for j in range(n_cols):
            hdr = headers[j]
            val = _clean_value(data_rows[0][j]) if j < len(data_rows[0]) else ""
            if hdr and val:
                parts.append(f"{hdr} {val}")
            elif val:
                parts.append(val)
        if parts:
            return f"{prefix}{'，'.join(parts)}"
        return None

    sentences = []
    for r in data_rows:
        label = _clean_value(r[0]) if r[0] else ""
        if not label and row_header_mode:
            continue

        parts = []
        for j in range(1, n_cols):
            val = _clean_value(r[j]) if j < len(r) else ""
            hdr = _clean_value(headers[j]) if j < len(headers) else ""
            if val:
                if hdr:
                    parts.append(f"{hdr} {val}")
                else:
                    parts.append(val)

        if parts:
            if label:
                sentences.append(f"{label}：{'，'.join(parts)}")
            else:
                sentences.append("，".join(parts))

    if not sentences:
        return None

    sep = "。" if len(sentences) <= 6 else "；"
    return f"{prefix}{sep.join(sentences)}"


# ── PDF 无 pipe 表格线性化 ───────────────────────────────────

def _is_value_line(line: str) -> bool:
    """判断是否为数据值行（以数字、比较符、范围符等开头）。"""
    v = line.strip()
    if not v:
        return False
    # 数字开头: "42.0", "1961", "-1394", "+312504"
    # 比较/范围开头: "<260", ">400", "≥10℃", "≤5500", "5000~10000"
    if v[0].isdigit():
        # 数字+中文标签模式: "6-8月空气", "1月平均气温（℃）" → 非值行
        cjk = sum(1 for c in v if '一' <= c <= '鿿')
        if cjk >= len(v) * 0.3:
            return False
        return True
    if v[0] in '≤≥<>＜＞−-+':
        return True
    # 数值范围 "8.5~12.5"
    if _HAS_DIGIT.match(v[0]):
        return True
    # PDF 断行续值: "或12.6~15.4", "和12.6~15.4" 等
    if v[0] in '或和及' and len(v) >= 2 and _HAS_DIGIT.search(v):
        return True
    return False


def _linearize_pdf_table(content: str, metadata: dict) -> Optional[str]:
    """尝试线性化 PDF 无 pipe 表格：label-value 自然分组。"""
    lines = [l.strip() for l in content.split("\n") if l.strip()]
    if len(lines) < 6:
        return None

    # 找到表题行
    cap_idx = None
    for i, l in enumerate(lines[:20]):
        if _TABLE_CAPTION.match(l):
            cap_idx = i
            break
    if cap_idx is None:
        return None

    after_cap = lines[cap_idx + 1:]
    if len(after_cap) < 4:
        return None

    # 前置过滤：需要足够的短行和数字行（网格表特征）
    short_ratio = sum(1 for l in after_cap if len(l) < 30) / len(after_cap)
    digit_ratio = sum(1 for l in after_cap if _HAS_DIGIT.search(l)) / len(after_cap)
    if short_ratio < 0.7 or digit_ratio < 0.4:
        return None

    # 跳过表头行：第一个数据值行之前的为表头
    # 但最后一个"表头"行通常是第一个数据 label，挪到数据区
    header_end = 0
    for i, l in enumerate(after_cap):
        if _is_value_line(l):
            header_end = i
            break
    if header_end < 1:
        return None

    data_start = header_end - 1  # 最后一行 header → 第一个 data label
    headers = after_cap[:data_start]
    data_lines = after_cap[data_start:]
    data_lines = [l for l in data_lines if len(l) >= 2]  # 过滤 PDF 单字碎片

    # 跳过表头行数过多（>8，过滤单字碎片后）或过少的表，结构太复杂
    headers = [h for h in headers if len(h) >= 2]  # 过滤 PDF 拆分的单字碎片
    if len(headers) > 8 or len(headers) == 0:
        return None

    # 跳过第一列是数值标签的表（如 "1, 2, 3" 等级编号），无法区分 label/value
    first_label = _clean_value(data_lines[0]) if data_lines else ""
    if first_label and len(first_label) <= 2 and first_label.isdigit():
        return None

    # 跳过含长文本的表（>40字单元格 → 叙述型，不宜列化）
    long_count = sum(1 for l in data_lines if len(l) > 40)
    if long_count >= max(1, len(data_lines) * 0.2):
        return None

    # Label-value 分组：遇到 label 行开新记录，值行归入当前记录
    records = []  # [(label, [values])]
    cur_label = None
    cur_vals = []

    for l in data_lines:
        if _is_value_line(l):
            cur_vals.append(_clean_value(l))
        else:
            if cur_label is not None:
                records.append((cur_label, cur_vals))
            cur_label = _clean_value(l)
            cur_vals = []

    if cur_label is not None:
        records.append((cur_label, cur_vals))

    # 过滤：至少要有 2 条有效记录
    records = [(lbl, vals) for lbl, vals in records if lbl and vals]
    if len(records) < 2:
        return None

    # 过滤纯编号 label
    formula_labels = sum(1 for lbl, _ in records if _FORMULA_CELL.match(lbl))
    if formula_labels >= len(records) * 0.4:
        return None

    # 构建上下文
    ctx = _build_context(metadata)
    cap_match = _TABLE_CAPTION.search(lines[cap_idx])
    table_title = lines[cap_idx][cap_match.start():].strip() if cap_match else ""
    if table_title:
        ctx = f"{ctx}，{table_title}" if ctx else table_title
    prefix = f"{ctx}中，" if ctx else ""

    # 表头信息作为补充上下文
    header_ctx = "、".join(h for h in headers if len(h) >= 2 and not _FORMULA_CELL.match(h))
    if header_ctx and len(header_ctx) < 80:
        prefix = f"{prefix}{header_ctx}："

    # 生成 caption（复用 pipe 表格的 schema 检测）
    fake_headers = [""] + headers  # 第一列为 label 占位
    fake_data_rows = [[lbl] + vals for lbl, vals in records]
    schema = _detect_table_schema(fake_headers, fake_data_rows)
    caption = _build_caption(schema)

    # 生成句子
    sentences = []
    for label, vals in records:
        if len(vals) == 1:
            sentences.append(f"{label} {vals[0]}")
        else:
            sentences.append(f"{label}：{'，'.join(vals)}")

    if not sentences:
        return None

    sep = "。" if len(sentences) <= 6 else "；"
    result = f"{prefix}{sep.join(sentences)}"

    if caption:
        # 在上下文前缀之后、数据内容之前插入 caption
        if result.startswith(prefix):
            result = prefix + caption + "。" + result[len(prefix):]
        else:
            result = caption + "。" + result

    return result
