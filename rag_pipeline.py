"""
农业区划 RAG Pipeline（LangGraph 实现）

使用 LangGraph StateGraph 编排完整的 RAG 流程：

  START → retrieve → judge ──(answer)──→ generate → END
                    ├──(fallback)──→ generate → END
                    └──(reject)───→ reject ──→ END

用法:
  from rag_pipeline import RAGPipeline
  rag = RAGPipeline()
  result = rag.query("黑龙江大豆冷害区划用什么指标")
"""

import os
import time
from dataclasses import dataclass, field
from typing import TypedDict, Annotated

from langgraph.graph import StateGraph, END

from hybrid_search import HybridSearcher
from judge import judge
from generator import generate, generate_rejection
from query_rewriter import expand_query


# ── State 定义 ───────────────────────────────────────

class RAGState(TypedDict):
    """LangGraph 状态，在节点间流转。"""
    query: str
    search_queries: list[str]   # 改写后的多路查询
    results: list[dict]         # 检索结果
    decision: str               # "answer" | "reject" | "fallback"
    judge_reason: str
    judge_confidence: float
    judge_method: str
    answer: str
    sources: list[dict]
    elapsed_ms: float


# ── 结果数据类 ───────────────────────────────────────

@dataclass
class RAGResult:
    """RAG 查询结果。"""
    query: str
    answer: str
    decision: str
    confidence: float
    reasoning: str
    sources: list[dict] = field(default_factory=list)
    elapsed_ms: float = 0.0
    method: str = ""


# ── RAG Pipeline (LangGraph) ─────────────────────────

class RAGPipeline:
    """
    基于 LangGraph 的农业区划 RAG 管道。

    图结构:
      retrieve → judge → [conditional] → generate / reject → END

    Args:
        enable_reranker: 是否启用 CrossEncoder 精排
        enable_rewrite:  是否启用查询改写（需要 LLM）
    """

    def __init__(self, enable_reranker: bool = True, enable_rewrite: bool = True, top_k: int = 5):
        self.enable_reranker = enable_reranker
        self.enable_rewrite = enable_rewrite
        self.top_k = top_k
        self._searcher: HybridSearcher | None = None
        self._graph = None
        self._init()

    def _init(self):
        self._searcher = HybridSearcher(enable_reranker=self.enable_reranker)
        self._graph = self._build_graph()

    # ── 节点 ──────────────────────────────────────

    def _retrieve_node(self, state: RAGState) -> dict:
        """检索节点：改写查询 → 多路检索 → 去重。"""
        search_queries = [state["query"]]
        if self.enable_rewrite:
            search_queries = expand_query(state["query"], mode="all")

        all_results = []
        for sq in search_queries:
            results = self._searcher.search(sq, top_k=self.top_k, expand_parent=True)
            all_results.extend(results)

        # 按 content 前 80 字符去重，保留最高相似度
        seen = {}
        for r in all_results:
            key = r["content"][:80]
            sim = r.get("similarity", 0)
            if key not in seen or sim > seen[key].get("similarity", 0):
                seen[key] = r

        deduped = sorted(seen.values(), key=lambda r: r.get("similarity", 0), reverse=True)
        return {
            "search_queries": search_queries,
            "results": deduped[:self.top_k],
        }

    def _judge_node(self, state: RAGState) -> dict:
        """判定节点：三层判断。"""
        j = judge(state["query"], state["results"])
        return {
            "decision": j["decision"],
            "judge_reason": j["reason"],
            "judge_confidence": j["confidence"],
            "judge_method": j["method"],
        }

    def _generate_node(self, state: RAGState) -> dict:
        """生成节点：基于检索结果生成答案。"""
        if state["decision"] == "reject":
            answer = generate_rejection(state["query"], state.get("judge_reason", ""))
        else:
            answer = generate(state["query"], state["results"])
            if state["decision"] == "fallback":
                answer = f"[资料部分相关，以下信息仅供参考]\n\n{answer}"

        sources = []
        for r in state["results"][:3]:
            meta = r.get("metadata", {})
            sources.append({
                "source_file": meta.get("source_file", ""),
                "province": meta.get("province", ""),
                "zoning_type": meta.get("zoning_type", ""),
                "similarity": r.get("similarity", 0),
            })

        return {"answer": answer, "sources": sources}

    # ── 路由 ──────────────────────────────────────

    def _route_after_judge(self, state: RAGState) -> str:
        """判定后路由：answer → generate, fallback → generate, reject → generate_rejection（走 generate 节点统一处理）。"""
        return "generate"

    # ── 构建图 ────────────────────────────────────

    def _build_graph(self):
        graph = StateGraph(RAGState)

        graph.add_node("retrieve", self._retrieve_node)
        graph.add_node("judge", self._judge_node)
        graph.add_node("generate", self._generate_node)

        graph.set_entry_point("retrieve")
        graph.add_edge("retrieve", "judge")
        graph.add_edge("judge", "generate")
        graph.add_edge("generate", END)

        return graph.compile()

    # ── 查询接口 ──────────────────────────────────

    def query(self, query: str) -> RAGResult:
        """端到端 RAG 查询。"""
        t0 = time.time()
        state = self._graph.invoke({"query": query})
        elapsed = round((time.time() - t0) * 1000, 1)

        return RAGResult(
            query=query,
            answer=state.get("answer", ""),
            decision=state.get("decision", "reject"),
            confidence=state.get("judge_confidence", 0),
            reasoning=state.get("judge_reason", ""),
            sources=state.get("sources", []),
            elapsed_ms=elapsed,
            method=state.get("judge_method", ""),
        )


# ── 测试 ──────────────────────────────────────────

if __name__ == "__main__":
    from pathlib import Path
    import json

    CHUNKS = Path(__file__).resolve().parent / "data" / "chunks.json"
    if not os.path.exists(CHUNKS):
        print("请先运行 python3 step1_parse.py 构建数据")
        exit(1)

    # 无 LLM 测试（Reranker 使用本地模型，不需要 API Key；rewrite 需要 LLM，关闭）
    rag = RAGPipeline(enable_reranker=True, enable_rewrite=False)
    print("=" * 70)
    print("  农业区划 RAG Pipeline 测试 (LangGraph + LangChain)")
    print("=" * 70)

    test_queries = [
        "黑龙江大豆冷害区划选用了哪些气象指标？",
        "冬小麦品质区划的归一化方法是什么？",
        "江西省柑橘种植的气候适宜性如何划分？",
    ]

    for q in test_queries:
        print(f"\n{'─' * 60}")
        print(f"[Query] {q}")
        try:
            result = rag.query(q)
            print(f"  决策: {result.decision} | 置信度: {result.confidence:.0%} | 耗时: {result.elapsed_ms:.0f}ms")
            print(f"  理由: {result.reasoning}")
            print(f"  方法: {result.method}")
            if result.sources:
                print(f"  来源: {result.sources[0]['source_file'][:50]} ({result.sources[0]['province']})")
                print(f"  内容预览: {result.answer[:200]}...")
        except Exception as e:
            print(f"  [!] 错误: {e}")
