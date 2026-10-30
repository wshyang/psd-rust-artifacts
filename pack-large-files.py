#!/usr/bin/env python3
"""Reversible gzip/chunk transport for large repository files (no Git mutations)."""
import argparse
from contextlib import contextmanager
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile

LIMIT = 50_000_000  # Decimal MB; below GitHub's 50 MiB warning threshold.
STORE = ".warehouse-packed"
BUFFER = 1024 * 1024


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_path(root, name):
    rel = PurePosixPath(name)
    if not name or rel.is_absolute() or ".." in rel.parts or str(rel) != name:
        raise ValueError(f"Unsafe relative path: {name!r}")
    path = root
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Refusing symlink path: {path}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes root: {name!r}")
    return path


def scan(repo):
    """Include ignored/untracked files, but never Git internals or symlinks."""
    for base, dirs, names in os.walk(repo, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in (".git", STORE)
                         and not (Path(base) / d).is_symlink())
        for name in sorted(names):
            path = Path(base) / name
            if name == ".git" or path.is_symlink():
                continue
            info = path.stat()
            if stat.S_ISREG(info.st_mode) and info.st_size > LIMIT:
                yield path


def save(store, manifest):
    tmp = safe_path(store, ".manifest.tmp")
    try:
        with tmp.open("w") as out:
            json.dump(manifest, out, indent=2, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        tmp.replace(store / "manifest.json")
    finally:
        tmp.unlink(missing_ok=True)


def load(store):
    path = store / "manifest.json"
    if not path.exists():
        # Refuse to adopt an unrelated directory or silently discard interrupted work.
        if any(p.name != ".lock" for p in store.iterdir()):
            raise ValueError(f"Nonempty packing store without manifest: {store}")
        return {"format": 1, "codec": "gzip", "chunk_bytes": LIMIT, "files": {}}
    if path.is_symlink():
        raise ValueError("Packing manifest must not be a symlink")
    data = json.loads(path.read_text())
    if (data.get("format"), data.get("codec"), data.get("chunk_bytes")) != (1, "gzip", LIMIT):
        raise ValueError("Unsupported packing manifest")
    if not isinstance(data.get("files"), dict):
        raise ValueError("Invalid file inventory")
    used = set()
    for name, entry in data["files"].items():
        safe_path(store.parent, name)
        if any(p in (".git", STORE) for p in PurePosixPath(name).parts):
            raise ValueError(f"Reserved source path: {name}")
        if (not isinstance(entry["size"], int) or entry["size"] <= LIMIT
                or not isinstance(entry["mode"], int) or not 0 <= entry["mode"] <= 0o777
                or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])):
            raise ValueError(f"Invalid original metadata: {name}")
        if not isinstance(entry["parts"], list) or not entry["parts"]:
            raise ValueError(f"Missing chunks: {name}")
        key = hashlib.sha256(name.encode()).hexdigest()
        for index, part in enumerate(entry["parts"]):
            expected = f"{key}/part-{index:06d}.gz"
            if (part["path"] != expected or part["path"] in used
                    or not isinstance(part["size"], int) or not 0 < part["size"] <= LIMIT
                    or not re.fullmatch(r"[a-f0-9]{64}", part["sha256"])):
                raise ValueError(f"Invalid chunk metadata: {name}")
            safe_path(store, part["path"])
            used.add(part["path"])
    return data


@contextmanager
def locked_store(repo, create=False):
    store = safe_path(repo, STORE)
    if create:
        store.mkdir(exist_ok=True)
    if not store.is_dir():
        raise ValueError(f"No packing store: {store}")
    lock = store / ".lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError(f"Packing lock exists: {lock}; do not run concurrently. "
                         "Remove a stale lock only after confirming no pack/restore is running.")
    try:
        yield store
    finally:
        lock.rmdir()


class ChunkWriter:
    def __init__(self, directory, key):
        self.directory, self.key = directory, key
        self.parts, self.stream = [], None

    def write(self, data):
        total = len(data)
        while data:
            if self.stream is None:
                self.name = f"part-{len(self.parts):06d}.gz"
                self.stream = (self.directory / self.name).open("xb")
                self.size, self.hash = 0, hashlib.sha256()
            piece = data[:LIMIT - self.size]
            self.stream.write(piece)
            self.hash.update(piece)
            self.size += len(piece)
            data = data[len(piece):]
            if self.size == LIMIT:
                self.finish_part()
        return total

    def finish_part(self):
        if self.stream is not None:
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.stream.close()
            self.stream = None
            self.parts.append({"path": f"{self.key}/{self.name}", "size": self.size,
                               "sha256": self.hash.hexdigest()})


class PartReader(io.RawIOBase):
    def __init__(self, store, parts):
        super().__init__()
        self.paths = iter(safe_path(store, p["path"]) for p in parts)
        self.stream = None

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            if self.stream is None:
                path = next(self.paths, None)
                if path is None:
                    return 0
                self.stream = path.open("rb")
            count = self.stream.readinto(buffer)
            if count:
                return count
            self.stream.close()
            self.stream = None

    def close(self):
        if self.stream is not None:
            self.stream.close()
        super().close()


