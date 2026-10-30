# Linux x86_64 offline asset warehouse

`vendor-assets.sh` is a **self-contained companion** to the existing `vendor.sh` /
`vendor2.sh` in `wshyang/psd-rust-artifacts`. This package is staged under
`new-temporal/psd-rust-artifacts/`; see [README.md](README.md) for copying it into
the artifacts repository as a subdirectory or beside the existing scripts. It
does not rewrite the root `Cargo.toml`, change the generic `vendor-cache` package,
or add members to that Cargo workspace. Generated assets live in
`warehouse-assets/` beside the script unless an absolute `--root` is supplied.

All commands in this guide assume you have changed into the directory containing
`vendor-assets.sh`. For the complete Apple Silicon Docker workflow, Cloudflare
Gateway CA export, and resuming existing downloads after moving the script, see
[DOCKER-MAC.md](DOCKER-MAC.md).

## What was verified here

- The existing split Rust archive was downloaded through the GitHub contents API;
  both parts matched the repository's SHA-256 file. Its contents are only the
  **rustc, Cargo and host rust-std installer archives**, plus `SHA256SUMS`. It does
  **not** contain Rustfmt, Clippy or LLVM coverage tools.
- Release names, versions and published SHA-256 digests were inspected for the
  Temporal executables, WASI SDK and QuickJS WASM modules. QuickJS and RustSec
  source archives were downloaded and hashed.
- Cargo-tool source archive checksums were read from the crates.io index.
- Shell syntax and network-free unit tests were run. These are **not** Cargo
  compilation, Temporal interoperability, or Function-sandbox validation.
- The sandbox cannot reach GitHub's release-asset download host or the official
  Rust/crates distribution endpoints. The complete binary warehouse has **not**
  been downloaded, built or end-to-end tested here. Run preparation on the
  connected machine where you maintain the actual warehouse.

## Pinned inventory

| Asset | Pin / scope |
|---|---|
| Temporal CLI, including dev server | `1.10.0`, Linux amd64 |
| Dedicated time-skipping server | `temporalio/sdk-java` native test server `1.40.0`, Linux amd64; not interchangeable with `server start-dev` |
| QuickJS-ng command module | `v0.18.0`, `qjs-wasi.wasm` |
| QuickJS-ng reactor module | `v0.18.0`, `qjs-wasi-reactor.wasm` |
| QuickJS source | `d7215b0ad3fc2c562e8317fd9bca058fd4863804` |
| WASI SDK | `29.0`, x86_64 Linux; matches QuickJS v0.18.0's release workflow |
| Stable Rust | `1.99.1`: rustc, Cargo, host std, Rustfmt, Clippy, LLVM tools and rust-src |
| Fuzzing Rust | `nightly-2026-11-09`: rustc, Cargo, host std, matching LLVM tools and rust-src |
| cargo-llvm-cov | `0.9.1` |
| cargo-deny | `0.20.2` |
| cargo-fuzz | `0.13.2` |
| sqlx-cli | `0.8.6`, Postgres + Rustls; no MySQL/SQLite/native-TLS defaults |
| RustSec database | source snapshot and Git bundle at `28d73fc95e5972af3403225a217def57dcaba3c4` |

The script never selects `latest` Rust or release URLs. On first preparation,
fixed-version/date Rust manifests are fetched over HTTPS and verified using their
published checksum files. Their exact component URLs and hashes are captured in
`components.lock.json` and included in the sealed inventory. Missing components
or a missing dated nightly cause a failure, not a silent toolchain substitution.
This first download is not equivalent to an independently audited signature.

Each Cargo CLI is bundled **separately** with its published `.crate` source,
**its own Cargo.lock**, its own `vendor/`, and its own offline `.cargo/config.toml`.
Application dependencies do not stand in for a CLI tool's dependency bundle.
All CLI libraries, build dependencies and their license files are retained by
Cargo vendoring. Tools are built from these bundles in offline mode.

## Prepare on a connected machine

Requirements:

- glibc Linux x86_64 (not Alpine/musl), Bash and Python 3.11+ with TLS support;
- Git, a native C/C++ compiler, Make, CMake and pkg-config;
- system headers and any native development libraries required by the pinned
  tool dependencies; shell/coreutils utilities used by Rust's installers;
- several GB of free space, potentially much more during compilation;
- HTTPS access to GitHub/release assets, codeload, static.rust-lang.org and the
  crates.io index/download endpoints.

