#!/usr/bin/env python3
"""Real-child end-to-end regression for automatic seed-pool attachment."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import brainstorm_pool_builder as pool_core


SEEDSPACE = 1_785_793_904_896
SMALL_COUNT = 100_000
LARGE_COUNT = 2_000_000


def luajit_command() -> list[str]:
    command = shlex.split(os.environ.get("LUAJIT", "")) or ["luajit"]
    if os.name == "nt" and command[0].startswith("/") and shutil.which("cygpath"):
        command[0] = subprocess.check_output(
            ["cygpath", "-w", command[0]], text=True).strip()
    if not shutil.which(command[0]) and not Path(command[0]).is_file():
        raise RuntimeError("LuaJIT is required for the native attachment regression")
    return command


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, **kwargs)


def write_criteria(path: Path, count: int, include_legendary: bool = True,
                   tag_key: str = "tag_charm") -> None:
    lines = [
        "poolver 1",
        "threads 4",
        "start 0",
        f"count {count}",
        f"checkpoint {count}",
        "chunk 2048",
        "resume 0",
        "format binary",
        "tag_route collect",
        f"tag {tag_key} 1 small 1 small 1",
    ]
    if include_legendary:
        lines.extend([
            "legendary_routes full",
            "legendary j_perkeo 1 small 1 small 0 charm",
            "soul_depth 1",
        ])
    lines.extend(["end", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def attach(path: Path, role: str) -> None:
    info = pool_core.PoolInfo(str(path)).as_dict()
    marker = pool_core.attach_completed_pool(
        path.name, role, str(path.parent), info["catalog_hash"])
    if not marker["valid"] or marker["role"] != role:
        raise AssertionError(f"failed to attach {path.name} as {role}: {marker}")


def scenario_dir(base: Path, name: str, sources: list[tuple[Path, str, str]]) -> Path:
    directory = base / name
    directory.mkdir()
    for source, target_name, role in sources:
        target = directory / target_name
        shutil.copy2(source, target)
        attach(target, role)
    write_items(directory)
    return directory


def write_items(directory: Path) -> None:
    names = sorted(path.name for path in directory.glob("*.bspool.attached"))
    (directory / "items.txt").write_text("\n".join(names) + "\n", encoding="utf-8")


def replace_marker_field(marker: Path, field: str, value: str) -> None:
    text = marker.read_text(encoding="utf-8")
    changed, count = re.subn(
        rf"^{re.escape(field)}\s+\S+\s*$", f"{field} {value}", text,
        count=1, flags=re.MULTILINE)
    if count != 1:
        raise AssertionError(f"marker {marker} has no unique {field} field")
    marker.write_text(changed, encoding="utf-8")


def claim_full_coverage(path: Path) -> None:
    data = path.read_bytes()
    prefix = data[:1024].decode("ascii", "strict").rstrip("\0")
    match = re.search(r"^header_bytes (\d+)$", prefix, re.MULTILINE)
    if not match:
        raise AssertionError("expected a BSP3 pool with header_bytes")
    header_bytes = int(match.group(1))
    header = data[:header_bytes].rstrip(b"\0").decode("ascii", "strict")
    header, count = re.subn(
        r"^range_end \d+$", f"range_end {SEEDSPACE}", header,
        count=1, flags=re.MULTILINE)
    if count != 1:
        raise AssertionError("pool header has no unique range_end")
    encoded = header.encode("ascii")
    if len(encoded) > header_bytes:
        raise AssertionError("expanded authoritative fixture header overflowed")
    path.write_bytes(encoded.ljust(header_bytes, b"\0") + data[header_bytes:])


def make_invalid_directory(base: Path, small_pool: Path) -> Path:
    directory = base / "invalid-discovery"
    directory.mkdir()
    variants = ["missing", "corrupt", "changed", "stale-id", "stale-catalog",
                "stale-profile", "unreadable"]
    for index, variant in enumerate(variants, 1):
        target = directory / f"{index:02d}-{variant}.bspool"
        shutil.copy2(small_pool, target)
        attach(target, "accelerator")
        marker = Path(str(target) + ".attached")
        if variant == "missing":
            target.unlink()
        elif variant == "corrupt":
            with target.open("r+b") as handle:
                handle.write(b"X")
        elif variant == "changed":
            with target.open("ab") as handle:
                handle.write(b"changed")
        elif variant == "stale-id":
            replace_marker_field(marker, "pool_id", "0000000000000000")
        elif variant == "stale-catalog":
            replace_marker_field(marker, "catalog_hash", "0000000000000000")
        elif variant == "stale-profile":
            replace_marker_field(marker, "snapshot_id", "0000000000000000")
        elif variant == "unreadable":
            marker.write_text("truncated marker\n", encoding="utf-8")
    write_items(directory)
    return directory


def main() -> int:
    snapshot = Path(sys.argv[1] if len(sys.argv) > 1 else "native_search.cfg").resolve()
    template = snapshot.with_name("case15.cfg")
    pack_template = snapshot.with_name("case33.cfg")
    if not snapshot.is_file() or not template.is_file() or not pack_template.is_file():
        raise SystemExit(
            "usage: pool_attachment_native_e2e.py <fixture case1.cfg>; "
            "run tests/native_equivalence.sh first")

    windows = os.name == "nt"
    suffix = ".exe" if windows else ""
    binary = (ROOT / "native" / f"brainstorm_native_search{suffix}").resolve()
    scanner = (ROOT / "native" / f"brainstorm_seed_pool{suffix}").resolve()
    if not binary.is_file() or not scanner.is_file():
        build = ROOT / "native" / ("build_windows.sh" if windows else "build.sh")
        run(["sh", str(build)], cwd=ROOT)

    lua = luajit_command()
    with tempfile.TemporaryDirectory(prefix="brainstorm_attachment_native_") as raw_tmp:
        base = Path(raw_tmp).resolve()
        aligned = base / "snapshot.cfg"
        aligned.write_bytes(subprocess.check_output(
            lua + [str(ROOT / "tests" / "align_snapshot_prng.lua"), str(snapshot)],
            cwd=ROOT))

        pools = base / "source-pools"
        pools.mkdir()
        small_criteria = pools / "small.cfg"
        large_criteria = pools / "large.cfg"
        small_pool = pools / "small.bspool"
        large_pool = pools / "large.bspool"
        tag_criteria = pools / "tag.cfg"
        tag_pool = pools / "tag.bspool"
        incompatible_criteria = pools / "incompatible.cfg"
        incompatible_pool = pools / "incompatible.bspool"
        small_seeds = pools / "small.txt"
        write_criteria(small_criteria, SMALL_COUNT)
        write_criteria(large_criteria, LARGE_COUNT)
        write_criteria(tag_criteria, SMALL_COUNT, include_legendary=False)
        write_criteria(incompatible_criteria, SMALL_COUNT,
                       include_legendary=False, tag_key="tag_1")
        run([str(scanner), "scan", str(aligned), str(small_criteria), str(small_pool)])
        run([str(scanner), "scan", str(aligned), str(large_criteria), str(large_pool)])
        run([str(scanner), "scan", str(aligned), str(tag_criteria), str(tag_pool)])
        run([str(scanner), "scan", str(aligned), str(incompatible_criteria),
             str(incompatible_pool)])
        run([str(scanner), "export", str(small_pool), str(small_seeds)])

        small_records = pool_core.PoolInfo(str(small_pool)).as_dict()["records"]
        large_records = pool_core.PoolInfo(str(large_pool)).as_dict()["records"]
        if not (0 < small_records < large_records):
            raise AssertionError(
                f"fixture must produce nonempty ordered pools, got {small_records}/{large_records}")

        chain = scenario_dir(base, "chain", [
            (small_pool, "01-small.bspool", "accelerator"),
            (large_pool, "02-large.bspool", "accelerator"),
        ])
        hit = scenario_dir(base, "hit", [
            (large_pool, "01-large.bspool", "accelerator"),
        ])
        fallback = scenario_dir(base, "fallback", [
            (small_pool, "01-small.bspool", "accelerator"),
        ])
        layered_pack = scenario_dir(base, "layered-pack", [
            (tag_pool, "01-tag.bspool", "accelerator"),
        ])
        incompatible = scenario_dir(base, "incompatible", [
            (incompatible_pool, "01-incompatible.bspool", "accelerator"),
        ])
        manual = scenario_dir(base, "manual", [
            (small_pool, "01-small.bspool", "accelerator"),
            (large_pool, "02-large.bspool", "accelerator"),
        ])
        stale = scenario_dir(base, "stale-profile", [
            (small_pool, "01-small.bspool", "accelerator"),
        ])
        missing = scenario_dir(base, "missing-after-selection", [
            (small_pool, "01-small.bspool", "accelerator"),
        ])
        corrupt = scenario_dir(base, "corrupt-after-selection", [
            (small_pool, "01-small.bspool", "accelerator"),
        ])

        authoritative_source = base / "authoritative-source.bspool"
        shutil.copy2(small_pool, authoritative_source)
        claim_full_coverage(authoritative_source)
        authoritative = scenario_dir(base, "authoritative", [
            (authoritative_source, "01-authoritative.bspool", "authoritative"),
        ])
        authoritative_mutated = scenario_dir(base, "authoritative-mutated", [
            (authoritative_source, "01-authoritative.bspool", "authoritative"),
        ])
        authoritative_pool_mutated = scenario_dir(base, "authoritative-pool-mutated", [
            (authoritative_source, "01-authoritative.bspool", "authoritative"),
        ])
        invalid = make_invalid_directory(base, small_pool)

        scenarios = {
            "hit": (hit, template),
            "chain": (chain, template),
            "fallback": (fallback, template),
            "layered-pack": (layered_pack, pack_template),
            "incompatible": (incompatible, template),
            "authoritative": (authoritative, template),
            "authoritative-mutated": (authoritative_mutated, template),
            "authoritative-pool-mutated": (authoritative_pool_mutated, template),
            "manual": (manual, template),
            "invalid-discovery": (invalid, template),
            "stale-profile": (stale, template),
            "missing-after-selection": (missing, template),
            "corrupt-after-selection": (corrupt, template),
        }
        driver = ROOT / "tests" / "pool_attachment_native_driver.lua"
        for name, (directory, scenario_template) in scenarios.items():
            run(lua + [
                str(driver), str(ROOT / "Brainstorm_reroll.lua"), str(ROOT),
                str(directory), str(binary), str(scenario_template),
                str(directory / "items.txt"), str(small_seeds), name,
            ], cwd=ROOT)

    print("AUTOMATIC ATTACHMENT REAL NATIVE E2E: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
