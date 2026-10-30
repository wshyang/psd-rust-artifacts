#!/bin/bash
set -euo pipefail
# Companion to vendor.sh/vendor2.sh: it does not modify their Cargo manifests.
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
exec python3 - "$@" <<'PYTHON_ASSET_BUNDLER'
"""Pinned Linux x86_64 companion to the flat Cargo dependency warehouse."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request

HOST = "x86_64-unknown-linux-gnu"
STABLE = "1.99.1"
NIGHTLY = "nightly-2026-11-09"
QJS_COMMIT = "d7215b0ad3fc2c562e8317fd9bca058fd4863804"
ADVISORY_COMMIT = "28d73fc95e5972af3403225a217def57dcaba3c4"
# name: (URL, SHA-256). No latest URLs are used.
ASSETS = {
    "temporal_cli_1.10.0_linux_amd64.tar.gz": (
        "https://github.com/temporalio/cli/releases/download/v1.10.0/temporal_cli_1.10.0_linux_amd64.tar.gz",
        "521f461f3d108902763bb71e85cd0fd382dc9357b59604ae03da6ae734d16890"),
    "temporal-test-server_1.40.0_linux_amd64.tar.gz": (
        "https://github.com/temporalio/sdk-java/releases/download/v1.40.0/temporal-test-server_1.40.0_linux_amd64.tar.gz",
        "524465a96274831e004326f6e2f2a5f3ef9aaed72c2069cf16c1d64b8b1ec3ca"),
    # SDK 29 matches QuickJS-ng v0.18.0's published release workflow.
    "wasi-sdk-29.0-x86_64-linux.tar.gz": (
        "https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-29/wasi-sdk-29.0-x86_64-linux.tar.gz",
        "87d1d1a2879d139cdc624b968efad3d4a97b8078cdff95e63ac88ecafd1a0171"),
    "qjs-wasi.wasm": (
        "https://github.com/quickjs-ng/quickjs/releases/download/v0.18.0/qjs-wasi.wasm",
        "2c218412cb8b17e46ee91bea7556ec38f5b9b6bd6312fddd3ceeed14aa71eaaa"),
    "qjs-wasi-reactor.wasm": (
        "https://github.com/quickjs-ng/quickjs/releases/download/v0.18.0/qjs-wasi-reactor.wasm",
        "8afa5d73287af5573613e15d5e633e91d8e096b40676f424f104087bc1416678"),
    "quickjs-ng-v0.18.0.tar.gz": (
        f"https://codeload.github.com/quickjs-ng/quickjs/tar.gz/{QJS_COMMIT}",
        "a49defe70cbcae9226d9fac025af116e87ebae2440dcff1ca3c80b79e1cdef0b"),
    "rustsec-advisory-db.tar.gz": (
        f"https://codeload.github.com/RustSec/advisory-db/tar.gz/{ADVISORY_COMMIT}",
        "b6ce50f853637b71d457c55fb7b24e157f66c94306f305524ef39567b7bba2fb"),
}
# Published .crate checksums from the crates.io index. Each tool keeps its OWN
# published Cargo.lock and vendor tree, rather than sharing the app's lockfile.
TOOLS = {
    "cargo-llvm-cov": ("0.9.1", "5e00647cc92940c4dd82249c480fe08fd3809767e89f056bcec46d8a7962ea5b", []),
    "cargo-deny": ("0.20.2", "e528dfcbe739af7ce37a77d3d6df1b29dd6887b1c701d888820c0f16b864f737", []),
    "cargo-fuzz": ("0.13.2", "5acfd01930e49823e58c30dd8012d3338a620377d7c7d4cc140ca4b2169400e2", []),
    "sqlx-cli": ("0.8.6", "4ddab32a90a16a1c7c9265f410761341cbbc70b27fe7f9563f41ae5c2b4ef1c4", ["--no-default-features", "--features", "rustls,postgres"]),
}
RUST_COMPONENTS = {
    STABLE: ["rustc", "cargo", "rust-std", "rustfmt-preview", "clippy-preview", "llvm-tools-preview", "rust-src"],
    NIGHTLY: ["rustc", "cargo", "rust-std", "llvm-tools-preview", "rust-src"],
}


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for data in iter(lambda: f.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def fetch(url, dest, checksum=None):
    """Download atomically; existing checked downloads are reusable."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if checksum is not None and sha(dest) != checksum:
            raise RuntimeError(f"Checksum mismatch in cached file: {dest}")
        return dest
    tmp = dest.with_name(dest.name + ".part")
    print(f"Downloading {dest.name}", flush=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "offline-warehouse/1"})
        with urllib.request.urlopen(req, timeout=120) as response, tmp.open("wb") as out:
            shutil.copyfileobj(response, out)
        if checksum is not None and sha(tmp) != checksum:
            raise RuntimeError(f"Download checksum mismatch: {url}")
        tmp.replace(dest)
    except (urllib.error.URLError, ssl.SSLError) as error:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        if isinstance(reason, ssl.SSLCertVerificationError):
            host = urllib.parse.urlsplit(url).hostname
            raise RuntimeError(
                f"TLS certificate verification failed downloading {url} (host: {host}): {reason}. "
                "Check the container clock and CA trust store. If your organization uses HTTPS "
                "inspection, obtain its approved PEM CA certificate through a trusted IT channel, "
                "verify its fingerprint, and install it inside the preparation container. "
                "On Ubuntu: place one CA per .crt file in /usr/local/share/ca-certificates/ "
                "and run update-ca-certificates. macOS Keychain trust is not inherited by Docker. "
                "Do not disable TLS verification. Keep existing downloads and rerun prepare. "
                "See offline-warehouse-assets.md for the Docker example."
            ) from error
        raise RuntimeError(f"Download failed for {url}: {error}") from error
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def within(path, root):
    return path.resolve().is_relative_to(root.resolve())


