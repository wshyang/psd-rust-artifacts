# Prepare Linux x86_64 assets from a Mac

These commands run the companion in Ubuntu through Docker Desktop. On Apple
Silicon, `--platform linux/amd64` is mandatory and requires working amd64
emulation. Native macOS/arm64 is not a supported preparation host. Expect slower
compilation under emulation and allow several GB of disk space.

## 1. Select the package directory

In this repository:

```bash
cd /absolute/path/to/new-temporal/psd-rust-artifacts
```

After copying the package elsewhere, instead change to the directory containing
`vendor-assets.sh`. Every Docker command below mounts **that directory** at
`/warehouse`. Its `warehouse-assets/` output persists on your Mac even though
the container is removed. Do not source the generated Linux `env.sh` on the Mac.

## 2. Standard Docker preparation (no custom CA)

```bash
docker run --rm --platform linux/amd64 \
  -v "$PWD:/warehouse" -w /warehouse \
  ubuntu:24.04 bash -lc '
    set -e
    apt-get update
    apt-get install -y --no-install-recommends \
      ca-certificates python3 git build-essential cmake pkg-config \
      xz-utils libssl-dev
    bash ./vendor-assets.sh diagnose
    bash ./vendor-assets.sh prepare --skip-build
  '
```

`diagnose` should report Linux, x86_64, glibc and `supported: true`.
`--skip-build` still installs/runs Linux Rust/Cargo and vendors each Cargo CLI's
sources, lockfile and dependencies; it does **not** build the four CLI binaries.
Remove `--skip-build` to also compile those executables during preparation.
The default build parallelism is two jobs; add `-e WAREHOUSE_JOBS=4` to
`docker run` if you deliberately want to increase it.

Ubuntu 24.04 is a convenient preparation example, **not a reproducibly pinned
build-host image or an offline OS package mirror**. Pin an approved image digest
and package sources for that requirement. Native build tools, headers and glibc
compatibility are still required on the deployment host.

## 3. Cloudflare WARP / Gateway certificate failures

An error such as:

```text
SSL: CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain
```

means the container cannot validate the presented chain. In this workflow the
first stable Rust checksum request is:

```text
https://static.rust-lang.org/dist/channel-rust-1.99.1.toml.sha256
```

Managed Cloudflare Gateway HTTPS inspection can cause this when the Mac trusts
its CA but Ubuntu does not. macOS Keychain trust is not inherited by Docker.
Personal WARP alone does not prove inspection, and a CA's presence alone does
not prove it was used by this particular connection. Do not disable an
organization's network controls or TLS verification to work around this.

### Find and export the approved CA on macOS

List certificate names without dumping device certificate identifiers:

```bash
security find-certificate -a /Library/Keychains/System.keychain \
  | grep '"labl"'
```

Look for your organization's Gateway CA, possibly named
`Gateway CA - Cloudflare Managed G1 ...`. Confirm the exact certificate with IT.
Do not select the Apple system identities or Microsoft Intune device-management
certificates just because they are also present.

Replace the example name below with the **full exact name** from your own
Keychain. The example is a placeholder, not an actual certificate name:

```bash
CA_NAME='Gateway CA - Cloudflare Managed G1 REPLACE_WITH_YOUR_CA_ID'
mkdir -p "$HOME/certs"
security find-certificate \
  -c "$CA_NAME" \
  -p /Library/Keychains/System.keychain \
  > "$HOME/certs/cloudflare-gateway-ca.crt"

openssl x509 \
  -in "$HOME/certs/cloudflare-gateway-ca.crt" \
  -noout -subject -issuer -dates -fingerprint -sha256
```

`security ... -p` exports a public PEM certificate, not a private key. Export
only the intended CA; each `.crt` file must contain one certificate. If the
lookup fails or OpenSSL cannot parse the file, stop and correct the name/export.
The output path is created by this command; it does not already exist by default.

**Confirm the SHA-256 certificate fingerprint with IT/security through a trusted
channel before installing it in the container.** Keychain `hpky` / `skid` values
are not the certificate's SHA-256 fingerprint. Do not blindly trust a certificate
copied from a failing connection or a generic certificate found online. IT can
provide the approved PEM CA directly if Keychain export is unavailable.
Keep this certificate outside the repository and generated bundle.

