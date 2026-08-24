"""
Step 2: 加载、切分、嵌入 & ChromaDB 索引（LangChain 实现）

使用 LangChain 统一接口：
  - HuggingFaceEmbeddings (BGE-small-zh)
  - RecursiveCharacterTextSplitter (按字符递归切分)
  - Chroma vectorstore (持久化)

用法:
  python3 step2_embed.py              # 增量构建
  python3 step2_embed.py --rebuild    # 强制重建
"""

import os
import re
import sys
import json
from pathlib import Path

from langchain.schema import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma


BASE_DIR = Path(__file__).resolve().parent
CHUNKS_PATH = BASE_DIR / "data" / "chunks.json"
CHUNKS_SPLIT_PATH = BASE_DIR / "data" / "chunks_split.json"
PERSIST_DIR = str(BASE_DIR / "vectordb")
COLLECTION_NAME = "agri_zoning"
EMBED_MODEL = "BAAI/bge-small-zh-v1.5"

# 中文适用的切分器：按段落标记递归切分，800字符/chunk，150字符重叠
TEXT_SPLITTER = RecursiveCharacterTextSplitter(
    separators=["\n\n", "\n", "。", "；", "，", " ", ""],
    chunk_size=800,
    chunk_overlap=150,
)

# H3 子标题模式（用于 >3000 字章节的回退切分）
_H3_SPLIT_RE = re.compile(
    r'^#{1,4}\s+\d+\.\d+'                         # ### 2.1.1  #### 6.9.2.1（markdown + 编号）
    r'|^#{1,4}\s+\S'                               # ### 算法概述  #### 产品规格（纯 markdown 标题）
    r'|^\d+\.\d+[\.\、\s]'                         # 1.1  2.3.1（无 markdown 前缀的编号）
    r'|^\（\d+）'                                  # （1）（2）
    r'|^[\(（]\d+[\)）][\s]'                       # (1) 1)
)


def _compact_header(metadata: dict) -> str:
    """从 metadata 构建紧凑关键词前缀，增强 embedding 信号。"""
    province = metadata.get("province", "")
    crop = metadata.get("crop", "")
    zoning_type = metadata.get("zoning_type", "")
    parts = [p for p in [province, crop, zoning_type] if p]
    return f"[{' '.join(parts)}] " if parts else ""


def load_documents() -> list[Document]:
    """从 step1_parse 产出的 chunks.json 加载为 LangChain Document 列表。
    跳过 excluded=True 和 quality=low 的 chunk。
    page_content = [省份 作物 区划] + 正文（heading_path 仅保留在 metadata）。"""
    with open(CHUNKS_PATH, "r", encoding="utf-8") as f:
        chunks = json.load(f)

    docs = []
    skipped_excluded = 0
    skipped_low = 0
    for c in chunks:
        if c.get("excluded"):
            skipped_excluded += 1
            continue
        if c.get("quality") == "low":
            skipped_low += 1
            continue
        m = c["metadata"]
        compact = _compact_header(m)
        page_content = compact + c["content"] if compact else c["content"]
        docs.append(Document(
            page_content=page_content,
            metadata={
                **c["metadata"],
                "chunk_id": c["id"],
                "source_id": c.get("source_id", c["metadata"].get("section_id", "")),
                "chunk_version": c.get("chunk_version", 1),
            },
        ))
    if skipped_excluded:
        print(f"         跳过 excluded: {skipped_excluded}")
    if skipped_low:
        print(f"         跳过 low quality: {skipped_low}")
    return docs


def _split_by_h3(doc: Document) -> list[Document]:
    """将 >3000 字的章节按 H3 子标题回退切分，保留 heading_path 谱系。"""
    text = doc.page_content
    heading_path = doc.metadata.get("heading_path", [])
    compact = _compact_header(doc.metadata)

    # 分离 compact header 前缀，提取正文
    body = text[len(compact):] if compact and text.startswith(compact) else text

    lines = body.split("\n")
    sub_sections = []
    current_lines = []
    current_title = ""

    for line in lines:
        stripped = line.strip()
        if _H3_SPLIT_RE.match(stripped):
            if current_lines:
                sub_sections.append((current_title, "\n".join(current_lines)))
            current_title = stripped
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sub_sections.append((current_title, "\n".join(current_lines)))

    if len(sub_sections) <= 1:
        return [doc]

    result = []
    for i, (title, content) in enumerate(sub_sections):
        new_path = list(heading_path) + ([title] if title else [])
        new_meta = dict(doc.metadata)
        new_meta["heading_path"] = new_path
        new_meta["heading_level"] = len(new_path)
        # 保留原始 section_id（保证 gold 匹配和上下文扩展仍按原 section 分组），
        # 细粒度子标题仅追加到 heading_path。
        new_meta["_h3_sub_index"] = i

        new_page_content = compact + content.strip()

        result.append(Document(page_content=new_page_content, metadata=new_meta))

    return result


