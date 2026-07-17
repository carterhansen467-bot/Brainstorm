#!/usr/bin/env python3
"""Assemble the tested Windows release without copying runtime/user state.

The source tree is developer-oriented.  The installed release is deliberately
smaller and separates files that Balatro loads from the standalone Seed Pool
Builder.  This script uses explicit allow-lists so a local settings file,
snapshot, scan checkpoint, pool, debug log, or stale binary cannot leak into a
release zip.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import zipfile
from pathlib import Path


RUNTIME_FILES = (
    "Brainstorm.lua",
    "Brainstorm_UI.lua",
    "Brainstorm_keyhandler.lua",
    "Brainstorm_main.lua",
    "Brainstorm_reroll.lua",
    "nativefs.lua",
    "win_spawn.lua",
    "lovely.toml",
    "manifest.json",
    "README.md",
    "LICENSE",
    "Seed Pool Builder.bat",
    "Seed Pool Organizer.bat",
)

WRAPPER_FILES = (
    "Install or Update Brainstorm.bat",
    "install-or-update.ps1",
    "README-WINDOWS.txt",
)


def require_file(path: Path, description: str) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"missing {description}: {path}")
    return path


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_release_manifest(wrapper: Path, mod_dir: Path) -> Path:
    manifest = wrapper / "RELEASE-MANIFEST.sha256"
    lines = []
    for path in sorted(p for p in mod_dir.rglob("*") if p.is_file()):
        relative = path.relative_to(mod_dir).as_posix()
        lines.append(f"{sha256(path)}  {relative}\n")
    with manifest.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(lines))
    return manifest


def write_zip(wrapper: Path, output: Path) -> None:
    """Write a deterministic archive rooted at Brainstorm-Windows/."""
    if output.exists():
        output.unlink()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(p for p in wrapper.rglob("*") if p.is_file()):
            relative = Path(wrapper.name) / path.relative_to(wrapper)
            info = zipfile.ZipInfo(relative.as_posix(), (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            zf.writestr(info, path.read_bytes())


def assemble(repo: Path, out_dir: Path, builder_exe: Path,
             organizer_exe: Path, search_exe: Path, pool_exe: Path, version: str,
             commit: str) -> tuple[Path, Path]:
    repo = repo.resolve()
    out_dir = out_dir.resolve()
    wrapper = out_dir / "Brainstorm-Windows"
    archive = out_dir / "brainstorm-windows-full.zip"

    require_file(builder_exe, "standalone Seed Pool Builder executable")
    require_file(organizer_exe, "standalone Seed Pool Organizer executable")
    require_file(search_exe, "in-game native search executable")
    require_file(pool_exe, "seed-pool scanner executable")
    for relative in RUNTIME_FILES:
        require_file(repo / relative, relative)
    require_file(repo / "native" / "seed_pool.example.cfg",
                 "seed-pool example criteria")
    release_assets = repo / "release" / "windows"
    for filename in WRAPPER_FILES:
        require_file(release_assets / filename, filename)
    require_file(release_assets / "SEED-POOL-BUILDER.txt",
                 "Seed Pool Builder release notes")

    # This target is always a generated staging directory, never an installed
    # mod.  Keeping it under the caller-selected out_dir makes cleanup scoped.
    if wrapper.exists():
        shutil.rmtree(wrapper)
    wrapper.mkdir(parents=True)
    mod_dir = wrapper / "Brainstorm"

    for relative in RUNTIME_FILES:
        copy_file(repo / relative, mod_dir / relative)
    assets = repo / "Assets"
    if assets.is_dir():
        for source in sorted(p for p in assets.rglob("*") if p.is_file()):
            copy_file(source, mod_dir / source.relative_to(repo))

    # Only the helper used by the running game remains under native/.  The
    # standalone scanner lives beside its UI in one clearly owned folder.
    copy_file(search_exe, mod_dir / "native" / "brainstorm_native_search.exe")
    builder_dir = mod_dir / "Seed Pool Builder"
    copy_file(builder_exe, builder_dir / "Seed Pool Builder.exe")
    copy_file(organizer_exe, builder_dir / "Seed Pool Organizer.exe")
    copy_file(pool_exe, builder_dir / "brainstorm_seed_pool.exe")
    copy_file(repo / "native" / "seed_pool.example.cfg",
              builder_dir / "seed_pool.example.cfg")
    copy_file(release_assets / "SEED-POOL-BUILDER.txt",
              builder_dir / "README.txt")

    for filename in WRAPPER_FILES:
        copy_file(release_assets / filename, wrapper / filename)
    with (wrapper / "VERSION.txt").open(
            "w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            f"version={version or 'untagged'}\ncommit={commit or 'unknown'}\n")
    write_release_manifest(wrapper, mod_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_zip(wrapper, archive)
    return wrapper, archive


def parse_args(argv: list[str]) -> argparse.Namespace:
    here = Path(__file__).resolve()
    default_repo = here.parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=default_repo)
    parser.add_argument("--out-dir", type=Path, default=default_repo / "out")
    parser.add_argument("--builder-exe", type=Path,
                        default=default_repo / "dist" / "Seed Pool Builder.exe")
    parser.add_argument("--organizer-exe", type=Path,
                        default=default_repo / "dist" / "Seed Pool Organizer.exe")
    parser.add_argument("--search-exe", type=Path,
                        default=default_repo / "native" / "brainstorm_native_search.exe")
    parser.add_argument("--pool-exe", type=Path,
                        default=default_repo / "native" / "brainstorm_seed_pool.exe")
    parser.add_argument("--version", default="")
    parser.add_argument("--commit", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        wrapper, archive = assemble(
            args.repo, args.out_dir, args.builder_exe.resolve(),
            args.organizer_exe.resolve(),
            args.search_exe.resolve(), args.pool_exe.resolve(),
            args.version, args.commit)
    except (FileNotFoundError, OSError) as error:
        print(f"Windows release packaging failed: {error}", file=sys.stderr)
        return 1
    print(f"assembled: {wrapper}")
    print(f"archive:   {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
