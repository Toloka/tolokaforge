"""RAG search tool for knowledge base retrieval"""

from typing import Any

import httpx

from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult


class SearchKBTool(Tool):
    """Search knowledge base using RAG"""

    def __init__(self, rag_url: str = "http://rag-service:8001"):
        policy = ToolPolicy(
            timeout_s=15.0,
            category=ToolCategory.READ,
            visibility=["agent"],
        )
        super().__init__(
            name="search_kb",
            description="Search the knowledge base for relevant information",
            policy=policy,
        )
        self.rag_url = rag_url

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query to find relevant documents",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Number of results to return (default: 5)",
                            "default": 5,
                        },
                        "alpha": {
                            "type": "number",
                            "description": "Weight for hybrid search: 0.0=BM25 only (keyword), 1.0=FAISS only (semantic), 0.5=balanced (default: 0.5)",
                            "default": 0.5,
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, query: str, top_k: int = 5, alpha: float = 0.5) -> ToolResult:
        """Execute hybrid search"""
        try:
            response = httpx.post(
                f"{self.rag_url}/search",
                json={"query": query, "top_k": top_k, "alpha": alpha},
                timeout=self.policy.timeout_s,
            )
            response.raise_for_status()
            results = response.json()

            if not results:
                return ToolResult(
                    success=True,
                    output="No relevant documents found.",
                    metadata={"count": 0},
                )

            # Format results
            method = results[0].get("retrieval_method", "hybrid") if results else "hybrid"
            output_lines = [f"Found {len(results)} relevant documents (method: {method}):\n"]
            for i, result in enumerate(results, 1):
                output_lines.append(f"\n[{i}] Document: {result['doc_id']}")
                output_lines.append(f"    Source: {result['source']}")
                output_lines.append(f"    Score: {result['score']:.3f}")
                output_lines.append(f"    Content: {result['text'][:200]}...")

            return ToolResult(
                success=True,
                output="\n".join(output_lines),
                metadata={
                    "count": len(results),
                    "top_score": results[0]["score"] if results else 0,
                    "method": method,
                },
            )

        except httpx.HTTPError as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Search failed: {str(e)}",
            )
