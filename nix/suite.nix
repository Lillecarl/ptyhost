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
# **What this costs.** A red run is a build that succeeded, so nix caches it.
# Running it again gives the same stored failure until an input changes.
# `--rebuild` is the way to make it run again.
{ runCommand }:
rec {
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
    runCommand "${name}-run" (env // { nativeBuildInputs = inputs; }) ''
      mkdir -p "$out"
      ${setup}

      # Only the suite is inside the guard. `tee` keeps the live output, so
      # `nix log` still shows the run as it goes, and `PIPESTATUS` reads the
      # code of the suite and not of `tee`.
      set +e
      ( ${command} ) 2>&1 | tee "$out/log"
      echo "''${PIPESTATUS[0]}" > "$out/status"
      set -e

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
