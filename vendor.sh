#!/bin/bash
set -euo pipefail

VENDOR_DIR="vendor"
CARGO_CONFIG_DIR=".cargo"
CARGO_CONFIG_FILE="$CARGO_CONFIG_DIR/config.toml"

echo "Creating a dummy src/lib.rs so Cargo recognizes this as a valid package..."
mkdir -p src
touch src/lib.rs

echo "Generating Cargo.lock and downloading registry data..."
cargo update

echo "Vendoring all crates.io dependencies into the '$VENDOR_DIR' directory..."
mkdir -p "$CARGO_CONFIG_DIR"
# The vendor command outputs the necessary source replacement config
cargo vendor "$VENDOR_DIR" > "$CARGO_CONFIG_FILE"

echo "Configuring Cargo for strict offline mode..."
cat <<EOF >> "$CARGO_CONFIG_FILE"

# Prevent Cargo from attempting any network requests
[net]
offline = true
EOF

echo "Done! All dependencies are cached in '$VENDOR_DIR'."
