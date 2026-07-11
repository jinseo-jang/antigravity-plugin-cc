"""Tests for ApprovalWaiter — asyncio.Future registry."""

from __future__ import annotations

import asyncio

import pytest

from cao.models import ApprovalDecision
from cao.runtime.approval_waiter import ApprovalWaiter, UnknownCallIdError


async def test_register_creates_pending_future() -> None:
    waiter = ApprovalWaiter()
    future = waiter.register_pending("c1")
    assert not future.done()
    assert "c1" in waiter.pending_ids
    future.cancel()


async def test_resolve_allow() -> None:
    waiter = ApprovalWaiter()
    future = waiter.register_pending("c1")
    waiter.resolve("c1", ApprovalDecision.ALLOW)
    assert future.done()
    assert await future == ApprovalDecision.ALLOW
    assert "c1" not in waiter.pending_ids


async def test_resolve_deny() -> None:
    waiter = ApprovalWaiter()
    future = waiter.register_pending("c1")
    waiter.resolve("c1", ApprovalDecision.DENY)
    assert future.done()
    assert await future == ApprovalDecision.DENY
    assert "c1" not in waiter.pending_ids


async def test_double_resolve_idempotent() -> None:
    waiter = ApprovalWaiter()
    waiter.register_pending("c1")
    waiter.resolve("c1", ApprovalDecision.ALLOW)
    # c1 already removed from registry; second call raises UnknownCallIdError
    with pytest.raises(UnknownCallIdError):
        waiter.resolve("c1", ApprovalDecision.DENY)


async def test_unknown_call_id_raises() -> None:
    waiter = ApprovalWaiter()
    with pytest.raises(UnknownCallIdError):
        waiter.resolve("does-not-exist", ApprovalDecision.ALLOW)


async def test_timeout_raises() -> None:
    waiter = ApprovalWaiter()
    future = waiter.register_pending("c1")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(future), timeout=0.01)
    future.cancel()


async def test_cancel_all_clears_registry() -> None:
    waiter = ApprovalWaiter()
    f1 = waiter.register_pending("c1")
    f2 = waiter.register_pending("c2")
    waiter.cancel_all()
    assert waiter.pending_ids == frozenset()
    assert f1.cancelled()
    assert f2.cancelled()


async def test_cancel_all_during_await_raises_cancelled() -> None:
    waiter = ApprovalWaiter()

    async def _waiter_task() -> ApprovalDecision:
        future = waiter.register_pending("cx")
        return await future

    task = asyncio.create_task(_waiter_task())
    await asyncio.sleep(0)
    waiter.cancel_all()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_remove_if_pending() -> None:
    waiter = ApprovalWaiter()
    waiter.register_pending("c1")
    waiter.remove_if_pending("c1")
    assert "c1" not in waiter.pending_ids
    waiter.remove_if_pending("does-not-exist")  # must not raise


async def test_next_call_id_is_sequential() -> None:
    waiter = ApprovalWaiter()
    assert waiter.next_call_id() == "1"
    assert waiter.next_call_id() == "2"
    assert waiter.next_call_id() == "3"


async def test_pending_details_includes_command() -> None:
    waiter = ApprovalWaiter()
    cid = waiter.next_call_id()
    fut = waiter.register_pending(cid, command="touch foo.sh", session_id="s1")
    assert waiter.pending_details() == [{"call_id": cid, "command": "touch foo.sh"}]
    fut.cancel()


async def test_pending_details_filters_by_session() -> None:
    waiter = ApprovalWaiter()
    f1 = waiter.register_pending("1", command="cmd-a", session_id="s1")
    f2 = waiter.register_pending("2", command="cmd-b", session_id="s2")
    assert waiter.pending_details("s1") == [{"call_id": "1", "command": "cmd-a"}]
    assert waiter.pending_details("s2") == [{"call_id": "2", "command": "cmd-b"}]
    f1.cancel()
    f2.cancel()


async def test_resolve_clears_pending_details() -> None:
    waiter = ApprovalWaiter()
    waiter.register_pending("1", command="cmd", session_id="s1")
    waiter.resolve("1", ApprovalDecision.ALLOW)
    assert waiter.pending_details() == []
