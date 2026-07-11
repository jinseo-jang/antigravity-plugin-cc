"""JSON-RPC 2.0 framing over asyncio streams.

All wire I/O is newline-delimited JSON (JSONL): one JSON object per line,
``\\n``-terminated.  No other framing is used.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, cast

logger = logging.getLogger("cao.ipc")


class FramingError(Exception):
    """Raised when a message cannot be decoded from the stream.

    Covers three cases: EOF without data, EOF before newline (truncated),
    and bytes that are not valid JSON.
    """


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read one newline-delimited JSON message from *reader*.

    Raises:
        FramingError: on EOF, truncated stream, or invalid JSON.
    """
    line = await reader.readline()
    if not line:
        # Pure EOF — stream closed before any bytes arrived.
        raise FramingError("Stream closed (EOF before newline)")
    if not line.endswith(b"\n"):
        # Partial data followed by EOF — truncated frame.
        raise FramingError("Truncated frame: EOF before newline")
    try:
        data: Any = json.loads(line)
    except json.JSONDecodeError as exc:
        raise FramingError(f"Invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise FramingError("Message is not a JSON object")
    # json.loads returns Any; we validated the dict above.
    return cast(dict[str, Any], data)


async def write_message(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
    """Write one newline-delimited JSON message to *writer* and drain."""
    writer.write(json.dumps(obj).encode() + b"\n")
    await writer.drain()
