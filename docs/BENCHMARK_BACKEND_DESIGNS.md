# Benchmark Backend Mini-Designs

This document captures backend/runtime design sketches per benchmark type.

When a benchmark needs services beyond the engine's built-ins (a real
database, a custom API, a bespoke topology) — the Case B/C path — a task
can declare its own docker-compose stack via an `environment_manifest`.
See the [multi-container tasks guide](multi_container_tasks.md).

## Coding

1. Runtime: sandboxed filesystem + shell tools.
2. Task shape: bug-fix/change request + repository/files fixtures.
3. Scoring: deterministic checks (tests/lints/output) + optional rubric scorer.
4. Key concerns: reproducibility, deterministic setup/teardown, safe command policy.

## STEM Reasoning/Coding

1. Runtime: coding backend with scientific/numeric dependencies.
2. Task shape: quantitative analysis, simulation, or computational workflow.
3. Scoring: deterministic numeric tolerances and structured output checks.
4. Key concerns: floating-point tolerance policy and reproducible environment seeds.

## Long-Horizon Docs

1. Runtime: retrieval (`search_kb`) + file reading + optional browser + optional headless office conversion (`libreoffice --headless`) for GDPval-style document workflows.
2. Task shape: multi-document evidence synthesis and decision making.
3. Scoring: rubric + evidence trace requirements + optional state checks.
4. Key concerns: chunking/index quality, citation traceability, long-context handling.

## Tool-Use

1. Runtime: MCP tool registry plus deterministic environment state.
2. Task shape: API/tool orchestration to satisfy structured user goals.
3. Scoring: tool-usage expectations + state checks + rubric for rationale quality.
4. Key concerns: tool schema stability, timeout behavior, graceful tool failures.

## Browser-Use

1. Runtime: browser tool + mock-web service.
2. Task shape: navigation/form/action workflows on controlled websites.
3. Scoring: state assertions + transcript/tool expectations.
4. Key concerns: deterministic selectors, navigation flake handling, route fixtures.

## Mobile-Use

1. Runtime: mobile tool abstraction on controlled app UIs via mock-web/DB.
2. Task shape: app and multi-app workflows (discovery, booking, ordering, notes/calendar).
3. Scoring: DB/state checks + transcript expectations.
4. Key concerns: shared dataset consistency and app-state reset determinism.

## Terminal-Use

1. Runtime: shell + file tools in restricted sandbox.
2. Task shape: inspect/transform/compute through terminal operations.
3. Scoring: deterministic file/state/output checks.
4. Key concerns: command allowlist policy, filesystem isolation, deterministic fixtures.

## Deep Research

1. Runtime: controlled mock-web + retrieval/search + summarization tools.
2. Task shape: investigate across many controlled sources and produce brief/report.
3. Scoring: rubric with citation/evidence requirements + contradiction handling.
4. Key concerns: source coverage, search relevance tuning, hallucination resistance.

## Knowledge/Reasoning

1. Runtime:
   - single-turn: prompt + scorer
   - multi-turn: orchestrator loop with optional tools
2. Task shape: benchmark QA/reasoning tasks with optional tool augmentation.
3. Scoring: exact/regex/structured checks or rubric scoring.
4. Key concerns: answer normalization, scoring consistency, optional tool policy.