def split_documents(docs: list[Document]) -> list[Document]:
    """切分文档：>3000 字的章节先尝试 H3 回退切分，
    再走 RecursiveCharacterTextSplitter(800, 150)。
    切分后按 section_id 分配 chunk_index/chunk_count。"""

    # Phase 1: H3 fallback for large sections
    phase1 = []
    h3_split_count = 0
    h3_empty_count = 0
    for d in docs:
        if len(d.page_content) > 3000:
            sub_docs = _split_by_h3(d)
            if len(sub_docs) > 1:
                h3_split_count += len(sub_docs) - 1
                # TODO: 过滤 H3 切分产生的空 body chunk（暂时关闭，待 CK 重建同步）
                # compact = _compact_header(d.metadata)
                # before = len(sub_docs)
                # sub_docs = [sd for sd in sub_docs
                #             if len(sd.page_content) - len(compact) >= 20]
                # h3_empty_count += before - len(sub_docs)
            phase1.extend(sub_docs)
        else:
            phase1.append(d)
    if h3_split_count:
        print(f"         H3 回退切分: +{h3_split_count} 个子章节"
              + (f", 过滤空body {h3_empty_count}" if h3_empty_count else ""))

    # Phase 2: RecursiveCharacterTextSplitter（所有 doc 统一走）
    to_split = []
    keep_intact = []
    for d in phase1:
        if len(d.page_content) <= 800:
            keep_intact.append(d)
        else:
            to_split.append(d)

    split_result = TEXT_SPLITTER.split_documents(to_split)

    print(f"         免切分: {len(keep_intact)} 条 (≤800字)")
    print(f"         被切分: {len(to_split)} 条 → {len(split_result)} 条")

    all_docs = split_result + keep_intact

    # Phase 2.5: 切分后子块补回 compact header（仅第一块保留了前缀）
    restored = 0
    for d in all_docs:
        compact = _compact_header(d.metadata)
        if compact and not d.page_content.startswith(compact):
            d.page_content = compact + d.page_content
            restored += 1
    if restored:
        print(f"         补回 compact header: {restored} 个子块")

    # Phase 3: 按 section_id 分组分配 chunk_index / chunk_count
    section_groups: dict[str, list[Document]] = {}
    for d in all_docs:
        sid = d.metadata.get("section_id", "")
        section_groups.setdefault(sid, []).append(d)

    for sid, group in section_groups.items():
        for i, d in enumerate(group):
            d.metadata["chunk_index"] = i
            d.metadata["chunk_count"] = len(group)

    return all_docs


def build_vectorstore(docs: list[Document], force_rebuild: bool = False) -> Chroma:
    """构建或加载 Chroma 向量存储。"""
    embeddings = HuggingFaceEmbeddings(
        model_name=EMBED_MODEL,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )

    if force_rebuild:
        print("[Step 2] 强制重建索引...")
        # 先删除旧 collection，否则 Chroma.from_documents 会追加而非替换
        import chromadb
        client = chromadb.PersistentClient(path=PERSIST_DIR)
        try:
            client.delete_collection(COLLECTION_NAME)
            print(f"         已删除旧 collection: {COLLECTION_NAME}")
        except Exception:
            pass
        return Chroma.from_documents(
            documents=docs,
            embedding=embeddings,
            collection_name=COLLECTION_NAME,
            persist_directory=PERSIST_DIR,
            collection_metadata={"hnsw:space": "cosine"},
        )

    # 增量：已有则加载，否则新建
    if os.path.exists(os.path.join(PERSIST_DIR, "chroma.sqlite3")):
        print(f"[Step 2] 加载已有索引 {PERSIST_DIR}...")
        return Chroma(
            collection_name=COLLECTION_NAME,
            embedding_function=embeddings,
            persist_directory=PERSIST_DIR,
        )

    print("[Step 2] 新建索引...")
    return Chroma.from_documents(
        documents=docs,
        embedding=embeddings,
        collection_name=COLLECTION_NAME,
        persist_directory=PERSIST_DIR,
        collection_metadata={"hnsw:space": "cosine"},
    )


def main():
    if not os.path.exists(CHUNKS_PATH):
        print(f"[!] 找不到 {CHUNKS_PATH}，请先运行 python3 step1_parse.py")
        sys.exit(1)

    print("[Step 2] 加载文档...")
    docs = load_documents()
    print(f"         原始 Document 数: {len(docs)}")

    print("[Step 2] 切分文档...")
    split_docs = split_documents(docs)
    print(f"         切分后 Document 数: {len(split_docs)}")
    avg_len = sum(len(d.page_content) for d in split_docs) / len(split_docs) if split_docs else 0
    print(f"         平均 chunk 长度: {avg_len:.0f} 字符")

    # 保存切分结果供 hybrid_search BM25 端复用（保证 BM25 和 Chroma 同源）
    split_data = []
    for d in split_docs:
        chunk_id = d.metadata.get("chunk_id", d.metadata.get("section_id", ""))
        cc = d.metadata.get("chunk_count", 1)
        ci = d.metadata.get("chunk_index", 0)
        unique_id = f"{chunk_id}_p{ci}" if cc > 1 else chunk_id
        d.metadata["chunk_id"] = unique_id  # 回写 metadata 保持一致
        split_data.append({
            "id": unique_id,
            "content": d.page_content,
            "source_id": d.metadata.get("source_id", d.metadata.get("section_id", "")),
            "chunk_version": d.metadata.get("chunk_version", 1),
            "metadata": d.metadata,
        })
    with open(CHUNKS_SPLIT_PATH, "w", encoding="utf-8") as f:
        json.dump(split_data, f, ensure_ascii=False, indent=2)
    print(f"         切分结果已保存: {CHUNKS_SPLIT_PATH} ({len(split_data)} chunks)")

    force = "--rebuild" in sys.argv
    vectorstore = build_vectorstore(split_docs, force_rebuild=force)

    print(f"\n[Step 2] 索引验证")
    print(f"         Collection: {vectorstore._collection.name}")
    print(f"         文档数: {vectorstore._collection.count()}")

    # 语义检索验证
    print(f"\n[Step 2] 语义检索测试...")
    results = vectorstore.similarity_search_with_score("大豆冷害区划指标", k=3)
    for i, (doc, score) in enumerate(results, 1):
        similarity = 1 - score  # cosine distance → similarity
        src = doc.metadata.get("source_file", "?")
        print(f"  [{i}] similarity={similarity:.4f} | {src[:40]}")
        print(f"      {doc.page_content[:100]}...")


if __name__ == "__main__":
    main()
