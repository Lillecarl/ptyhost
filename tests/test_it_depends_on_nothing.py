"""
What this package may import.

`ptyhost` exists so that two terminal widgets can share one pty layer
without either of them dragging its toolkit in behind it
(Lillecarl/pymux#85). That only holds while this package depends on
nothing, and nothing but a habit would keep it that way.

So this reads the imports of every module and holds them to the rule.
An import that breaks it fails here, and not in a widget a month
later.
"""
import ast
from pathlib import Path

import pytest

import ptyhost

PACKAGE = Path(ptyhost.__file__).parent

#: Everything outside the standard library. A pty is the operating
#: system, and `yawinpty` is the pty of Windows, which is the only
#: thing this package cannot write itself.
MAY_IMPORT_OUTSIDE = {"yawinpty", "asyncssh"}

#: The parsers and the toolkits. A widget brings its own, and this
#: package is what the widgets have in common.
MUST_NOT_IMPORT = {
    "prompt_toolkit",
    "textual",
    "rich",
    "pyte",
    "ptterm",
    "txterm",
    "pymux",
    "wcwidth",
}


def _modules():
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" not in path.parts:
            found.append(path)
    return found


MODULES = _modules()


def _outside_imports(path: Path):
    "The packages that one module imports from outside itself."
    outside = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                outside.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and not node.level:
            outside.add((node.module or "").split(".")[0])
    return outside


def test_there_is_something_to_read():
    "A reader that found no module would pass every rule below."
    names = {path.name for path in MODULES}
    assert {"process.py", "record.py"} <= names


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_module_imports_a_parser_or_a_toolkit(path):
    assert not (_outside_imports(path) & MUST_NOT_IMPORT)


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_module_imports_a_third_party_package(path):
    """
    Beyond the two that carry a pty this package cannot open itself.

    A dependency here is a dependency of every widget that draws a
    terminal, so each one has to be argued for rather than added.
    """
    outside = _outside_imports(path) - MAY_IMPORT_OUTSIDE
    unknown = {
        name
        for name in outside
        if name and name not in _STANDARD_LIBRARY
    }
    assert unknown == set(), "%s imports %s" % (path.name, sorted(unknown))


#: The standard library modules this package uses. Naming them is the
#: point: a new one is a new call on the operating system, and it should
#: be a deliberate line in this file.
_STANDARD_LIBRARY = {
    "abc",
    "argparse",
    "array",
    "asyncio",
    "base64",
    "codecs",
    "collections",
    "ctypes",
    "fcntl",
    "getpass",
    "io",
    "json",
    "logging",
    "os",
    "pathlib",
    "pty",
    "pwd",
    "resource",
    "select",
    "shutil",
    "signal",
    "struct",
    "sys",
    "termios",
    "time",
    "traceback",
    "tty",
    "typing",
}
