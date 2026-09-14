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

The Codespaces / devcontainer bootstrap (`scripts/setup/create_python_venv.sh`,
invoked by `.devcontainer/post_attach_container.sh`) runs `make use-public`
implicitly before `uv sync`, defaulting to the public variant that
Codespaces / GitHub-hosted runners can reach. Override with
`LOCK_VARIANT=jfrog` in the environment if you're running the bootstrap
on a Toloka-networked machine.

## Regenerating both lockfiles (rare — only when a dep changes)

When you add or bump a dependency in `pyproject.toml`, both committed
lockfiles have to be regenerated together. The `--config-file` flag forces
uv to resolve against the named index regardless of your personal
`~/.pip/pip.conf` / `~/.config/uv/uv.toml`, so this works from any machine
that can reach both endpoints:

The convenience path is `make refresh-locks` — it runs both `uv lock`
invocations, refreshes both committed lockfiles, and re-hydrates
`uv.lock` from `uv.lock.jfrog` at the end (biased toward Toloka Mac
callers). If you prefer the raw commands:

```bash
uv lock --config-file uv-jfrog.toml  && cp uv.lock uv.lock.jfrog
uv lock --config-file uv-public.toml && cp uv.lock uv.lock.public
./scripts/use-lock.sh jfrog  # or use-lock.sh public — leaves uv.lock as one specific variant
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

### Why the personal `~/.pip/pip.conf` matters for build-isolation

There is one place where `~/.pip/pip.conf` **is not optional** for
Toloka Mac devs: uv's build-isolation resolver. When `uv sync` reaches
a package that only ships as an sdist, it spins up a fresh venv to
compile it; that venv's resolver reads from a *separate* config surface
than the top-level `uv.lock` URLs. In this repo, `pyproject.toml`
declares `[tool.uv.pip] index-url = "https://pypi.org/simple/"` (safe
public default, works on CI + external contributors); the personal
`~/.pip/pip.conf` `index-url = <JFrog>` overrides it for build-iso, so
Toloka Macs resolve build-time deps through the mirror. Without the
personal `pip.conf` override, `uv sync --dev` on a Toloka Mac fails at
the first sdist build because the default pypi.org would try to reach
`files.pythonhosted.org`, which Defender blocks. The Claude plugin from
the migration announcement writes this `pip.conf` for you.

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
`uv.lock` is gitignored, so git will never touch it — it stays as
whatever you last copied. First `uv sync` will resolve happily against
that; the only way to fail is if you switch environments (e.g. run in a
container) without re-copying the appropriate variant.

**Q: I pulled a `pyproject.toml` change from upstream — do I have to
re-run `make use-*`?** Yes. `uv sync` against a stale `uv.lock` (one
matching an older `pyproject.toml`) will silently mutate the local
lockfile in place to converge; if you then run `make refresh-locks` and
commit, you'd land a mix of what uv picked. Rehydrate with
`make use-{jfrog,public}` first so `uv sync` runs against the committed
lock the maintainer intended.
