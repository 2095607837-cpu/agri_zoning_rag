"""
Chunk Knowledge 质量评估脚本
逐条调用 LLM 评估 chunk_knowledge 提取质量，增量写入结果。

用法:
  python3 evaluate_chunk_knowledge.py                    # 评估全部批次（跳过已有结果的）
  python3 evaluate_chunk_knowledge.py --batch 1          # 仅评估指定批次
  python3 evaluate_chunk_knowledge.py --retry            # 重跑已有的
  python3 evaluate_chunk_knowledge.py --workers 3        # 并发数
  python3 evaluate_chunk_knowledge.py --size 10           # 每批给 LLM 的条目数
"""

import json
import os
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from llm_client import call_llm

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
COMPACT_PATH = DATA_DIR / "chunk_knowledge_review_compact.json"

EVAL_PROMPT = """你是一个知识提取质量评估专家。你需要评估从农业气候区划文档 chunk 中提取的知识 metadata 的质量。

对每条记录，对比 source_preview（原文摘要）和提取出的各个字段，给出 1-5 的评分：
- 5: 所有字段准确，核心概念和术语与原文高度一致，摘要精准
- 4: 基本准确，个别术语或摘要细节不够精确，但不影响整体理解
- 3: 部分准确，存在一些提取偏差或遗漏，但核心内容仍有价值
- 2: 较多错误，核心概念偏移或摘要明显偏离原文内容
- 1: 严重错误，提取结果与原文完全不符或无法评估

评分依据：
1. core_concept 是否准确反映原文主题
2. technical_terms 是否确实出现在原文中（不要求逐字匹配，语义一致即可）
3. semantic_summary 是否准确概括原文要点
4. region 识别是否正确（对照 source_preview 开头的 [省份 类型] 标签）
5. affected_objects 是否合理
6. evaluation_method 是否正确识别了原文提到的方法

输出 JSON 数组，每条记录包含 chunk_id, score, good（好的方面）, bad（不好的方面），不要输出其他内容。

输入数据："""


