#!/usr/bin/env bash
# Hydrate ``uv.lock`` from one of the committed variants.
#
# Toloka's supply-chain-security policy blocks direct PyPI access from
# workstations (see docs/DEV_SETUP.md); every other consumer (CI, arena
# runners, external contributors, expert containers) resolves through
# public PyPI. This repo commits both lockfiles as ``uv.lock.jfrog`` and
# ``uv.lock.public`` and keeps ``uv.lock`` itself untracked — pick the
# variant that matches where you run:
#
#   scripts/use-lock.sh jfrog    # internal Toloka contributor
#   scripts/use-lock.sh public   # external contributor, CI, arena runner
#
# The Makefile wraps this as ``make use-jfrog`` / ``make use-public``.
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $0 (jfrog|public)" >&2
    exit 2
fi
variant="$1"

case "$variant" in
    jfrog|public) ;;
    *) echo "unknown variant: $variant (expected 'jfrog' or 'public')" >&2; exit 2 ;;
esac

src="uv.lock.$variant"
if [ ! -f "$src" ]; then
    echo "$src is missing; run this from the repo root" >&2
    exit 2
fi

cp -f "$src" uv.lock
echo "uv.lock <- $src"
