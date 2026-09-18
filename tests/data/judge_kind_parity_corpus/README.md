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
judge_scripts_per_chunk:
  chunked_rubric:
    - - - name: submit_report                     # chunk 0 script (1 turn = 1 tool call)
          arguments:
            reasons: '...'
            <chunk_0_id>: true|false|<float>
            <chunk_0_id>_justification: "because <id>\nVERDICT: MET"
    - - - name: submit_report                     # chunk 1 script
          arguments:
            reasons: '...'
            <chunk_1_id>: true|false|<float>
            <chunk_1_id>_justification: "because <id>\nVERDICT: MET"
  voted_rubric:
    - - - name: submit_report                     # sample 0 script (K=3 total)
          arguments:
            reasons: '...'
            <criterion_id>: true|false|<float>    # every criterion, same as judge_scripts.single_shot_rubric
            <criterion_id>_justification: "because <id>\nVERDICT: MET"
    - - - name: submit_report                     # sample 1 script
          arguments: { ... }                      # byte-identical to sample 0 (default n_samples=3)
    - - - name: submit_report                     # sample 2 script
          arguments: { ... }
  jury_rubric:
    - - - name: submit_report                     # panel member 0 script (3 total, one per DEFAULT_PANEL member)
          arguments:
            reasons: '...'
            <criterion_id>: true|false|<float>    # every criterion, byte-identical to judge_scripts.single_shot_rubric
            <criterion_id>_justification: "because <id>\nVERDICT: MET"
    - - - name: submit_report                     # panel member 1 script
          arguments: { ... }                      # byte-identical to member 0 (default panel size 3)
    - - - name: submit_report                     # panel member 2 script
          arguments: { ... }
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
- **Every fixture ships a `judge_scripts.single_shot_rubric` cassette,
  a `judge_scripts_per_chunk.chunked_rubric` cassette, a
  `judge_scripts_per_chunk.voted_rubric` cassette, AND a
  `judge_scripts_per_chunk.jury_rubric` cassette.** The chunked
  cassette carries one script per chunk at `chunk_size=5` — small-rubric
  and multi-turn fixtures (2–4 criteria) ship one script (single-chunk
  degenerate case, mirroring the single-shot content); large-rubric
  fixtures (8–15 criteria) ship 2–3 scripts, each covering its chunk's
  criterion ids in original rubric order. The voted cassette carries
  exactly 3 scripts (K=3, the default `n_samples`), each covering every
  criterion and byte-identical to the fixture's `single_shot_rubric`
  script — `voted_rubric` samples the SAME rubric evidence K times, so
  there is no per-sample criterion split the way chunking splits by
  criterion id. The jury cassette carries exactly 3 scripts (the
  default panel size), same rationale as the voted cassette and also
  byte-identical to `single_shot_rubric`'s script — a panel of 3
  differently-routed but identically-scripted clients still needs
  identical verdicts for the self/cross-kind κ=1.0 locks to hold. Per-
  criterion verdicts across the chunk, voted, and jury scripts MUST
  match the single-shot cassette's for identical cross-kind κ.
  Fixture-kind cassettes (like the parity lane's `_FlakyJudgeKind`) are
  authored in the test file, not here — the corpus stays reusable by
  future kinds without carrying a per-kind script.
- **Adding a new multi-client kind (chunking, K-sampling, or a
  cross-family panel):** register it in `pyproject.toml`, add a
  `judge_scripts_per_chunk.<your_kind>` block to every fixture, and name
  it in the pool-provider path — no harness change needed. The provider
  dispatches on cassette shape (presence of
  `judge_scripts_per_chunk[NAME]`), so any kind that calls
  `judge_model_provider.build(...)` more than once per `evaluate` gets
  its N scripts from that map — chunking (`chunked_rubric`, one script
  per chunk), K-sampling (`voted_rubric`, one script per sample), and a
  cross-family panel (`jury_rubric`, one script per panel member) are
  three instances of the same shape, not three different maps. A
  single-client kind (`single_shot_rubric`) falls back to
  `judge_scripts[NAME]` instead.

## Refreshing cassettes against a live judge

The `--live-parity` flag is reserved for #1572; the cassette-refresh
writeback is not wired today.
