#!/usr/bin/env python3
"""Regression checks for bounded variable-length .bspool header readers.

Run from any directory:

    python3 tests/pool_variable_header.py

Set LUAJIT=/path/to/luajit when LuaJIT is not on PATH.
"""

import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"
HEADER_V2 = 1024
HEADER_V3 = 8192
HEADER_V4 = 16 * 1024
HEADER_MAX = 256 * 1024

sys.path.insert(0, str(TOOLS))
import brainstorm_pool_builder as core  # noqa: E402


def load_web_module():
    spec = importlib.util.spec_from_file_location(
        "brainstorm_pool_builder_web_test", TOOLS / "pool_builder_web.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def padded_header(lines, size, late_lines=()):
    """Place late_lines beyond byte 1024 while keeping header_bytes early."""
    raw = ("\n".join(lines) + "\n").encode("ascii")
    if late_lines:
        late_at = 1200
        assert len(raw) < late_at
        raw += b"#" * (late_at - len(raw) - 1) + b"\n"
        raw += ("\n".join(late_lines) + "\n").encode("ascii")
    assert len(raw) <= size
    return raw.ljust(size, b"\0")


def make_fixtures(directory):
    paths = {}

    # Schemas 1 and 2 remain fixed at 1 KiB. Text beyond that boundary must
    # never be mistaken for header metadata.
    v1_prefix = (
        b"BRAINSTORM_SEED_POOL 1\n"
        b"header_bytes 1024\n"
        b"records 1\n"
    )
    v1_prefix += b"#" * (HEADER_V2 - len(v1_prefix) - 1) + b"\n"
    paths["v1"] = directory / "schema-1-fixed.bspool"
    paths["v1"].write_bytes(
        v1_prefix + b"records 99\nlineage_id must-not-be-read\nend\n"
    )

    # A second records line just beyond the valid fixed schema-2 header would
    # overwrite the first if a compatibility reader accidentally read farther.
    v2_prefix = (
        b"BRAINSTORM_SEED_POOL 2\n"
        b"header_bytes 1024\n"
        b"records 2\n"
    )
    v2_prefix += b"#" * (HEADER_V2 - len(v2_prefix) - 1) + b"\n"
    paths["v2"] = directory / "schema-2-fixed.bspool"
    paths["v2"].write_bytes(
        v2_prefix + b"records 99\nlineage_id must-not-be-read\nend\n"
    )

    string_fields = {
        "family_id": "family-a",
        "segment_id": "segment-b",
        "stage_hash": "00abc123",
        "lineage_id": "lineage-c",
        "derivation_id": "derive-d",
        "snapshot_id": "snapshot-e",
        "membership_digest": "membership-f",
        "metadata_digest": "metadata-g",
        "parent_snapshot_id": "parent-snapshot-h",
        "parent_segment_id": "parent-segment-i",
    }
    numeric_fields = {
        "scan_cursor": 123456,
        "input_cursor": 2345,
        "parent_records": 345,
        "parent_data_bytes": 4567,
        "input_record_start": 12,
        "input_record_end": 300,
        "shard_index": 2,
        "shard_total": 8,
    }
    late = [f"{key} {value}" for key, value in string_fields.items()]
    late += [f"{key} {value}" for key, value in numeric_fields.items()]
    late += [
        "composite_schema 1",
        "composite_operation union",
        "composite_inputs 2",
        "composite_metadata_complete 1",
        "composite_branch 1111111111111111 2222222222222222 3333333333333333 1 - -",
        "composite_branch 4444444444444444 5555555555555555 6666666666666666 1 - -",
        "composite_operand 7777777777777777 3333333333333333 2222222222222222 1 3 - first",
        "composite_operand 8888888888888888 6666666666666666 5555555555555555 1 4 - second",
    ]
    late += ["parent_coverage_complete 1", "end"]
    paths["v3"] = directory / "schema-3-valid.bspool"
    paths["v3"].write_bytes(
        padded_header(
            [
                "BRAINSTORM_SEED_POOL 3",
                f"header_bytes {HEADER_V3}",
                "records 7",
                "complete 1",
                "coverage_complete 0",
                "space natural",
                "encoding delta-varint-events-v1",
                "range_start 0",
                "range_end 1000",
            ],
            HEADER_V3,
            late,
        )
    )

    paths["v4"] = directory / "schema-4-valid.bspool"
    paths["v4"].write_bytes(
        padded_header(
            [
                "BRAINSTORM_SEED_POOL 4",
                f"header_bytes {HEADER_V4}",
                "records 11",
                "complete 1",
                "coverage_complete 1",
                "space natural",
                "encoding adaptive-events-v1",
                "range_start 0",
                "range_end 2000",
            ],
            HEADER_V4,
            late,
        )
    )

    # Future schemas may remain structurally inspectable, but every native
    # eligibility surface must fail closed until that schema is implemented.
    paths["unknown_schema"] = directory / "schema-5-unknown.bspool"
    paths["unknown_schema"].write_bytes(
        padded_header(
            [
                "BRAINSTORM_SEED_POOL 5",
                "header_bytes 1024",
                "records 13",
                "complete 1",
                "coverage_complete 1",
                "space natural",
                "encoding future-events-v2",
                "end",
            ],
            HEADER_V2,
        )
    )

    invalid = {
        "missing": ("BRAINSTORM_SEED_POOL 3\nrecords 1\nend\n", HEADER_V3),
        "small": (
            "BRAINSTORM_SEED_POOL 3\nheader_bytes 512\nrecords 1\nend\n",
            HEADER_V2,
        ),
        "oversized": (
            f"BRAINSTORM_SEED_POOL 3\nheader_bytes {HEADER_MAX + 1}\nrecords 1\nend\n",
            HEADER_V2,
        ),
        "truncated": (
            f"BRAINSTORM_SEED_POOL 3\nheader_bytes {HEADER_V3}\nrecords 1\nend\n",
            HEADER_V3 // 2,
        ),
    }
    for name, (text, actual_size) in invalid.items():
        path = directory / ("schema-3-%s.bspool" % name)
        path.write_bytes(text.encode("ascii").ljust(actual_size, b"\0"))
        paths["v3_" + name] = path

    invalid_v4 = {
        "missing": (
            "BRAINSTORM_SEED_POOL 4\n"
            "encoding adaptive-events-v1\nrecords 1\nend\n",
            HEADER_V4,
        ),
        "small": (
            "BRAINSTORM_SEED_POOL 4\nheader_bytes 512\n"
            "encoding adaptive-events-v1\nrecords 1\nend\n",
            HEADER_V2,
        ),
        "oversized": (
            f"BRAINSTORM_SEED_POOL 4\nheader_bytes {HEADER_MAX + 1}\n"
            "encoding adaptive-events-v1\nrecords 1\nend\n",
            HEADER_V2,
        ),
        "truncated": (
            f"BRAINSTORM_SEED_POOL 4\nheader_bytes {HEADER_V4}\n"
            "encoding adaptive-events-v1\nrecords 1\nend\n",
            HEADER_V4 // 2,
        ),
        "wrong_encoding": (
            f"BRAINSTORM_SEED_POOL 4\nheader_bytes {HEADER_V4}\n"
            "encoding delta-varint-events-v1\nrecords 1\nend\n",
            HEADER_V4,
        ),
    }
    for name, (text, actual_size) in invalid_v4.items():
        path = directory / ("schema-4-%s.bspool" % name)
        path.write_bytes(text.encode("ascii").ljust(actual_size, b"\0"))
        paths["v4_" + name] = path

    invalid_names = [
        "v3_missing", "v3_small", "v3_oversized", "v3_truncated",
        "v4_missing", "v4_small", "v4_oversized", "v4_truncated",
        "v4_wrong_encoding",
    ]
    return paths, string_fields, numeric_fields, invalid_names


def check_python(paths, string_fields, numeric_fields, invalid_names):
    v1 = core.read_pool_header(paths["v1"])
    assert v1.get("records") == "1", v1
    assert "lineage_id" not in v1, v1

    v2 = core.read_pool_header(paths["v2"])
    assert v2.get("records") == "2", v2
    assert "lineage_id" not in v2, v2

    v3 = core.read_pool_header(paths["v3"])
    assert v3.get("records") == "7", v3
    assert v3.get("lineage_id") == string_fields["lineage_id"], v3
    v4 = core.read_pool_header(paths["v4"])
    assert v4.get("records") == "11", v4
    assert v4.get("encoding") == "adaptive-events-v1", v4
    assert v4.get("lineage_id") == string_fields["lineage_id"], v4
    unknown = core.read_pool_header(paths["unknown_schema"])
    assert unknown.get("schema") == "5", unknown
    assert "schema 5 is not supported" in core.pool_native_incompatibility(unknown)
    for name in invalid_names:
        assert core.read_pool_header(paths[name]) == {}, (name, core.read_pool_header(paths[name]))

    web = load_web_module()
    prior_pool_dir = web.core.POOL_DIR
    try:
        web.core.POOL_DIR = str(paths["v3"].parent)
        listed = {pool["name"]: pool for pool in web.list_pools()}
    finally:
        web.core.POOL_DIR = prior_pool_dir

    # Round-trip through JSON so the assertions cover the API wire types, not
    # only the intermediate Python dictionary.
    item = json.loads(json.dumps(listed[paths["v3"].name]))
    assert item["schema"] == 3
    assert item["header_bytes"] == HEADER_V3
    assert item["coverage_complete"] is False
    assert item["parent_coverage_complete"] is True
    assert item["composite"] is True
    assert item["composite_branch_count"] == 2
    assert item["composite_operand_count"] == 2
    for key, expected in string_fields.items():
        assert item[key] == expected and isinstance(item[key], str), (key, item[key])
    for key, expected in numeric_fields.items():
        assert item[key] == expected and isinstance(item[key], int), (key, item[key])
    bsp4 = json.loads(json.dumps(listed[paths["v4"].name]))
    assert bsp4["schema"] == 4
    assert bsp4["header_bytes"] == HEADER_V4
    assert bsp4["encoding"] == "adaptive-events-v1"
    assert bsp4["metadata_capable"] is True
    assert bsp4["native_compatible"] is True
    future = json.loads(json.dumps(listed[paths["unknown_schema"].name]))
    assert future["schema"] == 5
    assert future["native_compatible"] is False
    assert "schema 5 is not supported" in future["native_incompatibility"]


def lua_command():
    configured = os.environ.get("LUAJIT", "").strip()
    command = shlex.split(configured) if configured else ["luajit"]
    if os.name == "nt" and command and command[0].startswith("/") \
            and shutil.which("cygpath"):
        command[0] = subprocess.check_output(
            ["cygpath", "-w", command[0]], text=True).strip()
    if not command or (shutil.which(command[0]) is None
                       and not Path(command[0]).is_file()):
        raise RuntimeError("LuaJIT not found; set LUAJIT=/path/to/luajit")
    return command


def check_lua(paths, invalid_names, directory):
    harness = directory / "variable_header_reader.lua"
    harness.write_text(
        r'''
local reroll, v1Path, v2Path, v3Path, v4Path, unknownSchemaPath =
	arg[1], arg[2], arg[3], arg[4], arg[5], arg[6]

local function boundedRead(path, size)
	assert(type(size) == "number" and size >= 0 and size <= 256 * 1024,
		"production pool-header reader requested an unbounded read")
	local f = assert(io.open(path, "rb"))
	local data = f:read(size)
	f:close()
	return data
end

package.loaded.brainstorm_nativefs = {
	read = boundedRead, write = function() end, getInfo = function() return nil end,
}
package.loaded.lovely = { mod_dir = "" }
G = { FUNCS = {} }
Brainstorm = {
	SETTINGS = { autoreroll = {}, multiAnteSearch = {} }, AUTOREROLL = {},
}
assert(loadfile(reroll))()

local v1 = assert(Brainstorm.readPoolHeader(v1Path))
assert(v1.schema == 1 and v1.records == 1 and v1.lineage_id == nil,
	"schema 1 did not remain fixed at 1024 bytes")
local v2 = assert(Brainstorm.readPoolHeader(v2Path))
assert(v2.schema == 2 and v2.records == 2 and v2.lineage_id == nil,
	"schema 2 did not remain fixed at 1024 bytes")
local v3 = assert(Brainstorm.readPoolHeader(v3Path))
assert(v3.schema == 3 and v3.records == 7 and v3.header_bytes == 8192)
assert(v3.metadata_capable and v3.native_compatible)
assert(v3.lineage_id == "lineage-c" and v3.scan_cursor == 123456)
assert(v3.parent_coverage_complete == 1)
assert(v3.composite_schema == 1 and v3.composite_operation == "union")
assert(#v3.composite_branches == 2 and #v3.composite_operands == 2)
local v4 = assert(Brainstorm.readPoolHeader(v4Path))
assert(v4.schema == 4 and v4.records == 11 and v4.header_bytes == 16384)
assert(v4.encoding == "adaptive-events-v1")
assert(v4.metadata_capable and v4.native_compatible)
assert(Brainstorm.poolNativeCompatible(v4))
assert(v4.lineage_id == "lineage-c" and v4.scan_cursor == 123456)
local unknownSchema = assert(Brainstorm.readPoolHeader(unknownSchemaPath))
assert(unknownSchema.schema == 5 and not unknownSchema.native_compatible)
assert(not Brainstorm.poolNativeCompatible(unknownSchema),
	"unknown pool schema crossed the production native compatibility gate")

for index = 7, #arg do
	assert(Brainstorm.readPoolHeader(arg[index]) == nil,
		"accepted invalid extended header " .. tostring(arg[index]))
end
''',
        encoding="utf-8",
    )
    command = lua_command()
    subprocess.run(
        command
        + [
            str(harness),
            str(REPO / "Brainstorm_reroll.lua"),
            str(paths["v1"]),
            str(paths["v2"]),
            str(paths["v3"]),
            str(paths["v4"]),
            str(paths["unknown_schema"]),
        ] + [str(paths[name]) for name in invalid_names],
        cwd=REPO,
        check=True,
    )

    # The independent pool oracle has its own bounded header reader. Empty
    # seed/snapshot fixtures isolate that reader without exercising RNG logic.
    snapshot = directory / "empty-snapshot.cfg"
    seeds = directory / "empty-seeds.txt"
    snapshot.write_text("", encoding="ascii")
    seeds.write_text("", encoding="ascii")
    oracle_base = command + [
        str(REPO / "tests" / "pool_lua_oracle.lua"),
        str(REPO / "Brainstorm_reroll.lua"),
        str(snapshot),
    ]
    for name in ("v1", "v2", "v3", "v4"):
        subprocess.run(
            oracle_base + [str(paths[name]), str(seeds)], cwd=REPO, check=True
        )
    for name in invalid_names:
        result = subprocess.run(
            oracle_base + [str(paths[name]), str(seeds)],
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        assert result.returncode != 0, "oracle accepted invalid %s header" % name


def main():
    with tempfile.TemporaryDirectory(prefix="brainstorm_variable_header_") as temp:
        directory = Path(temp)
        paths, string_fields, numeric_fields, invalid_names = make_fixtures(directory)
        check_python(paths, string_fields, numeric_fields, invalid_names)
        check_lua(paths, invalid_names, directory)
    print("PASS: bounded schema-1/2/3/4 Python, web, production-Lua, and oracle readers")


if __name__ == "__main__":
    main()
