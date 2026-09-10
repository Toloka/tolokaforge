# JudgeKind κ-parity corpus

Twenty replayable judge-input fixtures the canonical parity lane drives.
The lane lives at
[`tests/canonical/test_judge_kind_parity.py`](../../canonical/test_judge_kind_parity.py);
the gate contract, thresholds, and corpus-authoring rules are in
[`docs/JUDGE_KINDS.md § Parity gate`](../../../docs/JUDGE_KINDS.md#parity-gate).

## Shape

Each `<family>/<case>.yaml` file is one
`ParityCorpusEntry`:

```yaml
entry_id: <slug used in test ids + κ reporting>
rubric:
  criteria:
    - id: <criterion_id>
      description: '...'
      kind: binary  # or graded
      weight: 1.0
agent_system_prompt: '...'
transcript:
  - role: user
    content: '...'
  - role: assistant
    content: '...'
state_diff: null            # or a rendered diff string
disable_knowledge_search: false
custom_system_prompt: null
include_agent_system_prompt: true
judge_scripts:
  single_shot_rubric:
    - - name: submit_report
        arguments:
          reasons: '...'
          <criterion_id>: true|false|<float 0..1>
          <criterion_id>_justification: "because <id>\nVERDICT: MET"
```

Three families of fixture shape:

| Family | Count | Criterion count | Transcript |
| --- | --- | --- | --- |
| `small_rubrics/` | 10 | 2–4 | single-turn |
| `large_rubrics/` | 6 | 8–15 | single-turn |
| `multi_turn/` | 4 | 2–4 | 3–5 turns |

## Authoring rules

- **Every criterion id MUST appear in ≥ 2 fixtures with mixed True /
  False verdicts.** A criterion that always returns the same label
  pool-wide produces `κ = undefined` (chance agreement collapses to 1),
  which the gate reports as `insufficient_evidence` — blocking the lane.
  When adding a new criterion, add it to enough fixtures with mixed
  verdicts to keep its per-criterion pool label-variant.
- **Justifications use double-quoted YAML strings** so `\n` interprets
  as a real newline — the judge's verdict-consistency regex needs the
  `VERDICT: MET` (or `VERDICT: NOT MET`, `SCORE: <n>`) marker on its
  own line. Single-quoted YAML preserves `\n` as two literal characters
  and would fail rubric validation at trial time (a
  `VerdictConsistencyError`, not a κ mismatch).
- **Every fixture ships a `judge_scripts.single_shot_rubric` cassette.**
  Fixture-kind cassettes (like the parity lane's `_FlakyJudgeKind`)
  are authored in the test file, not here — the corpus stays reusable
  by future kinds without carrying a per-kind script.

## Refreshing cassettes against a live judge

```bash
pytest --live-parity tests/canonical/test_judge_kind_parity.py
```

Requires `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` and skips the
runtime-budget assertions because live dispatch has no bounded latency.
CI never runs `--live-parity` — real judge tokens are out of scope for
the gate. The writeback is idempotent (a second run over freshly
written cassettes produces zero diff), so a developer refreshing after
a preset edit gets a clean, single-commit diff.
