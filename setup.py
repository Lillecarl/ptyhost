#!/usr/bin/env python
import os
import sys

from setuptools import find_packages, setup

with open(os.path.join(os.path.dirname(__file__), "README.md")) as f:
    long_description = f.read()

# Nothing. Running a program on a pty needs the standard library and
# the operating system, and that is the point of this package: a widget
# that depends on it takes on no toolkit and no parser.
requirements = []

# Install yawinpty on Windows only.
if sys.platform.startswith("win"):
    requirements.append("yawinpty")


setup(
    name="ptyhost",
    version="0.1",
    license="LICENSE",
    url="https://github.com/Lillecarl/ptyhost",
    description="Run a program on a pty: start it, size it, pump its bytes.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    packages=find_packages("."),
    install_requires=requirements,
    package_data={"ptyhost": ["py.typed"]},
    # A recorder, not a test. A fault that only a real program shows, on
    # the machine of the person who hit it, can only reach a check by
    # being recorded there first, so the recorder has to run anywhere.
    entry_points={
        "console_scripts": ["ptyhost-record = ptyhost.record:main"],
    },
    python_requires=">=3.10",
)
