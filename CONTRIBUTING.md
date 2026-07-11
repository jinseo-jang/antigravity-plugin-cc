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

The version is pinned in five files (`pyproject.toml`, `plugin/.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, the `session_start.sh` install pin, and `CHANGELOG.md`). Bump them all at once, then tag and release:

```bash
scripts/bump-version.sh 0.2.0     # syncs all 5 pins (run with --self-check to verify the logic)
# then fill in the new CHANGELOG.md entry
git commit -am "chore: release v0.2.0" && git tag v0.2.0
git push origin main --tags
```

Publishing a GitHub Release for the tag triggers `.github/workflows/publish.yml`, which builds and uploads `claude-antigravity-orchestrator` to PyPI via Trusted Publishing (OIDC). `.github/release.yml` groups the notes by PR label.

## What to expect

Reviews happen in spare time — it may take days or weeks. Thanks for your patience.
