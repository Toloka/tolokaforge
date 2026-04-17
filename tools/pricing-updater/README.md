# pricing-updater

Fetch model pricing from the [OpenRouter API](https://openrouter.ai/api/v1/models) and update `tolokaforge/core/data/pricing.json`.

## Usage

```bash
# Update pricing.json from OpenRouter (merges with existing entries)
uv run pricing-updater update

# Replace all entries (no merge)
uv run pricing-updater update --no-merge

# Dry run — show fetched pricing without writing
uv run pricing-updater update --dry-run

# Write to a custom path
uv run pricing-updater update --output /tmp/pricing.json

# Show current pricing table
uv run pricing-updater show

# Filter by model name
uv run pricing-updater show minimax
```

## How it works

1. Fetches `GET https://openrouter.ai/api/v1/models` (no auth required)
2. Parses `pricing.prompt` and `pricing.completion` (per-token strings)
3. Converts to USD per 1M tokens
4. Merges with existing `pricing.json` (new entries override, old entries preserved)
5. Writes the result to `tolokaforge/core/data/pricing.json`
