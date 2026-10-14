# `flake.lock` for `default.nix`, through flake-compatish, so the flake and a
# build from the file read one lock. flake-compatish is pyterm's input, so it
# is two hops from the root of the lock.
let
  lock = builtins.fromJSON (builtins.readFile ../flake.lock);
  pyterm = lock.nodes.${lock.nodes.${lock.root}.inputs.pyterm};
  flake-compatish = builtins.fetchTree lock.nodes.${pyterm.inputs.flake-compatish}.locked;
in
import flake-compatish ../.
