# Canonical (Snapshot) Tests

This directory contains **canonization tests** that compare adapter, grading, and
conversion outputs against golden snapshots committed to version control.

## How it works

1. Each test produces a `dict` from real adapter/grading logic.
2. The `canon_snapshot` fixture either **asserts** equality against a golden
   `.json` file in `snapshots/`, or **updates** the golden file when
   `--update-canon` is passed.

## Updating snapshots

```bash
# Regenerate all golden snapshots
uv run pytest tests/canonical/ --update-canon -v

# Verify snapshots match (CI mode)
uv run pytest tests/canonical/ -v
```

## Directory layout

```
tests/canonical/
├── conftest.py                    # --update-canon flag + canon_snapshot fixture
├── snapshots/                     # Golden data (committed to git)
│   ├── native_minimal_calc/
│   │   ├── task_config.json
│   │   └── grading_config.json
│   └── ...
├── test_native_adapter_canon.py   # Adapter output canonization
├── test_grading_canon.py          # Grading output canonization
└── test_conversion_canon.py       # Conversion output canonization
```

## Adding a new canonical test

1. Write a test that calls real code and produces a `dict`.
2. Use `canon_snapshot("my_suite")` to get a `CanonSnapshot` helper.
3. Call `snap.assert_match(actual_dict, "my_output.json")`.
4. Run `--update-canon` once to generate the golden file.
5. Commit the new snapshot file.

## When snapshots drift

If a code change intentionally alters output, re-run `--update-canon`,
review the diff, and commit the updated snapshots alongside the code change.
