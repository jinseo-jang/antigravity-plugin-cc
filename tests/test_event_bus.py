"""Tests for EventBus: publish, fan-out, monotonic IDs, JSONL persistence, get_events."""

from __future__ import annotations

import json
from pathlib import Path


from cao.models import RuntimeEvent
from cao.runtime.event_bus import EventBus


async def test_publish_returns_runtime_event(tmp_path: Path) -> None:
    bus = EventBus(tmp_path)
    event = await bus.publish("s1", "session.started", {"k": "v"})
    assert isinstance(event, RuntimeEvent)
    assert event.session_id == "s1"
    assert event.event_type == "session.started"
    assert event.payload == {"k": "v"}


async def test_monotonic_ids_start_at_one(tmp_path: Path) -> None:
    bus = EventBus(tmp_path)
    ids = [
        (await bus.publish("sess", "tool.requested", {})).id for _ in range(5)
    ]
    assert ids == list(range(1, 6))


async def test_separate_sessions_each_start_at_one(tmp_path: Path) -> None:
    bus = EventBus(tmp_path)
    e1 = await bus.publish("sessA", "x", {})
    e2 = await bus.publish("sessB", "x", {})
    assert e1.id == 1
    assert e2.id == 1


async def test_jsonl_persistence_valid_fields(tmp_path: Path) -> None:
    bus = EventBus(tmp_path)
    await bus.publish("sess", "session.started", {"workspace": "/tmp"})
    await bus.publish("sess", "tool.requested", {"call_id": "c1"})

    jsonl = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(jsonl) == 2
    for line in jsonl:
        obj = json.loads(line)
        assert {"id", "session_id", "type", "timestamp_utc", "payload"} <= obj.keys()
        assert isinstance(obj["id"], int)
        assert obj["session_id"] == "sess"
    assert json.loads(jsonl[0])["type"] == "session.started"
    assert json.loads(jsonl[1])["type"] == "tool.requested"


async def test_jsonl_timestamp_z_suffix(tmp_path: Path) -> None:
    bus = EventBus(tmp_path)
    await bus.publish("s", "x", {})
    line = json.loads((tmp_path / "events.jsonl").read_text())
    assert line["timestamp_utc"].endswith("Z")


async def test_jsonl_flush_on_disk_immediately(tmp_path: Path) -> None:
    bus = EventBus(tmp_path)
    await bus.publish("s", "session.started", {})
    # File must be readable immediately — not just buffered
    content = (tmp_path / "events.jsonl").read_bytes()
    assert content.endswith(b"\n")
    assert len(content) > 0


async def test_get_events_filters_by_session(tmp_path: Path) -> None:
    bus = EventBus(tmp_path)
    await bus.publish("A", "x", {})
    await bus.publish("B", "y", {})
    await bus.publish("A", "z", {})

    a_events = await bus.get_events("A")
    b_events = await bus.get_events("B")
    assert len(a_events) == 2
    assert len(b_events) == 1
    assert all(e.session_id == "A" for e in a_events)


async def test_get_events_after_event_id(tmp_path: Path) -> None:
    bus = EventBus(tmp_path)
    for _ in range(5):
        await bus.publish("s", "x", {})

    later = await bus.get_events("s", after_event_id=3)
    assert [e.id for e in later] == [4, 5]


async def test_fan_out_order_jsonl_first(tmp_path: Path) -> None:
    order: list[str] = []

    async def second_sub(event: RuntimeEvent) -> None:
        order.append("second")

    bus = EventBus(tmp_path)
    # JSONL writer is always [0]; subscribe adds at [1]
    bus.subscribe(second_sub)

    # Intercept jsonl writer to record ordering
    original_jsonl_handle = bus._jsonl.handle

    async def instrumented_jsonl(event: RuntimeEvent) -> None:
        order.append("jsonl")
        await original_jsonl_handle(event)

    bus._subscribers[0] = instrumented_jsonl

    await bus.publish("s", "x", {})
    assert order == ["jsonl", "second"]


async def test_subscriber_failure_isolation(tmp_path: Path) -> None:
    received: list[str] = []

    async def bad_sub(event: RuntimeEvent) -> None:
        raise RuntimeError("boom")

    async def good_sub(event: RuntimeEvent) -> None:
        received.append(event.event_type)

    bus = EventBus(tmp_path)
    bus._subscribers.insert(1, bad_sub)  # after JSONL writer
    bus.subscribe(good_sub)

    # Must not raise, good_sub must still receive the event
    await bus.publish("s", "session.started", {})
    assert "session.started" in received


async def test_no_exception_propagated_from_subscriber(tmp_path: Path) -> None:
    async def always_raises(event: RuntimeEvent) -> None:
        raise ValueError("subscriber exploded")

    bus = EventBus(tmp_path)
    bus.subscribe(always_raises)
    # publish must not propagate the subscriber exception
    event = await bus.publish("s", "x", {})
    assert event.id == 1


async def test_init_session_counter_resumes(tmp_path: Path) -> None:
    bus1 = EventBus(tmp_path)
    for _ in range(3):
        await bus1.publish("s", "x", {})

    bus2 = EventBus(tmp_path)
    bus2.init_session_counter("s")
    e = await bus2.publish("s", "x", {})
    assert e.id == 4


async def test_make_publish_wrapper_compatible(tmp_path: Path) -> None:
    from cao.runtime.event_bus import make_publish_wrapper

    bus = EventBus(tmp_path)
    wrapper = make_publish_wrapper(bus)
    await wrapper("sess", "session.started", {})
    events = await bus.get_events("sess")
    assert len(events) == 1
