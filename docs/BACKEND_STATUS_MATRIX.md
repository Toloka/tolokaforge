# Backend Status Matrix

This matrix tracks benchmark-type backend readiness for OSS v1 execution.

Status values:
- `ready`: usable in OSS v1 now
- `in_progress`: partial support, active build
- `planned`: design defined, implementation pending

| Benchmark type | Backend status | Owner | Target phase | ETA | Notes |
| --- | --- | --- | --- | --- | --- |
| Mobile-use | ready | Harness core | Phase 1 | current | Public examples + CI active |
| Browser-use | ready | Harness core | Phase 1 | current | Public examples + CI active |
| Tool-use | ready | Harness core | Phase 1 | current | Public `native` + `terminal_bench` examples |
| Coding | ready | Harness core | Phase 3 | current | Public examples + deterministic/action-based scoring active |
| STEM reasoning/coding | ready | Harness core | Phase 3 | current | Numeric/scientific examples + scoring active |
| Long-horizon docs | ready | Harness core | Phase 3 | current | RAG-oriented examples + rubric/state scoring active |
| Terminal-use | ready | Harness core | Phase 3 | current | Shell-oriented examples + output/scoring templates active |
| Deep research | ready | Harness core | Phase 3 | current | Controlled-web PoC with 10 public sources + rubric/state scoring |
| Knowledge/reasoning | ready | Harness core | Phase 3 | current | Single-turn + multi-turn coverage with unified pipeline |

## Notes

1. Phase 1 shipped incrementally by readiness.
2. Phase 3 completion moved all public benchmark types to `ready`.
3. Update this matrix before every release-candidate tag.
