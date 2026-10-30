# Portable additions for `wshyang/psd-rust-artifacts`

This directory stages the offline-warehouse companion developed in
`wshyang/new-temporal`. It is self-contained: copy it to the artifacts repository
without taking any of the Temporal design documents in the parent directory.
Nothing here modifies the existing flat Cargo collector or its `Cargo.toml`.
The existing `vendor.sh` / `vendor2.sh` remain separate prerequisites for the
application's Cargo dependency warehouse; this companion does not replace them.

## Files

| File | Purpose |
| --- | --- |
| `vendor-assets.sh` | Bash/Python bundler: `prepare`, `verify`, `install-offline`, `diagnose`; optional post-preparation packing |
| `pack-large-files.py` | Cross-platform gzip + 50 MB chunk packing, integrity verification and restoration |
| `PACKING.md` | Repository-wide transport packaging and rejected Git push/history repair guidance |
| `offline-warehouse-assets.md` | Pinned inventory, prerequisites, offline use, integration/security limits |
| `DOCKER-MAC.md` | Apple Silicon Docker commands, approved Cloudflare Gateway CA export and trust setup |
| `tests/test_*.py` | Network-free bundler/packing regression tests; no Rust installation required |
| `.gitignore` | Excludes generated bundles and Python bytecode within this directory |

The bundle includes pinned Linux x86_64 Temporal dev/time-skipping executables,
QuickJS-ng command/reactor WASM and source, WASI SDK, stable and nightly Rust
components, four independently vendored Cargo CLI tools, and a RustSec snapshot.
See the [complete inventory and limitations](offline-warehouse-assets.md).
Upstream QuickJS WASM is **not** the application's hardened Function runtime.

## Run from this checkout

```bash
cd psd-rust-artifacts
bash ./vendor-assets.sh diagnose
# On a connected glibc Linux x86_64 host with the documented prerequisites:
bash ./vendor-assets.sh prepare
bash ./vendor-assets.sh verify
```

On macOS, follow [DOCKER-MAC.md](DOCKER-MAC.md) instead. `--skip-build` skips
only the four Cargo CLI executable builds; it still executes Linux Rust/Cargo.
Do not source the generated Linux environment scripts on macOS.

The default output is `warehouse-assets/` **beside the script**, regardless of
the caller's working directory. After this reorganization that means
`new-temporal/psd-rust-artifacts/warehouse-assets/`. No generated bundle or CA
certificate is committed. Use an absolute `--root` to select a different output
location. If you already have partial downloads elsewhere, keep them and point
`--root` at that directory; in Docker it must be a path visible inside the
container. Do not run two preparations against the same output directory.

## Optional Git transport packaging

For repositories intentionally storing the warehouse in Git, see
[PACKING.md](PACKING.md). `pack-large-files.py` compresses files over 50 MB into
chunks of at most 50,000,000 bytes and provides checksum-verified restoration.
It runs natively on Mac or Linux with Python 3.11+. Use a dry run first, and stop
all preparation/build processes before packing. Normal packing keeps originals;
`--remove-originals` explicitly switches to the transport layout.

`prepare --pack-repo /absolute/repo/path` invokes the helper after successful
preparation/verification and **removes verified originals**. Restore before using
the warehouse. The helper must be beside the shell script for this optional mode.
The shell's original preparation modes remain self-contained. Packing does not
rewrite Git history: oversized files in rejected commits must also be removed
from the outgoing history as described in the guide.

## Copy into the artifacts repository

Choose **one** layout. Run these examples with both repositories cloned locally,
replacing the two paths:

```bash
SOURCE="/absolute/path/to/new-temporal/psd-rust-artifacts"
DEST="/absolute/path/to/psd-rust-artifacts"
```

### Option A: preserve a self-contained subdirectory (recommended)

Choose a new, unused destination directory so existing files are not overwritten:

```bash
mkdir "$DEST/offline-assets"
cp -p "$SOURCE/vendor-assets.sh" "$SOURCE/pack-large-files.py" \
  "$SOURCE/README.md" "$SOURCE/PACKING.md" "$SOURCE/DOCKER-MAC.md" \
  "$SOURCE/offline-warehouse-assets.md" "$SOURCE/.gitignore" "$DEST/offline-assets/"
cp -R "$SOURCE/tests" "$DEST/offline-assets/tests"
cd "$DEST/offline-assets"
bash ./vendor-assets.sh diagnose
```

All documentation commands then run from `offline-assets/`. No parent files or
Cargo workspace changes are required. These explicit copies exclude generated
`warehouse-assets/` content even if you have prepared a bundle in the source.

### Option B: put the companion beside the existing collector

Copy `vendor-assets.sh`, `pack-large-files.py`, `PACKING.md`,
`offline-warehouse-assets.md`, `DOCKER-MAC.md` and both `tests/test_*.py` files into
the matching paths at the artifacts repository root, reviewing any destination
conflicts first. Copy this README as
`README-offline-assets.md` rather than replacing that repository's README.
**Merge** this directory's ignore rules into the destination `.gitignore`; do
not overwrite its existing rules. Run the commands from that repository root.
The default bundle is then its root `warehouse-assets/` directory.

A Git cherry-pick of this PR's commit preserves the `psd-rust-artifacts/` prefix;
it does not automatically flatten paths into the other repository. The manual
copy options above are useful when you want a different destination layout.
Review `git diff --stat` and staged files before committing in the target repo.
In **this `new-temporal` PR**, only the source package belongs in Git, not generated
binaries or vendor trees. If you deliberately track warehouse content in the
artifacts repository, follow `PACKING.md` and explicitly review the transport
files. Never commit private keys, organization certificates or local secrets.

## Validate before or after copying

From the directory containing `vendor-assets.sh`:

```bash
bash -n vendor-assets.sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s tests -p 'test_*.py' -v
bash ./vendor-assets.sh diagnose
```

Tests require Python 3.11+ and Bash. They are network-free and can run on macOS;
`diagnose` reports unsupported preparation hosts without creating a bundle.
Full download/build, Temporal interoperability, and Function sandbox validation
have **not** been completed here. A successful unit suite is not a completed
offline warehouse. See the operational guide for final offline acceptance tests.