```bash
chmod +x vendor-assets.sh
./vendor-assets.sh prepare
```

The default prepares archives and source bundles, installs both toolchains in the
bundle (no sudo or system Rust changes), builds the four Cargo tools, runs version
smoke checks, and finally writes `bundle-manifest.json`. Native build parallelism
is bounded to two jobs by default; override with `WAREHOUSE_JOBS`.

```bash
WAREHOUSE_JOBS=4 ./vendor-assets.sh prepare
```

For a source-only CLI bundle, while still including prebuilt Temporal/QuickJS and
installed Rust components:

```bash
./vendor-assets.sh prepare --skip-build
```

This intentionally marks `cargo_tool_binaries_built: false`. Do not describe that
result as having the four CLI executables installed. **`--skip-build` is not a
cross-platform download-only mode:** it still installs and runs Linux Rust/Cargo
and performs version smoke checks. The preparation host must therefore support
the selected Linux x86_64 glibc binaries.

### Diagnose an OS, architecture or libc rejection

```bash
./vendor-assets.sh diagnose
```

This prints the detected OS, CPU architecture, Python libc report and the system
GNU libc probe, without downloading anything or creating a bundle directory.
The updated preflight queries `os.confstr("CS_GNU_LIBC_VERSION")`, then
`getconf GNU_LIBC_VERSION`, with `platform.libc_ver()` as a fallback. The old
Python-only check could falsely reject glibc hosts with an incomplete Python
libc report. Unknown libc, musl-only hosts, non-Linux systems and non-x86_64 hosts
are still rejected rather than forcing incompatible executable installation.

If the host really is macOS, Windows, ARM64 Linux or Alpine, prepare on a glibc
x86_64 Linux machine/VM, or use Docker with an explicit `linux/amd64` platform.
For example, from the warehouse directory on a connected preparation machine:

```bash
docker run --rm --platform linux/amd64 \
  -v "$PWD:/warehouse" -w /warehouse ubuntu:24.04 \
  bash -lc 'apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates python3 git build-essential cmake pkg-config \
    xz-utils libssl-dev && \
    bash ./vendor-assets.sh prepare --skip-build'
```

This container command is a preparation example, not a pinned OS package mirror.
Pin its image digest and package sources for reproducible build-host provisioning.
An ARM host needs working amd64 emulation; preparation can be slower. Docker may
produce root-owned files on Linux hosts. Do not remove the host check or treat
`--skip-build` as permission to run Linux executables directly on macOS/Windows.

Use `--root /absolute/path` to select another bundle directory. A nonempty
unrelated directory is refused. Do not run concurrent preparations against one
root. A failed preparation leaves reusable checked downloads but is not a
completed/sealed bundle. A failed installation must be retried through
`prepare` on the connected machine if there is no longer a complete manifest.

Preparation itself never needs the root warehouse's Cargo manifest. Each CLI is
vendored from a temporary CWD outside it, with an isolated CARGO_HOME, so the
root warehouse's existing `[net] offline = true` / source replacement cannot
block acquisition. Upstream tool Cargo configuration is preserved as `.upstream`
rather than allowed to override the bundler's source configuration.

### TLS certificate verification failures in Docker

If preparation stops at `channel.toml.sha256`, the first stable Rust request is
`https://static.rust-lang.org/dist/channel-rust-1.99.1.toml.sha256`.
The script now reports the requested URL and trust-store guidance on certificate
verification errors. A redirect may involve a different host. Successful GitHub
asset downloads do not establish that the Rust distribution endpoint is trusted.

`self-signed certificate in certificate chain` means Python could not validate
the certificate chain against the container's trust store. Corporate VPN/proxy
or security-software HTTPS inspection is one possible cause, not a diagnosis.
macOS Keychain trust is **not automatically inherited by Ubuntu containers**.
Check the container clock and network policy as well. If inspection is expected,
obtain the approved CA certificate from your IT/security team through a trusted
channel and verify its SHA-256 fingerprint with them. Do not blindly trust a
certificate copied from the failing connection. Use a PEM CA certificate (one
certificate per `.crt` file), not a private key or the website's leaf certificate.

From the Mac warehouse directory, with the approved certificate stored outside
the warehouse (replace the example path):

