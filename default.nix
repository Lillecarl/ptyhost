# The package this repository builds. The suite that judges it lives in
# `nix/checks.nix`, which declares its own inputs.
#
# **It takes no python dependency at all.** Running a program on a pty needs
# the standard library and the operating system, and that is the whole point
# of this package: a widget that depends on it takes on no toolkit and no
# parser. Lillecarl/pymux#85.
#
# Nothing else belongs in this repository: the dev shell and the collection
# that assembles this with its siblings live in pyterm.
{
  lib,
  buildPythonPackage,
  setuptools,
  callPackage,
}:
let
  package = buildPythonPackage {
    pname = "ptyhost";
    version = "0.1";
    src = lib.cleanSource ./.;
    pyproject = true;

    # Only ruff and pytest configuration live in pyproject.toml, so the build
    # backend has to be named here rather than read from it.
    build-system = [ setuptools ];
    dependencies = [ ];

    # The suite runs as `checks.unit`, against the installed package.
    doCheck = false;
    pythonImportsCheck = [ "ptyhost" ];

    passthru = { inherit checks; };

    meta = {
      description = "Run a program on a pty: start it, size it, pump its bytes";
      homepage = "https://github.com/Lillecarl/ptyhost";
      license = lib.licenses.bsd3;
      mainProgram = "ptyhost-record";
    };
  };

  # Only the tests, not the whole repository. A copy of everything makes the
  # test runs rebuild on every unrelated edit.
  #
  # `pyproject.toml` comes with them: pytest reads its settings from the root
  # it finds, and a root with no config file is a root with no settings.
  testSources = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./tests
      ./pyproject.toml
    ];
  };

  checks = callPackage ./nix/checks.nix { inherit package testSources; };
in
package