def unpack(archive, dest):
    """Extract into a new directory; reject escaping paths and special files.

    Write regular files before links; no member may have a link as its parent.
    Never use tarfile.extractall on these inputs (Python 3.11 compatible).
    """
    dest.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        names = set()
        link_names = {str(Path(m.name)) for m in members if m.issym() or m.islnk()}
        for m in members:
            name = Path(m.name)
            if name.is_absolute() or ".." in name.parts or not within(dest / name, dest):
                raise RuntimeError(f"Unsafe archive path: {m.name}")
            if not (m.isdir() or m.isfile() or m.issym() or m.islnk()):
                raise RuntimeError(f"Unsupported archive member: {m.name}")
            normalized = str(name)
            if normalized in names:
                raise RuntimeError(f"Duplicate archive member: {m.name}")
            names.add(normalized)
            for parent in name.parents:
                if str(parent) in link_names:
                    raise RuntimeError(f"Archive writes through a link: {m.name}")
        for m in members:
            path = dest / m.name
            if m.isdir():
                path.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                path.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(m) as src, path.open("xb") as out:
                    shutil.copyfileobj(src, out)
                path.chmod(m.mode & 0o777)
        # Hardlinks must point to regular files already materialized.
        for m in members:
            if m.islnk():
                target = dest / m.linkname
                if not within(target, dest) or not target.is_file() or target.is_symlink():
                    raise RuntimeError(f"Unsafe archive hardlink: {m.name}")
                path = dest / m.name
                path.parent.mkdir(parents=True, exist_ok=True)
                os.link(target, path)
        for m in members:
            if m.issym():
                if Path(m.linkname).is_absolute():
                    raise RuntimeError(f"Absolute archive symlink: {m.name}")
                path = dest / m.name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(m.linkname)
        for m in members:
            if not within(dest / m.name, dest):
                raise RuntimeError(f"Archive symlink escapes extraction root: {m.name}")


def single_root(path):
    dirs = list(path.iterdir())
    if len(dirs) != 1 or not dirs[0].is_dir():
        raise RuntimeError(f"Expected one package root in {path}")
    return dirs[0]


def run(args, *, cwd=None, env=None, stdout=None):
    print("+", " ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, env=env, stdout=stdout, check=True)


def replace_tree(source, dest):
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, symlinks=True)