```bash
ORG_CA="$HOME/certs/organization-root-ca.crt"
test -f "$ORG_CA" || { echo "CA file not found"; exit 1; }
openssl x509 -in "$ORG_CA" -noout -subject -issuer -fingerprint -sha256
# Confirm this fingerprint with IT before continuing.
docker run --rm --platform linux/amd64 \
  -v "$PWD:/warehouse" -w /warehouse \
  --mount "type=bind,source=$ORG_CA,target=/usr/local/share/ca-certificates/organization-root-ca.crt,readonly" \
  ubuntu:24.04 bash -lc '
    set -e
    apt-get update
    apt-get install -y --no-install-recommends \
      ca-certificates python3 git build-essential cmake pkg-config xz-utils libssl-dev
    update-ca-certificates
    export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
    export CARGO_HTTP_CAINFO=/etc/ssl/certs/ca-certificates.crt
    export GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt
    bash ./vendor-assets.sh prepare --skip-build
  '
```

The mounted CA is available when `ca-certificates` is configured, and the explicit
update rebuilds the system bundle before preparation. The three environment
variables select that same verified trust bundle for Python, Cargo and Git;
they do not turn verification off. If `apt-get` itself cannot bootstrap on your
network, use your organization's approved CA-enabled base image/package mirror
instead of weakening TLS checks. Additional approved CA certificates can be
mounted separately using the same one-certificate-per-file convention.

Keep the existing `warehouse-assets/` directory: preparation reuses its checked
downloads. With `--rm`, container trust changes disappear at exit, so include the
CA mount and trust setup on every new container run (or use an approved base
image). This example still leaves the four Cargo CLI binaries unbuilt; omit
`--skip-build` if you also want to compile them now. Do not use `curl -k`,
`GIT_SSL_NO_VERIFY`, `CARGO_HTTP_SSL_VERIFY=false` or an unverified Python SSL
context. If no inspection is expected or the approved CA does not resolve the
failure, ask IT to investigate the chain/network path rather than adding unknown
certificates.

## Optional repository-wide 50 MB transport chunks

See [PACKING.md](PACKING.md) for `pack-large-files.py` and the optional
`prepare --pack-repo /absolute/repository/path` hook. The hook runs only after
successful preparation and verification, compresses every regular file over
50,000,000 bytes in the selected repository (excluding Git internals, symlinks
and packing stores), splits it into at-most-50,000,000-byte chunks, and removes
verified originals. It requires the Python helper beside this shell script.

Packing is **not a runnable warehouse layout**. Restore originals with the
helper before `verify`, `prepare`, `install-offline` or executing any bundled
tool. Do not place `.warehouse-packed/` inside a sealed bundle or rewrite its
manifest to accommodate missing originals. The helper has its own integrity
manifest. Packing does not repair oversized blobs in earlier Git commits;
follow the guide's rejected-push history instructions as well.

## Transfer and use offline

Transfer **the entire directory**, including dotfiles, executable permissions and
symlinks, together with the script. Transfer the existing root Cargo warehouse
separately as before. `.cache/` is disposable; the sealed archives, vendor trees,
lockfiles, runtimes and toolchains are not.

```bash
./vendor-assets.sh verify
source warehouse-assets/env.sh

rustc --version
cargo fmt --version
cargo clippy --version
cargo llvm-cov --version
cargo deny --version
cargo sqlx --version
```

The environment file explicitly selects the bundled stable compiler and matching
`llvm-cov` / `llvm-profdata`, and enables Cargo offline mode. It is relocatable.
For fuzzing use the **nightly** environment, rather than `cargo +nightly`, which
could trigger rustup network access or choose an unpinned installation:

```bash
source warehouse-assets/env-fuzz.sh
cargo fuzz --version
# In a project with its fuzz sources, lockfile and dependencies also vendored:
# cargo fuzz run <target>
```

For reinstalling the toolchains and rebuilding CLI executables on the offline
machine from the retained archives and sources:

```bash
./vendor-assets.sh install-offline
```

That path verifies the existing inventory before modifying anything and uses
`cargo install --path ... --locked --offline`. Build outputs are temporary and
are not mistaken for reproducible source bundles. Native compiler/linker/system
headers are still required. Run with networking disabled at the OS/container
level for the strongest offline acceptance test: Cargo's offline flag does not
sandbox arbitrary build scripts.

The inventory records the preparation host's libc version. Linux x86_64 is not a
complete binary ABI guarantee: a tool built on a newer glibc may need an offline
rebuild on an older supported host. OS package repositories / a pinned build
container are a separate layer not supplied by this script.

## Temporal local executable selection

The bundle exports these **warehouse convenience variables**, not SDK-recognized
auto-discovery settings:

