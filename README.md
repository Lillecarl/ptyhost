# ptyhost

Run a program on a pty. Start it, size it, pump its bytes, record them.

**No parsing, no drawing, no toolkit.** What the program writes goes to
a callback, and whoever built the `Process` decides what that means.

```python
from ptyhost import Process
from ptyhost.backends.posix import PosixBackend

backend = PosixBackend.from_command(["bash"])
process = Process(backend, receive=print)
process.set_size(80, 24)
process.start()
```

## Why it is a package of its own

It came out of [ptterm], which is a terminal widget for
prompt-toolkit. A second widget for [Textual] needs the same pty layer
and must not pull prompt-toolkit in behind it, so the layer needed a
home that neither widget owns.

The family, and the one job each name claims:

| | job |
| --- | --- |
| `pyte` | parse, and hold a screen |
| `ptyhost` | run a program and carry its bytes |
| `ptterm` | draw it with prompt-toolkit |
| `txterm` | draw it with Textual |
| `pymux` | arrange several of them |

`Lillecarl/pymux#85` holds the argument, and `#11` the four layers.

## What is in it

- `Process` — the program: `start`, `set_size`, `write_input`,
  `suspend`, `resume`, `kill`, and what it is called and where it is.
- `backends/` — the pty itself. `posix`, `win32` and `asyncssh`, behind
  one `Backend` interface.
- `record.py` — `ptyhost-record`, which runs a program on a pty and
  writes down every byte it drew. A fault that only a real program
  shows can then be replayed anywhere.

## What it does not decide

- **How big a cell is.** `PosixBackend` takes a `cell` and puts it in
  the size of the pty. The number is what a screen answers to
  "CSI 16 t", so the screen passes it and nothing is claimed twice.
- **What a key sends.** `write_input` writes text as it stands.
  Encoding a key, and bracketing a paste, both read modes that the
  screen holds.

[ptterm]: https://github.com/Lillecarl/ptterm
[Textual]: https://github.com/Textualize/textual
