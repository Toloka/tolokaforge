# Vendored Harbor adaptations

Source repository: `https://github.com/laude-institute/harbor`

Pinned revision: `8083897a5df169d804c5afefd116c8fe6ffd9f8e`

License: Apache-2.0. See `NOTICE` and the source repository's `LICENSE`.

Adapted source map:

- `base.py` derives the retry/non-retry error taxonomy from
  `src/harbor/agents/installed/base.py`.
- `claude_code.py` derives the headless command and declarative flag handling from
  `src/harbor/agents/installed/claude_code.py`.
- `trajectory.py` derives ATIF step/tool-observation construction from Harbor's
  Claude Code trajectory conversion.

Patch log:

1. Replaced Harbor environment access with caller-supplied values. Tolokaforge
   injects adapter-required credentials exclusively through `SecretManager`.
2. Replaced shell command interpolation and `tee` pipelines with argv execution and
   separately captured stdout/stderr.
3. Replaced Harbor environment/context models with Tolokaforge `Message`,
   `Trajectory`, and termination models.
4. Added authenticated streamable-HTTP MCP configuration with a per-trial bearer
   token and removed subscription/OAuth credential paths.
5. Restricted the imported surface to the behavior exercised by BYOH; installation,
   prompt templating, and unrelated provider modes remain upstream-only.

