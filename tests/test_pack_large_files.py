"""Network-free packing tests; small limits exercise multi-chunk behavior cheaply."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "pack-large-files.py"
spec = importlib.util.spec_from_file_location("packer", SCRIPT)
packer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packer)


class PackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        self.limit = patch.object(packer, "LIMIT", 1024)
        self.limit.start()
        self.addCleanup(self.limit.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def make_file(self, name="warehouse-assets/bin/tool with spaces", size=5000):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = os.urandom(size)
        path.write_bytes(data)
        path.chmod(0o751)
        return path, data

    def manifest(self):
        return json.loads((self.repo / packer.STORE / "manifest.json").read_text())

    def test_decimal_default_limit(self):
        self.assertEqual(self.limit.temp_original, 50_000_000)

    def test_roundtrip_chunks_modes_links_and_reruns(self):
        path, data = self.make_file()
        link = path.parent / "alias"
        link.symlink_to(path.name)
        packer.pack(self.repo, remove=True)
        self.assertFalse(path.exists())
        self.assertTrue(link.is_symlink())
        entry = self.manifest()["files"][path.relative_to(self.repo).as_posix()]
        self.assertGreater(len(entry["parts"]), 1)
        for part in entry["parts"]:
            chunk = self.repo / packer.STORE / part["path"]
            self.assertLessEqual(chunk.stat().st_size, 1024)
            self.assertEqual(packer.digest(chunk), part["sha256"])
        packer.pack(self.repo, remove=True)
        packer.restore_or_verify(self.repo)
        packer.restore_or_verify(self.repo, restore=True)
        packer.restore_or_verify(self.repo, restore=True)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(link.read_bytes(), data)
        self.assertEqual(path.stat().st_mode & 0o777, 0o751)
        packer.pack(self.repo, remove=True)
        self.assertFalse(path.exists())

    def test_dry_run_boundary_and_exclusions(self):
        self.make_file("small", size=1023)
        self.make_file("exact", size=1024)
        self.make_file("large", size=1025)
        self.make_file(".git/objects/large")
        self.make_file("nested/.git/config")
        (self.repo / "alias").symlink_to("large")
        (self.repo / "directory-alias").symlink_to(".git", target_is_directory=True)
        before = sorted(str(p) for p in self.repo.rglob("*"))
        packer.main(["pack", "--repo", str(self.repo), "--dry-run"])
        self.assertEqual([p.name for p in packer.scan(self.repo)], ["large"])
        self.assertEqual(before, sorted(str(p) for p in self.repo.rglob("*")))

    def test_default_pack_keeps_originals(self):
        path, data = self.make_file()
        packer.pack(self.repo)
        self.assertEqual(path.read_bytes(), data)
        packer.restore_or_verify(self.repo)

    def test_corrupt_chunk_never_removes_original(self):
        path, data = self.make_file()
        packer.pack(self.repo)
        entry = next(iter(self.manifest()["files"].values()))
        chunk = self.repo / packer.STORE / entry["parts"][0]["path"]
        chunk.write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            packer.pack(self.repo, remove=True)
        self.assertEqual(path.read_bytes(), data)

    def test_missing_chunk_never_publishes_partial_restore(self):
        path, _ = self.make_file()
        packer.pack(self.repo, remove=True)
        entry = next(iter(self.manifest()["files"].values()))
        (self.repo / packer.STORE / entry["parts"][-1]["path"]).unlink()
        with self.assertRaises(OSError):
            packer.restore_or_verify(self.repo, restore=True)
        self.assertFalse(path.exists())
        self.assertFalse(list(path.parent.glob(".warehouse-restore-*")))

    def test_existing_changed_original_is_not_overwritten_or_deleted(self):
        path, _ = self.make_file()
        packer.pack(self.repo)
        path.write_bytes(b"changed" * 1000)
        with self.assertRaises(ValueError):
            packer.restore_or_verify(self.repo, restore=True)
        with self.assertRaises(ValueError):
            packer.pack(self.repo, remove=True)
        self.assertEqual(path.read_bytes(), b"changed" * 1000)

    def test_compression_failure_retains_original(self):
        path, data = self.make_file()
        with patch.object(packer.ChunkWriter, "write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                packer.pack(self.repo, remove=True)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.manifest()["files"], {})

    def test_manifest_failure_retains_original(self):
        path, data = self.make_file()
        original_save = packer.save
        def fail_nonempty(store, manifest):
            if manifest["files"]:
                raise OSError("disk full")
            original_save(store, manifest)
        with patch.object(packer, "save", side_effect=fail_nonempty):
            with self.assertRaises(OSError):
                packer.pack(self.repo, remove=True)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.manifest()["files"], {})

    def test_untrusted_manifest_path_and_symlink_parent_rejected(self):
        path, _ = self.make_file("folder/tool")
        packer.pack(self.repo, remove=True)
        store = self.repo / packer.STORE
        original = self.manifest()
        changed = json.loads(json.dumps(original))
        changed["files"]["../escape"] = changed["files"].pop("folder/tool")
        packer.save(store, changed)
        with self.assertRaises(ValueError):
            packer.restore_or_verify(self.repo, restore=True)
        packer.save(store, original)
        path.parent.rmdir()
        path.parent.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaises(ValueError):
            packer.restore_or_verify(self.repo, restore=True)

    def test_store_symlink_and_unrelated_store_rejected(self):
        store = self.repo / packer.STORE
        store.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaises(ValueError):
            packer.pack(self.repo)
        store.unlink()
        store.mkdir()
        (store / "unrelated").write_text("keep")
        with self.assertRaises(ValueError):
            packer.pack(self.repo)
        self.assertEqual((store / "unrelated").read_text(), "keep")

    def test_lock_prevents_concurrent_mutation(self):
        with packer.locked_store(self.repo, create=True):
            with self.assertRaises(ValueError):
                packer.pack(self.repo)

    def test_bad_original_checksum_prevents_publication(self):
        path, _ = self.make_file()
        packer.pack(self.repo, remove=True)
        manifest = self.manifest()
        next(iter(manifest["files"].values()))["sha256"] = "0" * 64
        packer.save(self.repo / packer.STORE, manifest)
        with self.assertRaises(ValueError):
            packer.restore_or_verify(self.repo, restore=True)
        self.assertFalse(path.exists())

    def test_warehouse_seal_verifies_after_restoration(self):
        import types
        shell = (SCRIPT.parent / "vendor-assets.sh").read_text()
        source = shell.split("<<'PYTHON_ASSET_BUNDLER'\n", 1)[1].rsplit("\nPYTHON_ASSET_BUNDLER", 1)[0]
        bundler = types.ModuleType("bundler_for_roundtrip")
        exec(compile(source, "vendor-assets.sh", "exec"), bundler.__dict__)
        path, data = self.make_file()
        root = self.repo / "warehouse-assets"
        (root / "small.txt").write_text("unchanged")
        with patch.object(bundler, "ASSETS", {}), patch.object(bundler, "TOOLS", {}):
            bundler.seal(root, False)
            manifest_bytes = (root / "bundle-manifest.json").read_bytes()
            packer.pack(self.repo, remove=True)
            with self.assertRaises((RuntimeError, OSError)):
                bundler.verify(root)
            packer.restore_or_verify(self.repo, restore=True)
            bundler.verify(root)
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual((root / "bundle-manifest.json").read_bytes(), manifest_bytes)

    def test_chunk_symlink_and_manifest_temp_symlink_rejected(self):
        path, data = self.make_file()
        packer.pack(self.repo)
        store = self.repo / packer.STORE
        entry = next(iter(self.manifest()["files"].values()))
        chunk = store / entry["parts"][0]["path"]
        chunk.unlink()
        chunk.symlink_to(path)
        with self.assertRaises(ValueError):
            packer.pack(self.repo, remove=True)
        self.assertEqual(path.read_bytes(), data)
        (store / ".manifest.tmp").symlink_to(path)
        with self.assertRaises(ValueError):
            packer.save(store, self.manifest())
        self.assertEqual(path.read_bytes(), data)

    def test_cli_rejects_sealed_bundle_as_repository_root(self):
        (self.repo / ".warehouse-owner.json").write_text("{}")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            packer.main(["pack", "--repo", str(self.repo), "--remove-originals"])
        self.assertFalse((self.repo / packer.STORE).exists())

    def test_cli_help_runs_on_host(self):
        result = subprocess.run(["python3", SCRIPT, "--help"], text=True,
                                capture_output=True, check=True)
        self.assertIn("--remove-originals", result.stdout)


if __name__ == "__main__":
    unittest.main()
