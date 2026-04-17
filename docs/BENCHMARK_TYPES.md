# Benchmark Types

Tolokaforge OSS v1 supports the following benchmark types (ARC-AGI excluded by design):

1. Coding
2. STEM reasoning/coding
3. Long-horizon docs
4. Tool-use
5. Browser-use
6. Mobile-use
7. Terminal-use
8. Deep research
9. Knowledge/reasoning

## Resource Requirements

| Benchmark type | Requirements |
| --- | --- |
| Knowledge/reasoning | API key only (single-turn) or API key + orchestrator (multi-turn) |
| Tool-use | API key + tool dependencies (often Docker + JSON DB) |
| Coding | API key + Docker/container runtime |
| STEM reasoning/coding | API key + Docker/container runtime |
| Terminal-use | API key + sandbox shell runtime |
| Browser-use | API key + Playwright + mock-web services |
| Mobile-use | API key + Playwright + mock-web/DB services |
| Long-horizon docs | API key + RAG service (+ LibreOffice headless for GDPval-style office document workflows) |
| Deep research | API key + controlled mock-web + search/index tooling |

## Public Example Expectations

1. At least 2 public examples per benchmark type.
2. Each example must run end-to-end in CI.
3. Each example must include non-trivial scorer constraints (including failure-mode checks).

## Related Docs

1. `docs/KNOWLEDGE_REASONING.md`
2. `docs/FUTURE_DEVELOPMENT.md`