def prepare_rust(root, channel):
    folder = root / "downloads" / channel
    if channel == STABLE:
        url = f"https://static.rust-lang.org/dist/channel-rust-{STABLE}.toml"
    else:
        date = channel.removeprefix("nightly-")
        url = f"https://static.rust-lang.org/dist/{date}/channel-rust-nightly.toml"
    # First preparation trusts the fixed-version HTTPS distribution manifest.
    # Archive both it and its published checksum; subsequent runs verify them.
    sums = fetch(url + ".sha256", folder / "channel.toml.sha256")
    digest = sums.read_text().split()[0]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise RuntimeError("Malformed Rust distribution checksum")
    manifest = fetch(url, folder / "channel.toml", digest)
    data = tomllib.loads(manifest.read_text())
    identity = data["pkg"]["rustc"]["version"]
    if channel == STABLE and not identity.startswith(STABLE + " "):
        raise RuntimeError(f"Unexpected stable compiler: {identity}")
    if channel != STABLE and "nightly" not in identity:
        raise RuntimeError(f"Expected nightly compiler, got {identity}")
    records = []
    for name in RUST_COMPONENTS[channel]:
        targets = data["pkg"][name]["target"]
        pkg = targets.get(HOST, targets.get("*"))
        if not pkg or not pkg.get("available"):
            raise RuntimeError(f"Required Rust component unavailable: {channel}/{name}")
        archive = folder / (name + ".tar.xz")
        fetch(pkg["xz_url"], archive, pkg["xz_hash"])
        records.append({"component": name, "url": pkg["xz_url"], "sha256": pkg["xz_hash"]})
    write_json(folder / "components.lock.json", {"manifest_sha256": digest, "compiler": identity, "components": records})


def install_rust(root, channel):
    prefix = root / "toolchains" / channel
    for name in RUST_COMPONENTS[channel]:
        with tempfile.TemporaryDirectory(prefix="warehouse-rust-") as tmp:
            stage = Path(tmp) / "extract"
            unpack(root / "downloads" / channel / (name + ".tar.xz"), stage)
            package = single_root(stage)
            run(["bash", package / "install.sh", "--prefix=" + str(prefix), "--disable-ldconfig"], cwd=package)
    run([prefix / "bin/rustc", "--version"])
    return prefix


def cargo_env(root, prefix, offline):
    env = os.environ.copy()
    env.update({"PATH": str(prefix / "bin") + ":" + str(root / "bin") + ":" + env.get("PATH", ""),
                "RUSTC": str(prefix / "bin/rustc"), "RUSTDOC": str(prefix / "bin/rustdoc"),
                "CARGO_HOME": str(root / ".cache/cargo"), "CARGO_NET_OFFLINE": str(offline).lower(),
                "CARGO_BUILD_JOBS": env.get("WAREHOUSE_JOBS", "2")})
    env.pop("RUSTUP_TOOLCHAIN", None)
    return env


def prepare_tool(root, prefix, name, version, checksum):
    archive = fetch(f"https://static.crates.io/crates/{name}/{name}-{version}.crate",
                    root / "downloads" / f"{name}-{version}.crate", checksum)
    # CWD outside the warehouse prevents its parent .cargo/config.toml from
    # forcing offline/source replacement before this tool has been vendored.
    with tempfile.TemporaryDirectory(prefix="warehouse-tool-") as tmp:
        stage = Path(tmp) / "extract"
        unpack(archive, stage)
        source = single_root(stage)
        lock = source / "Cargo.lock"
        if not lock.exists():
            raise RuntimeError(f"Published {name} lacks Cargo.lock; refusing an unpinned tool bundle")
        before = sha(lock)
        (source / ".cargo").mkdir(exist_ok=True)
        # Do not let source-provided Cargo configuration select remote sources.
        for cfg in [source / ".cargo/config", source / ".cargo/config.toml"]:
            if cfg.exists():
                cfg.rename(cfg.with_name(cfg.name + ".upstream"))
        with (source / "vendor-config.tmp").open("w") as out:
            run([prefix / "bin/cargo", "vendor", "--locked", "vendor"], cwd=source,
                env=cargo_env(root, prefix, False), stdout=out)
        config = (source / "vendor-config.tmp").read_text()
        (source / "vendor-config.tmp").unlink()
        (source / ".cargo/config.toml").write_text(config + "\n[net]\noffline = true\n")
        if sha(lock) != before:
            raise RuntimeError(f"Vendoring changed published lockfile for {name}")
        write_json(source / "warehouse-provenance.json", {"crate": name, "version": version,
                   "crate_sha256": checksum, "published_lock_sha256": before})
        replace_tree(source, root / "tools" / f"{name}-{version}")


