# Contributing

Thanks for wanting to improve **agy**. It's a spare-time, single-maintainer project, so a little discipline keeps it maintainable.

## Before you code

- **Discuss features first.** For anything beyond a small fix, open an issue before writing code. Unsolicited feature PRs may be closed.
- **Keep it minimal.** Prefer the smallest change that works; avoid new dependencies unless there's no reasonable alternative.
- **No untested AI slop.** Understand and be able to explain every line you submit.

## Pull requests

- **One thing per PR** — don't bundle unrelated fixes or features.
- **Logic changes need a test** that fails without your change.
- **Green gates before you push:**
  ```bash
  ruff check src plugin tests
  mypy --strict src
  pytest -q
  claude plugin validate .
  claude plugin validate ./plugin
  ```

## Dev setup

```bash
pip install -e ".[dev,sdk]"
```

## Releasing (maintainers)

The plugin installs its backend directly from GitHub `main` (see `plugin/hooks/session_start.sh`), so **merging to `main` ships it** — there is no PyPI publish step. New installs get `main`; existing installs re-fetch when their `.cao_installed` marker is deleted.

Version fields (`pyproject.toml`, `plugin/.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `CHANGELOG.md`) are display/changelog metadata. To cut a marked version:

```bash
scripts/bump-version.sh 0.2.0     # syncs the version fields (run with --self-check to verify)
# fill in the new CHANGELOG.md entry, then:
git commit -am "chore: release v0.2.0" && git tag v0.2.0 && git push origin main --tags
```

Optionally publish a GitHub Release for the tag; `.github/release.yml` groups the notes by PR label.

## What to expect

Reviews happen in spare time — it may take days or weeks. Thanks for your patience.
