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
PERSIST_DIR = str(BASE_DIR / "vectordb")
COLLECTION_NAME = "agri_zoning"
EMBED_MODEL = "BAAI/bge-small-zh-v1.5"

# 中文适用的切分器：按段落标记递归切分，1000字符/chunk，200字符重叠
TEXT_SPLITTER = RecursiveCharacterTextSplitter(
    separators=["\n\n", "\n", "。", "；", "，", " ", ""],
    chunk_size=1000,
    chunk_overlap=200,
)


def load_documents() -> list[Document]:
    """从 step1_parse 产出的 chunks.json 加载为 LangChain Document 列表。
    跳过 excluded=True 和 quality=low 的 chunk。"""
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
        header = f"[{m.get('province', '')} {m.get('crop', '')} {m.get('zoning_type', '')}] "
        docs.append(Document(
            page_content=header + c["content"],
            metadata={
                **c["metadata"],
                "chunk_id": c["id"],
            },
        ))
    if skipped_excluded:
        print(f"         跳过 excluded: {skipped_excluded}")
    if skipped_low:
        print(f"         跳过 low quality: {skipped_low}")
    return docs


def _has_table(content: str) -> bool:
    """检测是否包含 markdown 表格"""
    return bool(re.search(r'\|.+\|', content))


def split_documents(docs: list[Document]) -> list[Document]:
    """切分文档。≤800字的短chunk和含表格的chunk保持完整。
    被切分的文档在子文档 metadata 中保留 parent_content，供检索时展开。"""
    to_split = []
    keep_intact = []
    for d in docs:
        if len(d.page_content) <= 800 or _has_table(d.page_content):
            keep_intact.append(d)
        else:
            to_split.append(d)

    # 切分前将原始内容写入 metadata，LangChain splitter 的子文档会自动继承
    for d in to_split:
        d.metadata["parent_content"] = d.page_content

    split_result = TEXT_SPLITTER.split_documents(to_split)

    print(f"         免切分: {len(keep_intact)} 条 (≤800字或含表格)")
    print(f"         被切分: {len(to_split)} 条 → {len(split_result)} 条")

    return split_result + keep_intact


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
