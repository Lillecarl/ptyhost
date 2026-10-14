# ptyhost

> This repository is a copy. Work on ptyhost happens in
> [pyterm](https://github.com/Lillecarl/pyterm), which exports its `ptyhost/`
> directory here on every push. Send changes there, and issues to
> [Lillecarl/pymux](https://github.com/Lillecarl/pymux/issues).
> The docs are at <https://lillecarl.github.io/ptyhost/>.

<!-- The lines above are pyterm's nix/shared/readme-head.md, written by sync-sources. -->

Run a program on a pty. Start it, size it, pump its bytes, record them.

**No parsing, no drawing, no toolkit.** What the program writes goes to
a callback, and whoever built the `Process` decides what that means.

```python
import anyio
from ptyhost import Process
from ptyhost.backends.posix import PosixBackend, spawn_of

backend = PosixBackend(spawn_of(["bash"]))


async def main():
    async with anyio.create_task_group() as task_group:
        process = Process(backend, receive=print)
        process.set_size(80, 24)
        await process.start(task_group)


anyio.run(main)
```

## Why it is a package of its own

It came out of [ptterm], which is a terminal widget for
prompt-toolkit. A second widget for [Textual] needs the same pty layer
and must not pull prompt-toolkit in behind it, so the layer needed a
home that neither widget owns. Lillecarl/pymux#85 holds the argument.

## What is in it

- `Process` — the program: `start`, `set_size`, `write_input`,
  `suspend`, `resume`, `kill`, and what it is called and where it is.
- `backends/` — the pty itself, `posix` and `asyncssh` behind one
  `Backend` interface. `darwin` reads a process's name and directory on
  macOS.
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

<!-- The lines below are pyterm's nix/shared/readme-tail.md, written by sync-sources. -->

## Building

    nix build github:Lillecarl/ptyhost    # from this repository alone
    nix build --file . ptyhost            # in a checkout of pyterm

The suites that judge it run in pyterm, as `checks.ptyhost-*`.

## The collection

ptyhost is one source of [pyterm](https://github.com/Lillecarl/pyterm), and
each source claims one job:

| source | job |
| --- | --- |
| [pyte](https://lillecarl.github.io/pyte/) | parse, and hold a screen |
| [ptyhost](https://lillecarl.github.io/ptyhost/) | run a program on a pty, and carry its bytes |
| [ptterm](https://lillecarl.github.io/ptterm/) | draw one terminal, with prompt-toolkit |
| [txterm](https://lillecarl.github.io/txterm/) | draw one terminal, with Textual |
| [pymux](https://lillecarl.github.io/pymux/) | arrange several of them |
| [pysmithay](https://lillecarl.github.io/pysmithay/) | a headless Wayland compositor, for a GUI program in a pane |
| [pyterm-pytest](https://lillecarl.github.io/pyterm-pytest/) | the test equipment the suites share |

[pyterm's site](https://lillecarl.github.io/pyterm/) holds every source's
docs.
