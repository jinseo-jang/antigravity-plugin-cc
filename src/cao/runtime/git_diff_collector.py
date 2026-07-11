"""GitDiffCollector: objective filesystem verifier via a private shadow git repo.

All git calls via asyncio.create_subprocess_exec (no shell=True, no GitPython,
no blocking subprocess).

BL-1 shadow-git: the collector owns a private repo at ``state_dir/shadow.git``
and snapshots the workspace working tree against it (``--git-dir`` points at the
shadow, ``--work-tree`` at the workspace). This means ANY directory — a non-git
dir, or a git repo without a commit — produces a full CONTENT diff of the entire
working tree, including brand-new/untracked files, while still honoring the
workspace's own ``.gitignore``. ``no_git_repo`` is True only when the ``git``
binary itself is missing/unusable.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cao.models import DiffSummary, FileChange, RiskFlag

logger = logging.getLogger("cao.git_diff")

_GIT_TIMEOUT: float = 30.0

# git's well-known empty tree object (SHA-1). Used as the diff base when there is
# no before-snapshot, so a brand-new workspace's first diff still works.
# ponytail: hardcoded SHA-1 constant; ceiling: a SHA-256 shadow repo would use a
# different empty-tree hash — upgrade path is `git hash-object -t tree /dev/null`
# if we ever init the shadow with --object-format=sha256 (we don't; default SHA-1).
_EMPTY_TREE: str = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# ponytail: risk patterns per git_diff_collector.md §7; fnmatch on basename + path
_RISK_PATTERNS: dict[str, tuple[list[str], str]] = {
    "package_manager": (
        [
            "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
            "requirements.txt", "requirements*.txt", "Pipfile", "Pipfile.lock",
            "pyproject.toml", "poetry.lock", "Cargo.toml", "Cargo.lock",
            "go.mod", "go.sum",
        ],
        "Dependency changes can introduce supply-chain risk or break reproducible builds.",
    ),
    "ci_config": (
        [".gitlab-ci.yml", "Jenkinsfile", ".travis.yml", ".github/**", ".circleci/**"],
        "CI configuration changes affect the entire build and deployment pipeline.",
    ),
    "container": (
        ["Dockerfile", "Dockerfile.*", "docker-compose.yml", "docker-compose*.yml", ".dockerignore"],
        "Container definition changes affect runtime environment and security posture.",
    ),
    "db_migrations": (
        ["migrations/**", "alembic/**", "db/migrate/**", "**/migrations/*.sql"],
        "Database migrations are irreversible in production.",
    ),
    "env_config": (
        [".env", ".env.*", "*.env", "config/secrets.*", "secrets/**", "*.pem", "*.key", "*.crt", "*.p12"],
        "Environment and credential files must never be committed or modified carelessly.",
    ),
}

_AUTH_KEYWORDS = frozenset({
    "auth", "oauth", "jwt", "token", "secret", "credential",
    "permission", "acl", "rbac", "session", "password", "crypto", "encrypt", "sign",
})


class GitCommandTimeoutError(Exception):
    pass


def _matches_pattern(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(prefix + "/")
    if pattern.startswith("**/"):
        sub_pattern = pattern[3:]
        parts = path.replace("\\", "/").split("/")
        for i in range(len(parts)):
            if fnmatch.fnmatch("/".join(parts[i:]), sub_pattern):
                return True
        return False
    basename = Path(path).name
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(basename, pattern)


def _compute_risk_flags(paths: list[str]) -> list[RiskFlag]:
    flags: list[RiskFlag] = []
    for category, (patterns, note) in _RISK_PATTERNS.items():
        matched = [p for p in paths if any(_matches_pattern(p, pat) for pat in patterns)]
        if matched:
            flags.append(RiskFlag(category=category, files=matched, note=note))
    auth_matched = [p for p in paths if any(kw in p.lower() for kw in _AUTH_KEYWORDS)]
    if auth_matched:
        flags.append(RiskFlag(
            category="auth_security",
            files=auth_matched,
            note="Changes to authentication-related files detected.",
        ))
    return flags


class GitDiffCollector:
    def __init__(self, state_dir: Path, timeout_seconds: float = _GIT_TIMEOUT) -> None:
        self._state_dir = state_dir
        self._timeout = timeout_seconds

    async def _run_git(self, args: list[str], cwd: str) -> tuple[str, str, int]:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise GitCommandTimeoutError(f"git {args[0]} timed out after {self._timeout}s")
        rc = proc.returncode if proc.returncode is not None else 0
        return (
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            rc,
        )

    async def _run_git_bytes(self, args: list[str], cwd: str) -> tuple[bytes, bytes, int]:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise GitCommandTimeoutError(f"git {args[0]} timed out after {self._timeout}s")
        rc = proc.returncode if proc.returncode is not None else 0
        return stdout, stderr, rc

    def _write_json(self, filename: str, data: Any) -> None:
        (self._state_dir / filename).write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )

    def _shadow_dir(self) -> Path:
        return self._state_dir / "shadow.git"

    async def _ensure_shadow(self, workspace: str) -> bool:
        """Init the private shadow repo once. False only if git is missing/unusable."""
        shadow = self._shadow_dir()
        try:
            if not (shadow / "HEAD").exists():
                _, err, rc = await self._run_git(
                    ["--git-dir=" + str(shadow), "--work-tree=" + workspace, "init"],
                    workspace,
                )
                if rc != 0:
                    logger.warning("shadow git init failed rc=%d: %s", rc, err.strip())
                    return False
                info = shadow / "info"
                info.mkdir(parents=True, exist_ok=True)
                # Never capture the workspace's own top-level .git/ when the workspace
                # is itself a git repo. git already skips a dir named .git; this makes
                # the exclusion explicit and self-documenting.
                (info / "exclude").write_text("/.git/\n", encoding="utf-8")
            return True
        except FileNotFoundError:
            logger.warning("git binary not found; objective diff unavailable")
            return False

    async def _shadow_git(self, args: list[str], workspace: str) -> tuple[str, str, int]:
        shadow = str(self._shadow_dir())
        return await self._run_git(
            ["--git-dir=" + shadow, "--work-tree=" + workspace, *args], workspace
        )

    async def _shadow_git_bytes(
        self, args: list[str], workspace: str
    ) -> tuple[bytes, bytes, int]:
        shadow = str(self._shadow_dir())
        return await self._run_git_bytes(
            ["--git-dir=" + shadow, "--work-tree=" + workspace, *args], workspace
        )

    async def _write_tree(self, workspace: str) -> str:
        """Stage the whole working tree into the shadow index and return its tree sha.

        ``add -A`` honors the workspace .gitignore and the shadow /.git/ exclude.
        """
        await self._shadow_git(["add", "-A"], workspace)
        out, _, _ = await self._shadow_git(["write-tree"], workspace)
        return out.strip()

    async def before_snapshot(self, workspace_root: str | Path) -> str | None:
        """Snapshot the working tree into the shadow repo; return the before-tree sha.

        Returns None only when the git binary itself is missing/unusable.
        """
        cwd = str(Path(workspace_root).resolve())
        if not await self._ensure_shadow(cwd):
            return None

        before_tree = await self._write_tree(cwd)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self._write_json, "before.json", {"before_tree": before_tree}
        )
        logger.debug("before_snapshot done before_tree=%s", before_tree)
        return before_tree

    async def after_snapshot(
        self, workspace_root: str | Path, base_commit: str | None
    ) -> DiffSummary:
        """Snapshot the working tree; diff before-tree↔after-tree; write after.json + diff.patch.

        ``base_commit`` is the before-tree sha from before_snapshot (falls back to the
        empty tree so a first-ever session still diffs). Produces a full CONTENT diff of
        every file — tracked, untracked, and brand-new — for ANY directory.
        """
        cwd = str(Path(workspace_root).resolve())
        now = datetime.now(timezone.utc)

        if not await self._ensure_shadow(cwd):
            logger.warning("after_snapshot: git unavailable at %s", cwd)
            diff_summary = DiffSummary(no_git_repo=True, snapshot_timestamp=now)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._write_json, "after.json", diff_summary.model_dump(mode="json")
            )
            return diff_summary

        after_tree = await self._write_tree(cwd)
        base = base_commit or _EMPTY_TREE

        name_status_out, _, _ = await self._shadow_git(
            ["diff", "--name-status", base, after_tree], cwd
        )
        diff_stat_out, _, _ = await self._shadow_git(
            ["diff", "--stat", base, after_tree], cwd
        )
        diff_binary_raw, _, _ = await self._shadow_git_bytes(
            ["diff", "--binary", base, after_tree], cwd
        )

        changed_files: list[FileChange] = []
        deleted_files: list[FileChange] = []
        for raw_line in name_status_out.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            code = parts[0][0]
            path = parts[-1]
            if code == "D":
                deleted_files.append(FileChange(path=path, status=code))
            else:
                changed_files.append(FileChange(path=path, status=code))

        patch_path: str | None = None
        if diff_binary_raw:
            p = self._state_dir / "diff.patch"
            loop2 = asyncio.get_running_loop()
            await loop2.run_in_executor(None, p.write_bytes, diff_binary_raw)
            patch_path = str(p)

        all_paths = [fc.path for fc in changed_files] + [fc.path for fc in deleted_files]
        risk_flags = _compute_risk_flags(all_paths)

        diff_summary = DiffSummary(
            base_commit=base_commit,
            diff_name_status=name_status_out,
            diff_stat=diff_stat_out,
            changed_files=changed_files,
            # Tree-diff classifies new files as 'A' in changed_files; the field is kept
            # (always empty now) so DiffSummary's shape and readers stay unchanged.
            untracked_files=[],
            deleted_files=deleted_files,
            patch_path=patch_path,
            snapshot_timestamp=now,
            no_git_repo=False,
            risk_flags=risk_flags,
        )

        loop3 = asyncio.get_running_loop()
        await loop3.run_in_executor(
            None, self._write_json, "after.json", diff_summary.model_dump(mode="json")
        )
        logger.debug(
            "after_snapshot done changed=%d deleted=%d", len(changed_files), len(deleted_files)
        )
        return diff_summary