def load_entries() -> list[dict]:
    with open(COMPACT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_existing_results() -> dict[str, dict]:
    """加载所有已有的评估结果，key 为 chunk_id。"""
    existing = {}
    for p in sorted(DATA_DIR.glob("knowledge_review_results_batch_*.json")):
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            for item in data:
                existing[item["chunk_id"]] = item
    return existing


def build_eval_input(entries: list[dict]) -> str:
    """将一批条目构造成 LLM 评估输入。"""
    compact = []
    for e in entries:
        item = {
            "chunk_id": e["chunk_id"],
            "source_preview": e["source_preview"],
            "core_concept": e.get("core_concept", []),
            "technical_terms": e.get("technical_terms", []),
            "semantic_summary": e.get("semantic_summary", ""),
            "region": e.get("region"),
            "affected_objects": e.get("affected_objects", []),
            "evaluation_method": e.get("evaluation_method", []),
        }
        compact.append(item)
    return json.dumps(compact, ensure_ascii=False, indent=2)


def evaluate_batch(entries: list[dict], batch_id: int) -> list[dict]:
    """调用 LLM 评估一批条目，带重试。"""
    user_input = build_eval_input(entries)
    user_msg = EVAL_PROMPT + "\n" + user_input

    for attempt in range(3):
        try:
            resp = call_llm(
                [{"role": "user", "content": user_msg}],
                temperature=0.2,
                stream=False,
                json_mode=False,
            )
            # 提取 JSON 数组
            start = resp.find("[")
            end = resp.rfind("]") + 1
            if start >= 0 and end > start:
                results = json.loads(resp[start:end])
                # 验证格式
                valid = []
                for r in results:
                    if isinstance(r, dict) and "chunk_id" in r and "score" in r:
                        valid.append(r)
                if valid:
                    return valid

            raise ValueError(f"Invalid response format: {resp[:300]}")
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                # 返回错误占位
                return [
                    {
                        "chunk_id": e.get("chunk_id", "unknown"),
                        "score": 0,
                        "good": "N/A",
                        "bad": f"LLM eval failed: {e}",
                    }
                    for e in entries
                ]


def main():
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 2
    target_batch = sys.argv[sys.argv.index("--batch") + 1] if "--batch" in sys.argv else None
    retry = "--retry" in sys.argv
    eval_size = int(sys.argv[sys.argv.index("--size") + 1]) if "--size" in sys.argv else 8

    print(f"[Eval] 加载数据: {COMPACT_PATH}")
    all_entries = load_entries()
    print(f"       共 {len(all_entries)} 条")

    existing = load_existing_results()
    print(f"       已有评估结果: {len(existing)} 条")

    # 按原始 8 个批次文件分组（通过 batch 文件确定每批包含的 chunk_id）
    # 如果 batch 文件不存在，则从 all_entries 均分
    batch_groups = {}
    for i in range(1, 9):
        batch_file = DATA_DIR / f"knowledge_review_batch_{i}.json"
        if batch_file.exists():
            with open(batch_file, "r", encoding="utf-8") as f:
                batch_data = json.load(f)
            batch_groups[i] = [e["chunk_id"] for e in batch_data]
        else:
            batch_groups[i] = []

    if not any(batch_groups.values()):
        # fallback: 均分
        chunk_ids = [e["chunk_id"] for e in all_entries]
        per_batch = (len(chunk_ids) + 7) // 8
        for i in range(8):
            batch_groups[i + 1] = chunk_ids[i * per_batch : (i + 1) * per_batch]

    # 建立 chunk_id -> entry 映射
    entry_map = {e["chunk_id"]: e for e in all_entries}

    # 筛选待评估的批次
    batches_to_run = []
    for batch_id in sorted(batch_groups.keys()):
        if target_batch and str(batch_id) != target_batch:
            continue

        chunk_ids = batch_groups[batch_id]
        pending_ids = []
        for cid in chunk_ids:
            if cid in existing and not retry:
                continue
            if cid in entry_map:
                pending_ids.append(cid)

        if pending_ids:
            batches_to_run.append((batch_id, pending_ids))
            print(f"       Batch {batch_id}: {len(pending_ids)} 条待评估")

    if not batches_to_run:
        print("\n[Eval] 全部完成，无需评估")
        return

    t0 = time.time()

    for batch_id, pending_ids in batches_to_run:
        output_path = DATA_DIR / f"knowledge_review_results_batch_{batch_id}.json"
        batch_entries = [entry_map[cid] for cid in pending_ids]

        # 合并已有结果
        batch_results = []
        done_ids = set()
        if output_path.exists() and not retry:
            with open(output_path, "r", encoding="utf-8") as f:
                batch_results = json.load(f)
            done_ids = {r["chunk_id"] for r in batch_results if r.get("score", 0) > 0}

        remaining = [e for e in batch_entries if e["chunk_id"] not in done_ids]
        if not remaining:
            print(f"\n[Batch {batch_id}] 全部完成")
            continue

        print(f"\n[Batch {batch_id}] {len(remaining)} 条，每批 {eval_size} 条，workers={workers}")

        # 切分成 eval_size 一批
        sub_batches = [remaining[i : i + eval_size] for i in range(0, len(remaining), eval_size)]

        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {}
            for sb in sub_batches:
                f = ex.submit(evaluate_batch, sb, batch_id)
                futures[f] = sb

            for f in as_completed(futures):
                try:
                    results = f.result()
                except Exception as exc:
                    sb = futures[f]
                    results = [
                        {"chunk_id": e["chunk_id"], "score": 0, "good": "N/A", "bad": str(exc)}
                        for e in sb
                    ]

                batch_results.extend(results)
                completed += len(futures[f])

                # 增量写入
                with open(output_path, "w", encoding="utf-8") as fout:
                    json.dump(batch_results, fout, ensure_ascii=False, indent=2)

                elapsed = time.time() - t0
                rate = completed / elapsed if elapsed > 0 else 0
                remaining_total = len(remaining) - completed
                eta = remaining_total / rate if rate > 0 else 0
                print(f"  [{completed}/{len(remaining)}] 速率={rate:.1f}条/s ETA={eta:.0f}s", flush=True)

    elapsed = time.time() - t0
    print(f"\n[Eval] 完成，耗时 {elapsed:.0f}s")


if __name__ == "__main__":
    main()
