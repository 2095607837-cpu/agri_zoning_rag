"""
Step 3: Chunk Knowledge Extraction

对每个 chunk 调用 LLM，提取检索增强 metadata（concept_evidence、user_expressions 等）。
结果缓存到 data/chunk_knowledge.json，支持断点续跑。

用法:
  python3 step3_chunk_knowledge.py              # 增量（跳过已缓存的）
  python3 step3_chunk_knowledge.py --retry      # 重跑失败的
  python3 step3_chunk_knowledge.py --workers 4  # 并发数
"""

import json
import os
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from llm_client import call_llm

BASE_DIR = Path(__file__).resolve().parent
CHUNKS_PATH = BASE_DIR / "data" / "chunks.json"
PROMPT_PATH = BASE_DIR / "prompts" / "chunk_knowledge_extraction.md"
OUTPUT_PATH = BASE_DIR / "data" / "chunk_knowledge.json"

BATCH_SAVE_INTERVAL = 20  # 每处理 20 个保存一次


def load_prompt() -> str:
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def load_chunks() -> list[dict]:
    with open(CHUNKS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_cache() -> dict[str, dict]:
    if not OUTPUT_PATH.exists():
        return {}
    with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    # {"_meta": {...}, "chunk_id": {...}, ...}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def save_cache(cache: dict[str, dict], meta: dict):
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    output = {"_meta": meta}
    output.update(cache)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


def call_extraction(chunk: dict, prompt_template: str) -> dict:
    """调用 LLM 提取单个 chunk 的知识 metadata。"""
    cid = chunk["id"]
    ctype = chunk["metadata"].get("type", "text")
    content = chunk["content"]

    # 截断过长内容（prompt + few-shot 约 4000 字，留给 content 约 2000 字）
    max_content = 2500
    if len(content) > max_content:
        content = content[:max_content] + "\n...(truncated)"

    user_msg = f"""以下是一个知识库 chunk，请按上述要求提取知识 metadata。

```json
{{
  "chunk_id": "{cid}",
  "type": "{ctype}",
  "content": {json.dumps(content, ensure_ascii=False)}
}}
```

仅输出 JSON，不要输出其他内容。"""

    for attempt in range(3):
        try:
            resp = call_llm(
                [
                    {"role": "system", "content": prompt_template},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.3,
                stream=False,
                json_mode=False,
            )
            # 提取 JSON
            start = resp.find("{")
            end = resp.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(resp[start:end])
                result["_chunk_version"] = chunk.get("chunk_version", 1)
                result["_generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                return result
            else:
                raise ValueError(f"No JSON in response: {resp[:200]}")
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                return {
                    "chunk_id": cid,
                    "_error": str(e),
                    "_chunk_version": chunk.get("chunk_version", 1),
                    "_generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }


def main():
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 3
    retry = "--retry" in sys.argv

    print(f"[Step 3] 加载 prompt: {PROMPT_PATH}")
    prompt = load_prompt()

    print(f"[Step 3] 加载 chunks: {CHUNKS_PATH}")
    chunks = load_chunks()
    print(f"         共 {len(chunks)} 个 chunk")

    cache = load_cache()
    print(f"         已缓存: {len(cache)} 个")

    # 筛选待处理的 chunk
    pending = []
    for c in chunks:
        cid = c["id"]
        if cid in cache:
            entry = cache[cid]
            # 跳过已成功的
            if "_error" not in entry:
                continue
            # retry 模式下重试失败的
            if retry:
                pending.append(c)
        else:
            pending.append(c)

    if not pending:
        print(f"\n[Step 3] 全部完成，无需处理")
        return

    print(f"[Step 3] 待处理: {len(pending)} 个 (workers={workers})")
    t0 = time.time()

    completed = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(call_extraction, c, prompt): c["id"] for c in pending}

        for f in as_completed(futures):
            cid = futures[f]
            try:
                result = f.result()
            except Exception as e:
                result = {"chunk_id": cid, "_error": str(e)}

            cache[cid] = result
            completed += 1

            if "_error" in result:
                errors += 1

            # 定期保存
            if completed % BATCH_SAVE_INTERVAL == 0:
                elapsed = time.time() - t0
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (len(pending) - completed) / rate if rate > 0 else 0
                save_cache(cache, {
                    "total_chunks": len(chunks),
                    "cached": len(cache),
                    "completed": completed,
                    "pending": len(pending),
                    "errors": errors,
                })
                print(f"  [{completed}/{len(pending)}] {completed*100//len(pending)}% "
                      f"速率={rate:.1f}/s ETA={eta:.0f}s 错误={errors}", flush=True)

    # 最终保存
    elapsed = time.time() - t0
    save_cache(cache, {
        "total_chunks": len(chunks),
        "cached": len(cache),
        "completed": completed,
        "errors": errors,
        "elapsed_seconds": round(elapsed, 1),
    })

    print(f"\n[Step 3] 完成: {completed} 个, 错误 {errors}, 耗时 {elapsed:.0f}s")
    print(f"         输出: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
