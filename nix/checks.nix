# The suite that judges ptyhost.
#
# It declares its own inputs, so `default.nix` holds the package and carries
# nothing that only a test needs.
#
# `package` and `testSources` come from `default.nix`: the first because a
# suite runs against the installed package, the second because it knows where
# the repository root is and this file does not.
#
# `nix/suite.nix` says why a check is two derivations.
{
  python,
  pytest,
  anyio,
  callPackage,
  package,
  testSources,
}:
let
  inherit (callPackage ./suite.nix { }) suite;

  # anyio carries the pytest plugin that runs a coroutine test. Without it
  # pytest fails one with "async def functions are not natively supported",
  # so no async test in this repository runs at all.
  pythonWithTests = python.withPackages (ps: [
    package
    pytest
    anyio
  ]);

  # Narrow a run to one file or one test while hunting:
  #
  #     PTYHOST_TESTS=tests/test_winsize.py \
  #       nix build --file . checks.ptyhost-unit
  selection = builtins.getEnv "PTYHOST_TESTS";

  prepare = ''
    cp -r ${testSources}/tests .
    cp ${testSources}/pyproject.toml .
    chmod -R +w .
    export HOME="$TMPDIR"
    export LANG=C.UTF-8
    export PYTHONDONTWRITEBYTECODE=1
  '';
in
{
  # Everything here needs a pty and nothing else. There is no screen to
  # compare against, because this package holds none.
  unit = suite {
    name = "ptyhost-unit";
    inputs = [ pythonWithTests ];
    env = { inherit selection; };
    setup = prepare;
  } "python -m pytest $selection -q -p no:cacheprovider";
}
