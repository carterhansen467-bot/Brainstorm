#!/usr/bin/env python3
"""Regression checks for the Windows package split and safe updater inputs."""

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGER_PATH = ROOT / "release" / "package_windows.py"
SPEC = importlib.util.spec_from_file_location("brainstorm_windows_packager",
                                              PACKAGER_PATH)
PACKAGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGER)


def write(path, data=b"fixture"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class WindowsReleaseLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="brainstorm-win-release-")
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        for relative in PACKAGER.RUNTIME_FILES:
            write(self.repo / relative, (relative + "\n").encode())
        write(self.repo / "Assets" / "BrainstormLogo.jpg", b"logo")
        write(self.repo / "native" / "seed_pool.example.cfg", b"count 10\n")
        for filename in PACKAGER.WRAPPER_FILES + ("SEED-POOL-BUILDER.txt",):
            source = ROOT / "release" / "windows" / filename
            destination = self.repo / "release" / "windows" / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        # These are intentionally tempting files the allow-list must ignore.
        write(self.repo / "settings.lua", b"private settings")
        write(self.repo / "native_search.cfg", b"profile snapshot")
        write(self.repo / "seed_pools" / "huge.bspool", b"pool")
        write(self.repo / "seed_pools" / "huge.bspool.attached", b"attachment")
        write(self.repo / "native" / "stale.pdb", b"symbols")
        write(self.repo / "tools" / "developer-only.py", b"source")
        self.builder = self.base / "artifacts" / "Seed Pool Builder.exe"
        self.organizer = self.base / "artifacts" / "Seed Pool Organizer.exe"
        self.search = self.base / "artifacts" / "brainstorm_native_search.exe"
        self.scanner = self.base / "artifacts" / "brainstorm_seed_pool.exe"
        write(self.builder, b"builder exe")
        write(self.organizer, b"organizer exe")
        write(self.search, b"search exe")
        write(self.scanner, b"scanner exe")

    def tearDown(self):
        self.temp.cleanup()

    def assemble(self):
        return PACKAGER.assemble(
            self.repo, self.base / "out", self.builder, self.organizer, self.search,
            self.scanner, "win-test", "abc123")

    def test_runtime_and_builder_are_split_without_user_state(self):
        wrapper, archive = self.assemble()
        mod = wrapper / "Brainstorm"
        builder = mod / "Seed Pool Builder"
        self.assertTrue((mod / "native" / "brainstorm_native_search.exe").is_file())
        self.assertFalse((mod / "native" / "brainstorm_seed_pool.exe").exists())
        self.assertTrue((builder / "Seed Pool Builder.exe").is_file())
        self.assertTrue((builder / "Seed Pool Organizer.exe").is_file())
        self.assertTrue((builder / "brainstorm_seed_pool.exe").is_file())
        self.assertFalse((mod / "Seed Pool Builder.exe").exists())
        for relative in ("settings.lua", "native_search.cfg", "seed_pools",
                         "tools", "native/stale.pdb"):
            self.assertFalse((mod / relative).exists(), relative)
        self.assertTrue(archive.is_file())

    def test_manifest_covers_exact_payload_and_zip_has_one_wrapper(self):
        wrapper, archive = self.assemble()
        mod = wrapper / "Brainstorm"
        manifest = wrapper / "RELEASE-MANIFEST.sha256"
        entries = {}
        for line in manifest.read_text(encoding="utf-8").splitlines():
            digest, relative = line.split("  ", 1)
            entries[relative] = digest
        actual = {path.relative_to(mod).as_posix()
                  for path in mod.rglob("*") if path.is_file()}
        self.assertEqual(set(entries), actual)
        for relative, expected in entries.items():
            digest = hashlib.sha256((mod / relative).read_bytes()).hexdigest()
            self.assertEqual(expected, digest)
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
        self.assertTrue(names)
        self.assertTrue(all(name.startswith("Brainstorm-Windows/") for name in names))
        self.assertIn("Brainstorm-Windows/Install or Update Brainstorm.bat", names)

    def test_missing_binary_aborts_before_packaging(self):
        self.scanner.unlink()
        with self.assertRaises(FileNotFoundError):
            self.assemble()

    def test_updater_has_narrow_legacy_cleanup_and_state_guards(self):
        script = (ROOT / "release" / "windows" /
                  "install-or-update.ps1").read_text(encoding="utf-8")
        self.assertIn('Join-Path $Destination "Seed Pool Builder.exe"', script)
        self.assertIn('Join-Path $Destination "native\\brainstorm_seed_pool.exe"', script)
        self.assertIn("seed_pools/, settings.lua", script)
        self.assertIn("Release manifest is empty", script)
        self.assertIn("Release payload contains an unlisted file", script)
        self.assertIn("$PayloadFiles.Count -ne $Entries.Count", script)
        self.assertNotIn("Remove-Item -LiteralPath $Destination -Recurse", script)
        self.assertNotIn("robocopy", script.lower())

    def test_frozen_builder_finds_parent_mod_and_bundled_scanner(self):
        mod = self.base / "installed" / "Brainstorm-custom-name"
        app = mod / "Seed Pool Builder"
        write(mod / "Brainstorm_main.lua")
        write(mod / "manifest.json")
        scanner_name = "brainstorm_seed_pool.exe" if os.name == "nt" \
            else "brainstorm_seed_pool"
        write(app / scanner_name)
        fake_exe = app / "Seed Pool Builder.exe"
        write(fake_exe)
        probe = """
import runpy, sys
sys.frozen = True
sys.executable = {executable!r}
values = runpy.run_path({script!r}, run_name='builder_layout_probe')
print(values['MOD_DIR'])
print(values['BUILDER_DIR'])
print(values['POOL_BIN'])
""".format(executable=str(fake_exe),
           script=str(ROOT / "tools" / "brainstorm_pool_builder.py"))
        env = dict(os.environ)
        env.pop("BRAINSTORM_MOD_DIR", None)
        result = subprocess.run([sys.executable, "-c", probe], env=env,
                                check=True, text=True, capture_output=True)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[-3], str(mod))
        self.assertEqual(lines[-2], str(app))
        self.assertEqual(lines[-1], str(app / scanner_name))

    def test_frozen_organizer_finds_parent_pool_library(self):
        mod = self.base / "installed" / "Renamed-Brainstorm"
        app = mod / "Seed Pool Builder"
        write(mod / "Brainstorm_main.lua")
        write(mod / "manifest.json")
        fake_exe = app / "Seed Pool Organizer.exe"
        write(fake_exe)
        probe = """
import runpy, sys
sys.frozen = True
sys.executable = {executable!r}
values = runpy.run_path({script!r}, run_name='organizer_layout_probe')
print(values['MOD_DIR'])
print(values['APP_DIR'])
print(values['POOL_DIR'])
""".format(executable=str(fake_exe),
           script=str(ROOT / "tools" / "pool_organizer_web.py"))
        env = dict(os.environ)
        env.pop("BRAINSTORM_MOD_DIR", None)
        result = subprocess.run([sys.executable, "-c", probe], env=env,
                                check=True, text=True, capture_output=True)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[-3], str(mod))
        self.assertEqual(lines[-2], str(app))
        self.assertEqual(lines[-1], str(mod / "seed_pools"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