def install_tools(root, prefix):
    with tempfile.TemporaryDirectory(prefix="warehouse-build-") as tmp:
        for name, (version, _, flags) in TOOLS.items():
            source = root / "tools" / f"{name}-{version}"
            env = cargo_env(root, prefix, True)
            env["CARGO_TARGET_DIR"] = str(Path(tmp) / name)
            run([prefix / "bin/cargo", "install", "--path", ".", "--locked", "--offline", "--force",
                 "--root", root, *flags], cwd=source, env=env)


def install_release_assets(root):
    for archive, executable in [("temporal_cli_1.10.0_linux_amd64.tar.gz", "temporal"),
                                ("temporal-test-server_1.40.0_linux_amd64.tar.gz", "temporal-test-server")]:
        with tempfile.TemporaryDirectory(prefix="warehouse-exe-") as tmp:
            stage = Path(tmp) / "extract"
            unpack(root / "downloads" / archive, stage)
            found = [p for p in stage.rglob(executable) if p.is_file()]
            if len(found) != 1:
                raise RuntimeError(f"Expected exactly one {executable} in {archive}")
            runtime = root / "runtimes" / executable
            relative_executable = found[0].relative_to(stage)
            replace_tree(stage, runtime)  # preserve licenses and any sibling libraries
            real_executable = runtime / relative_executable
            real_executable.chmod(0o755)
            dest = root / "bin" / executable
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.unlink(missing_ok=True)
            dest.symlink_to(os.path.relpath(real_executable, dest.parent))
    for name, dest in [("wasi-sdk-29.0-x86_64-linux.tar.gz", "wasi-sdk"),
                       ("quickjs-ng-v0.18.0.tar.gz", "quickjs/source"),
                       ("rustsec-advisory-db.tar.gz", "rustsec/snapshot")]:
        with tempfile.TemporaryDirectory(prefix="warehouse-tree-") as tmp:
            stage = Path(tmp) / "extract"
            unpack(root / "downloads" / name, stage)
            replace_tree(single_root(stage), root / dest)
    for name in ["qjs-wasi.wasm", "qjs-wasi-reactor.wasm"]:
        source = root / "downloads" / name
        if source.read_bytes()[:8] != b"\x00asm\x01\x00\x00\x00":
            raise RuntimeError(f"Invalid core WASM header: {name}")
        shutil.copy2(source, root / "quickjs" / name)
    (root / "quickjs/STATUS.txt").write_text(
        "Upstream QuickJS-ng v0.18.0 modules, NOT the hardened application Function runner.\n"
        "App bootstrap, deterministic imports, typed I/O, limits, dayjs/UTC plugin, big.js,\n"
        "runner_build and red-team validation remain implementation requirements.\n")


def prepare_advisory_git(root):
    # Include actual Git history for consumers that require a Git database;
    # a checksummed source snapshot is included separately as well.
    dest = root / "rustsec/advisory-db.bundle"
    with tempfile.TemporaryDirectory(prefix="warehouse-advisories-") as tmp:
        run(["git", "init", "--bare", tmp])
        run(["git", "-C", tmp, "fetch", "--no-tags", "https://github.com/RustSec/advisory-db.git", ADVISORY_COMMIT])
        run(["git", "-C", tmp, "update-ref", "refs/heads/warehouse-pin", ADVISORY_COMMIT])
        run(["git", "-C", tmp, "symbolic-ref", "HEAD", "refs/heads/warehouse-pin"])
        run(["git", "-C", tmp, "bundle", "create", dest, "refs/heads/warehouse-pin"])
        run(["git", "-C", tmp, "bundle", "verify", dest])


