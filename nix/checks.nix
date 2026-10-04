# The suite that judges ptyhost.
#
# It declares its own inputs, so `default.nix` holds the package and carries
# nothing that only a test needs.
#
# `testEnv` and `testSources` come from `default.nix`: the first because a
# suite runs against the installed package, the second because it knows where
# the repository root is and this file does not.
#
# `nix/suite.nix` says why a check is two derivations.
{
  # The python the suite runs on: a virtualenv of ptyhost, what ptyhost
  # declares, and its `test` extra. `default.nix` builds it from
  # `pyproject.toml`, so what a suite may import is what the package
  # declares and there is no second list here. Lillecarl/pymux#319.
  testEnv,
  callPackage,
  testSources,
  # The linter and formatter that the `ruff` check runs.
  ruff,
}:
let
  inherit (callPackage ./suite.nix { }) suite;

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
    inputs = [ testEnv ];
    env = { inherit selection; };
    setup = prepare;
  } "python -m pytest $selection -q -p no:cacheprovider";

  # The style of ptyhost, held by the linter and the formatter rather
  # than by a run.
  #
  # `ruff check` holds the selected rules and `ruff format --check`
  # holds the layout at width 120, both read from the `pyproject.toml`
  # beside them. Neither can see the one thing the lazy annotations
  # rest on -- the presence of `from __future__ import annotations`
  # in every file -- so a grep holds that: UP037 unquotes only where
  # the import made the annotation lazy, and stays silent without it.
  # `ruff.toml` beside the umbrella says what each rule is for.
  #
  # The package stays out of the shared `prepare`: a `ptyhost/`
  # beside the tests shadows the installed package, and the suite
  # above judges the artifact, not the tree. The `ruff` check never
  # imports.
  ruff = suite {
    name = "ptyhost-ruff";
    inputs = [ ruff ];
    setup = prepare + ''
      cp -r ${testSources}/ptyhost .
    '';
  } ''
    export RUFF_CACHE_DIR="$TMPDIR/ruff"
    ruff check ptyhost tests
    ruff format --check ptyhost tests
    missing=$(grep -rL '^from __future__ import annotations' --include='*.py' --exclude-dir='.*' --exclude-dir='__pycache__' ptyhost tests || true)
    if [ -n "$missing" ]; then
      echo "files without the future import:"
      echo "$missing"
      exit 1
    fi
  '';
}
