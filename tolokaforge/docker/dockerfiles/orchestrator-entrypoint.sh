#!/usr/bin/env bash
#
# Orchestrator image entrypoint. Keeps the base image lean — the engine plus the
# native backend, no harness adapters bundled — and installs ONLY the adapters a
# given run asks for, into the image's venv, just before handing off to the CLI.
#
# An adapter registers its ``tolokaforge.adapters`` entry point from its installed
# distribution metadata, so it must be pip-installed (not merely on PYTHONPATH)
# for the engine to discover it. Two request channels, additive:
#
#   * Mounted wheels — every ``*.whl`` under ``$TOLOKAFORGE_ADAPTERS_DIR``
#     (default ``/work/adapters``). The zero-index path: mount a directory of
#     adapter wheels (e.g. an internal adapter not on a public index) and they
#     install at start. Their dependencies resolve from the configured index
#     (PyPI by default); mount those wheels too for a fully offline run.
#   * Named specs — whitespace-separated requirement specifiers in
#     ``$TOLOKAFORGE_ADAPTERS`` (e.g. ``tolokaforge-adapter-terminal-bench==1.2.0``).
#     Honors pip's standard ``PIP_INDEX_URL`` / ``PIP_EXTRA_INDEX_URL`` for an
#     internal index.
#
# With neither set, nothing installs and the image stays exactly the native base.
set -euo pipefail

ADAPTERS_DIR="${TOLOKAFORGE_ADAPTERS_DIR:-/work/adapters}"

specs=()
if [[ -d "${ADAPTERS_DIR}" ]]; then
    shopt -s nullglob
    for whl in "${ADAPTERS_DIR}"/*.whl; do
        specs+=("${whl}")
    done
    shopt -u nullglob
fi
if [[ -n "${TOLOKAFORGE_ADAPTERS:-}" ]]; then
    # shellcheck disable=SC2206  # intentional word-splitting of a spec list
    specs+=(${TOLOKAFORGE_ADAPTERS})
fi

if [[ ${#specs[@]} -gt 0 ]]; then
    echo "orchestrator: installing ${#specs[@]} adapter(s): ${specs[*]}" >&2
    pip install --no-cache-dir --no-compile "${specs[@]}" >&2
fi

exec tolokaforge "$@"