def write_env(root):
    for filename, channel in [("env.sh", STABLE), ("env-fuzz.sh", NIGHTLY)]:
        (root / filename).write_text(
            '# Source this file from Bash; do not execute it.\n'
            'export WAREHOUSE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"\n'
            f'export WAREHOUSE_TOOLCHAIN="$WAREHOUSE_ROOT/toolchains/{channel}"\n'
            f'export PATH="$WAREHOUSE_TOOLCHAIN/bin:$WAREHOUSE_ROOT/bin:$WAREHOUSE_TOOLCHAIN/lib/rustlib/{HOST}/bin:$PATH"\n'
            'export RUSTC="$WAREHOUSE_TOOLCHAIN/bin/rustc"\n'
            'export RUSTDOC="$WAREHOUSE_TOOLCHAIN/bin/rustdoc"\n'
            f'export LLVM_COV="$WAREHOUSE_TOOLCHAIN/lib/rustlib/{HOST}/bin/llvm-cov"\n'
            f'export LLVM_PROFDATA="$WAREHOUSE_TOOLCHAIN/lib/rustlib/{HOST}/bin/llvm-profdata"\n'
            'export WASI_SDK_PATH="$WAREHOUSE_ROOT/wasi-sdk"\n'
            'export WAREHOUSE_TEMPORAL_CLI="$WAREHOUSE_ROOT/bin/temporal"\n'
            'export WAREHOUSE_TEMPORAL_TEST_SERVER="$WAREHOUSE_ROOT/bin/temporal-test-server"\n'
            'export CARGO_NET_OFFLINE=true\n')
    # WAREHOUSE_* paths are our convenience variables, NOT SDK auto-discovery.


def smoke(root, tools_built):
    with tempfile.TemporaryDirectory(prefix="warehouse-smoke-") as tmp:
        for channel in RUST_COMPONENTS:
            prefix = root / "toolchains" / channel
            env = cargo_env(root, prefix, True)
            for command in [[prefix / "bin/rustc", "--version"], [prefix / "bin/cargo", "--version"],
                            [prefix / f"lib/rustlib/{HOST}/bin/llvm-cov", "--version"],
                            [prefix / f"lib/rustlib/{HOST}/bin/llvm-profdata", "--version"]]:
                run(command, cwd=tmp, env=env)
        prefix = root / "toolchains" / STABLE
        env = cargo_env(root, prefix, True)
        for sub in ["fmt", "clippy"] + (["llvm-cov", "deny", "fuzz", "sqlx"] if tools_built else []):
            run([prefix / "bin/cargo", sub, "--version"], cwd=tmp, env=env)
        run([root / "bin/temporal", "--version"], cwd=tmp)
        run([root / "wasi-sdk/bin/clang", "--version"], cwd=tmp)
    # Not a Temporal time-skipping integration test or a sandbox security test.


def seal(root, tools_built):
    records = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts[0] == ".cache" or str(rel) == "bundle-manifest.json":
            continue
        if path.is_symlink():
            if not within(path, root):
                raise RuntimeError(f"Bundle symlink escapes root: {rel}")
            records[str(rel)] = {"symlink": os.readlink(path)}
        elif path.is_file():
            records[str(rel)] = {"sha256": sha(path), "mode": path.stat().st_mode & 0o777}
    write_json(root / "bundle-manifest.json", {"format": 1, "target": HOST, "stable": STABLE,
               "nightly": NIGHTLY, "cargo_tool_binaries_built": tools_built,
               "build_libc": platform.libc_ver(), "preparation_host": host_info(),
               "quickjs_application_hardening": "NOT IMPLEMENTED",
               "temporal_sdk_integration": "NOT VALIDATED", "rustsec_commit": ADVISORY_COMMIT,
               "files": records})


