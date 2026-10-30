# Source this file from Bash; do not execute it.
export WAREHOUSE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export WAREHOUSE_TOOLCHAIN="$WAREHOUSE_ROOT/toolchains/nightly-2026-11-09"
export PATH="$WAREHOUSE_TOOLCHAIN/bin:$WAREHOUSE_ROOT/bin:$WAREHOUSE_TOOLCHAIN/lib/rustlib/x86_64-unknown-linux-gnu/bin:$PATH"
export RUSTC="$WAREHOUSE_TOOLCHAIN/bin/rustc"
export RUSTDOC="$WAREHOUSE_TOOLCHAIN/bin/rustdoc"
export LLVM_COV="$WAREHOUSE_TOOLCHAIN/lib/rustlib/x86_64-unknown-linux-gnu/bin/llvm-cov"
export LLVM_PROFDATA="$WAREHOUSE_TOOLCHAIN/lib/rustlib/x86_64-unknown-linux-gnu/bin/llvm-profdata"
export WASI_SDK_PATH="$WAREHOUSE_ROOT/wasi-sdk"
export WAREHOUSE_TEMPORAL_CLI="$WAREHOUSE_ROOT/bin/temporal"
export WAREHOUSE_TEMPORAL_TEST_SERVER="$WAREHOUSE_ROOT/bin/temporal-test-server"
export CARGO_NET_OFFLINE=true
