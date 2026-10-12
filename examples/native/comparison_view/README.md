# Comparison view example — documents, corrections and holds

Reference pack for `state_checks.comparison_view`: a state hash against a golden
path, read through a view that decides which records count and keys a generated id by
its record's content. See [docs/GRADING.md § Comparison view](../../../docs/GRADING.md#comparison-view)
and [ADR-0053](../../../docs/adr/0053-comparison-view-before-the-state-hash.md).

The task asks the agent to file client C-1's invoice INV-2002 and a correction against
it. The golden path files the invoice as `DOC-002` and cites it. The pack's tools number
every record in filing order and log every lookup, so a correct trajectory can leave a
different database than the golden path does:

| A trajectory that… | leaves | the view… |
|---|---|---|
| looks the client and the document up | rows in `lookup_log` | drops the table (`exclude_tables`) |
| files a draft, supersedes it, files again | a superseded `DOC-002`, the final document as `DOC-003` | drops the superseded draft (`exclude_records`) and keys the final document by `client_id` + `source_id`, rewriting the correction that cites it (`normalize_ids`) |
| places a hold and releases it | a released hold | drops it, unless a correction cites it (`exclude_records` with `unless_referenced_by`) |

Each of these grades like the golden path. A correction citing the superseded draft does
not: the reference is left as it was, and the view diff names it. A trial that files the
invoice twice without superseding either copy cannot be re-keyed — two documents share
one key — and fails with the collision as the reason.

`fixtures/unstable_fields.json` declares the generated ids and the timestamps. The
documents' id stays in the hash anyway: once re-keyed it is a function of content, and a
reference to the wrong document must not pass.

## Layout

```
examples/native/comparison_view/
  project.yaml
  run_config.yaml
  dataset/tasks/file_document_correction/
    task.yaml
    grading.yaml               # the hash, its golden path and the comparison view
    initial_state.json
    mcp_server.py              # the document-desk tools
    system_prompt.md
    fixtures/
      tools.json
      unstable_fields.json
```

## Validate

```bash
uv run tolokaforge validate --tasks "examples/native/comparison_view/dataset/**/task.yaml" --strict-authoring
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/native/comparison_view/run_config.yaml
```

`tests/canonical/test_comparison_view_example_pack.py` grades scripted trajectories of
this pack through both substrates and pins the verdict and the recorded view of each.