def verify(root):
    data = json.loads((root / "bundle-manifest.json").read_text())
    if data["target"] != HOST:
        raise RuntimeError("Unexpected bundle target")
    for name, info in data["files"].items():
        path = root / name
        if Path(name).is_absolute() or ".." in Path(name).parts or not within(path, root):
            raise RuntimeError(f"Unsafe manifest path: {name}")
        if "symlink" in info:
            if not path.is_symlink() or os.readlink(path) != info["symlink"]:
                raise RuntimeError(f"Changed symlink: {name}")
        elif not path.is_file() or path.is_symlink() or sha(path) != info["sha256"] or (path.stat().st_mode & 0o777) != info["mode"]:
            raise RuntimeError(f"Missing/modified file: {name}")
    actual = {str(p.relative_to(root)) for p in root.rglob("*")
              if (p.is_file() or p.is_symlink()) and p.relative_to(root).parts[0] != ".cache"
              and str(p.relative_to(root)) != "bundle-manifest.json"}
    if actual != set(data["files"]):
        raise RuntimeError("Unexpected or missing files outside the ignored .cache directory")
    # Recheck externally pinned downloads, not just the locally generated inventory.
    for name, (_, expected) in ASSETS.items():
        if sha(root / "downloads" / name) != expected:
            raise RuntimeError(f"Pinned asset mismatch: {name}")
    for name, (version, expected, _) in TOOLS.items():
        if sha(root / "downloads" / f"{name}-{version}.crate") != expected:
            raise RuntimeError(f"Pinned tool source mismatch: {name}")
    print(f"Verified {len(data['files'])} inventory entries. Tool binaries built: {data['cargo_tool_binaries_built']}")
    return data


def host_info():
    """Probe the preparation host, not merely the libc linked to Python.

    platform.libc_ver() can be empty or report 'libc' for otherwise usable
    glibc hosts. Prefer the system's GNU libc query and retain diagnostics.
    This is a preflight check, not a guarantee of every binary's ABI needs.
    """
    system = platform.system()
    machine = platform.machine()
    libc_name, libc_version = platform.libc_ver()
    info = {"target": HOST, "system": system, "machine": machine,
            "python_libc": {"name": libc_name, "version": libc_version},
            "glibc_version": None, "glibc_probe": None, "probe_notes": []}

    def accept_glibc(text, probe):
        parts = (text or "").strip().split()
        if len(parts) == 2 and parts[0].lower() == "glibc" and all(
                part.isdigit() for part in parts[1].split(".")):
            info.update(glibc_version=parts[1], glibc_probe=probe)
            return True
        return False

    if system == "Linux":
        try:
            accept_glibc(os.confstr("CS_GNU_LIBC_VERSION"), "os.confstr")
        except (AttributeError, OSError, ValueError) as exc:
            info["probe_notes"].append(f"confstr unavailable: {exc}")
        if not info["glibc_version"]:
            getconf = shutil.which("getconf")
            if getconf:
                try:
                    result = subprocess.run([getconf, "GNU_LIBC_VERSION"],
                        capture_output=True, text=True, timeout=5, check=False)
                    if result.returncode != 0 or not accept_glibc(result.stdout, "getconf"):
                        info["probe_notes"].append("getconf did not identify GNU libc")
                except (OSError, subprocess.TimeoutExpired) as exc:
                    info["probe_notes"].append(f"getconf unavailable: {exc}")
            else:
                info["probe_notes"].append("getconf not installed")
        if not info["glibc_version"]:
            accept_glibc(f"{libc_name} {libc_version}", "platform.libc_ver")
    info["supported"] = (system == "Linux" and machine.strip().lower() in
                         ["x86_64", "amd64"] and info["glibc_version"] is not None)
    return info


