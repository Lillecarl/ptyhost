"""
A recording as an asciicast, judged by asciinema's own reader.

A parser of ours would read back what our writer meant. `asciinema
convert` reads what the file says. Lillecarl/pymux#610.
"""

from __future__ import annotations

import base64
import json
import subprocess

import pytest

from ptyhost.asciicast import asciicast

#: "é" split across two reads, the way a pty splits one.
OUT = [b"hello \xc3", b"\xa9\r\n", b"\x1b[1mbold\x1b[0m\r\n"]


def a_recording(**extra) -> dict:
    events = [
        [0.0, "out", base64.b64encode(OUT[0]).decode()],
        [0.5, "in", base64.b64encode(b"q").decode()],
        [1.25, "out", base64.b64encode(OUT[1]).decode()],
        [1.5, "size", "100x30"],
        [9.0, "out", base64.b64encode(OUT[2]).decode()],
    ]
    return {"lines": 24, "columns": 80, "term": "xterm-256color", "events": events, **extra}


def asciinema(tmp_path, cast: str, output_format: str) -> bytes:
    (tmp_path / "in.cast").write_text(cast)
    subprocess.run(
        ["asciinema", "convert", "-f", output_format, str(tmp_path / "in.cast"), str(tmp_path / "out")],
        check=True,
    )
    return (tmp_path / "out").read_bytes()


@pytest.mark.parametrize("version", [2, 3])
def test_asciinema_reads_back_every_byte_drawn(tmp_path, version):
    # asciinema's raw output starts with the size, as XTWINOPS 8.
    assert asciinema(tmp_path, asciicast(a_recording(), version), "raw") == b"\x1b[8;24;80t" + b"".join(OUT)


@pytest.mark.parametrize("version", [2, 3])
def test_asciinema_keeps_the_times_and_the_resize(tmp_path, version):
    "Converted to v2, every time is from the start, whichever version went in."
    converted = asciinema(tmp_path, asciicast(a_recording(), version), "asciicast-v2")
    events = [json.loads(line) for line in converted.splitlines()[1:]]
    assert [event[:2] for event in events] == [[0.0, "o"], [0.5, "i"], [1.25, "o"], [1.5, "r"], [9.0, "o"]]
    assert events[1][2] == "q"
    assert events[2][2] == "é\r\n"
    assert events[3][2] == "100x30"


def test_the_header_says_the_size_the_terminal_and_the_start():
    header = json.loads(asciicast(a_recording(timestamp=1760000000)).splitlines()[0])
    assert header == {
        "version": 3,
        "term": {"cols": 80, "rows": 24, "type": "xterm-256color"},
        "timestamp": 1760000000,
    }


def test_the_idle_limit_goes_in_the_header_and_the_times_stay(tmp_path):
    """
    asciinema's rule: a player shortens a pause, the file keeps it, so
    the limit can change later.
    """
    cast = asciicast(a_recording(idle_time_limit=5.0), idle_time_limit=2.0)
    assert json.loads(cast.splitlines()[0])["idle_time_limit"] == 2.0
    assert json.loads(asciicast(a_recording(idle_time_limit=5.0)).splitlines()[0])["idle_time_limit"] == 5.0
    times = [json.loads(line)[0] for line in asciinema(tmp_path, cast, "asciicast-v2").splitlines()[1:]]
    assert times[-1] == 9.0


def test_times_do_not_drift_over_a_long_recording():
    "Each delta comes from the time since the start, rounded once."
    events = [[n * 0.0004, "out", base64.b64encode(b"x").decode()] for n in range(1, 10001)]
    cast = asciicast({"lines": 1, "columns": 1, "events": events})
    total = sum(json.loads(line)[0] for line in cast.splitlines()[1:])
    assert total == pytest.approx(4.0, abs=0.0005)
