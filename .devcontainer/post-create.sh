#!/usr/bin/env bash
# Devcontainer post-create for the public tolokaforge engine.
#
# Sets up everything an OSS developer needs to run an evaluation:
#   - uv (package manager)
#   - the project venv via uv sync + Playwright (reuses scripts/setup/*)
#   - Git LFS objects (test fixtures)
#   - pre-commit hooks
#   - a .env seeded from any provided API-key secret
#
# Docker-in-Docker is provided by the devcontainer feature, so `tolokaforge run`
# can build and run the containerised runner. No internal/Azure auth is used.
set -euo pipefail

echo "[post-create] installing uv..."
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# Make uv available to this script and to future interactive shells.
export PATH="$HOME/.local/bin:$PATH"
[ -f "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env" || true

echo "[post-create] creating venv (uv sync + Playwright)..."
scripts/setup/create_python_venv.sh

echo "[post-create] initializing Git LFS (non-fatal)..."
scripts/setup/init_git_lfs.sh || echo "[post-create] git-lfs step skipped/failed (non-fatal)"

echo "[post-create] installing pre-commit hooks (non-fatal)..."
uv run pre-commit install || echo "[post-create] pre-commit install skipped (non-fatal)"

# Seed .env. We never bake secrets into the image; if a key is present in the
# environment (e.g. a Codespaces secret), copy it into .env, otherwise leave the
# documented template for the developer to fill in.
if [ ! -f .env ]; then
  cp .env.example .env
  echo "[post-create] created .env from .env.example"
fi
for var in OPENROUTER_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY GOOGLE_API_KEY GEMINI_API_KEY; do
  val="${!var:-}"
  if [ -n "$val" ]; then
    if grep -qE "^#?[[:space:]]*${var}=" .env; then
      sed -i "s|^#\?[[:space:]]*${var}=.*|${var}=${val}|" .env
    else
      echo "${var}=${val}" >> .env
    fi
    echo "[post-create] wrote ${var} to .env from an environment secret"
  fi
done

echo "[post-create] done."
echo "[post-create] Run an example eval (needs an LLM key in .env):"
echo "    scripts/with_env.sh uv run tolokaforge run --config examples/native/coding/run_config.yaml"
