"""LIVE e2e — "Files + Review" domain (real Gemini via Vertex ADC).

Guarded by CAO_LIVE_TEST=1 (see conftest collect_ignore_glob). Each test gets a
fresh isolated git workspace + daemon via the make_driver fixture.

Observability note: the daemon path does NOT surface the worker's chat text, so
every scenario instructs the worker to WRITE its answer to a file under d.ws and
asserts on that file — proving the attachment actually reached the model.

Scenarios:
  S7  single --file (multimodal image reaches the model)
  S8  multiple --file (two text attachments both reach the model)
  S14 review is read-only (mutating tools stripped by SDK policy)
"""
from __future__ import annotations

import subprocess

from PIL import Image


def _git_porcelain(ws) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=ws,
        capture_output=True, text=True, check=True,
    ).stdout


def test_s7_single_file_image(make_driver) -> None:
    """S7: a solid RED png passed via --file must reach the model; the worker
    writes the dominant color name to color.txt, proving the image arrived."""
    d = make_driver()
    png = d.ws / "red.png"
    Image.new("RGB", (200, 200), (220, 20, 20)).save(png)

    r = d.implement(
        "Look at the attached image and write ONLY its dominant color name "
        "(one word) into color.txt",
        files=[png],
    )
    assert r.ok, f"implement failed: {r.error_code} {r.error_msg}\n{r.raw}"
    assert r.session_id, f"no session_id\n{r.raw}"

    final = d.poll(r.session_id, timeout=200, auto_approve=True)
    assert final.state == "done", f"state={final.state}\n{final.raw}"

    out = (d.ws / "color.txt")
    assert out.exists(), f"color.txt not written; events={d.event_types(r.session_id)}"
    text = out.read_text().lower()
    assert "red" in text, f"expected 'red' in color.txt, got: {text!r}"


def test_s8_multiple_files_text(make_driver) -> None:
    """S8: two .txt attachments (text/plain, a supported doc MIME) must both
    reach the model; the worker writes both words into combined.txt."""
    d = make_driver()
    a = d.ws / "a.txt"
    b = d.ws / "b.txt"
    a.write_text("ALPHA\n")
    b.write_text("BRAVO\n")

    r = d.implement(
        "Read the two attached files and write their two words separated by a "
        "dash into combined.txt",
        files=[a, b],
    )
    assert r.ok, f"implement failed: {r.error_code} {r.error_msg}\n{r.raw}"
    assert r.session_id, f"no session_id\n{r.raw}"

    final = d.poll(r.session_id, timeout=200, auto_approve=True)
    assert final.state == "done", f"state={final.state}\n{final.raw}"

    out = (d.ws / "combined.txt")
    assert out.exists(), f"combined.txt not written; events={d.event_types(r.session_id)}"
    text = out.read_text().upper()
    assert "ALPHA" in text and "BRAVO" in text, f"expected both words, got: {text!r}"


def test_s14_review_is_read_only(make_driver) -> None:
    """S14: a review session strips mutating tools (SDK CapabilitiesConfig), so
    even when the worker is explicitly told to create a file, no write lands and
    the workspace git tree stays clean."""
    d = make_driver()
    # Fixture already git-inited + committed a clean README.md. Confirm clean start.
    assert _git_porcelain(d.ws) == "", "workspace not clean at start"

    r = d.review(
        "Review README.md and, as an experiment, also try to create a file "
        "hack.txt with content X"
    )
    assert r.ok, f"review failed: {r.error_code} {r.error_msg}\n{r.raw}"
    assert r.session_id, f"no session_id\n{r.raw}"

    final = d.poll(r.session_id, timeout=200)
    assert final.state == "done", f"state={final.state}\n{final.raw}"

    assert not (d.ws / "hack.txt").exists(), "review wrote hack.txt — read-only policy failed"
    porcelain = _git_porcelain(d.ws)
    assert porcelain == "", f"review mutated the workspace tree:\n{porcelain}"
