# How a suite is built, and why it is two derivations.
#
# Nix takes the output of a build that failed away. So a suite that fails the
# build leaves nothing to look at, and every artifact it made has to be
# fetched by running it again. That is backwards: the run that failed is the
# one whose output somebody wants.
#
# So a suite is two derivations.
#
# * The **run** executes the suite and does not fail because the suite failed.
#   Its output holds the log, whatever the suite wrote, and `status`, which
#   is the exit code.
# * The **verdict** reads `status` and fails when it is not zero. It holds
#   nothing, and it names the run so that a person knows where to look.
#
# `checks.<name>` is the verdict. `checks.<name>.run` is the evidence.
#
# **The run can still fail, and must.** Only the exit code of the suite is
# caught. Everything before it is setup, and a setup that fails fails the
# build, so a missing input is still loud. A builder that dies writes no
# `status`, the run fails, and the verdict is never reached.
#
# **What this costs, and the one case that is not worth it.** A red run is a
# build that succeeded, so nix caches it, and running it again gives the
# stored failure until an input changes. That is right when the suite
# answered: the log is the evidence, and it has to survive.
#
# It is wrong when the suite could not answer at all. A picture suite whose
# display server never came up pins a red that nothing clears: `--rebuild`
# re-runs the suite and then throws the new output away, because it compares
# rather than replaces, and the only way back is deleting the output and its
# referrers from the store by hand. That cost two sessions an hour each.
# Lillecarl/pymux#216.
#
# So a suite exits with `couldNotRun` to say "this is not my answer", and the
# run fails on that code alone. Nix keeps nothing, and the next build tries
# again. The log is tailed on the way out, because it is about to go with the
# output.
{ runCommand }:
rec {
  # What a suite exits with when it could not produce an answer.
  #
  # Free of everything that means something else: 1 is any error, 2 is usage,
  # 124 to 127 belong to `timeout` and the shell, and 128 and up are signals.
  # The run passes it in as `PYTERM_COULD_NOT_RUN`, so a suite reads the
  # number from the thing that acts on it rather than repeating it.
  couldNotRun = 97;

  # The run. `command` writes what it likes into `$out`, which is a
  # directory. Anything in `setup` runs before the guard, so a failure there
  # fails the build.
  run =
    {
      name,
      inputs ? [ ],
      env ? { },
      setup ? "",
    }:
    command:
    runCommand "${name}-run" (
      env
      // {
        nativeBuildInputs = inputs;
        PYTERM_COULD_NOT_RUN = toString couldNotRun;
      }
    ) ''
      mkdir -p "$out"
      ${setup}

      # Only the suite is inside the guard. `tee` keeps the live output, so
      # `nix log` still shows the run as it goes, and `PIPESTATUS` reads the
      # code of the suite and not of `tee`.
      set +e
      ( ${command} ) 2>&1 | tee "$out/log"
      echo "''${PIPESTATUS[0]}" > "$out/status"
      set -e

      # A run that could not run is not an answer, so nix must not keep it.
      # The tail goes to stderr because the output it lives in is about to
      # be thrown away with the build.
      if [ "$(cat "$out/status")" = "${toString couldNotRun}" ]; then
        echo "${name} could not run, so there is nothing to judge:" >&2
        echo "" >&2
        tail -n 30 "$out/log" >&2
        exit 1
      fi

      echo "${name} ended with $(cat "$out/status")"
    '';

  # The verdict on one run.
  verdict =
    name: from:
    runCommand name { passthru = { run = from; }; } ''
      status="$(cat ${from}/status)"
      if [ "$status" != "0" ]; then
        echo "${name}: the suite ended with $status" >&2
        echo "" >&2
        tail -n 30 "${from}/log" >&2
        echo "" >&2
        echo "The whole log, and everything the run left, is at:" >&2
        echo "    ${from}" >&2
        exit 1
      fi
      touch "$out"
    '';

  # Both halves at once, which is what a check is.
  suite = args: command: verdict args.name (run args command);
}