- `WAREHOUSE_TEMPORAL_CLI`
- `WAREHOUSE_TEMPORAL_TEST_SERVER`

The Rust SDK defaults can still download executables unless the application
explicitly passes an existing path. In SDK/Core `1.1.2`, the relevant Core API is
`temporalio_sdk_core::ephemeral_server::EphemeralExe::ExistingPath`:

```rust
use temporalio_sdk_core::ephemeral_server::{EphemeralExe, TestServerConfig};

let config = TestServerConfig::builder()
    .exe(EphemeralExe::ExistingPath(test_server_path))
    .build();
let server = config.start_server().await?;
// Keep the handle alive for the test; shut it down explicitly afterwards.
```

Use the CLI path with `TemporalDevServerConfig` for the dev server. High-level
SDK local-environment options also expose executable selection. Do not rely on
the `CachedDownload` defaults in an offline environment.

The chosen binary releases and SDK 1.1.2 still need an integration test exercising
workflow start, Updates, time-skipping timers and replay. The script's version
smoke check is **not** that test and does not start persistent servers.

## QuickJS: cached upstream modules, not a finished application sandbox

Both modules are pinned source/runtime inputs. They do **not** implement the
application's hardened bootstrap or enforce the Wasmtime/WASI host's policy.
The bundle labels them accordingly in `quickjs/STATUS.txt` and the manifest.

Still required during implementation:

- pinned Day.js + UTC plugin and big.js packages;
- bootstrap/lockdown code, frozen declared inputs and strict typed outputs;
- deterministic clock/random imports, fuel, stack/memory/table limits;
- no network, inherited secrets, directories or user module loading;
- `runner_build`, any precompiled artifact tied to the exact Wasmtime/target
  configuration, and the RT-01...RT-14 / S14 tests;
- reconciliation of precompiled Wasmtime loading with `forbid(unsafe_code)`.

The exact QuickJS source and its `.github/workflows/release.yml` are included.
For an **offline upstream rebuild** using the bundled SDK (requires local CMake
and Make), follow that workflow in a separate working copy/build directory:

```bash
source warehouse-assets/env.sh
cmake -S "$WAREHOUSE_ROOT/quickjs/source" -B /tmp/qjs-command-build \
  -DCMAKE_TOOLCHAIN_FILE="$WASI_SDK_PATH/share/cmake/wasi-sdk.cmake" \
  -DQJS_BUILD_WERROR=ON
cmake --build /tmp/qjs-command-build --target qjs_exe

cmake -S "$WAREHOUSE_ROOT/quickjs/source" -B /tmp/qjs-reactor-build \
  -DCMAKE_TOOLCHAIN_FILE="$WASI_SDK_PATH/share/cmake/wasi-sdk.cmake" \
  -DQJS_BUILD_WERROR=ON -DQJS_WASI_REACTOR=ON
cmake --build /tmp/qjs-reactor-build --target qjs_wasi
```

These produce new build outputs, not a claim of byte-identical reproduction of
upstream CI artifacts. Preserve the shipped modules and checksums; don't overwrite
them with unvalidated rebuilds. Native `rquickjs` from the Cargo warehouse and this
WASM runtime remain distinct alternatives, potentially based on different
QuickJS versions.

## Advisory database and audit tools

The RustSec snapshot is a point-in-time database, **not current vulnerability
coverage forever**. The Git bundle can be cloned without internet:

```bash
git clone warehouse-assets/rustsec/advisory-db.bundle /path/to/advisory-copy
```

For `cargo-deny`, seed its configured advisory database cache from that clone;
its cache uses a URL-derived directory name. Configure the pinned tool's database
path and offline/staleness policy deliberately. `cargo deny --frozen check
advisories` prohibits database fetching; it may correctly reject an old snapshot.
Do not falsify the database's age to make a check pass. The source bundle includes
the pinned tool's implementation and configuration documentation.

## Verification boundaries

`verify` checks all recorded file contents, executable modes and symlink targets,
rejects unexpected files outside `.cache`, and rechecks known upstream asset and
CLI source digests. Keep or sign the manifest externally if authenticity against
malicious modification of both files and inventory is required.

The final sign-off still needs an offline run on the intended deployment host:
application compilation, SQLx offline checks plus actual DB tests, Temporal time
skipping/replay, coverage and fuzz smoke tests, and the Function sandbox corpus.
Neither a crate cache nor this supplemental asset bundle alone proves those.
