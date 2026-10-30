"""Network-free tests for the asset bundler, not binary integration tests."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "vendor-assets.sh"
source = SCRIPT.read_text().split("<<'PYTHON_ASSET_BUNDLER'\n", 1)[1].rsplit("\nPYTHON_ASSET_BUNDLER", 1)[0]
bundler = types.ModuleType("vendor_assets")
exec(compile(source, str(SCRIPT), "exec"), bundler.__dict__)


class AssetTests(unittest.TestCase):
    def test_shell_and_help(self):
        subprocess.run(["bash", "-n", SCRIPT], check=True)
        result = subprocess.run(["bash", SCRIPT, "--help"], capture_output=True, text=True, check=True)
        self.assertIn("install-offline", result.stdout)

    def test_relocated_script_uses_its_directory_not_callers(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            package = base / "copied package"
            caller = base / "unrelated caller"
            package.mkdir()
            caller.mkdir()
            script = package / "vendor-assets.sh"
            script.write_text(SCRIPT.read_text())
            result = subprocess.run(["bash", script, "diagnose"], cwd=caller,
                                    capture_output=True, text=True, check=True)
            self.assertIn("supported", json.loads(result.stdout))
            # A missing bundle must be sought beside the copied script, with no writes.
            result = subprocess.run(["bash", script, "verify"], cwd=caller,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(str(package / "warehouse-assets/bundle-manifest.json"), result.stderr)
            external = base / "existing assets"
            result = subprocess.run(["bash", script, "verify", "--root", str(external)],
                                    cwd=caller, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(str(external / "bundle-manifest.json"), result.stderr)
            self.assertEqual(list(caller.iterdir()), [])
            self.assertEqual(list(package.iterdir()), [script])
            self.assertFalse(external.exists())

    def test_pack_repo_only_runs_after_successful_seal_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            repo = Path(tmp).resolve()
            root = repo / "warehouse-assets"
            events = []
            stack.enter_context(patch.object(bundler.Path, "cwd", return_value=SCRIPT.parent))
            stack.enter_context(patch.object(bundler, "require_host"))
            stack.enter_context(patch.object(bundler.shutil, "which", return_value="/usr/bin/tool"))
            for name in ["fetch", "prepare_rust", "install_rust", "install_release_assets",
                         "prepare_tool", "prepare_advisory_git", "write_env", "smoke"]:
                stack.enter_context(patch.object(bundler, name))
            stack.enter_context(patch.object(bundler, "seal", side_effect=lambda *a: events.append("seal")))
            stack.enter_context(patch.object(bundler, "verify", side_effect=lambda *a: events.append("verify")))
            run = stack.enter_context(patch.object(bundler, "run", side_effect=lambda *a: events.append("pack")))
            bundler.main(["prepare", "--root", str(root), "--skip-build", "--pack-repo", str(repo)])
            self.assertEqual(events, ["seal", "verify", "pack"])
            self.assertEqual(run.call_args.args[0][1:], [SCRIPT.parent / "pack-large-files.py",
                             "pack", "--repo", repo, "--remove-originals"])
            run.reset_mock()
            with patch.object(bundler, "smoke", side_effect=RuntimeError("smoke failed")):
                with self.assertRaises(RuntimeError):
                    bundler.main(["prepare", "--root", str(root), "--skip-build", "--pack-repo", str(repo)])
            run.assert_not_called()

    def test_pack_repo_invalid_arguments_fail_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            for arguments in (["verify", "--pack-repo", str(repo)],
                              ["prepare", "--pack-repo", "relative"],
                              ["prepare", "--root", str(repo), "--pack-repo", str(repo)]):
                with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        bundler.main(arguments)
                self.assertEqual(list(repo.iterdir()), [])

    def test_fetch_certificate_failures_are_actionable_and_fail_closed(self):
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped), tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "channel.toml.sha256"
                partial = dest.with_name(dest.name + ".part")
                partial.write_bytes(b"incomplete")
                error = bundler.ssl.SSLCertVerificationError(1, "self-signed certificate in chain")
                if wrapped:
                    error = bundler.urllib.error.URLError(error)
                url = "https://static.rust-lang.org/dist/channel-rust-1.99.1.toml.sha256"
                with patch.object(bundler.urllib.request, "urlopen", side_effect=error) as request:
                    with self.assertRaises(RuntimeError) as caught:
                        bundler.fetch(url, dest)
                message = str(caught.exception)
                self.assertIn(url, message)
                self.assertIn("update-ca-certificates", message)
                self.assertIn("Do not disable TLS verification", message)
                self.assertFalse(dest.exists())
                self.assertFalse(partial.exists())
                request.assert_called_once()
                self.assertNotIn("context", request.call_args.kwargs)

    def test_fetch_other_network_failure_includes_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = "https://example.invalid/asset"
            error = bundler.urllib.error.URLError("connection refused")
            with patch.object(bundler.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(RuntimeError) as caught:
                    bundler.fetch(url, Path(tmp) / "asset")
            self.assertIn(url, str(caught.exception))
            self.assertNotIn("CA certificate", str(caught.exception))

    def test_fetch_reuses_verified_download_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "asset"
            dest.write_bytes(b"verified")
            with patch.object(bundler.urllib.request, "urlopen") as request:
                self.assertEqual(bundler.fetch("https://example.invalid/asset", dest,
                    hashlib.sha256(b"verified").hexdigest()), dest)
                request.assert_not_called()

    def test_fetch_success_remains_atomic_and_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "asset"
            with patch.object(bundler.urllib.request, "urlopen", return_value=io.BytesIO(b"ok")):
                bundler.fetch("https://example.invalid/asset", dest, hashlib.sha256(b"ok").hexdigest())
            self.assertEqual(dest.read_bytes(), b"ok")
            self.assertFalse(dest.with_name("asset.part").exists())

    def test_glibc_confstr_with_incomplete_python_report(self):
        with patch.object(bundler.platform, "system", return_value="Linux"), \
             patch.object(bundler.platform, "machine", return_value="x86_64"), \
             patch.object(bundler.platform, "libc_ver", return_value=("libc", "")), \
             patch.object(bundler.os, "confstr", return_value="glibc 2.39"), \
             patch.object(bundler.subprocess, "run") as run:
            info = bundler.host_info()
            self.assertTrue(info["supported"])
            self.assertEqual(info["glibc_probe"], "os.confstr")
            self.assertEqual(info["glibc_version"], "2.39")
            run.assert_not_called()

    def test_glibc_getconf_fallback_and_architecture_normalization(self):
        result = subprocess.CompletedProcess([], 0, "glibc 2.36\n", "")
        with patch.object(bundler.platform, "system", return_value="Linux"), \
             patch.object(bundler.platform, "machine", return_value="AMD64"), \
             patch.object(bundler.platform, "libc_ver", return_value=("", "")), \
             patch.object(bundler.os, "confstr", side_effect=ValueError("unsupported")), \
             patch.object(bundler.shutil, "which", return_value="/usr/bin/getconf"), \
             patch.object(bundler.subprocess, "run", return_value=result) as run:
            info = bundler.host_info()
            self.assertTrue(info["supported"])
            self.assertEqual(info["glibc_probe"], "getconf")
            self.assertEqual(run.call_args.kwargs["timeout"], 5)
            self.assertEqual(run.call_args.args[0], ["/usr/bin/getconf", "GNU_LIBC_VERSION"])

    def test_python_glibc_fallback_when_system_queries_unavailable(self):
        with patch.object(bundler.platform, "system", return_value="Linux"), \
             patch.object(bundler.platform, "machine", return_value="x86_64"), \
             patch.object(bundler.platform, "libc_ver", return_value=("glibc", "2.31")), \
             patch.object(bundler.os, "confstr", return_value=None), \
             patch.object(bundler.shutil, "which", return_value=None):
            info = bundler.host_info()
            self.assertTrue(info["supported"])
            self.assertEqual(info["glibc_probe"], "platform.libc_ver")

    def test_unsupported_hosts_remain_rejected_with_diagnostics(self):
        cases = [("Darwin", "arm64", ("", "")),
                 ("Windows", "AMD64", ("", "")),
                 ("Linux", "aarch64", ("glibc", "2.36")),
                 ("Linux", "x86_64", ("musl", "1.2.5")),
                 ("Linux", "x86_64", ("", ""))]
        for system, machine, libc in cases:
            with self.subTest(system=system, machine=machine, libc=libc), \
                 patch.object(bundler.platform, "system", return_value=system), \
                 patch.object(bundler.platform, "machine", return_value=machine), \
                 patch.object(bundler.platform, "libc_ver", return_value=libc), \
                 patch.object(bundler.os, "confstr", side_effect=ValueError("unsupported")), \
                 patch.object(bundler.shutil, "which", return_value=None):
                info = bundler.host_info()
                self.assertFalse(info["supported"])
                with self.assertRaises(RuntimeError) as error:
                    bundler.require_host(info)
                self.assertIn(system, str(error.exception))
                self.assertIn(machine, str(error.exception))
                self.assertIn("--skip-build still runs", str(error.exception))

    def test_getconf_timeout_fails_closed(self):
        with patch.object(bundler.platform, "system", return_value="Linux"), \
             patch.object(bundler.platform, "machine", return_value="x86_64"), \
             patch.object(bundler.platform, "libc_ver", return_value=("", "")), \
             patch.object(bundler.os, "confstr", return_value=None), \
             patch.object(bundler.shutil, "which", return_value="/usr/bin/getconf"), \
             patch.object(bundler.subprocess, "run", side_effect=subprocess.TimeoutExpired("getconf", 5)):
            info = bundler.host_info()
            self.assertFalse(info["supported"])
            self.assertIn("getconf unavailable", info["probe_notes"][0])

    def test_diagnose_needs_no_supported_host_or_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "must-not-be-created"
            info = {"supported": False, "system": "Darwin", "machine": "arm64"}
            output = io.StringIO()
            with patch.object(bundler, "host_info", return_value=info), \
                 patch.object(bundler, "require_host") as require, \
                 patch.object(bundler, "fetch") as fetch, contextlib.redirect_stdout(output):
                bundler.main(["diagnose", "--root", str(root)])
                require.assert_not_called()
                fetch.assert_not_called()
            self.assertEqual(json.loads(output.getvalue()), info)
            self.assertFalse(root.exists())

    def test_unsupported_prepare_skip_build_stops_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "must-not-be-created"
            info = {"supported": False, "system": "Darwin", "machine": "arm64",
                    "python_libc": {"name": "", "version": ""}, "glibc_version": None}
            with patch.object(bundler, "host_info", return_value=info), \
                 patch.object(bundler, "fetch") as fetch:
                with self.assertRaisesRegex(RuntimeError, "architecture='arm64'"):
                    bundler.main(["prepare", "--skip-build", "--root", str(root)])
                fetch.assert_not_called()
            self.assertFalse(root.exists())

    def test_static_pins(self):
        self.assertEqual(bundler.HOST, "x86_64-unknown-linux-gnu")
        for url, digest in bundler.ASSETS.values():
            self.assertTrue(url.startswith("https://"))
            self.assertNotIn("/latest", url)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        for _, digest, _ in bundler.TOOLS.values():
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertIn("clippy-preview", bundler.RUST_COMPONENTS[bundler.STABLE])
        self.assertIn("llvm-tools-preview", bundler.RUST_COMPONENTS[bundler.NIGHTLY])

    def test_download_verification_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "asset"
            content = b"test artifact"
            digest = hashlib.sha256(content).hexdigest()
            with patch.object(bundler.urllib.request, "urlopen", return_value=io.BytesIO(content)) as request:
                bundler.fetch("https://example.invalid/pin", dest, digest)
                bundler.fetch("https://example.invalid/pin", dest, digest)
                self.assertEqual(request.call_count, 1)
            dest.write_bytes(b"changed")
            with self.assertRaises(RuntimeError):
                bundler.fetch("https://example.invalid/pin", dest, digest)

    def test_wrong_download_leaves_no_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "asset"
            with patch.object(bundler.urllib.request, "urlopen", return_value=io.BytesIO(b"bad")):
                with self.assertRaises(RuntimeError):
                    bundler.fetch("https://example.invalid/pin", dest, "0" * 64)
            self.assertFalse(dest.exists())
            self.assertFalse(dest.with_name("asset.part").exists())

    def archive(self, path, entries):
        with tarfile.open(path, "w") as tar:
            for name, kind, value in entries:
                item = tarfile.TarInfo(name)
                if kind == "file":
                    item.size = len(value)
                    item.mode = 0o755
                    tar.addfile(item, io.BytesIO(value))
                else:
                    item.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                    item.linkname = value
                    tar.addfile(item)

    def test_safe_archive_and_relative_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.archive(root / "a.tar", [("pkg/bin/tool", "file", b"ok"),
                ("pkg/bin/alias", "symlink", "tool"), ("pkg/bin/hard", "hardlink", "pkg/bin/tool")])
            bundler.unpack(root / "a.tar", root / "out")
            self.assertEqual((root / "out/pkg/bin/alias").read_bytes(), b"ok")
            self.assertEqual((root / "out/pkg/bin/hard").read_bytes(), b"ok")

    def test_archive_escape_duplicate_and_link_parent_rejected(self):
        cases = [[("../escape", "file", b"bad")], [("/escape", "file", b"bad")],
                 [("a", "symlink", "../../escape")],
                 [("a", "symlink", "real"), ("a/file", "file", b"bad")],
                 [("a", "file", b"one"), ("./a", "file", b"two")]]
        for entries in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.archive(root / "a.tar", entries)
                with self.assertRaises(RuntimeError):
                    bundler.unpack(root / "a.tar", root / "out")
                self.assertFalse((root / "escape").exists())

    def test_inventory_checks_files_permissions_links_and_extras(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(bundler, "ASSETS", {}), patch.object(bundler, "TOOLS", {}):
            root = Path(tmp)
            (root / "a").write_text("original")
            (root / "link").symlink_to("a")
            bundler.seal(root, False)
            self.assertFalse(bundler.verify(root)["cargo_tool_binaries_built"])
            (root / ".cache").mkdir()
            (root / ".cache/untracked").write_text("fine")
            bundler.verify(root)
            (root / "extra").write_text("unexpected")
            with self.assertRaises(RuntimeError): bundler.verify(root)
            (root / "extra").unlink()
            (root / "a").write_text("tampered")
            with self.assertRaises(RuntimeError): bundler.verify(root)
            (root / "a").write_text("original")
            (root / "a").chmod(0o777)
            with self.assertRaises(RuntimeError): bundler.verify(root)

    def test_rust_manifest_uses_exact_channel_and_required_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # All installer packages are mocked; no distribution downloads occur.
            data = {"pkg": {n: {"target": {bundler.HOST if n != "rust-src" else "*":
                    {"available": True, "xz_url": "https://example.invalid/" + n, "xz_hash": "a" * 64}}}
                    for n in bundler.RUST_COMPONENTS[bundler.STABLE]}}
            data["pkg"]["rustc"]["version"] = "1.99.1 (commit date)"
            manifest = b"mock manifest"
            calls = []
            def fetch(url, dest, checksum=None):
                calls.append((url, checksum))
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes((hashlib.sha256(manifest).hexdigest() + " channel.toml").encode() if url.endswith(".sha256") else manifest)
                return dest
            with patch.object(bundler, "fetch", side_effect=fetch), patch.object(bundler.tomllib, "loads", return_value=data):
                bundler.prepare_rust(root, bundler.STABLE)
            lock = json.loads((root / "downloads/1.99.1/components.lock.json").read_text())
            self.assertEqual(len(lock["components"]), 7)
            self.assertIn("channel-rust-1.99.1.toml", calls[0][0])

    def test_cargo_env_isolated_and_offline(self):
        env = bundler.cargo_env(Path("/bundle"), Path("/bundle/toolchain"), True)
        self.assertEqual(env["CARGO_NET_OFFLINE"], "true")
        self.assertEqual(env["CARGO_HOME"], "/bundle/.cache/cargo")
        self.assertEqual(env["RUSTC"], "/bundle/toolchain/bin/rustc")

    def test_tool_bundle_preserves_published_lock_and_isolates_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "bundle"
            root.mkdir()
            (root / "Cargo.toml").write_text("parent stays unchanged")
            archive = base / "tool.crate"
            lock = b"published tool lock"
            self.archive(archive, [("tool-1.0/Cargo.toml", "file", b"[package]"),
                                  ("tool-1.0/Cargo.lock", "file", lock),
                                  ("tool-1.0/.cargo/config.toml", "file", b"upstream config")])
            def fetch(url, dest, checksum=None):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(archive.read_bytes())
                return dest
            def run(args, *, cwd=None, env=None, stdout=None):
                self.assertFalse(Path(cwd).is_relative_to(root))
                self.assertEqual(env["CARGO_NET_OFFLINE"], "false")
                self.assertFalse((cwd / ".cargo/config.toml").exists())
                self.assertIn("--locked", args)
                (cwd / "vendor").mkdir()
                (cwd / "vendor/checksum-fixture").write_text("test")
                stdout.write('[source.crates-io]\nreplace-with = "vendored-sources"\n'
                             '[source.vendored-sources]\ndirectory = "vendor"\n')
            with patch.object(bundler, "fetch", side_effect=fetch), patch.object(bundler, "run", side_effect=run):
                bundler.prepare_tool(root, base / "compiler", "tool", "1.0", bundler.sha(archive))
            target = root / "tools/tool-1.0"
            self.assertEqual((target / "Cargo.lock").read_bytes(), lock)
            self.assertTrue((target / ".cargo/config.toml.upstream").exists())
            self.assertIn("offline = true", (target / ".cargo/config.toml").read_text())
            self.assertEqual((root / "Cargo.toml").read_text(), "parent stays unchanged")

    def test_tool_without_published_lock_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = base / "tool.crate"
            self.archive(archive, [("tool-1.0/Cargo.toml", "file", b"[package]")])
            with patch.object(bundler, "fetch", return_value=archive), patch.object(bundler, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "lacks Cargo.lock"):
                    bundler.prepare_tool(base / "root", base / "compiler", "tool", "1.0", "0" * 64)
                run.assert_not_called()

    def test_generated_env_scripts_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundler.write_env(root)
            for name in ["env.sh", "env-fuzz.sh"]:
                subprocess.run(["bash", "-n", root / name], check=True)
            text = (root / "env-fuzz.sh").read_text()
            self.assertIn(bundler.NIGHTLY, text)
            self.assertIn("LLVM_PROFDATA", text)


if __name__ == "__main__":
    unittest.main()
