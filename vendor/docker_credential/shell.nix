{nixpkgs ? import <nixpkgs> {}}:
with nixpkgs;
  mkShell {
    buildInputs = [llvmPackages.bintools];
    shellhook = ''
    '';
  }
