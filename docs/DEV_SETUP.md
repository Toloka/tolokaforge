# Development setup

`tolokaforge` uses [uv](https://docs.astral.sh/uv/) for dependency management.
This page covers the one non-obvious wrinkle: **which `uv.lock` variant to use**.

## The two-lockfile pattern

Toloka's supply-chain-security policy (announced 2026-08-28 in
`#team-tech`, effective 2026-09-11) blocks direct access to public package
registries (`pypi.org`, `files.pythonhosted.org`, `npmjs.org`,
`repo.maven.apache.org`, `dev.azure.com`) from workstations and K8s.
Internal contributors resolve packages through the sanctioned mirror at
[toloka.jfrog.io](https://toloka.jfrog.io) instead; every other consumer
(GitHub-hosted CI, arena runners, external contributors) resolves through
public PyPI.

A single committed `uv.lock` would flip URLs between `toloka.jfrog.io` and
`pypi.org` depending on whose machine last ran `uv lock`, and any rebase
would pick a side arbitrarily. Worse, when a Toloka-side lockfile leaks
into a runtime container that lives outside the corporate network — an
arena runner, an expert's evaluation container, a customer dataset —
`uv sync` inside that container fails because `toloka.jfrog.io` is
unreachable.

The fix, endorsed by the same `#team-tech` thread that announced the
migration, is to commit **two** lockfiles and make `uv.lock` itself an
untracked, locally-generated file:

- **`uv.lock.jfrog`** — resolved against
  `https://toloka.jfrog.io/artifactory/api/pypi/pypi-virtual/simple/`.
- **`uv.lock.public`** — resolved against `https://pypi.org/simple/`.
- **`uv.lock`** — untracked (in `.gitignore`); regenerated locally by
  copying from one of the two variants above.

## Which variant to use

- **Internal Toloka contributor**, working from a Toloka-issued Mac /
  K8s job / anywhere behind the SecOps policy: `make use-jfrog`.
- **External contributor, CI runner, arena runner, expert eval container,
  or any environment without JFrog credentials**: `make use-public`.

Both targets wrap `scripts/use-lock.sh`, which copies the chosen variant
to `uv.lock`. Run this **once at clone time** (or whenever you switch
environments), then use `uv sync` / `make install` as usual.

```bash
git clone git@github.com:Toloka/tolokaforge.git
cd tolokaforge
make use-jfrog       # or `make use-public`
make install         # runs `uv sync`
```

## Regenerating both lockfiles (rare — only when a dep changes)

When you add or bump a dependency in `pyproject.toml`, both committed
lockfiles have to be regenerated together. The `--config-file` flag forces
uv to resolve against the named index regardless of your personal
`~/.pip/pip.conf` / `~/.config/uv/uv.toml`, so this works from any machine
that can reach both endpoints:

```bash
uv lock --config-file uv-jfrog.toml  && cp uv.lock uv.lock.jfrog
uv lock --config-file uv-public.toml && cp uv.lock uv.lock.public
```

Commit `uv.lock.jfrog` and `uv.lock.public` together. The two files must
name the same package set at the same versions — only the `url` and
`sha256` fields per package should differ. If a package appears on one
side and not the other, the JFrog mirror's cache is stale for that
package — ping the JFrog admin in `#team-tech` to zap it.

## Configuring pip / uv defaults

Independent of this repo, most Toloka-issued Macs should have their
personal package-manager defaults pointing at JFrog too, so ad-hoc
`pip install` and one-off `uv pip` commands don't fail:

```ini
# ~/.pip/pip.conf
[global]
index-url = https://toloka.jfrog.io/artifactory/api/pypi/pypi-virtual/simple/
```

```toml
# ~/.config/uv/uv.toml
[[index]]
url = "https://toloka.jfrog.io/artifactory/api/pypi/pypi-virtual/simple/"
default = true
```

Auth uses an identity token in `~/.netrc`:

```
machine toloka.jfrog.io
    login you@toloka.ai
    password <identity-token-from-jfrog-profile>
```

Generate the identity token from the top-right avatar in
[toloka.jfrog.io](https://toloka.jfrog.io) → **Edit Profile** → **Generate
an Identity Token**. There's also a Claude plugin that writes the
`pip.conf` / `uv.toml` / `npmrc` / gradle init for you — see the JFrog
Artifactory quick-start guide in Notion (linked from the migration
announcement in `#team-tech`).

The two-lockfile pattern in this repo is independent of these personal
defaults — the `--config-file` flag on `uv lock` always overrides them.

## FAQ

**Q: I'm on a Toloka Mac. Why doesn't `uv sync` "just work" against the
public lock?** Because JFrog is only reachable through JFrog's mirror
URL, and the public lockfile pins to `files.pythonhosted.org`, which
Defender blocks. Run `make use-jfrog` first.

**Q: I'm on GitHub Actions / arena runner. Why doesn't `uv sync` "just
work" against the JFrog lock?** Because that runner has no JFrog
credentials and `toloka.jfrog.io` is unreachable from GitHub-hosted VMs.
Run `make use-public` before `uv sync`.

**Q: Can I just override with `--index-url` per-invocation?** Not
reliably. `uv sync` reads `uv.lock` directly; the URLs baked into the
lock are the source of truth. Overriding the index-url on the command
line doesn't rewrite the lock.

**Q: What happens if I forget to run `make use-{jfrog,public}` first?**
The most likely `uv.lock` on your disk is whatever the previous
`git checkout` / `git rebase` merged into your worktree — either the
`jfrog` or `public` variant, depending on how git resolved things. With
`uv.lock` gitignored, git will never touch it, so it stays as whatever
you last copied. First `uv sync` will resolve happily against that; the
only way to fail is if you switch environments (e.g. run in a container)
without re-copying the appropriate variant.
