"""Tests for GitDiffCollector: shadow-git snapshots, DiffSummary, risk flags.

Shadow-git behaviour (BL-1): the collector brings its own private git repo
(``state_dir/shadow.git``) and snapshots the workspace working tree against it.
Consequences:
- ANY directory (git repo, non-git, or git repo without a commit) produces a
  full CONTENT diff, including new/untracked files.
- ``no_git_repo`` is True ONLY when the ``git`` binary is missing/unusable.
- New files surface as added ('A') inside ``changed_files`` (not untracked_files).
- The workspace ``.gitignore`` is honored; the workspace's own ``.git/`` is never captured.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cao.models import DiffSummary
from cao.runtime.git_diff_collector import GitDiffCollector


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """A workspace that is itself a git repo with one commit (has its own .git/)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=str(repo), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=str(repo), check=True, capture_output=True
    )
    (repo / "hello.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True
    )
    return repo


@pytest.fixture()
def plain_dir(tmp_path: Path) -> Path:
    """A non-git workspace (no .git at all)."""
    ws = tmp_path / "plain"
    ws.mkdir()
    return ws


async def test_before_snapshot_creates_before_json(tmp_path: Path, git_repo: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    col = GitDiffCollector(state)
    base = await col.before_snapshot(git_repo)
    assert base is not None and len(base) >= 7  # a tree sha
    assert (state / "before.json").exists()


async def test_before_snapshot_works_for_non_git(tmp_path: Path, plain_dir: Path) -> None:
    """Shadow-git means a non-git workspace yields a real before tree (not None)."""
    state = tmp_path / "state"
    state.mkdir()
    (plain_dir / "a.txt").write_text("hi\n")
    col = GitDiffCollector(state)
    base = await col.before_snapshot(plain_dir)
    assert base is not None and len(base) >= 7


async def test_after_snapshot_creates_artifacts(tmp_path: Path, git_repo: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    col = GitDiffCollector(state)
    base = await col.before_snapshot(git_repo)
    (git_repo / "hello.py").write_text("print('world')\n")
    ds = await col.after_snapshot(git_repo, base)
    assert (state / "after.json").exists()
    assert (state / "diff.patch").exists()
    assert ds.patch_path is not None
    assert "hello.py" in ds.diff_stat or "hello.py" in ds.diff_name_status


async def test_non_git_workspace_full_content_diff(tmp_path: Path, plain_dir: Path) -> None:
    """A non-git workspace still gets a full content diff of new files (BL-1 core)."""
    state = tmp_path / "state"
    state.mkdir()
    (plain_dir / "existing.txt").write_text("original\n")
    col = GitDiffCollector(state)
    base = await col.before_snapshot(plain_dir)
    (plain_dir / "brand_new.py").write_text("UNIQUE_NEW_CONTENT_9f\n")
    ds = await col.after_snapshot(plain_dir, base)
    assert ds.no_git_repo is False
    assert any(fc.path == "brand_new.py" and fc.status == "A" for fc in ds.changed_files)
    assert ds.patch_path is not None
    assert b"UNIQUE_NEW_CONTENT_9f" in Path(ds.patch_path).read_bytes()


async def test_new_untracked_file_content_appears_in_changed_files(
    tmp_path: Path, git_repo: Path
) -> None:
    """A brand-new (previously untracked) file surfaces as 'A' with its content."""
    state = tmp_path / "state"
    state.mkdir()
    col = GitDiffCollector(state)
    base = await col.before_snapshot(git_repo)
    (git_repo / "new_file.py").write_text("NEW_FILE_BODY_xyz\n")
    ds = await col.after_snapshot(git_repo, base)
    assert any(fc.path == "new_file.py" and fc.status == "A" for fc in ds.changed_files)
    assert ds.patch_path is not None
    assert b"NEW_FILE_BODY_xyz" in Path(ds.patch_path).read_bytes()


async def test_gitignore_honored(tmp_path: Path, plain_dir: Path) -> None:
    """Files matched by the workspace .gitignore must be excluded from the diff."""
    state = tmp_path / "state"
    state.mkdir()
    (plain_dir / ".gitignore").write_text("*.log\n")
    col = GitDiffCollector(state)
    base = await col.before_snapshot(plain_dir)
    (plain_dir / "debug.log").write_text("SENSITIVE_IGNORED_DATA\n")
    (plain_dir / "real.py").write_text("kept\n")
    ds = await col.after_snapshot(plain_dir, base)
    paths = [fc.path for fc in ds.changed_files]
    assert "real.py" in paths
    assert "debug.log" not in paths
    assert ds.patch_path is not None
    assert b"SENSITIVE_IGNORED_DATA" not in Path(ds.patch_path).read_bytes()


async def test_workspace_own_git_dir_not_captured(tmp_path: Path, git_repo: Path) -> None:
    """The workspace's own .git/ internals must never appear in the diff."""
    state = tmp_path / "state"
    state.mkdir()
    col = GitDiffCollector(state)
    base = await col.before_snapshot(git_repo)
    (git_repo / "hello.py").write_text("changed\n")
    ds = await col.after_snapshot(git_repo, base)
    all_paths = [fc.path for fc in ds.changed_files] + [fc.path for fc in ds.deleted_files]
    assert not any(p == ".git" or p.startswith(".git/") for p in all_paths)


async def test_brand_new_file_empty_base(tmp_path: Path, plain_dir: Path) -> None:
    """after_snapshot with base_commit=None diffs vs the empty tree (first session)."""
    state = tmp_path / "state"
    state.mkdir()
    (plain_dir / "first.py").write_text("FIRST_SESSION_CONTENT\n")
    col = GitDiffCollector(state)
    ds = await col.after_snapshot(plain_dir, base_commit=None)
    assert ds.no_git_repo is False
    assert any(fc.path == "first.py" for fc in ds.changed_files)
    assert ds.patch_path is not None
    assert b"FIRST_SESSION_CONTENT" in Path(ds.patch_path).read_bytes()


async def test_non_git_workspace_no_git_repo_flag_is_false(
    tmp_path: Path, plain_dir: Path
) -> None:
    """no_git_repo is False for a non-git dir now that we bring our own git."""
    state = tmp_path / "state"
    state.mkdir()
    col = GitDiffCollector(state)
    ds = await col.after_snapshot(plain_dir, base_commit=None)
    assert ds.no_git_repo is False


async def test_deleted_file_in_deleted_files(tmp_path: Path, git_repo: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    col = GitDiffCollector(state)
    base = await col.before_snapshot(git_repo)
    (git_repo / "hello.py").unlink()
    ds = await col.after_snapshot(git_repo, base)
    assert any(fc.path == "hello.py" for fc in ds.deleted_files)


async def test_risk_flag_auth_file(tmp_path: Path, git_repo: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    col = GitDiffCollector(state)
    base = await col.before_snapshot(git_repo)
    (git_repo / "auth_utils.py").write_text("# auth\n")
    ds = await col.after_snapshot(git_repo, base)
    categories = [rf.category for rf in ds.risk_flags]
    assert "auth_security" in categories


async def test_risk_flag_env_file(tmp_path: Path, git_repo: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    col = GitDiffCollector(state)
    base = await col.before_snapshot(git_repo)
    (git_repo / ".env").write_text("SECRET=x\n")
    ds = await col.after_snapshot(git_repo, base)
    categories = [rf.category for rf in ds.risk_flags]
    assert "env_config" in categories


async def test_large_diff_patch_not_truncated_on_disk(tmp_path: Path, git_repo: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    col = GitDiffCollector(state)
    base = await col.before_snapshot(git_repo)
    (git_repo / "hello.py").write_text("\n".join(f"x_{i} = {i}" for i in range(700)) + "\n")
    ds = await col.after_snapshot(git_repo, base)
    assert ds.patch_path is not None
    patch_bytes = Path(ds.patch_path).read_bytes()
    assert len(patch_bytes) > 8192, "patch must be full-size on disk"


async def test_binary_file_no_crash(tmp_path: Path, git_repo: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    col = GitDiffCollector(state)
    base = await col.before_snapshot(git_repo)
    (git_repo / "img.bin").write_bytes(bytes(range(256)) * 10)
    ds = await col.after_snapshot(git_repo, base)
    assert isinstance(ds, DiffSummary)
