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

# Seed .env. We never bake secrets into the image.
#
# Important: scripts/with_env.sh loads .env with `set -o allexport`, so values in
# .env OVERRIDE the process environment. .env.example ships an *active*
# placeholder (OPENROUTER_API_KEY=your-openrouter-api-key-here); if left as-is it
# would (a) masquerade as "set" and (b) clobber a real key provided via a
# Codespaces secret / shell env — causing a 401. So we neutralize the placeholder
# and only ever write *real* values.
if [ ! -f .env ]; then
  cp .env.example .env
  echo "[post-create] created .env from .env.example"
fi

# Comment out the active placeholder so it can't shadow a real key.
sed -i 's|^[[:space:]]*OPENROUTER_API_KEY=your-openrouter-api-key-here.*|# OPENROUTER_API_KEY=  # set a real key here, or provide one via a Codespaces secret / shell env|' .env

# If a real key is present in the environment (e.g. a Codespaces secret with
# repository access), write it into .env.
for var in OPENROUTER_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY GOOGLE_API_KEY GEMINI_API_KEY; do
  val="${!var:-}"
  [ -n "$val" ] || continue
  [ "$val" = "your-openrouter-api-key-here" ] && continue
  if grep -qE "^#?[[:space:]]*${var}=" .env; then
    sed -i "s|^#\?[[:space:]]*${var}=.*|${var}=${val}|" .env
  else
    echo "${var}=${val}" >> .env
  fi
  echo "[post-create] wrote ${var} to .env from an environment secret"
done

if ! grep -qE '^OPENROUTER_API_KEY=.' .env; then
  echo "[post-create] NOTE: no OpenRouter key configured yet. Set a Codespaces secret"
  echo "[post-create]       named OPENROUTER_API_KEY (with access to this repo) and rebuild,"
  echo "[post-create]       or add the key to .env, before running an eval."
fi

echo "[post-create] done."
echo "[post-create] Run an example eval (needs an LLM key in .env):"
echo "    scripts/with_env.sh uv run tolokaforge run --config examples/native/coding/run_config.yaml"
