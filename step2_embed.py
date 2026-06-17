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

# 中文适用的切分器：按段落标记递归切分，500字符/chunk，50字符重叠
TEXT_SPLITTER = RecursiveCharacterTextSplitter(
    separators=["\n\n", "\n", "。", "；", "，", " ", ""],
    chunk_size=500,
    chunk_overlap=50,
)


def load_documents() -> list[Document]:
    """从 step1_parse 产出的 chunks.json 加载为 LangChain Document 列表。"""
    with open(CHUNKS_PATH, "r", encoding="utf-8") as f:
        chunks = json.load(f)

    docs = []
    for c in chunks:
        docs.append(Document(
            page_content=c["content"],
            metadata={
                **c["metadata"],
                "chunk_id": c["id"],
            },
        ))
    return docs


def split_documents(docs: list[Document]) -> list[Document]:
    """用 RecursiveCharacterTextSplitter 切分文档。"""
    return TEXT_SPLITTER.split_documents(docs)


def build_vectorstore(docs: list[Document], force_rebuild: bool = False) -> Chroma:
    """构建或加载 Chroma 向量存储。"""
    embeddings = HuggingFaceEmbeddings(
        model_name=EMBED_MODEL,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )

    if force_rebuild:
        print("[Step 2] 强制重建索引...")
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
