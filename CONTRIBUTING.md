# Contributing

Thanks for wanting to improve **agy**. It's a spare-time, single-maintainer project, so a little discipline keeps it maintainable.

## Workflow at a glance

1. **Open an issue** to discuss anything beyond a small fix (see *Before you code*).
2. **Fork** the repo and branch off `main` (`git checkout -b fix/short-description`).
3. **Set up** your dev env (see *Dev setup*) and make a single-purpose change with a test.
4. **Run the gates** (see *Pull requests*) — they must be green.
5. **Open a PR** against `main` and fill in the PR template.
6. A single maintainer reviews in spare time; address feedback, then it's squash-merged. Releases are the maintainer's job (see [RELEASING.md](RELEASING.md)).

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

Full runbook: **[RELEASING.md](RELEASING.md)**. In short — the backend installs the git tag matching the plugin version (`v<version>`), so a release is *bump the version + push the matching tag* (no PyPI):

```bash
scripts/bump-version.sh 0.2.0     # sync version fields (run --self-check to verify)
# fill in the new CHANGELOG.md entry, then:
git commit -am "chore: release v0.2.0" && git tag v0.2.0 && git push origin main --tags
```

Existing users get it when they update the plugin (its version bumps → the hook installs the new tag). For same-version dev iteration, set `CAO_BACKEND_REF=main` or delete `<plugin-data>/.cao_installed`.

## What to expect

Reviews happen in spare time — it may take days or weeks. Thanks for your patience.
