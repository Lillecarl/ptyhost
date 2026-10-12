"""
A recording as an asciicast, the format asciinema writes and plays.

https://docs.asciinema.org/manual/asciicast/v3/ and `.../v2/`. Version 3
is what asciinema 3 writes: each event's time is the time since the one
before. Version 2 counts every time from the start. Both carry output,
input and a resize, so a recording loses nothing it had, except what an
asciicast player does not draw: an image or another extension is only
bytes to it.

**The idle limit goes in the header and the times stay true.** That is
asciinema's own rule: a player shortens each pause longer than the
limit, so a person can change the limit later without the recording.
"""

from __future__ import annotations

import base64
import codecs
import json

__all__ = ["CODES", "asciicast"]

#: A session event's kind, and its asciicast code.
CODES = {"out": "o", "in": "i", "size": "r"}


def _header(recording: dict, version: int, idle_time_limit: float | None) -> dict:
    header: dict = {"version": version}
    if version == 2:
        header.update(width=recording["columns"], height=recording["lines"])
        if recording.get("term"):
            header["env"] = {"TERM": recording["term"]}
    else:
        term = {"cols": recording["columns"], "rows": recording["lines"]}
        if recording.get("term"):
            term["type"] = recording["term"]
        header["term"] = term
    if recording.get("timestamp") is not None:
        header["timestamp"] = int(recording["timestamp"])
    if idle_time_limit is not None:
        header["idle_time_limit"] = idle_time_limit
    return header


def asciicast(recording: dict, version: int = 3, idle_time_limit: float | None = None) -> str:
    """
    The asciicast of `recording`, which is what a `.session.json` holds.

    `idle_time_limit` replaces the one the recording carries, if any.
    """
    if version not in (2, 3):
        raise ValueError("asciicast version %r: only 2 and 3 are written" % version)
    if idle_time_limit is None:
        idle_time_limit = recording.get("idle_time_limit")

    lines = [json.dumps(_header(recording, version, idle_time_limit), separators=(",", ":"))]
    # One decoder per direction: a piece ends where a read ended, often
    # inside a character, and the next piece of the same direction holds
    # the rest of it.
    decoders = {kind: codecs.getincrementaldecoder("utf-8")("replace") for kind in ("out", "in")}
    previous = 0
    for seconds, kind, data in recording["events"]:
        if kind not in CODES:
            continue
        text = data if kind == "size" else decoders[kind].decode(base64.b64decode(data))
        if not text:
            continue
        # Whole milliseconds, taken from the time since the start, so a
        # rounding error never adds up over a long recording.
        now = round(seconds * 1000)
        time = "%d.%03d" % divmod(now - previous if version == 3 else now, 1000)
        previous = now
        lines.append("[%s, %s, %s]" % (time, json.dumps(CODES[kind]), json.dumps(text)))
    return "\n".join(lines) + "\n"
