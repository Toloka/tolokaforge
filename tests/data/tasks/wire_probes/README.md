# wire_probes - non-scoring wire-shape smoke task-pack

Multi-step / behavioural task-pack that elicits known formatting/codec
wire-shape quirks under realistic tool-calling load. NON-SCORING (combine-only
grading, no oracle). One merged shared domain; each testcase enables only the
tools it needs.

- `dataset/testcases/record_*` - atomic behavioural probes (the wire-shape
  classes that are NOT convertible to a deterministic
  `tolokaforge.testing.certify.suite` capability probe: id-threading, fan-in,
  enum/format/validation discipline, parallel calls, deep nesting, free-form
  objects).
- `dataset/testcases/cascade_*` - complex multi-turn cascades derived (shape +
  flow only, policy stripped, entities renamed) from public benchmarks
  (tau2-bench telecom, API-Bank tool-registry). See `NOTICE`.

How to run:
- Run via the engine: `tolokaforge run --config <run.yaml whose task_packs point at dataset/>`,
  then inspect the trajectories / wire-shapes in the output. NON-SCORING: you observe the
  emitted shapes (stringified args, dropped ids, flattened unions, ...), there is no pass/fail.
- A per-model pytest driver is a planned follow-up: `tests/integration/test_wire_probe_smoke.py`.

The convertible atomic probes are separate capability tests under
`tolokaforge/testing/certify/suite/` (dict-map, decimal,
discriminated-union, recursive, heterogeneous-array, allOf, …).
