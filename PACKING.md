# Large-file transport for Git: gzip + 50 MB chunks

`pack-large-files.py` scans an explicitly selected repository/directory for
regular files **larger than 50,000,000 bytes**, streams each through gzip, and
splits the compressed stream into parts of **at most 50,000,000 bytes** (decimal
50 MB, slightly below 50 MiB). The final part can be smaller. Already-compressed
archives and existing `.part-aa` files are included: compression may not shrink
them, but the resulting chunks still obey the size limit.

This is optional transport packaging, not a change to the pinned artifacts.
It does not invoke Git, stage files, commit, push, rewrite history or modify
`.gitignore`. Git LFS or release/object storage is usually better for a warehouse
of this size; chunking does not remove repository size/performance constraints
or GitHub quotas. Every revision of a large artifact can still grow Git history.

## What is scanned and what is preserved

- Includes ignored and untracked files, so the existing warehouse ignore rule
  does not stop packing. Review a dry run: this can include unrelated large
  files inside the selected repository too.
- Skips every `.git` directory/file, symlinks (including directory symlinks),
  and `.warehouse-packed` directories. It does not recurse through symlinks.
- Small files and symlinks remain in place. It does **not** collect the whole
  repository into an archive; you must transfer the unchanged smaller files too.
- Stores gzip chunks in `<repo>/.warehouse-packed/<path-hash>/part-000000.gz`,
  etc. Each original gets its own compressed stream. Individual chunks are not
  standalone gzip archives: use `restore`, not `gunzip` on individual parts.
- `manifest.json` records original relative path, byte count, SHA-256 and POSIX
  permission bits, plus each chunk's order, size and SHA-256.
- Restores original bytes and executable permissions. It does not preserve
  timestamps, owners, ACLs/xattrs or hard-link identity. Special permission bits
  (setuid/setgid/sticky) are refused rather than silently discarded.
- Packing verifies chunk hashes and a full decompression round trip before
  optional original removal. Per-file publication permits resuming normal
  interrupted runs; existing differing files and corrupt chunks fail closed.
- A `.lock` directory prevents concurrent pack/restore/packed-verify operations.
  It does not lock other tools: stop preparation/builds and other writers first.
  After an abrupt kill, remove a stale lock only after confirming no operation
  is running. Orphan chunks from interruption before manifest publication are
  retained for inspection; do not blindly delete the store or your originals.

The manifest is an integrity record, not an independent signature. Keep a trusted
copy/signature or trusted Git commit for authenticity. Do not pack secrets into
a public repository; compression provides no encryption.

## Pack an already prepared repository (Mac or Linux)

Requires Python 3.11+; no Rust, Docker, network or Linux-only binaries are needed.
The examples assume the helper was copied to the artifacts repository root.
If you kept it in `offline-assets/`, use `python3 offline-assets/pack-large-files.py`
instead. Change to the **artifacts repository root**, not the package subdirectory:

```bash
cd /absolute/path/to/psd-rust-artifacts

# Inspect scope without creating or changing anything.
python3 pack-large-files.py pack --repo "$PWD" --dry-run

# Create and validate chunks, retaining original files as a first safe step.
python3 pack-large-files.py pack --repo "$PWD"

# Revalidate the stored chunks, then remove matching originals for Git transport.
python3 pack-large-files.py pack --repo "$PWD" --remove-originals

# Verify chunk hashes AND decompressed original hashes without restoring files.
python3 pack-large-files.py verify --repo "$PWD"
```

You can use `--remove-originals` on the first packing run if desired. Keep a
separate backup before converting a valuable warehouse. Allow disk space for
the original plus compressed chunks while each file is packed. Output reports
each file; compression/checksum scans of large files can take time, especially
on Mac Docker bind mounts. Native Mac Python is often preferable for this step.

**The packed warehouse cannot run its tools or pass `vendor-assets.sh verify`**
until restored. We intentionally do not rewrite its pinned downloads, component
lockfiles or sealed bundle manifest to pretend the packed layout is runnable.
The packing store must be outside the sealed `warehouse-assets/` directory.

## Automatically pack after successful preparation

Copy `pack-large-files.py` beside `vendor-assets.sh`, then on the Linux
preparation host (repository root example):

```bash
bash ./vendor-assets.sh prepare --skip-build --pack-repo "$PWD"
```

`--pack-repo` must be an **absolute** existing directory containing, but not equal
to, the bundle directory. The bundler completes its smoke checks and seal, verifies
the bundle, then invokes packing **with original removal enabled**. Packing
never runs after a failed preparation. The option is deliberately explicit:
without it, the normal runnable warehouse layout remains unchanged.

