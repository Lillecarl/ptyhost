# The package this repository builds. The suite that judges it lives in
# `nix/checks.nix`, which declares its own inputs.
#
# **It takes one python dependency: anyio.** Running a program on a pty
# waits -- for the child, for the master side, for a turn of the loop --
# and anyio is how this package waits. No toolkit and no parser besides:
# a widget that depends on it takes on nothing that draws.
# Lillecarl/pymux#85.
#
# **This is a pyproject.nix builders package, not a nixpkgs one.** What it
# needs is declared in `pyproject.toml` and the renderer reads it; an
# environment is a virtualenv rather than a PYTHONPATH. Lillecarl/pymux#319.
#
# Nothing else belongs in this repository: the dev shell and the collection
# that assembles this with its siblings live in pyterm.
{
  lib,
  stdenv,
  python,
  pyprojectHook,
  resolveBuildSystem,
  mkVirtualEnv,
  mkProject,
  callPackage,
  # The linter and formatter that the `ruff` check runs.
  ruff,
}:
let
  package =
    (mkProject {
      root = ./.;
      inherit python;
      extra = rendered: {
        passthru = rendered.passthru // { inherit checks; };

        meta = rendered.meta // {
          description = "Run a program on a pty: start it, size it, pump its bytes";
          homepage = "https://github.com/Lillecarl/ptyhost";
          license = lib.licenses.bsd3;
          mainProgram = "ptyhost-record";
        };
      };
    })
      {
        inherit stdenv pyprojectHook resolveBuildSystem;
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
      # The `ruff` check reads the package, where the suite above
      # reads the installed one and never looks here.
      ./ptyhost
    ];
  };

  # What the suite runs on: ptyhost, everything it declares, and the `test`
  # extra beside them in the same file.
  testEnv = mkVirtualEnv "ptyhost-test-env" { ptyhost = [ "test" ]; };

  checks = callPackage ./nix/checks.nix { inherit testEnv testSources ruff; };
in
package
