# CLAUDE.md

Read `AGENTS.md` for all project instructions — it is the single source of truth.

## Claude-Specific Notes

- Claude Code reads this file automatically on session start
- All rules, commands, architecture, and conventions are in `AGENTS.md`
- Prefer available specialized MCP servers like `dev`, `context7`, `github`, `perplexity` over generic bash calling
- This repo uses a Claude Code GitHub Action for PR hygiene reviews (`.github/workflows/claude-code-review.yml`)
- The hygiene review checks: root cleanliness, no temp artifacts, script locations, tool registration, no project-specific content on main
