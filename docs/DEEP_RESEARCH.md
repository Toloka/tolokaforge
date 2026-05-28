# Deep Research Benchmark Type

## Goal

Evaluate research-style agent behavior over a **controlled web corpus**:

1. Agents search and browse mock websites controlled by the benchmark.
2. Agents synthesize findings into a report.
3. Scoring emphasizes evidence quality, coverage, and consistency.

## OSS v1 Scope

1. Harness support for deep-research workflows.
2. Proof-of-concept public examples in `examples/deep_research/dataset/tasks/deep_research/`.
   - Current PoC includes 10 controlled mock source pages across two example tasks.
3. Full-scale content (hundreds/thousands of sites) is out of OSS v1 content scope.

## Runtime Pattern

1. `mock-web` serves controlled source pages.
2. Tools typically include:
   - `browser`
   - `search_kb` (or controlled search tool)
   - `read_file`
3. Optional RAG service for indexed corpora.

## Task Authoring Notes

1. Keep source content deterministic and versioned.
2. Define required claims and acceptable evidence in scorer rules.
3. Include explicit failure modes:
   - unsupported claims
   - missing citations
   - contradiction between cited sources

## Scoring Pattern

Use a blend of:
1. Deterministic checks (required fields/citations/structure).
2. Rubric scoring for synthesis quality and evidence attribution.

## Example Locations

1. `examples/deep_research/dataset/tasks/deep_research/deep_research_public_example_01/`
2. `examples/deep_research/dataset/tasks/deep_research/deep_research_public_example_02/`