### Prepare with the approved CA

After confirming the certificate, run from the package directory selected in
step 1:

```bash
ORG_CA="$HOME/certs/cloudflare-gateway-ca.crt"

docker run --rm --platform linux/amd64 \
  -v "$PWD:/warehouse" -w /warehouse \
  --mount "type=bind,source=$ORG_CA,target=/usr/local/share/ca-certificates/cloudflare-gateway-ca.crt,readonly" \
  ubuntu:24.04 bash -lc '
    set -e
    apt-get update
    apt-get install -y --no-install-recommends \
      ca-certificates python3 git build-essential cmake pkg-config \
      xz-utils libssl-dev
    update-ca-certificates
    export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
    export CARGO_HTTP_CAINFO=/etc/ssl/certs/ca-certificates.crt
    export GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt
    bash ./vendor-assets.sh diagnose
    bash ./vendor-assets.sh prepare --skip-build
  '
```

This uses the system CA bundle for Python, Cargo and Git and **keeps TLS
verification enabled**. If additional approved CAs are required, mount each as a
separate `.crt` file. If `apt-get` itself cannot bootstrap on your network, use
an organization-approved CA-enabled base image or package mirror, not insecure
TLS options. If this still fails, have IT inspect the actual chain/network path.

The `--rm` flag discards container trust changes at exit: include the mount and
trust setup each time, or use an approved CA-enabled base image. Never use
`curl -k`, `GIT_SSL_NO_VERIFY`, `CARGO_HTTP_SSL_VERIFY=false`, or an unverified
Python SSL context as a workaround.

## 4. Resume downloads after moving the script

Keep existing partial `warehouse-assets/` downloads. The script reuses checked
files, but a failed preparation is not a completed/sealed bundle.

If the downloads are already beside the script, simply rerun the appropriate
command above. If they remain in a different Mac directory, add a second mount
to that Docker command (replace the source with an existing absolute path):

```bash
--mount "type=bind,source=/absolute/path/to/existing/warehouse-assets,target=/existing-assets"
```

Then change its final preparation command to:

```bash
bash ./vendor-assets.sh prepare --root /existing-assets --skip-build
```

This avoids redownloading solely because the source package was reorganized.
Do not run simultaneous preparations against the same bundle. For a later full
build, rerun without `--skip-build` using the same mounts and root.

## 5. Optional repository-wide packing for Git

See [PACKING.md](PACKING.md) for gzip compression, 50,000,000-byte chunks,
restore commands, and fixing already-committed oversized files after a rejected
push. You can run the packer natively on your Mac after the container exits.

To pack automatically after preparation, copy `pack-large-files.py` beside the
shell script and change the Docker command's final line to:

```bash
bash ./vendor-assets.sh prepare --skip-build --pack-repo /warehouse
```

This **removes verified oversized originals** after successful preparation.
Restore before verifying or using the warehouse. `/warehouse` must mount the
whole repository if you also want to pack root-level artifacts outside the
package. With the script under `offline-assets/`, mount the repository root,
set `-w /warehouse/offline-assets`, and still pass `--pack-repo /warehouse`.
An external `--root /existing-assets` is not contained in `/warehouse`; use the
standalone packer on the appropriate repository instead. Do not run packing
while preparation or another writer is active.

## 6. Verify and transfer

After preparation completes, Python 3.11+ on your Mac can verify the bundle
without executing Linux binaries:

```bash
bash ./vendor-assets.sh verify
# For an existing bundle outside the package, use its Mac path instead:
# bash ./vendor-assets.sh verify --root /absolute/path/to/existing/warehouse-assets
```

Transfer the entire bundle preserving dotfiles, modes and symlinks, plus the
script, to a compatible Linux x86_64 host. Follow
[offline installation and verification](offline-warehouse-assets.md#transfer-and-use-offline).
Verification checks integrity; it does not replace offline builds, Temporal
integration tests or the application's Function sandbox tests.
