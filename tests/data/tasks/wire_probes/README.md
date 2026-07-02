# wire_probes - non-scoring wire-shape smoke task-pack

Multi-step / behavioural task-pack that elicits known formatting/codec
wire-shape quirks under realistic tool-calling load. NON-SCORING (combine-only
grading, no oracle). One merged shared domain; each testcase enables only the
tools it needs.

- `dataset/testcases/record_*` - atomic behavioural probes (the wire-shape
  classes that are NOT convertible to a deterministic `tests/integration/llm`
  capability test: id-threading, fan-in, enum/format/validation discipline,
  parallel calls, deep nesting, free-form objects).
- `dataset/testcases/cascade_*` - complex multi-turn cascades derived (shape +
  flow only, policy stripped, entities renamed) from public benchmarks
  (tau2-bench telecom, API-Bank tool-registry) and a policy-stripped
  manufacturing operations domain. See `NOTICE`.

Runners:
- static gate (no API): `tests/canonical/test_wire_probe_pack_valid.py`
- live behavioural (per-model, API-gated): `tests/integration/test_wire_probe_smoke.py`
  - planned follow-up, not in this pack yet.

The convertible atomic probes are separate capability tests under
`tests/integration/llm/` (dict-map, decimal, discriminated-union, recursive,
heterogeneous-array, allOf, …).
