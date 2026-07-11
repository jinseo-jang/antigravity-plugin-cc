"""Pydantic v2 data models for the CAO runtime.

Single source of truth for all shared data shapes. Every module imports from here.
"""

import enum
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

# BL-25: masks a secret file's real path in policy events (hook writes it,
# digest excludes it from the out-of-workspace count). One value, one place.
SECRET_PATH_MASK = "<secret>"


class Session(BaseModel):
    """Active or past session metadata."""

    session_id: str
    workspace: str
    state: Literal["idle", "running", "suspended", "done", "cancelled", "crashed", "timed_out"]
    created_at: datetime


class RuntimeEvent(BaseModel):
    """Wire envelope for every event published through the EventBus.

    The on-disk/wire field is ``type``; Python code uses ``event_type`` to
    avoid shadowing the built-in.  Use ``model_dump(by_alias=True)`` for
    serialisation to JSON/JSONL.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: int
    session_id: str
    event_type: str = Field(alias="type")
    timestamp_utc: datetime
    payload: dict[str, Any]

    @field_serializer("timestamp_utc")
    def _serialize_ts(self, v: datetime) -> str:
        """Millisecond precision, explicit Z suffix per event_bus_and_persistence.md §2."""
        ms = v.microsecond // 1000
        return v.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"


class ToolRequest(BaseModel):
    """A request to execute a tool, received over IPC."""

    call_id: str
    session_id: str
    tool_name: str
    arguments: dict[str, Any]


# ---------------------------------------------------------------------------
# Task 002 additions
# ---------------------------------------------------------------------------


class ApprovalDecision(str, enum.Enum):
    """Human (or timeout-implicit) decision on a pending tool call."""

    ALLOW = "allow"
    DENY = "deny"


class PolicyDecision(str, enum.Enum):
    """Decision returned by PolicyEngine.evaluate()."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class ToolCallAutoAllowed(BaseModel):
    """Event: tool call auto-allowed by policy (no human approval needed)."""

    call_id: str
    session_id: str
    tool_name: str


class ToolCallPendingApproval(BaseModel):
    """Event: tool call is suspended waiting for human approval."""

    call_id: str
    session_id: str
    tool_name: str


class ToolCallApproved(BaseModel):
    """Event: pending tool call approval was granted."""

    call_id: str
    session_id: str


class ToolCallDenied(BaseModel):
    """Event: tool call was denied (policy, rejection, or implicit timeout deny)."""

    call_id: str
    session_id: str
    tool_name: str


class ToolCallApprovalTimedOut(BaseModel):
    """Event: pending approval timed out; implicit DENY applied."""

    call_id: str
    session_id: str


# ---------------------------------------------------------------------------
# Task 003 additions
# ---------------------------------------------------------------------------


class SnapshotStatus(BaseModel):
    """Git porcelain status strings from before/after snapshots."""

    before: str = ""
    after: str = ""


class FileChange(BaseModel):
    """A changed or deleted file with its git status code (M/A/D/R/C)."""

    path: str
    status: str


class RiskFlag(BaseModel):
    """A category of risk detected among changed files."""

    category: str
    files: list[str]
    note: str


class DiffSummary(BaseModel):
    """Objective filesystem diff produced by GitDiffCollector.

    Field names match git_diff_collector.md §6.  All inter-module data shapes
    live in models.py per F-7.
    """

    base_commit: str | None = None
    status: SnapshotStatus = Field(default_factory=SnapshotStatus)
    diff_name_status: str = ""
    diff_stat: str = ""
    changed_files: list[FileChange] = Field(default_factory=list)
    untracked_files: list[str] = Field(default_factory=list)
    deleted_files: list[FileChange] = Field(default_factory=list)
    patch_path: str | None = None
    snapshot_timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    no_git_repo: bool = False
    risk_flags: list[RiskFlag] = Field(default_factory=list)


class Digest(BaseModel):
    """Rendered Markdown digest returned by DigestGenerator.render()."""

    markdown: str
    digest_path: str  # absolute path to digest.md on disk
