#!/usr/bin/env python3
"""对已有 chunks.json 做表格线性化后处理。

用法:
  python3 linearize_chunks.py                    # 原地覆盖 chunks.json
  python3 linearize_chunks.py --output chunks_linearized.json  # 输出到新文件
"""

import json
import sys
from pathlib import Path
from table_linearizer import linearize

BASE_DIR = Path(__file__).resolve().parent
INPUT = BASE_DIR / "data" / "chunks.json"


def main():
    output = BASE_DIR / "data" / "chunks.json"
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        output = Path(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else output

    with open(INPUT) as f:
        chunks = json.load(f)

    table_n = 0
    linearized_n = 0
    for c in chunks:
        if c["metadata"].get("type") != "table":
            continue
        table_n += 1
        result = linearize(c["content"], c["metadata"])
        if result:
            c["content"] = result
            linearized_n += 1

    print(f"表格 chunks: {table_n} → 已线性化: {linearized_n}")

    with open(output, "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)

    print(f"输出: {output}")


if __name__ == "__main__":
    main()
