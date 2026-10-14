# ptyhost on its own. Work on it happens in pyterm
# (https://github.com/Lillecarl/pyterm), the repository this one is exported
# from; this file builds this directory against its siblings there, at the
# pyterm commit `flake.lock` names.
let
  compat = import ./nix/compat.nix;
in
{
  inputs ? compat.inputs,
  system ? builtins.currentSystem,
  # Your own nixpkgs, or null for the one pyterm pins.
  pkgs ? null,
}:
let
  pyterm = import inputs.pyterm (
    {
      inherit (inputs.pyterm) inputs;
      inherit system;
      sourceOverrides.ptyhost = ./.;
    }
    // (if pkgs == null then { } else { inherit pkgs; })
  );
in
{
  inherit (pyterm) ptyhost shell;
  default = pyterm.ptyhost;
  checks = pyterm.ptyhost.checks;
  pages = pyterm.pages.ptyhost;
}
