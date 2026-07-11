# Releasing (maintainers)

`agy` ships entirely from GitHub — **no PyPI**:

- the **plugin frontend** (commands, hooks) is served from `main` via the marketplace, and
- the **Python backend** is installed by the `SessionStart` hook from the **git tag that matches the plugin version** (`v<version>`).

So a release is: **bump the version, then create + push the matching tag.**

> ⚠️ The tag is load-bearing. If `plugin.json` says `0.2.0` but tag `v0.2.0` isn't on GitHub, every user's backend install fails (`@v0.2.0` won't resolve). Always bump **and** tag **and** push together.

## 1. Pre-release checks (all green)

```bash
ruff check src plugin tests
mypy --strict src
pytest -q
claude plugin validate .
claude plugin validate ./plugin
```

## 2. Bump the version

```bash
scripts/bump-version.sh 0.2.0        # syncs pyproject.toml, plugin.json, marketplace.json, CHANGELOG.md
scripts/bump-version.sh --self-check # sanity-check the bump logic
```

Then edit `CHANGELOG.md` to fill in the new `## [0.2.0]` section.

## 3. Commit, tag, push (all together)

```bash
git commit -am "chore: release v0.2.0"
git tag v0.2.0
git push origin main --tags
```

Optionally publish a GitHub Release for the tag — `.github/release.yml` groups the notes by PR label.

## 4. Verify the release installs

In a throwaway config dir (doesn't touch your real setup):

```bash
tmp=$(mktemp -d)
CLAUDE_CONFIG_DIR="$tmp" claude plugin marketplace add jinseo-jang/antigravity-plugin-cc
CLAUDE_CONFIG_DIR="$tmp" claude plugin install agy@agy
CLAUDE_CONFIG_DIR="$tmp" claude plugin list --json | grep -q agy && echo "plugin OK"
rm -rf "$tmp"
```

Then start a Claude Code session with the plugin enabled; the hook installs the backend from `v<version>`. If it fails, check `<plugin-data>/.cao_install.log`.

## How upgrades reach users

| User | What happens |
|---|---|
| **New** | `/plugin marketplace add` + `/plugin install agy@agy` → first session installs the `v<version>` backend. |
| **Existing (upgrade)** | They update the plugin → its `plugin.json` version bumps → the hook installs the new tag next session (temp-swap: a failed upgrade keeps the old backend). |
| **Stay / rollback** | Not updating keeps the current version; the tag makes it reproducible even across reinstalls. Downgrading the plugin rolls the backend back. |
| **Dev / bleeding-edge** | `export CAO_BACKEND_REF=main` (or delete `<plugin-data>/.cao_installed`) to track `main` instead of a tag. |