def check_entry(store, entry, output=None):
    for part in entry["parts"]:
        path = safe_path(store, part["path"])
        if path.stat().st_size != part["size"] or digest(path) != part["sha256"]:
            raise ValueError(f"Missing/modified chunk: {path}")
    size, sha = 0, hashlib.sha256()
    with PartReader(store, entry["parts"]) as raw, io.BufferedReader(raw) as reader:
        with gzip.GzipFile(fileobj=reader, mode="rb") as source:
            while data := source.read(min(BUFFER, entry["size"] - size + 1)):
                size += len(data)
                if size > entry["size"]:
                    raise ValueError("Decompressed data exceeds recorded size")
                sha.update(data)
                if output is not None:
                    output.write(data)
    if size != entry["size"] or sha.hexdigest() != entry["sha256"]:
        raise ValueError("Restored size/checksum mismatch")


def matches(path, entry):
    return (path.is_file() and path.stat().st_size == entry["size"]
            and stat.S_IMODE(path.stat().st_mode) == entry["mode"]
            and digest(path) == entry["sha256"])


def pack(repo, remove=False):
    with locked_store(repo, create=True) as store:
        manifest = load(store)
        # Verify old chunks before removing originals or adding more entries.
        for entry in manifest["files"].values():
            check_entry(store, entry)
        save(store, manifest)
        for path in scan(repo):
            name = path.relative_to(repo).as_posix()
            path = safe_path(repo, name)
            if name in manifest["files"]:
                if not matches(path, manifest["files"][name]):
                    raise ValueError(f"Original changed since packing: {name}; preserve it and use a fresh store")
            else:
                info = path.stat()
                if stat.S_IMODE(info.st_mode) & ~0o777:
                    raise ValueError(f"Refusing special permission bits: {name}")
                key = hashlib.sha256(name.encode()).hexdigest()
                target = safe_path(store, key)
                if target.exists():
                    raise ValueError(f"Unreferenced chunk directory: {target}; inspect interrupted work first")
                print(f"Packing {name} ({info.st_size:,} bytes)", flush=True)
                with tempfile.TemporaryDirectory(prefix=".tmp-", dir=store) as tmp:
                    writer = ChunkWriter(Path(tmp), key)
                    sha, size = hashlib.sha256(), 0
                    try:
                        with path.open("rb") as source, gzip.GzipFile(
                                filename="", fileobj=writer, mode="wb", compresslevel=6, mtime=0) as out:
                            while data := source.read(BUFFER):
                                sha.update(data)
                                size += len(data)
                                out.write(data)
                    finally:
                        writer.finish_part()
                    entry = {"size": size, "sha256": sha.hexdigest(),
                             "mode": stat.S_IMODE(info.st_mode), "parts": writer.parts}
                    if size != info.st_size or not matches(path, entry):
                        raise ValueError(f"Source changed during packing: {name}")
                    # Publish chunks, then manifest, then (optionally) remove original.
                    Path(tmp).rename(target)
                # On failure keep chunks for inspection; the original still exists.
                check_entry(store, entry)
                manifest["files"][name] = entry
                save(store, manifest)
            if remove:
                # Recheck immediately before deletion; concurrent writers remain unsupported.
                if not matches(path, manifest["files"][name]):
                    raise ValueError(f"Source changed before removal: {name}")
                path.unlink()
                print(f"Removed verified original: {name}", flush=True)
        print(f"Packed inventory: {len(manifest['files'])} files in {store}")
        if not remove:
            print("Originals retained. Use pack --remove-originals before staging for Git.")
        else:
            print("Transport state: restore originals before running warehouse verify/tools.")


def restore_or_verify(repo, restore=False):
    with locked_store(repo) as store:
        manifest = load(store)
        if not (store / "manifest.json").is_file():
            raise ValueError("Packing manifest missing")
        for name, entry in manifest["files"].items():
            path = safe_path(repo, name)
            if not restore:
                check_entry(store, entry)
                continue
            if path.exists():
                if not matches(path, entry):
                    raise ValueError(f"Refusing to overwrite different original: {name}")
                check_entry(store, entry)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".warehouse-restore-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as out:
                    check_entry(store, entry, out)
                    out.flush()
                    os.fsync(out.fileno())
                os.chmod(tmp, entry["mode"])
                # Hard-link publication refuses to overwrite a concurrently created file.
                os.link(tmp, path)
                print(f"Restored {name}", flush=True)
            finally:
                Path(tmp).unlink(missing_ok=True)
        print(f"{'Restored' if restore else 'Verified'} {len(manifest['files'])} packed files.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["pack", "restore", "verify"])
    parser.add_argument("--repo", type=Path, required=True, help="explicit repository/directory root to scan")
    parser.add_argument("--remove-originals", action="store_true", help="pack only: remove each original after verified packing")
    parser.add_argument("--dry-run", action="store_true", help="pack only: list files over 50,000,000 bytes without writes")
    args = parser.parse_args(argv)
    if args.mode != "pack" and (args.remove_originals or args.dry_run):
        parser.error("--remove-originals and --dry-run are only valid with pack")
    repo = args.repo.resolve()
    if not repo.is_dir() or repo == repo.parent:
        parser.error("--repo must be an existing non-filesystem-root directory")
    if (repo / ".warehouse-owner.json").exists() or (repo / "bundle-manifest.json").exists():
        parser.error("--repo must be the containing repository, not the sealed warehouse bundle itself")
    if args.dry_run:
        for path in scan(repo):
            print(f"{path.stat().st_size:>14,}  {path.relative_to(repo)}")
        return
    if args.mode == "pack":
        pack(repo, args.remove_originals)
    else:
        restore_or_verify(repo, args.mode == "restore")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, EOFError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