def require_host(info):
    if not info["supported"]:
        libc = info["python_libc"]
        raise RuntimeError(
            f"Target requires glibc Linux x86_64; detected OS={info['system']!r}, "
            f"architecture={info['machine']!r}, Python libc="
            f"{libc['name'] or 'unknown'} {libc['version']}, "
            f"system glibc={info['glibc_version'] or 'not detected'}. "
            "--skip-build still runs the Linux toolchain. "
            "Run './vendor-assets.sh diagnose' for details. Use a glibc Linux "
            "x86_64 host or a linux/amd64 Debian/Ubuntu container; do not bypass this check.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["prepare", "verify", "install-offline", "diagnose"])
    parser.add_argument("--root", type=Path, default=Path.cwd() / "warehouse-assets")
    parser.add_argument("--skip-build", action="store_true", help="skip only Cargo-tool executable builds; still requires glibc Linux x86_64")
    parser.add_argument("--pack-repo", type=Path, metavar="ABSOLUTE_PATH",
                        help="prepare only: afterwards gzip/split files over 50 MB across this repo and remove verified originals; restore before use")
    args = parser.parse_args(argv)
    packer = Path.cwd() / "pack-large-files.py"
    if args.pack_repo is not None:
        if args.mode != "prepare":
            parser.error("--pack-repo is only supported with prepare")
        if not args.pack_repo.is_absolute() or not args.pack_repo.is_dir():
            parser.error("--pack-repo must be an existing absolute repository directory")
        args.pack_repo = args.pack_repo.resolve()
        bundle = args.root.resolve()
        if args.pack_repo == args.pack_repo.parent or bundle == args.pack_repo or not bundle.is_relative_to(args.pack_repo):
            parser.error("--pack-repo must contain (but not equal) the bundle directory and cannot be the filesystem root")
        if not packer.is_file():
            parser.error("--pack-repo requires pack-large-files.py beside vendor-assets.sh")
    if args.skip_build and args.mode != "prepare":
        parser.error("--skip-build is only supported with prepare")
    if args.mode == "diagnose":
        print(json.dumps(host_info(), indent=2))
        return
    root = args.root.resolve()
    if args.mode == "verify":
        verify(root)
        return
    require_host(host_info())
    for name in ["bash", "git", "cc", "c++", "make", "cmake", "pkg-config"]:
        if not shutil.which(name):
            raise RuntimeError(f"Required preparation/rebuild prerequisite missing: {name}")
    if root == root.parent:
        raise RuntimeError("Refusing to use the filesystem root as a bundle directory")
    owner = root / ".warehouse-owner.json"
    if root.exists() and any(root.iterdir()) and not owner.is_file():
        raise RuntimeError("Refusing to overwrite a nonempty directory not owned by this bundler")
    if owner.exists() and json.loads(owner.read_text()) != {"format": 1, "target": HOST}:
        raise RuntimeError("Incompatible bundle ownership marker")
    if args.mode == "prepare":
        if (root / "bundle-manifest.json").exists():
            verify(root)  # Never silently bless changes to a completed bundle.
        root.mkdir(parents=True, exist_ok=True)
        write_json(owner, {"format": 1, "target": HOST})
        for name, (url, digest) in ASSETS.items():
            fetch(url, root / "downloads" / name, digest)
        for channel in RUST_COMPONENTS:
            prepare_rust(root, channel)
        # A previous success marker must not survive a failed rebuild.
        (root / "bundle-manifest.json").unlink(missing_ok=True)
    else:
        verify(root)
        (root / "bundle-manifest.json").unlink()
    for channel in RUST_COMPONENTS:
        install_rust(root, channel)
    prefix = root / "toolchains" / STABLE
    install_release_assets(root)
    if args.mode == "prepare":
        for name, (version, checksum, _) in TOOLS.items():
            prepare_tool(root, prefix, name, version, checksum)
        prepare_advisory_git(root)
    if not args.skip_build:
        install_tools(root, prefix)
    write_env(root)
    smoke(root, not args.skip_build)
    seal(root, not args.skip_build)
    print("Complete. Preserve the entire bundle plus this script; verify after transfer.")
    print("Temporal SDK compatibility and app Function sandbox tests remain required.")
    if args.pack_repo is not None:
        verify(root)
        run([sys.executable, packer, "pack", "--repo", args.pack_repo, "--remove-originals"])
        print("Repository packed for transport. Restore with pack-large-files.py before warehouse verify/use.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

PYTHON_ASSET_BUNDLER
