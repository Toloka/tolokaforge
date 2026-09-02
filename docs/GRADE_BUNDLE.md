# Grade Bundle Format v1.0

The **grade bundle** is a self-contained, part-addressable artifact holding everything the grader needs to score a trial: initial state, final state, filesystem snapshot, agent trajectory, custom checks, knowledge base, and the grading configuration itself. Bundles are portable across storage locations, bit-extractable by external tools, and content-addressable — the same trial serialised twice produces byte-identical bytes with matching digest.

## Purpose

The bundle is the wire between the runner (which produces trial artifacts) and any grader consumer (in-process, standalone service, trajectory-storage-backed, or a third-party analysis tool). Consumers parse the manifest, verify per-part digests, and read only the parts they need. There is no engine dependency: `manifest.json` + a POSIX filesystem + `jq` + `sha256sum` + `tar` are enough to consume a bundle end-to-end.

## Layout

```
<bundle-dir>/
├── manifest.json              # schema version, trial id, per-part digests
├── initial_state.json         # trial-start state (canonical JSON, sort_keys=True, %.6g floats)
├── final_state.json           # trial-end state (canonical JSON)
├── final_state_stable.json    # final state with unstable fields normalised
├── filesystem.tar             # workspace snapshot (USTAR, deterministic entries)
├── trajectory.json            # agent messages, tool calls, LLM turns
├── grading_config.json        # the grading block from the task pack
├── checks/                    # optional; per-check bytes (custom-check payloads)
│   ├── manifest.json          # nested manifest with per-file digests
│   └── <check-name>/...
└── kb/                        # optional; per-hit KB payloads
    ├── manifest.json
    └── <hit-id>/...
```

Every part named in the top-level `manifest.json` MUST be present on disk with a matching SHA-256 digest. Parts not named in the manifest are ignored by conforming readers.

## Manifest schema v1.0

```json
{
  "schema_version": "1.0",
  "trial_id": "<opaque string, e.g. task-name/2024-05-14T12:34:56Z-abc123>",
  "parts": {
    "initial_state.json":       { "sha256": "<hex>", "size": 1234 },
    "final_state.json":         { "sha256": "<hex>", "size": 4567 },
    "final_state_stable.json":  { "sha256": "<hex>", "size": 4321 },
    "filesystem.tar":           { "sha256": "<hex>", "size": 8192 },
    "trajectory.json":          { "sha256": "<hex>", "size": 23456 },
    "grading_config.json":      { "sha256": "<hex>", "size": 890 },
    "checks/manifest.json":     { "sha256": "<hex>", "size": 234 },
    "kb/manifest.json":         { "sha256": "<hex>", "size": 234 }
  }
}
```

Fields:
- `schema_version` — string, `"MAJOR.MINOR"`, digit-only components.
- `trial_id` — opaque string identifying the trial; producers should keep it stable across replays.
- `parts` — map of `rel_path -> {sha256, size}`. The map key IS the file's location relative to the bundle directory; `sha256` is the hex-encoded digest over the file's exact bytes on disk; `size` is the byte length.

The `checks/` and `kb/` subtrees are optional. When present, each carries its own nested `<subtree>/manifest.json` with per-file digests for the subtree's contents; the top-level manifest names only the nested manifest's digest.

## Deterministic serialisation rules

The bundle is content-addressable, which requires bit-exact serialisation. Every producer — Python, or a re-implementation in any other language — MUST follow these rules exactly.

**JSON parts** (`manifest.json`, `initial_state.json`, `final_state.json`, `final_state_stable.json`, `trajectory.json`, `grading_config.json`, nested `manifest.json` files):
- Encoding: UTF-8, no BOM.
- Structure: `json.dumps(payload, sort_keys=True, separators=(",", ":"))`. Keys sorted lexicographically at every nesting level; no whitespace between tokens.
- Floats: formatted with `%.6g` (6 significant digits, trailing-zero stripping) before serialisation. `0.123456789` writes as `"0.123457"`. This matches the byte-parity harness's canonicalisation.
- No trailing newline.

**`filesystem.tar`**:
- Format: **USTAR (POSIX.1-1988), no PAX extensions.** This is load-bearing for cross-language byte identity. Python's `tarfile` module defaults to PAX (auto-injects extension headers for names > 100 chars, links > 100 chars, sizes > 8 GB, or non-ASCII paths); producers in other languages MUST emit USTAR — a PAX-format tar of the same tree diverges the bytes on any long path or non-ASCII name.
- Entries added in `sorted(name)` order (lexicographic, POSIX path).
- Each entry: `type = REGTYPE` (regular file), `mtime = 0`, `uid = 0`, `gid = 0`, `uname = ""`, `gname = ""`, `mode = 0o644`, no `pax_headers`.
- Exclusion policy: subtrees named `.git`, `.venv`, `node_modules`, `dist`, `.next` are omitted at any depth.
- Empty tar (no included files) is a valid file — 1024 zero bytes.

**Per-part digests** (`sha256` field in the manifest):
- SHA-256 computed on the file's exact bytes on disk, post-canonicalisation.
- Digest naming: hex-encoded lower-case, no separators.

**Manifest file** (`manifest.json`) is written LAST, after every other part exists and its digest is known.

## Content-addressability

The bundle's canonical name is `sha256(manifest.json)` — the SHA-256 of the manifest file's bytes.

Because parts' digests live inside the manifest, changing any part changes its manifest entry, which changes the manifest, which changes the bundle's canonical name. The name recursively certifies every byte in every part.

The manifest itself does NOT contain its own name (that would be recursive). Consumers compute the name over the manifest bytes on read.

## Schema-version compatibility

Readers refuse unknown MAJOR versions and accept unknown MINOR versions.

- `"1.0"`, `"1.5"`, `"1.99"` — accepted by a v1 reader.
- `"2.0"` — refused.
- Missing `schema_version` key, empty string, non-string type, malformed value (`"1"` no dot, `"1.0.0"` three components, `"abc"` non-digit) — refused with `BundleSchemaVersionError`.

Producers MUST write `schema_version` as a `"MAJOR.MINOR"` digit-only string.

## External-consumer pattern

An external tool that consumes a bundle needs no engine dependency:

1. Read `manifest.json` from the bundle directory.
2. Validate `schema_version` — reject unknown MAJOR.
3. For each part named in `parts`: read the file at the map key (which IS the relative path), compute SHA-256 over its bytes, compare against `parts[name].sha256`. On mismatch, refuse the bundle.
4. Load the parts the consumer needs — parse the JSON parts, extract the tar, etc.

A pure-shell approximation using `jq`, `sha256sum`, and `tar` is possible and demonstrates the format is truly language-neutral. The Python reference reader is `tolokaforge.core.grading.bundle.load_grade_bundle`; consumers in other languages implement the same manifest walk.

## Not covered in v1.0

- Optional `provenance.json` sibling for engine-bump debugging (not covered by the manifest digest): [#1428](https://github.com/Toloka/tolokaforge/issues/1428).
- Cross-language parser example (`jq` + `sha256sum` + `tar tvf`): [#1430](https://github.com/Toloka/tolokaforge/issues/1430).
- Pre-materialised `db_probes.json` for offline `db_probe` grading (a snapshot substrate refuses `db_probe` in v1.0 because the DSN is only reachable inside the task's docker network): [#1438](https://github.com/Toloka/tolokaforge/issues/1438).
- Indexed KB snapshot for offline `knowledge_search` (a snapshot substrate returns `None` in v1.0 because the optional `kb/` subtree carries raw bytes without a queryable index): [#1439](https://github.com/Toloka/tolokaforge/issues/1439).
