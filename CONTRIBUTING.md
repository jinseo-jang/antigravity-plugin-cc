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

Bump every version pin in one shot, then tag — see the [Releases](README.md#releases) section:

```bash
scripts/bump-version.sh 0.2.0
```

## What to expect

Reviews happen in spare time — it may take days or weeks. Thanks for your patience.
