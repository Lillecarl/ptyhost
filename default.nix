# The package this repository builds. The suite that judges it lives in
# `nix/checks.nix`, which declares its own inputs.
#
# **It takes no python dependency at all on this platform.** Running a program
# on a pty needs the standard library and the operating system, and that is
# the whole point of this package: a widget that depends on it takes on no
# toolkit and no parser. Lillecarl/pymux#85.
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
}:
let
  # What the wheel is built from, and nothing else. A denylist would carry
  # `tests` and the `.ruff_cache` a local run rewrites, and a source that a
  # test run changes rebuilds everything above it. Lillecarl/pymux#320.
  projectRoot = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      # Not only the `.py` files: `py.typed` is what tells a checker that
      # the annotations here are meant to be read. `setup.py` named it in
      # `package_data`, and hatchling takes the whole directory.
      (lib.fileset.fileFilter (file: file.hasExt "py" || file.name == "py.typed") ./ptyhost)
      ./pyproject.toml
      ./README.md
      ./LICENSE
    ];
  };

  package =
    (mkProject {
      inherit projectRoot python;
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
    ];
  };

  # What the suite runs on: ptyhost, everything it declares, and the `test`
  # extra beside them in the same file.
  testEnv = mkVirtualEnv "ptyhost-test-env" { ptyhost = [ "test" ]; };

  checks = callPackage ./nix/checks.nix { inherit testEnv testSources; };
in
package
