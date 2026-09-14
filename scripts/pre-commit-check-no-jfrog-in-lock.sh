#!/usr/bin/env bash
# Pre-commit guard: reject a `uv.lock` containing `toloka.jfrog.io` URLs.
#
# The tolokaforge repo is public; its committed `uv.lock` should resolve
# through public PyPI. Toloka Macs have their personal `~/.config/uv/uv.toml`
# pointing at Toloka's JFrog mirror (written by the Toloka Claude plugin, per
# the 2026-08-28 #team-tech supply-chain announcement), so a local `uv lock`
# on a Toloka machine can silently produce a lockfile carrying internal URLs.
# Committing that lockfile would leak Toloka-internal infrastructure into the
# public tree and break external contributors / arena runners / CI, whose
# environments can't reach `toloka.jfrog.io`.
#
# The check is scoped to staged `uv.lock` files at any depth (workspace root,
# tolokaforge_models/, tolokaforge_coding_harnesses/, external adapters). It
# runs on `pre-commit` and is invisible to anyone whose `uv lock` output
# already resolves against pypi.org.
set -euo pipefail

# Files pre-commit passes are staged files matching the hook's `files` regex.
# Empty argv means nothing to check (hook wired with `pass_filenames: true`).
if [ $# -eq 0 ]; then
    exit 0
fi

status=0
for file in "$@"; do
    if [ ! -f "$file" ]; then
        # File deleted or renamed; nothing to check.
        continue
    fi
    if grep -q 'toloka\.jfrog\.io' "$file"; then
        count=$(grep -c 'toloka\.jfrog\.io' "$file")
        echo "ERROR: ${file} contains ${count} reference(s) to \`toloka.jfrog.io\`." >&2
        echo "" >&2
        echo "  This lockfile was regenerated on a machine whose personal uv/pip config" >&2
        echo "  points at Toloka's JFrog mirror. That's fine for local use, but the" >&2
        echo "  committed lockfile must resolve through public PyPI so external" >&2
        echo "  contributors, arena runners, and CI (which can't reach toloka.jfrog.io)" >&2
        echo "  can install it." >&2
        echo "" >&2
        echo "  Fix: regenerate the lockfile in an environment without Toloka's" >&2
        echo "  personal config — a GitHub Codespace, a one-off Docker container:" >&2
        echo "    docker run --rm -v \"\$PWD\":/w -w /w python:3.12-slim \\" >&2
        echo "      sh -c \"pip install uv && uv lock\"" >&2
        echo "  Then re-stage \`${file}\` and commit." >&2
        status=1
    fi
done

exit $status