For the Docker command in [DOCKER-MAC.md](DOCKER-MAC.md), when `/warehouse`
mounts the whole destination repository, replace its final preparation line with:

```bash
bash ./vendor-assets.sh prepare --skip-build --pack-repo /warehouse
```

If the script stays in a subdirectory, mount the whole repository and set its
working directory appropriately, for example `-v "$PWD:/warehouse" -w
/warehouse/offline-assets`, then use the same absolute `--pack-repo /warehouse`.
This includes root-level files such as `toolchain-1.99.1.tar.xz.part-aa` as well
as the nested warehouse. Mounting only the package subdirectory cannot scan
files outside that mount. Keep the approved CA mount and trust setup unchanged.

The default bundle is still beside the script. For a root-level existing bundle
with the script in `offline-assets/`, also pass `--root /warehouse/warehouse-assets`.
On future preparation runs, restore packed originals **first**. Packing refuses
changed originals recorded in an existing manifest; it is not a version-upgrade
manager. To refresh pinned content, restore and back up the old transport store,
then deliberately start a fresh store after preparing and validating new assets.

## Restore after cloning or before using the warehouse

From the destination repository root:

```bash
python3 pack-large-files.py verify --repo "$PWD"
python3 pack-large-files.py restore --repo "$PWD"
bash ./vendor-assets.sh verify
```

Adjust script paths/`--root` for a subdirectory layout. Restoration works offline
on macOS or Linux; executing the restored Linux toolchain still requires a
compatible Linux x86_64 host. Keep all chunks and the manifest: restore does not
delete them. A matching existing original is accepted; a different existing file
is never overwritten. A corrupt/missing chunk cannot publish a partial restored
file. Symlink/path escapes are rejected. Restored files are published using a
same-filesystem hard link; use a filesystem supporting hard links (normal APFS
and Linux filesystems do).

**Do not stage restored oversized originals again.** For a source checkout, add
appropriate local ignore rules or remove them again with verified packing before
staging. The warehouse ignore rule is intentionally retained by this package.
Avoid broad `git add -f .` commands.

## Fix the rejected Git push: files AND history

Your rejection means oversized blobs exist in the commits being pushed. A new
commit deleting/replacing those files does **not** remove the old blobs from
history. Packing alone cannot repair that. This helper never rewrites history.

First pack/remove originals as above, review what remains, and stage only the
intended transport content. If you intentionally keep this warehouse in Git:

```bash
# The warehouse is normally ignored; force-add ONLY the packed store and the
# remaining small warehouse files after --remove-originals has succeeded.
git add -f -- .warehouse-packed warehouse-assets

# Stage deletions of already tracked oversized originals, including any root
# archives converted to chunks. This stages other tracked changes too: review!
git add -u
git diff --cached --stat
git status --short
```

Also stage the helper/documentation and any other intended small files explicitly.
When using a subdirectory layout, replace `warehouse-assets` above with its actual
path. Do not commit while packing/restoring is active: a temporary store/lock is
not a complete transport artifact. Review the manifest and staged chunks.

**Case A: all offending blobs were introduced only in the latest local commit,
and that commit has never been published/shared.** After staging the replacements
and deletions, amend that commit instead of adding a cleanup commit:

```bash
git commit --amend --no-edit
```

Then inspect the outgoing history and retry your normal push to your intended
branch. A rejected push alone does not prove this case applies: some offending
blobs may be in earlier local commits, or the tip may already have been shared
elsewhere. Amending the tip is not enough in those cases.

**Case B: oversized blobs occur in multiple unpushed commits or earlier history.**
Back up your repository, identify the introducing commits, and rewrite/squash the
unpublished commits deliberately (or use a separately planned history-filtering
migration). Coordinate before rewriting any shared history. Do not blindly
force-push, delete `.git`, or run a hard reset to fix this error.

A read-only audit of all blobs reachable from the current `HEAD`:

```bash
git rev-list --objects HEAD \
  | git cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)' \
  | awk '$1 == "blob" && $3 > 50000000 { print }'
```

This includes older reachable commits, not just the working tree. If the remote
tracking branch exists, audit the outgoing range (for example
`origin/main..HEAD`) in place of `HEAD` to focus on unpublished history. Blobs
larger than GitHub's hard limit in outgoing history must be removed from that
history before retrying. Smaller chunks address the per-file error, not a
promise that a multi-GB Git repository is an appropriate long-term store.
