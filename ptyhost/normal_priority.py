"""
Run a program at normal priority: `python -m ptyhost.normal_priority
PROGRAM [ARGUMENT...]`.

For a caller that cannot run code between its fork and the exec, as
`anyio.open_process` cannot. `ptyhost.spawn.at_normal_priority` says
what changes.
"""

from __future__ import annotations

import os
import sys
from typing import NoReturn

from .spawn import at_normal_priority


def main(argv: list[str]) -> NoReturn:
    if not argv:
        sys.exit("usage: python -m ptyhost.normal_priority PROGRAM [ARGUMENT...]")
    at_normal_priority()
    try:
        os.execvp(argv[0], argv)
    except OSError as e:
        # 127 is what a shell answers for a program it cannot run.
        print("%s: %s" % (argv[0], e.strerror), file=sys.stderr)
        sys.exit(127)


if __name__ == "__main__":
    main(sys.argv[1:])
