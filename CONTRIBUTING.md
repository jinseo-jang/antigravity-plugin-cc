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

The plugin installs its backend directly from GitHub `main` (see `plugin/hooks/session_start.sh`), so **merging to `main` ships it** — there is no PyPI publish step.

**Bumping the version is what upgrades existing users.** The install hook reinstalls the backend whenever the plugin version (`plugin/.claude-plugin/plugin.json`) changes. So a release is: bump the version, merge, and users get the new backend the next time they update the plugin. To cut one:

```bash
scripts/bump-version.sh 0.2.0     # syncs pyproject / plugin.json / marketplace.json / CHANGELOG (run --self-check to verify)
# fill in the new CHANGELOG.md entry, then:
git commit -am "chore: release v0.2.0" && git tag v0.2.0 && git push origin main --tags
```

Optionally publish a GitHub Release for the tag; `.github/release.yml` groups the notes by PR label. (For same-version dev iteration, delete `<plugin-data>/.cao_installed` to force a backend reinstall.)

## What to expect

Reviews happen in spare time — it may take days or weeks. Thanks for your patience.
