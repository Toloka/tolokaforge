# CLAUDE.md

Read `AGENTS.md` for all project instructions — it is the single source of truth.

## Claude-Specific Notes

- Claude Code reads this file automatically on session start
- All rules, commands, architecture, and conventions are in `AGENTS.md`
- Prefer available specialized MCP servers like `dev`, `context7`, `github`, `perplexity` over generic bash calling
- This repo uses a Claude Code GitHub Action for PR hygiene reviews (`.github/workflows/claude-review.yml`)
- The hygiene review checks: root cleanliness, no temp artifacts, script locations, tool registration, no project-specific content on main
- **dev MCP server changes need a user-side reload.** After any edit under
  `tools/dev-mcp/`, ask the user to reconnect (`/mcp`) — until they do,
  every dev tool call runs the OLD server code. This applies whether the
  edit came from you or from a subagent's stage.
- **Skills are loaded on demand.** Invoke them with `/<skill-name>`:
  - `/brainstorming` — turn an idea into an approved design before any implementation
  - `/code-review` — review the current branch against `AGENTS.md` rules
  - `/writing-development-tickets`, `/executing-development-tickets` —
    ticket lifecycle (the executing pipeline drives architect → critic →
    stage implementers → reviewer via the agents in `.claude/agents/`)
  - `/implement-milestone` — drive a whole GitHub milestone: sequence
    issues, run the ticket pipeline per issue, merge on green CI
- Slash-prefixed skills are user-invocable only — don't fabricate skill
  names. The list above is canonical.
