# langfuse-uploader

Upload tolokaforge trial bundles into [Langfuse](https://langfuse.com) so runs can be
explored as traces: per-turn generations with token/cost usage, tool-call spans,
grades as scores, and screenshots as media.

## Quickstart

```bash
export LANGFUSE_HOST=https://your-langfuse-host
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...

# upload a finished run
uv run langfuse-uploader upload results/<run-name>

# or poll while a run is in progress (state kept in <run_dir>/.langfuse_uploaded.json)
uv run langfuse-uploader watch results/<run-name>
```

API keys come from the target Langfuse project (Project Settings → API Keys). The keys
select the project traces land in, so pointing the same command at another environment
(dev vs prod) is purely a matter of env values — no code or flag changes.

## Mapping

| tolokaforge | Langfuse |
|---|---|
| run (`results/<run-name>`) | session (`sessionId`) + tag |
| trial (`trials/<task_id>/<trial_index>/`) | trace |
| assistant message | generation (usage/cost per turn from `metrics.usage.calls`) |
| tool message | span (input = tool-call arguments, output = tool result) |
| `grade.yaml` | scores (`binary_pass`, `score` + reasons, `component:*`) |
| `metrics.yaml` totals | trace metadata |
| base64 image blocks | Langfuse media (uploaded via the media API) |

Trace, observation and score ids are deterministic (`uuid5` over `--run-tag` / `--label` /
task id / trial index): re-running the uploader over the same run updates existing traces
instead of duplicating them. Bump `--run-tag` to force fresh traces.

## Options

| Option | Env | Default | Purpose |
|---|---|---|---|
| `--host` | `LANGFUSE_HOST` | — | Langfuse base URL |
| — | `LANGFUSE_PUBLIC_KEY` | — | project public key |
| — | `LANGFUSE_SECRET_KEY` | — | project secret key |
| `--label` | — | run dir name | grouping label in trace names/tags |
| `--session` | — | run dir name | Langfuse session id |
| `--run-tag` | — | `v1` | trace id namespace |
| `--media-put-via` | `LANGFUSE_MEDIA_PUT_VIA` | — | `host:port` override for presigned media PUTs |
| `--interval` (watch) | — | `15` | poll interval, seconds |

Notes:

- Credentials are read from the environment only (never CLI arguments), so they don't
  leak into shell history or process lists.
- Ingestion batches are chunked to stay under the Langfuse request body limit (~4.5 MB).
- Base64 images are uploaded through the Langfuse media API and replaced with reference
  tokens; if a media upload fails, the block is replaced with a small placeholder and the
  trial upload continues.
- `--media-put-via` is only needed when the presigned upload URL points at an object-store
  hostname not reachable from your machine (e.g. an in-cluster store) — route it through a
  port-forward while the signed `Host` header is preserved.

## CI

```yaml
- name: Upload trajectories to Langfuse
  if: always()
  env:
    LANGFUSE_HOST: ${{ vars.LANGFUSE_HOST }}
    LANGFUSE_PUBLIC_KEY: ${{ secrets.LANGFUSE_PUBLIC_KEY }}
    LANGFUSE_SECRET_KEY: ${{ secrets.LANGFUSE_SECRET_KEY }}
  run: uv run langfuse-uploader upload results/${{ env.RUN_NAME }} --label ${{ github.workflow }}
```
