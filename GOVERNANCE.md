# Governance

tolokaforge is an open-source project sponsored by [Toloka](https://toloka.ai/). This document describes how decisions are made, who is responsible for what, and how to get involved.

## Roles

**Maintainer.** Reviews and merges PRs, triages issues, cuts releases, and shepherds the roadmap. The active maintainer is listed under the repository's [About](https://github.com/Toloka/tolokaforge) sidebar (currently Ciro Gamboa on the Toloka team).

**Contributor.** Anyone who has had a PR merged. Contributors are credited in release notes and on the [GitHub contributors page](https://github.com/Toloka/tolokaforge/graphs/contributors).

**Sponsor.** Toloka provides engineering time, hosting for the arena / release infrastructure, and product direction. Sponsor-driven priorities are visible in the public roadmap at [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Decision-making

Most decisions happen in the open on issues and PRs.

- **Small changes** (bug fixes, docs, small features) — reviewed and merged by the maintainer on their normal cadence.
- **Compatibility-surface changes** (task contracts, task-pack formats, run-config schemas, CLI surface, published Python API) — need a design discussion first. Open a [Discussion](https://github.com/Toloka/tolokaforge/discussions) or file an umbrella issue. See `AGENTS.md` §Core Rule 5 for the migration expectations.
- **Architectural changes** — captured in an ADR under [`docs/adr/`](docs/adr/) before implementation lands. Existing ADRs are the template.

The maintainer has the final say on merges. When a decision is contested, the maintainer explains the reasoning in the issue or PR thread and links to the ADR when applicable.

## Contribution ladder

Progression is by contribution, not by tenure:

1. **First-time contributor** — file an issue, open a small PR, ask a question. All welcome.
2. **Regular contributor** — several merged PRs, engaged in reviews. Ping the maintainer if you'd like triage rights.
3. **Triager** — can label and close issues, cannot merge. Granted on ask after regular contribution.
4. **Maintainer** — merge rights and release rights. Added when the workload justifies it and the maintainer has confidence in your judgement on the compatibility-surface rules.

## Release cadence

Releases follow semantic versioning and are cut on demand — usually every 2–4 weeks — after a set of related milestones lands. The current release table and per-release ADR links are in [`docs/ROADMAP.md`](docs/ROADMAP.md). Release mechanics are in [`docs/RELEASING.md`](docs/RELEASING.md).

## Reporting

- **Bugs, features, questions:** GitHub issues (templates auto-load).
- **Design discussion:** GitHub Discussions.
- **Code of Conduct issues:** contact the maintainer directly via the email listed on their GitHub profile, or via a private security-advisory report at [github.com/Toloka/tolokaforge/security/advisories/new](https://github.com/Toloka/tolokaforge/security/advisories/new).

## Security

Security vulnerabilities should NOT be reported in public issues. Use the private [security advisory](https://github.com/Toloka/tolokaforge/security/advisories/new) form on GitHub, or contact the maintainer directly.
