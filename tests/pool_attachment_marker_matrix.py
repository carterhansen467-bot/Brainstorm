#!/usr/bin/env python3
"""Mutate every attachment marker contract field through both readers."""

from __future__ import annotations

import hashlib
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


def lua_command() -> list[str]:
    command = shlex.split(os.environ.get("LUAJIT", "")) or ["luajit"]
    if os.name == "nt" and command[0].startswith("/") and shutil.which("cygpath"):
        command[0] = subprocess.check_output(
            ["cygpath", "-w", command[0]], text=True).strip()
    if not shutil.which(command[0]) and not Path(command[0]).is_file():
        raise RuntimeError("LuaJIT is required for the attachment marker matrix")
    return command


def write_pool(path: Path) -> None:
    lines = [
        "BRAINSTORM_SEED_POOL 2",
        "modelver 6",
        "charset 123456789ABCDEFGHIJKLMNPQRSTUVWXYZ",
        f"seedspace {pool_core.SEEDSPACE}",
        "space natural",
        "range_start 0",
        "range_end 100000",
        "catalog_hash 1111111111111111",
        "criteria_hash 2222222222222222",
        "pool_id marker-matrix",
        "snapshot_id 3333333333333333",
        "tag_route collect",
        "tag tag_charm 1 small 1 small 1",
        "legendary_routes full",
        "legendary j_perkeo 1 small 1 small 0 charm",
        "soul_depth 1",
        "records 7",
        "complete 1",
        "coverage_complete 1",
        "end",
        "",
    ]
    path.write_bytes("\n".join(lines).encode("ascii").ljust(1024, b"\0") + b"records")


def replace_line(text: str, field: str, value: str) -> str:
    changed, count = re.subn(
        rf"^{re.escape(field)}(?:\s+.*)?$", f"{field} {value}", text,
        count=1, flags=re.MULTILINE)
    if count != 1:
        raise AssertionError(f"baseline marker lacks {field}")
    return changed


def signature_hash(text: str) -> str:
    schema = re.search(r"^signature_schema\s+(\S+)$", text, re.MULTILINE).group(1)
    predicates = re.findall(r"^predicate\s+(.*)$", text, re.MULTILINE)
    body = f"signature_schema {schema}\n" + "".join(
        f"predicate {predicate}\n" for predicate in predicates)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def rehash(text: str) -> str:
    return replace_line(text, "signature_hash", signature_hash(text))


def main() -> int:
    lua = lua_command()
    checker = ROOT / "tests" / "pool_attachment_marker_check.lua"
    reroll = ROOT / "Brainstorm_reroll.lua"
    with tempfile.TemporaryDirectory(prefix="brainstorm_attachment_marker_") as raw_tmp:
        directory = Path(raw_tmp).resolve()
        pool = directory / "matrix.bspool"
        write_pool(pool)
        attached = pool_core.attach_completed_pool(
            pool.name, "accelerator", str(directory), "1111111111111111")
        if not attached["valid"]:
            raise AssertionError("baseline marker was not valid")
        marker = Path(str(pool) + ".attached")
        baseline = marker.read_text(encoding="utf-8")
        stat = pool.stat()

        def check(expected: str) -> None:
            builder = pool_core.read_pool_attachment(
                str(pool), current_catalog_hash="1111111111111111")
            if bool(builder and builder["valid"]) != (expected == "valid"):
                raise AssertionError(
                    f"Builder reader disagreed with {expected}: {builder}")
            subprocess.run(lua + [str(checker), str(reroll), str(directory),
                           str(marker), str(pool), str(stat.st_size),
                           str(stat.st_mtime), expected], check=True, cwd=ROOT)

        check("valid")
        mutations: dict[str, str] = {
            "marker_schema": baseline.replace(
                "BRAINSTORM_POOL_ATTACHMENT 1", "BRAINSTORM_POOL_ATTACHMENT 2", 1),
            "enabled": replace_line(baseline, "enabled", "0"),
            "role": replace_line(baseline, "role", "authoritative"),
            "role_invalid": replace_line(baseline, "role", "invalid"),
            "pool_file": replace_line(baseline, "pool_file", "other.bspool"),
            "pool_id": replace_line(baseline, "pool_id", "0000000000000000"),
            "catalog_hash": replace_line(
                baseline, "catalog_hash", "0000000000000000"),
            "criteria_hash": replace_line(
                baseline, "criteria_hash", "0000000000000000"),
            "snapshot_id": replace_line(
                baseline, "snapshot_id", "0000000000000000"),
            "signature_schema": replace_line(baseline, "signature_schema", "2"),
            "signature_hash": replace_line(baseline, "signature_hash", "0" * 64),
            "file_size": replace_line(baseline, "file_size", str(stat.st_size + 1)),
            "file_mtime_ns": replace_line(
                baseline, "file_mtime_ns", str(stat.st_mtime_ns + 2_000_000_000)),
            "file_mtime_malformed": replace_line(baseline, "file_mtime_ns", "missing"),
            "truncated": baseline.replace("end\n", "", 1),
            "duplicate": baseline.replace(
                "end\n", "pool_id duplicate\nend\n", 1),
        }
        predicates = re.findall(r"^predicate\s+.*$", baseline, re.MULTILINE)
        if len(predicates) != 2:
            raise AssertionError("matrix fixture should have two predicates")
        reordered = baseline
        reordered = reordered.replace(predicates[0], "__FIRST__", 1)
        reordered = reordered.replace(predicates[1], predicates[0], 1)
        reordered = reordered.replace("__FIRST__", predicates[1], 1)
        mutations["predicate_order"] = rehash(reordered)
        mutations["predicate_value"] = rehash(
            baseline.replace("j_perkeo", "j_caino", 1))

        for name, value in mutations.items():
            marker.write_text(value, encoding="utf-8")
            try:
                check("invalid")
            except Exception as exc:
                raise AssertionError(f"marker mutation {name} was not rejected") from exc
        marker.write_text(baseline, encoding="utf-8")
        check("valid")

    print("POOL ATTACHMENT MARKER MUTATION MATRIX: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
