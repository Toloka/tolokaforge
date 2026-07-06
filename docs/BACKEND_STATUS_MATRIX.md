# Backend Status Matrix

This matrix tracks benchmark-type backend readiness in the open-source
harness.

Status values:
- `ready`: usable now with public examples + CI coverage
- `in_progress`: partial support, active build
- `planned`: design defined, implementation pending

| Benchmark type | Status | Owner | Notes |
| --- | --- | --- | --- |
| Mobile-use | ready | Harness core | Public examples + CI active |
| Browser-use | ready | Harness core | Public examples + CI active |
| Tool-use (Tau) | ready | Harness core | Adapter implemented |
| Tool-use (MCP JSON) | ready | Harness core | Adapter implemented |
| Coding | ready | Harness core | Public examples + deterministic/action-based scoring active |
| STEM reasoning/coding | ready | Harness core | Numeric/scientific examples + scoring active |
| Long-horizon docs | ready | Harness core | RAG-oriented examples + rubric/state scoring active |
| Terminal-use | ready | Harness core | Shell-oriented examples + output/scoring templates active |
| Deep research | ready | Harness core | Controlled-web PoC with 10 public sources + rubric/state scoring |
| Knowledge/reasoning | ready | Harness core | Single-turn + multi-turn coverage with unified pipeline |

Update this matrix before every release-candidate tag.
