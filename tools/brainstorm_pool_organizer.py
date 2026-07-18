#!/usr/bin/env python3
"""Inspect and splice Brainstorm BSP3 seed pools without replaying game RNG.

The organizer treats the header's ``records`` and ``data_bytes`` fields as the
last committed checkpoint.  This means a paused pool can be inspected and
split safely while a later, uncommitted tail is ignored.  Schema-3 occurrence
metadata is copied with each selected seed; schema-2 pools remain inspectable,
but cannot be split by position because they predate occurrence metadata.

Typical workflow::

    python3 tools/brainstorm_pool_organizer.py inspect seed_pools/input.bspool
    python3 tools/brainstorm_pool_organizer.py split seed_pools/input.bspool out
    python3 tools/brainstorm_pool_organizer.py combine output.bspool \
        perkeo.bspool negative-tag.bspool --operation union

If a seed belongs to more than one exact category, ``split`` writes a JSON
plan and stops.  Copy the plan's source snapshot id and choose exactly one of
the listed categories for every ambiguous seed in a choices file, then rerun::

    python3 tools/brainstorm_pool_organizer.py split input.bspool out \
        --choices choices.json

Choices file format::

    {
      "source_snapshot_id": "0123456789abcdef",
      "choices": {"ABCD1234": "legendary:j_perkeo:A3:small:shop:o1:none"}
    }

Only the Python standard library is required.
"""

from __future__ import print_function

import argparse
import base64
import collections
import heapq
import json
import os
import re
import struct
import sys
import tempfile
from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import quote

try:
    # Keep header discovery identical to the standalone builder, especially
    # the bounded schema-3 header_bytes second read.
    from brainstorm_pool_builder import read_pool_header_text
except ImportError:  # Imported as tools.brainstorm_pool_organizer in tests.
    from tools.brainstorm_pool_builder import read_pool_header_text


HEADER_PREFIX_BYTES = 1024
HEADER_EVENTS_BYTES = 8192
HEADER_MAX_BYTES = 256 * 1024
BLOCK_MAX_RECORDS = 8192
EVENT_WRITE_RECORDS = 1024
BLOCK2_HEADER_BYTES = 32
BLOCK3_HEADER_BYTES = 48
INDEX2_ENTRY_BYTES = 24
INDEX3_ENTRY_BYTES = 32
FOOTER2_BYTES = 40
FOOTER3_BYTES = 80
MAX_METADATA_BYTES = 16 * 1024 * 1024

FNV64_OFFSET = 1469598103934665603
FNV64_PRIME = 1099511628211
FNV32_OFFSET = 2166136261
FNV32_PRIME = 16777619
MASK64 = (1 << 64) - 1

NATURAL_CHARSET = "123456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
SETTABLE_CHARSET = "123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
TOTAL_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
NATURAL_SEEDSPACE = 1785793904896
SETTABLE_SEEDSPACE = 2318107019760
TOTAL_SEEDSPACE = 2901713047668

KIND_NAMES = {1: "tag", 2: "legendary", 3: "voucher"}
PHASE_NAMES = {0: "boss", 1: "small", 2: "big"}
SOURCE_NAMES = {0: "none", 1: "shop", 2: "charm", 3: "ethereal"}
FLAG_NAMES = ((1, "negative"), (2, "charm-required"), (4, "purchased"))

# BSP3 deliberately permits unknown length-delimited metadata descriptors so
# newer organizer features can coexist with older native readers.  Composite
# pools use one such descriptor to record the source-filter branch(es) that
# admitted each seed.  Native scanners that predate this feature safely skip
# it; the organizer understands and preserves it.
PROVENANCE_DESCRIPTOR_KIND = 0x80
PROVENANCE_DESCRIPTOR_BYTES = 9
OPERAND_DESCRIPTOR_KIND = 0x81
OPERAND_DESCRIPTOR_BYTES = 9
COMPOSITE_SCHEMA = 1
COMPOSITE_OPERATIONS = ("union", "intersection", "difference")
COMPOSITE_MAX_INPUTS = 64
COMPOSITE_HEADER_SIZES = (HEADER_EVENTS_BYTES, 16 * 1024, 32 * 1024,
                          64 * 1024, 128 * 1024, HEADER_MAX_BYTES)
COMPOSITE_HEADER_SPARE_BYTES = 4 * 1024
SORT_CACHE_BYTES = 64 * 1024 * 1024

CRITERIA_DIRECTIVES = {
    "tag_route", "tag", "route_tag", "legendary", "route_legendary",
    "soul_depth", "voucher", "route_voucher", "voucher_exclude",
    "route_voucher_exclude",
}


class PoolError(ValueError):
    """Raised when a pool cannot be trusted or organized safely."""


def _header_token(value: str) -> str:
    """Encode arbitrary short header text as one whitespace-free token."""
    raw = str(value or "").encode("utf-8")
    if not raw:
        return "-"
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_header_token(value: str) -> str:
    if value == "-":
        return ""
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise PoolError("composite header contains an invalid text token")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        return raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise PoolError("composite header contains an invalid text token")


def provenance_descriptor(branch_id: int) -> bytes:
    if branch_id < 0 or branch_id > MASK64:
        raise PoolError("composite branch id is outside uint64")
    return bytes((PROVENANCE_DESCRIPTOR_KIND,)) + struct.pack(">Q", branch_id)


def provenance_branch_id(raw: bytes) -> Optional[int]:
    if (len(raw) == PROVENANCE_DESCRIPTOR_BYTES
            and raw[0] == PROVENANCE_DESCRIPTOR_KIND):
        return struct.unpack(">Q", raw[1:])[0]
    return None


def operand_descriptor(operand_id: int) -> bytes:
    if operand_id < 0 or operand_id > MASK64:
        raise PoolError("composite operand id is outside uint64")
    return bytes((OPERAND_DESCRIPTOR_KIND,)) + struct.pack(">Q", operand_id)


def operand_id_from_descriptor(raw: bytes) -> Optional[int]:
    if len(raw) == OPERAND_DESCRIPTOR_BYTES and raw[0] == OPERAND_DESCRIPTOR_KIND:
        return struct.unpack(">Q", raw[1:])[0]
    return None


def fnv64(data: bytes, value: int = FNV64_OFFSET) -> int:
    for byte in data:
        value = ((value ^ byte) * FNV64_PRIME) & MASK64
    return value


def fnv32(data: bytes, value: int = FNV32_OFFSET) -> int:
    for byte in data:
        value = ((value ^ byte) * FNV32_PRIME) & 0xFFFFFFFF
    return value


def crc64(data: bytes, value: int = 0) -> int:
    """CRC64-ECMA-182, matching the native schema-3 implementation."""
    for byte in data:
        value ^= byte << 56
        for _ in range(8):
            value = ((value << 1) ^ 0x42F0E1EBA9EA3693) & MASK64 \
                if value & (1 << 63) else (value << 1) & MASK64
    return value


def hash_fields(kind: str, a: int, b: int, c: int, d: int) -> int:
    text = "%s:%016x:%016x:%016x:%016x" % (kind, a, b, c, d)
    return fnv64(text.encode("ascii"))


def encode_varint(value: int) -> bytes:
    if value < 0 or value > MASK64:
        raise PoolError("varint value is outside uint64")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def decode_varint(payload: bytes, at: int) -> Tuple[int, int]:
    start = at
    value = 0
    shift = 0
    while at < len(payload) and shift <= 63:
        byte = payload[at]
        at += 1
        if shift == 63 and byte & 0x7E:
            raise PoolError("varint overflows uint64")
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            minimal = 1
            probe = value
            while probe >= 128:
                probe >>= 7
                minimal += 1
            if at - start != minimal:
                raise PoolError("non-canonical varint")
            return value, at
        shift += 7
    raise PoolError("truncated or oversized varint")


def rank_to_seed(rank: int, charset: str) -> str:
    if charset == NATURAL_CHARSET:
        chars = []
        value = rank
        for _ in range(8):
            chars.append(charset[value % 34])
            value //= 34
        return "".join(chars)
    if charset == SETTABLE_CHARSET:
        base = 35
    elif charset == TOTAL_CHARSET:
        base = 36
    else:
        raise PoolError("unknown seed charset")
    block = base
    length = 1
    value = rank
    while length < 8 and value >= block:
        value -= block
        block *= base
        length += 1
    chars = []
    for _ in range(length):
        chars.append(charset[value % base])
        value //= base
    return "".join(chars)


def _flag_text(flags: int) -> str:
    names = [name for bit, name in FLAG_NAMES if flags & bit]
    unknown = flags & ~sum(bit for bit, _ in FLAG_NAMES)
    if unknown:
        names.append("0x%02x" % unknown)
    return "+".join(names) if names else "none"


def _phase_text(phase: int) -> str:
    return PHASE_NAMES.get(phase, "phase-%d" % phase)


def _source_text(source: int) -> str:
    return SOURCE_NAMES.get(source, "source-%d" % source)


@dataclass(frozen=True)
class Occurrence:
    raw: bytes
    kind: Optional[int]
    key: Optional[str]
    ante: Optional[int]
    phase: Optional[int]
    source: Optional[int]
    ordinal: Optional[int]
    flags: Optional[int]

    @classmethod
    def decode(cls, raw: bytes) -> "Occurrence":
        # Unknown length-delimited descriptors are retained verbatim.  That is
        # the forward-compatible behavior promised by the BSP3 contract.
        if len(raw) < 2 or raw[0] not in KIND_NAMES:
            return cls(raw, None, None, None, None, None, None, None)
        key_len = raw[1]
        if not key_len or len(raw) != key_len + 7:
            raise PoolError("known occurrence descriptor has an invalid length")
        key_raw = raw[2:2 + key_len]
        try:
            key = key_raw.decode("ascii")
        except UnicodeDecodeError:
            raise PoolError("occurrence key is not ASCII")
        if any(ch.isspace() or ord(ch) < 33 or ord(ch) > 126 for ch in key):
            raise PoolError("occurrence key contains unsafe characters")
        ante, phase, source, ordinal, flags = raw[2 + key_len:7 + key_len]
        if not ante:
            raise PoolError("occurrence Ante must be positive")
        return cls(raw, raw[0], key, ante, phase, source, ordinal, flags)

    @property
    def known(self) -> bool:
        return self.kind is not None

    @property
    def provenance_id(self) -> Optional[int]:
        return provenance_branch_id(self.raw)

    @property
    def is_provenance(self) -> bool:
        return self.provenance_id is not None

    @property
    def operand_id(self) -> Optional[int]:
        return operand_id_from_descriptor(self.raw)

    @property
    def is_operand(self) -> bool:
        return self.operand_id is not None

    @property
    def category_id(self) -> Optional[str]:
        if not self.known:
            return None
        return "%s:%s:A%d:%s:%s:o%d:%s" % (
            KIND_NAMES[self.kind], quote(self.key, safe="_.-"), self.ante,
            _phase_text(self.phase), _source_text(self.source), self.ordinal,
            _flag_text(self.flags),
        )

    @property
    def label(self) -> str:
        if not self.known:
            return "Unknown descriptor %s" % self.raw.hex()
        extra = []
        if self.source:
            extra.append(_source_text(self.source))
        if self.ordinal:
            noun = "visit" if self.kind == 3 else "occurrence"
            extra.append("%s %d" % (noun, self.ordinal))
        if self.flags:
            extra.append(_flag_text(self.flags))
        suffix = " (%s)" % ", ".join(extra) if extra else ""
        return "%s %s - Ante %d %s%s" % (
            KIND_NAMES[self.kind].title(), self.key, self.ante,
            _phase_text(self.phase).title(), suffix,
        )

    def as_dict(self) -> Dict[str, object]:
        if self.is_provenance:
            return {
                "known": True,
                "kind": "provenance",
                "branch_id": "%016x" % self.provenance_id,
            }
        if self.is_operand:
            return {
                "known": True,
                "kind": "operand",
                "operand_id": "%016x" % self.operand_id,
            }
        if not self.known:
            return {"known": False, "raw_hex": self.raw.hex()}
        return {
            "known": True,
            "category_id": self.category_id,
            "label": self.label,
            "kind": KIND_NAMES[self.kind],
            "key": self.key,
            "ante": self.ante,
            "phase": _phase_text(self.phase),
            "source": _source_text(self.source),
            "ordinal": self.ordinal,
            "flags": _flag_text(self.flags),
        }


@dataclass(frozen=True)
class Record:
    rank: int
    occurrences: Tuple[Occurrence, ...]


@dataclass(frozen=True)
class CompositeBranch:
    """One original source/filter branch retained by a composite pool."""

    branch_id: int
    criteria_hash: int
    snapshot_id: int
    coverage_complete: bool
    pool_id: str
    label: str
    criteria: Tuple[str, ...]

    @property
    def token(self) -> str:
        return "%016x" % self.branch_id

    def as_dict(self) -> Dict[str, object]:
        return {
            "branch_id": self.token,
            "criteria_hash": "%016x" % self.criteria_hash,
            "snapshot_id": "%016x" % self.snapshot_id,
            "coverage_complete": self.coverage_complete,
            "pool_id": self.pool_id,
            "label": self.label,
            "criteria": list(self.criteria),
        }


@dataclass(frozen=True)
class CompositeOperand:
    """One exact input snapshot participating in the current set operation."""

    operand_id: int
    snapshot_id: int
    criteria_hash: int
    coverage_complete: bool
    records: int
    pool_id: str
    label: str

    @property
    def token(self) -> str:
        return "%016x" % self.operand_id

    def as_dict(self) -> Dict[str, object]:
        return {
            "operand_id": self.token,
            "snapshot_id": "%016x" % self.snapshot_id,
            "criteria_hash": "%016x" % self.criteria_hash,
            "coverage_complete": self.coverage_complete,
            "records": self.records,
            "pool_id": self.pool_id,
            "label": self.label,
        }


def _operand_expression(operand_id: int) -> Dict[str, object]:
    return {"operand": "%016x" % operand_id}


def _validate_expression(value, declared: set, depth: int = 0,
                         budget: Optional[List[int]] = None):
    """Validate and canonicalize a bounded composite set-expression tree."""
    if budget is None:
        budget = [8192]
    budget[0] -= 1
    if budget[0] < 0 or depth > 64:
        raise PoolError("composite expression is too large or deeply nested")
    if not isinstance(value, dict):
        raise PoolError("composite expression node must be an object")
    if set(value) == {"operand"}:
        token = value["operand"]
        if (not isinstance(token, str)
                or not re.fullmatch(r"[0-9a-fA-F]{16}", token)):
            raise PoolError("composite expression has an invalid operand id")
        operand_id = int(token, 16)
        if operand_id not in declared:
            raise PoolError("composite expression names an undeclared operand")
        return _operand_expression(operand_id)
    if set(value) != {"op", "inputs"}:
        raise PoolError("composite expression node has unknown fields")
    operation = value["op"]
    inputs = value["inputs"]
    if operation not in COMPOSITE_OPERATIONS or not isinstance(inputs, list) \
            or len(inputs) < 2 or len(inputs) > COMPOSITE_MAX_INPUTS:
        raise PoolError("composite expression operation/inputs are invalid")
    return {
        "op": operation,
        "inputs": [_validate_expression(item, declared, depth + 1, budget)
                   for item in inputs],
    }


def expression_matches(value: Dict[str, object], operands: set) -> bool:
    token = value.get("operand")
    if token is not None:
        return int(token, 16) in operands
    operation = value["op"]
    matches = [expression_matches(item, operands) for item in value["inputs"]]
    if operation == "union":
        return any(matches)
    if operation == "intersection":
        return all(matches)
    return matches[0] and not any(matches[1:])


def expression_operand_ids(value: Dict[str, object]) -> List[int]:
    """Return every operand leaf, retaining duplicates for validation."""
    token = value.get("operand")
    if token is not None:
        return [int(token, 16)]
    result = []
    for item in value["inputs"]:
        result.extend(expression_operand_ids(item))
    return result


def expression_text(value: Dict[str, object], labels=None) -> str:
    """Compact human-readable expression used in reports and the local UI."""
    token = value.get("operand")
    if token is not None:
        return str((labels or {}).get(token, token[:8]))
    operation = value["op"]
    joiner = " OR " if operation == "union" else " AND "
    if operation == "difference":
        first = expression_text(value["inputs"][0], labels)
        rest = " OR ".join(expression_text(item, labels)
                           for item in value["inputs"][1:])
        return "(%s MINUS (%s))" % (first, rest)
    return "(" + joiner.join(expression_text(item, labels)
                              for item in value["inputs"]) + ")"


@dataclass(frozen=True)
class Block:
    offset: int
    first_record: int
    count: int
    rank_bytes: int
    metadata_bytes: int
    associations: int
    first_rank: int
    last_rank: int
    header_bytes: int

    @property
    def payload_bytes(self) -> int:
        return self.rank_bytes + self.metadata_bytes


class PoolHeader:
    def __init__(self, text: str):
        self.text = text
        self.lines = []  # type: List[Tuple[str, str, str]]
        self.values = collections.defaultdict(list)  # type: Dict[str, List[str]]
        saw_end = False
        for raw in text.splitlines():
            parts = raw.split(None, 1)
            if not parts:
                continue
            key = parts[0]
            value = parts[1].strip() if len(parts) == 2 else ""
            self.lines.append((key, value, raw))
            self.values[key].append(value)
            if key == "end":
                saw_end = True
                break
        if not saw_end:
            raise PoolError("pool header has no end marker")

    def one(self, key: str, required: bool = True, default: str = "") -> str:
        values = self.values.get(key, [])
        if not values:
            if required:
                raise PoolError("pool header is missing %s" % key)
            return default
        if len(values) != 1:
            raise PoolError("pool header repeats %s" % key)
        return values[0]

    def integer(self, key: str, base: int = 10, required: bool = True,
                default: int = 0) -> int:
        value = self.one(key, required, str(default))
        try:
            parsed = int(value, base)
        except ValueError:
            raise PoolError("pool header %s is not a valid integer" % key)
        if parsed < 0 or parsed > MASK64:
            raise PoolError("pool header %s is outside uint64" % key)
        return parsed


class BSPoolReader:
    """Verified view of exactly one committed BSP2/BSP3 snapshot."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        text = read_pool_header_text(self.path)
        if not text:
            raise PoolError("cannot read a bounded Brainstorm pool header")
        self.header = PoolHeader(text)
        magic = self.header.one("BRAINSTORM_SEED_POOL")
        try:
            self.schema = int(magic)
        except ValueError:
            raise PoolError("invalid Brainstorm pool schema")
        if self.schema not in (2, 3):
            raise PoolError("organizer supports committed BSP2/BSP3 pools (got BSP%d)" % self.schema)
        self.header_bytes = self.header.integer("header_bytes")
        expected_header = HEADER_EVENTS_BYTES if self.schema == 3 else HEADER_PREFIX_BYTES
        if self.header_bytes != expected_header:
            # The format permits future bounded BSP3 headers. Output preserves
            # their size, while BSP2 remains the fixed historical 1 KiB.
            if self.schema != 3 or not (HEADER_PREFIX_BYTES <= self.header_bytes <= HEADER_MAX_BYTES):
                raise PoolError("pool header_bytes is invalid")
        self.encoding = self.header.one("encoding")
        expected_encoding = "delta-varint-events-v1" if self.schema == 3 \
            else "delta-varint-blocks-v1"
        if self.encoding != expected_encoding:
            raise PoolError("BSP%d pool has incompatible encoding %s" % (self.schema, self.encoding))
        self.modelver = self.header.integer("modelver")
        if self.modelver > 0x7FFFFFFF:
            raise PoolError("pool modelver is outside the supported integer range")
        # These fingerprints are part of the minimum native pool contract,
        # even for read-only inspection.
        self.header.integer("catalog_hash", 16)
        self.header.integer("criteria_hash", 16)
        self.charset = self.header.one("charset")
        if self.charset == NATURAL_CHARSET:
            expected_space = NATURAL_SEEDSPACE
            self.space_name = "natural"
            self.space_index = 0
        elif self.charset == SETTABLE_CHARSET:
            expected_space = SETTABLE_SEEDSPACE
            self.space_name = "settable"
            self.space_index = 2
        elif self.charset == TOTAL_CHARSET:
            expected_space = TOTAL_SEEDSPACE
            self.space_name = "total"
            self.space_index = 1
        else:
            raise PoolError("pool charset is unsupported")
        self.seedspace = self.header.integer("seedspace")
        if self.seedspace != expected_space:
            raise PoolError("pool seedspace does not match its charset")
        self.range_start = self.header.integer("range_start")
        self.range_end = self.header.integer("range_end")
        self.records = self.header.integer("records")
        self.data_bytes = self.header.integer("data_bytes")
        self.complete = self.header.integer("complete")
        self.coverage_complete = self.header.integer(
            "coverage_complete", required=False, default=self.complete)
        if self.complete not in (0, 1) or self.coverage_complete not in (0, 1):
            raise PoolError("complete flags must be 0 or 1")
        if self.coverage_complete and not self.complete:
            raise PoolError("unfinished pool cannot claim complete coverage")
        if not (0 <= self.range_start < self.range_end <= self.seedspace):
            raise PoolError("pool range is invalid")
        if self.records > self.range_end - self.range_start:
            raise PoolError("pool record count exceeds its range")
        self.composite_schema = 0
        self.composite_operation = ""
        self.composite_expression = None
        self.composite_operands = {}  # type: Dict[int, CompositeOperand]
        self.composite_branches = self._parse_composite_header()
        self.file_bytes = os.path.getsize(self.path)
        self.data_end = self.header_bytes + self.data_bytes
        if self.file_bytes < self.data_end:
            raise PoolError("pool is shorter than its committed data boundary")
        self.blocks = []  # type: List[Block]
        self.membership_digest = FNV64_OFFSET
        self.metadata_digest = FNV64_OFFSET if self.schema == 3 else 0
        self._scan_committed_blocks()
        self._validate_header_digests()
        if self.complete:
            self._validate_final_index()

    @property
    def metadata_capable(self) -> bool:
        return self.schema == 3

    @property
    def occurrence_metadata_complete(self) -> bool:
        if self.schema != 3:
            return False
        if not self.is_composite:
            return True
        value = self.header.integer(
            "composite_metadata_complete", required=False, default=1)
        if value not in (0, 1):
            raise PoolError("composite_metadata_complete must be 0 or 1")
        return bool(value)

    @property
    def pool_id(self) -> str:
        return self.header.one("pool_id", required=False, default="-")

    @property
    def family_id(self) -> int:
        return self.header.integer("family_id", 16, required=False, default=0)

    @property
    def segment_id(self) -> int:
        return self.header.integer("segment_id", 16, required=False, default=0)

    @property
    def lineage_id(self) -> int:
        return self.header.integer("lineage_id", 16, required=False, default=0)

    @property
    def criteria_hash(self) -> int:
        return self.header.integer("criteria_hash", 16)

    @property
    def catalog_hash(self) -> int:
        return self.header.integer("catalog_hash", 16)

    @property
    def snapshot_id(self) -> int:
        computed = hash_fields("snapshot", self.segment_id, self.records,
                               self.data_bytes, self.membership_digest)
        declared = self.header.integer("snapshot_id", 16, required=False, default=0)
        if declared and self.segment_id and declared != computed:
            raise PoolError("pool snapshot_id does not match its committed checkpoint")
        return declared or computed

    @property
    def snapshot_token(self) -> str:
        return "%016x" % self.snapshot_id

    def seed(self, rank: int) -> str:
        return rank_to_seed(rank, self.charset)

    @property
    def is_composite(self) -> bool:
        return bool(self.composite_schema)

    def _parse_composite_header(self) -> Dict[int, CompositeBranch]:
        raw_schema = self.header.one(
            "composite_schema", required=False, default="")
        has_composite_lines = any(
            key.startswith("composite_") for key in self.header.values)
        if not raw_schema:
            if has_composite_lines:
                raise PoolError("composite pool metadata is missing composite_schema")
            return {}
        try:
            schema = int(raw_schema)
        except ValueError:
            raise PoolError("composite_schema is not an integer")
        if schema != COMPOSITE_SCHEMA:
            raise PoolError("composite pool schema %d is unsupported" % schema)
        if self.schema != 3:
            raise PoolError("composite provenance requires a BSP3 pool")
        operation = self.header.one("composite_operation")
        if operation not in COMPOSITE_OPERATIONS:
            raise PoolError("composite pool has an unknown set operation")
        if self.header.one("composite_route_policy") != "provenance-only":
            raise PoolError("composite pool has an unknown route policy")
        metadata_complete = self.header.integer("composite_metadata_complete")
        if metadata_complete not in (0, 1):
            raise PoolError("composite_metadata_complete must be 0 or 1")
        raw_branches = self.header.values.get("composite_branch", [])
        if not raw_branches or len(raw_branches) > 4096:
            raise PoolError("composite pool needs between 1 and 4096 branches")
        partial = {}
        for value in raw_branches:
            parts = value.split()
            if len(parts) != 6:
                raise PoolError("composite_branch has the wrong number of fields")
            branch_text, criteria_text, snapshot_text, coverage_text, pool_text, label_text = parts
            if (not re.fullmatch(r"[0-9a-fA-F]{16}", branch_text)
                    or not re.fullmatch(r"[0-9a-fA-F]{16}", criteria_text)
                    or not re.fullmatch(r"[0-9a-fA-F]{16}", snapshot_text)
                    or coverage_text not in ("0", "1")):
                raise PoolError("composite_branch identity fields are malformed")
            branch_id = int(branch_text, 16)
            if branch_id in partial:
                raise PoolError("composite pool repeats branch %016x" % branch_id)
            partial[branch_id] = (
                int(criteria_text, 16), int(snapshot_text, 16),
                coverage_text == "1", _decode_header_token(pool_text),
                _decode_header_token(label_text),
            )
        criteria = collections.defaultdict(list)
        for value in self.header.values.get("composite_criterion", []):
            parts = value.split()
            if len(parts) != 2 or not re.fullmatch(r"[0-9a-fA-F]{16}", parts[0]):
                raise PoolError("composite_criterion is malformed")
            branch_id = int(parts[0], 16)
            if branch_id not in partial:
                raise PoolError("composite criterion names an undeclared branch")
            raw = _decode_header_token(parts[1])
            if (not raw or any(ord(ch) < 32 or ord(ch) > 126 for ch in raw)
                    or raw.split(None, 1)[0] not in CRITERIA_DIRECTIVES):
                raise PoolError("composite criterion is not a safe pool directive")
            criteria[branch_id].append(raw)
        branches = {}
        for branch_id, values in partial.items():
            criteria_hash, snapshot_id, coverage, pool_id, label = values
            branches[branch_id] = CompositeBranch(
                branch_id, criteria_hash, snapshot_id, coverage, pool_id,
                label, tuple(criteria[branch_id]))
        raw_operands = self.header.values.get("composite_operand", [])
        if len(raw_operands) < 2 or len(raw_operands) > COMPOSITE_MAX_INPUTS:
            raise PoolError("composite pool needs between 2 and %d operands" %
                            COMPOSITE_MAX_INPUTS)
        declared_inputs = self.header.integer("composite_inputs")
        if declared_inputs != len(raw_operands):
            raise PoolError("composite_inputs disagrees with its operand definitions")
        operands = {}
        for value in raw_operands:
            parts = value.split()
            if len(parts) != 7:
                raise PoolError("composite_operand has the wrong number of fields")
            operand_text, snapshot_text, criteria_text, coverage_text, records_text, \
                pool_text, label_text = parts
            if (not re.fullmatch(r"[0-9a-fA-F]{16}", operand_text)
                    or not re.fullmatch(r"[0-9a-fA-F]{16}", snapshot_text)
                    or not re.fullmatch(r"[0-9a-fA-F]{16}", criteria_text)
                    or coverage_text not in ("0", "1")
                    or not re.fullmatch(r"[0-9]+", records_text)):
                raise PoolError("composite_operand identity fields are malformed")
            operand_id = int(operand_text, 16)
            records = int(records_text)
            if operand_id in operands or records > MASK64:
                raise PoolError("composite pool repeats or overflows an operand")
            operands[operand_id] = CompositeOperand(
                operand_id, int(snapshot_text, 16), int(criteria_text, 16),
                coverage_text == "1", records,
                _decode_header_token(pool_text),
                _decode_header_token(label_text))
        if not self.complete:
            raise PoolError("composite pools must be atomically completed snapshots")
        if self.coverage_complete and (not all(
                operand.coverage_complete for operand in operands.values())
                or not all(branch.coverage_complete
                           for branch in branches.values())):
            raise PoolError("composite coverage exceeds one of its source snapshots")
        encoded_expression = self.header.one("composite_expression")
        try:
            raw_expression = json.loads(_decode_header_token(encoded_expression))
        except (TypeError, ValueError):
            raise PoolError("composite expression is not valid JSON")
        self.composite_expression = _validate_expression(
            raw_expression, set(operands))
        expression_operands = expression_operand_ids(self.composite_expression)
        if (self.composite_expression.get("op") != operation
                or len(expression_operands) != len(operands)
                or set(expression_operands) != set(operands)):
            raise PoolError("composite expression disagrees with its operation/operands")
        self.composite_operands = operands
        self.composite_schema = schema
        self.composite_operation = operation
        return branches

    def _scan_committed_blocks(self) -> None:
        total_records = 0
        offset = self.header_bytes
        with open(self.path, "rb") as handle:
            while offset < self.data_end:
                if self.schema == 3:
                    header_bytes = handle_read(handle, offset, BLOCK3_HEADER_BYTES)
                    fields = struct.unpack("<4sBBBBIIIIQQQ", header_bytes)
                    magic, size, z1, z2, z3, count, rank_bytes, meta_bytes, \
                        associations, first, last, checksum = fields
                    if magic != b"BSP3" or size != BLOCK3_HEADER_BYTES or z1 or z2 or z3:
                        raise PoolError("malformed committed BSP3 block at byte %d" % offset)
                    if not meta_bytes or meta_bytes > MAX_METADATA_BYTES:
                        raise PoolError("BSP3 block metadata size is invalid")
                    block_header_bytes = BLOCK3_HEADER_BYTES
                else:
                    header_bytes = handle_read(handle, offset, BLOCK2_HEADER_BYTES)
                    magic, count, rank_bytes, checksum, first, last = struct.unpack(
                        "<4sIIIQQ", header_bytes)
                    if magic != b"BSP2":
                        raise PoolError("malformed committed BSP2 block at byte %d" % offset)
                    meta_bytes = 0
                    associations = 0
                    block_header_bytes = BLOCK2_HEADER_BYTES
                if not count or count > BLOCK_MAX_RECORDS:
                    raise PoolError("pool block record count is invalid")
                if rank_bytes > (count - 1) * 6:
                    raise PoolError("pool rank payload is too large")
                payload_bytes = rank_bytes + meta_bytes
                if offset + block_header_bytes + payload_bytes > self.data_end:
                    raise PoolError("committed pool boundary cuts through a block")
                payload = handle_read(handle, offset + block_header_bytes, payload_bytes)
                if self.schema == 3:
                    calculated = crc64(header_bytes[4:40])
                    calculated = crc64(payload, calculated)
                    if calculated != checksum:
                        raise PoolError("BSP3 block checksum differs at byte %d" % offset)
                    decoded_metadata = self._decode_metadata(
                        payload[rank_bytes:], count, associations)
                    if self.is_composite:
                        self._validate_composite_metadata(decoded_metadata)
                    self.metadata_digest = fnv64(payload[rank_bytes:], self.metadata_digest)
                elif fnv32(payload) != checksum:
                    raise PoolError("BSP2 block checksum differs at byte %d" % offset)
                ranks = self._decode_ranks(payload[:rank_bytes], count, first, last)
                for rank in ranks:
                    if rank < self.range_start or rank >= self.range_end:
                        raise PoolError("pool contains a rank outside its declared range")
                self.membership_digest = fnv64(header_bytes, self.membership_digest)
                self.membership_digest = fnv64(payload, self.membership_digest)
                self.blocks.append(Block(
                    offset, total_records, count, rank_bytes, meta_bytes,
                    associations, first, last, block_header_bytes,
                ))
                total_records += count
                offset += block_header_bytes + payload_bytes
        if offset != self.data_end or total_records != self.records:
            raise PoolError("committed blocks do not match records/data_bytes")
        if bool(self.records) != bool(self.blocks):
            raise PoolError("pool block index is empty or inconsistent")

    def _validate_composite_metadata(
            self, per_record: Sequence[Tuple[Occurrence, ...]]) -> None:
        declared_branches = set(self.composite_branches)
        declared_operands = set(self.composite_operands)
        for items in per_record:
            branches = {item.provenance_id for item in items
                        if item.is_provenance}
            operands = {item.operand_id for item in items if item.is_operand}
            if not branches or not operands:
                raise PoolError("composite record is missing branch or operand provenance")
            if branches - declared_branches:
                raise PoolError("composite record names an undeclared source branch")
            if operands - declared_operands:
                raise PoolError("composite record names an undeclared set operand")
            if not expression_matches(self.composite_expression, operands):
                raise PoolError("composite record provenance does not satisfy its set expression")

    def _validate_header_digests(self) -> None:
        declared_membership = self.header.integer(
            "membership_digest", 16, required=False, default=0)
        if declared_membership and declared_membership != self.membership_digest:
            raise PoolError("membership_digest differs from committed pool bytes")
        if self.schema == 3:
            declared_metadata = self.header.integer(
                "metadata_digest", 16, required=False, default=0)
            if declared_metadata and declared_metadata != self.metadata_digest:
                raise PoolError("metadata_digest differs from committed event metadata")

    def _validate_final_index(self) -> None:
        entry_bytes = INDEX3_ENTRY_BYTES if self.schema == 3 else INDEX2_ENTRY_BYTES
        footer_bytes = FOOTER3_BYTES if self.schema == 3 else FOOTER2_BYTES
        expected_size = self.data_end + len(self.blocks) * entry_bytes + footer_bytes
        if self.file_bytes != expected_size:
            raise PoolError("complete pool has trailing or missing index bytes")
        with open(self.path, "rb") as handle:
            footer = handle_read(handle, self.file_bytes - footer_bytes, footer_bytes)
            magic = b"BSPIDX3\n" if self.schema == 3 else b"BSPIDX2\n"
            if footer[:8] != magic:
                raise PoolError("complete pool index footer is missing")
            index_offset, blocks, records, data_bytes = struct.unpack_from("<QQQQ", footer, 8)
            if (index_offset != self.data_end or blocks != len(self.blocks)
                    or records != self.records or data_bytes != self.data_bytes):
                raise PoolError("complete pool index footer disagrees with committed data")
            if self.schema == 3:
                member, metadata = struct.unpack_from("<QQ", footer, 40)
                if member != self.membership_digest or metadata != self.metadata_digest:
                    raise PoolError("complete pool footer digest differs")
                if any(footer[56:72]) or crc64(footer[:72]) != struct.unpack_from("<Q", footer, 72)[0]:
                    raise PoolError("complete BSP3 footer checksum differs")
            raw_index = handle_read(handle, index_offset, len(self.blocks) * entry_bytes)
        for number, block in enumerate(self.blocks):
            at = number * entry_bytes
            offset, first_record, count, rank_bytes = struct.unpack_from("<QQII", raw_index, at)
            if (offset, first_record, count, rank_bytes) != (
                    block.offset, block.first_record, block.count, block.rank_bytes):
                raise PoolError("complete pool index entry %d differs" % number)
            if self.schema == 3:
                metadata_bytes, associations = struct.unpack_from("<II", raw_index, at + 24)
                if (metadata_bytes, associations) != (block.metadata_bytes, block.associations):
                    raise PoolError("complete BSP3 index metadata differs")

    @staticmethod
    def _decode_ranks(payload: bytes, count: int, first: int, last: int) -> List[int]:
        ranks = [first]
        at = 0
        while len(ranks) < count:
            delta, at = decode_varint(payload, at)
            if not delta or ranks[-1] > MASK64 - delta:
                raise PoolError("invalid rank delta")
            ranks.append(ranks[-1] + delta)
        if at != len(payload) or ranks[-1] != last:
            raise PoolError("rank payload does not match its block header")
        return ranks

    @staticmethod
    def _decode_metadata(payload: bytes, records: int,
                         expected_associations: int) -> List[Tuple[Occurrence, ...]]:
        at = 0
        descriptor_count, at = decode_varint(payload, at)
        if descriptor_count > expected_associations:
            raise PoolError("metadata has more descriptors than associations")
        per_record = [[] for _ in range(records)]  # type: List[List[Occurrence]]
        prior = None  # type: Optional[bytes]
        associations = 0
        for _ in range(descriptor_count):
            length, at = decode_varint(payload, at)
            if not length or length > len(payload) - at:
                raise PoolError("metadata descriptor is truncated")
            raw = payload[at:at + length]
            at += length
            if prior is not None and prior >= raw:
                raise PoolError("metadata descriptors are not strictly ordered")
            prior = raw
            occurrence = Occurrence.decode(raw)
            matches, at = decode_varint(payload, at)
            if not matches or matches > records:
                raise PoolError("metadata descriptor match count is invalid")
            record = None  # type: Optional[int]
            for index in range(matches):
                value, at = decode_varint(payload, at)
                if index == 0:
                    record = value
                else:
                    if not value:
                        raise PoolError("metadata record delta must be positive")
                    record += value
                if record >= records:
                    raise PoolError("metadata association is outside its block")
                per_record[record].append(occurrence)
            associations += matches
        if at != len(payload) or associations != expected_associations:
            raise PoolError("metadata byte/association count differs")
        return [tuple(items) for items in per_record]

    def _read_block_records(self, handle, block: Block) -> Tuple[Record, ...]:
        payload = handle_read(handle, block.offset + block.header_bytes,
                              block.payload_bytes)
        ranks = self._decode_ranks(payload[:block.rank_bytes], block.count,
                                   block.first_rank, block.last_rank)
        if self.schema == 3:
            occurrences = self._decode_metadata(
                payload[block.rank_bytes:], block.count, block.associations)
        else:
            occurrences = [tuple() for _ in range(block.count)]
        return tuple(Record(rank, items)
                     for rank, items in zip(ranks, occurrences))

    def iter_records(self) -> Iterator[Record]:
        """Yield records in rank order, independent of physical block order.

        Native multi-threaded writers commit each block atomically, but worker
        completion order is intentionally nondeterministic. Every individual
        block is sorted; the file as a whole need not be. A block-level heap
        restores global order without loading the whole pool. Decoded active
        blocks use a bounded LRU cache so heavily interleaved or adversarial
        inputs cannot make memory scale with total pool size.
        """
        if not self.blocks:
            return
        physically_sorted = all(
            self.blocks[index - 1].last_rank < self.blocks[index].first_rank
            for index in range(1, len(self.blocks)))
        with open(self.path, "rb") as handle:
            if physically_sorted:
                prior = None
                for block in self.blocks:
                    for record in self._read_block_records(handle, block):
                        if prior is not None and record.rank <= prior:
                            raise PoolError("pool repeats or misorders a rank across blocks")
                        prior = record.rank
                        yield record
                return

            cache = collections.OrderedDict()
            cache_bytes = 0

            def records_for(block_index):
                nonlocal cache_bytes
                if block_index in cache:
                    records, weight = cache.pop(block_index)
                    cache[block_index] = (records, weight)
                    return records
                block = self.blocks[block_index]
                records = self._read_block_records(handle, block)
                # Parsed Python objects are larger than their encoded bytes.
                # This conservative estimate bounds the common case while
                # always retaining the one block currently being consumed.
                weight = max(block.payload_bytes * 4, block.count * 128)
                cache[block_index] = (records, weight)
                cache_bytes += weight
                while cache_bytes > SORT_CACHE_BYTES and len(cache) > 1:
                    _old_index, (_old_records, old_weight) = cache.popitem(last=False)
                    cache_bytes -= old_weight
                return records

            heap = [(block.first_rank, index, 0)
                    for index, block in enumerate(self.blocks)]
            heapq.heapify(heap)
            prior = None
            while heap:
                expected_rank, block_index, record_index = heapq.heappop(heap)
                records = records_for(block_index)
                record = records[record_index]
                if record.rank != expected_rank:
                    raise PoolError("pool block sort cursor disagrees with its rank")
                if prior is not None and record.rank <= prior:
                    raise PoolError("pool repeats or misorders a rank across blocks")
                prior = record.rank
                yield record
                record_index += 1
                if record_index < len(records):
                    heapq.heappush(
                        heap, (records[record_index].rank,
                               block_index, record_index))


def handle_read(handle, offset: int, count: int) -> bytes:
    handle.seek(offset)
    data = handle.read(count)
    if len(data) != count:
        raise PoolError("pool is truncated at byte %d" % offset)
    return data


def record_categories(record: Record, selected: Optional[set] = None) -> List[str]:
    categories = {item.category_id for item in record.occurrences if item.known}
    categories.discard(None)
    if selected is not None:
        categories.intersection_update(selected)
    return sorted(categories)


def source_summary(reader: BSPoolReader) -> Dict[str, object]:
    return {
        "path": reader.path,
        "schema": reader.schema,
        "records": reader.records,
        "committed_data_bytes": reader.data_bytes,
        "complete": bool(reader.complete),
        "coverage_complete": bool(reader.coverage_complete),
        "metadata_capable": reader.metadata_capable,
        "occurrence_metadata_complete": reader.occurrence_metadata_complete,
        "pool_id": reader.pool_id,
        "family_id": "%016x" % reader.family_id if reader.family_id else "",
        "lineage_id": "%016x" % reader.lineage_id if reader.lineage_id else "",
        "segment_id": "%016x" % reader.segment_id if reader.segment_id else "",
        "snapshot_id": reader.snapshot_token,
        "criteria_hash": "%016x" % reader.criteria_hash,
        "catalog_hash": "%016x" % reader.catalog_hash,
        "modelver": reader.modelver,
        "space": reader.space_name,
        "range_start": reader.range_start,
        "range_end": reader.range_end,
        "composite": reader.is_composite,
        "composite_operation": reader.composite_operation,
        "composite_branch_count": len(reader.composite_branches),
        "composite_operand_count": len(reader.composite_operands),
        "composite_expression": reader.composite_expression,
        "composite_expression_text": expression_text(
            reader.composite_expression,
            {operand.token: operand.label or operand.pool_id or operand.token[:8]
             for operand in reader.composite_operands.values()})
            if reader.composite_expression else "",
        "composite_branches": [
            reader.composite_branches[key].as_dict()
            for key in sorted(reader.composite_branches)
        ],
        "composite_operands": [
            reader.composite_operands[key].as_dict()
            for key in sorted(reader.composite_operands)
        ],
    }


def analyze(reader: BSPoolReader, selected: Optional[set] = None,
            ambiguity_limit: Optional[int] = None) -> Dict[str, object]:
    counts = collections.Counter()
    details = {}  # type: Dict[str, Dict[str, object]]
    ambiguous = []
    ambiguous_count = 0
    unmatched = 0
    opaque_associations = 0
    provenance_counts = collections.Counter()
    operand_counts = collections.Counter()
    records_without_provenance = 0
    records_without_operands = 0
    for record in reader.iter_records():
        categories = record_categories(record, selected)
        record_provenance = set()
        record_operands = set()
        for item in record.occurrences:
            if item.known and item.category_id in categories:
                details.setdefault(item.category_id, item.as_dict())
            elif item.is_provenance:
                record_provenance.add(item.provenance_id)
            elif item.is_operand:
                record_operands.add(item.operand_id)
            elif not item.known:
                opaque_associations += 1
        for branch_id in record_provenance:
            provenance_counts[branch_id] += 1
        if reader.is_composite and not record_provenance:
            records_without_provenance += 1
        for operand_id in record_operands:
            operand_counts[operand_id] += 1
        if reader.is_composite and not record_operands:
            records_without_operands += 1
        for category in categories:
            counts[category] += 1
        if not categories:
            unmatched += 1
        elif len(categories) > 1:
            ambiguous_count += 1
            if ambiguity_limit is None or len(ambiguous) < ambiguity_limit:
                ambiguous.append({
                    "seed": reader.seed(record.rank),
                    "rank": record.rank,
                    "candidates": categories,
                })
    categories = []
    for category in sorted(counts):
        item = dict(details[category])
        item["records"] = counts[category]
        categories.append(item)
    return {
        "organizer_schema": 1,
        "source": source_summary(reader),
        "categories": categories,
        "category_count": len(categories),
        "ambiguous_count": ambiguous_count,
        "ambiguous": ambiguous,
        "ambiguities_truncated": ambiguous_count - len(ambiguous),
        "unmatched_count": unmatched,
        "opaque_associations": opaque_associations,
        "records_without_provenance": records_without_provenance,
        "records_without_operands": records_without_operands,
        "provenance_counts": {
            "%016x" % key: value for key, value in sorted(provenance_counts.items())
        },
        "operand_counts": {
            "%016x" % key: value for key, value in sorted(operand_counts.items())
        },
    }


class BSP3OutputWriter:
    def __init__(self, reader: BSPoolReader, category_id: str, label: str,
                 final_path: str, header_bytes: Optional[int] = None,
                 header_builder=None, allow_empty: bool = False):
        self.reader = reader
        self.category_id = category_id
        self.label = label.replace("\r", " ").replace("\n", " ").replace("\0", " ")
        self.label = self.label.encode("ascii", "replace").decode("ascii")[:135]
        self.final_path = final_path
        self.header_bytes = header_bytes or reader.header_bytes
        if not (HEADER_PREFIX_BYTES <= self.header_bytes <= HEADER_MAX_BYTES):
            raise PoolError("organizer output header size is invalid")
        self.header_builder = header_builder
        self.allow_empty = allow_empty
        directory = os.path.dirname(final_path)
        fd, self.temp_path = tempfile.mkstemp(prefix=".organizer-", suffix=".tmp",
                                              dir=directory)
        self.handle = os.fdopen(fd, "w+b")
        self.handle.write(b"\0" * self.header_bytes)
        self.buffer = []  # type: List[Record]
        self.blocks = []  # type: List[Block]
        self.records = 0
        self.data_bytes = 0
        self.membership_digest = FNV64_OFFSET
        self.metadata_digest = FNV64_OFFSET
        self.closed = False
        self.last_added_rank = None  # type: Optional[int]

    def add(self, record: Record) -> None:
        if self.last_added_rank is not None and record.rank <= self.last_added_rank:
            raise PoolError("organizer output records are not strictly ordered")
        self.last_added_rank = record.rank
        self.buffer.append(record)
        if len(self.buffer) >= EVENT_WRITE_RECORDS:
            self._flush()

    def _flush(self) -> None:
        if not self.buffer:
            return
        records = sorted(self.buffer, key=lambda item: item.rank)
        if any(records[index - 1].rank >= records[index].rank
               for index in range(1, len(records))):
            raise PoolError("output category contains duplicate ranks")
        rank_payload = bytearray()
        for index in range(1, len(records)):
            rank_payload.extend(encode_varint(records[index].rank - records[index - 1].rank))
        descriptors = collections.defaultdict(list)  # type: Dict[bytes, List[int]]
        for index, record in enumerate(records):
            seen = set()
            for occurrence in record.occurrences:
                if occurrence.raw in seen:
                    continue
                seen.add(occurrence.raw)
                descriptors[occurrence.raw].append(index)
        metadata = bytearray(encode_varint(len(descriptors)))
        associations = 0
        for raw in sorted(descriptors):
            indexes = descriptors[raw]
            metadata.extend(encode_varint(len(raw)))
            metadata.extend(raw)
            metadata.extend(encode_varint(len(indexes)))
            metadata.extend(encode_varint(indexes[0]))
            for index in range(1, len(indexes)):
                metadata.extend(encode_varint(indexes[index] - indexes[index - 1]))
            associations += len(indexes)
        if not metadata or len(metadata) > MAX_METADATA_BYTES:
            raise PoolError("output metadata block is too large")
        offset = self.handle.tell()
        header = bytearray(BLOCK3_HEADER_BYTES)
        header[:4] = b"BSP3"
        header[4] = BLOCK3_HEADER_BYTES
        struct.pack_into("<IIIIQQ", header, 8, len(records), len(rank_payload),
                         len(metadata), associations, records[0].rank,
                         records[-1].rank)
        checksum = crc64(bytes(header[4:40]))
        checksum = crc64(bytes(rank_payload), checksum)
        checksum = crc64(bytes(metadata), checksum)
        struct.pack_into("<Q", header, 40, checksum)
        self.handle.write(header)
        self.handle.write(rank_payload)
        self.handle.write(metadata)
        self.membership_digest = fnv64(bytes(header), self.membership_digest)
        self.membership_digest = fnv64(bytes(rank_payload), self.membership_digest)
        self.membership_digest = fnv64(bytes(metadata), self.membership_digest)
        self.metadata_digest = fnv64(bytes(metadata), self.metadata_digest)
        self.blocks.append(Block(
            offset, self.records, len(records), len(rank_payload), len(metadata),
            associations, records[0].rank, records[-1].rank, BLOCK3_HEADER_BYTES,
        ))
        self.records += len(records)
        self.data_bytes += BLOCK3_HEADER_BYTES + len(rank_payload) + len(metadata)
        self.buffer = []

    def finalize(self) -> Dict[str, object]:
        self._flush()
        if not self.records and not self.allow_empty:
            raise PoolError("refusing to write an empty organizer category")
        index_offset = self.header_bytes + self.data_bytes
        if self.handle.tell() != index_offset:
            raise PoolError("organizer output byte accounting failed")
        for block in self.blocks:
            self.handle.write(struct.pack(
                "<QQIIII", block.offset, block.first_record, block.count,
                block.rank_bytes, block.metadata_bytes, block.associations))
        footer = bytearray(FOOTER3_BYTES)
        footer[:8] = b"BSPIDX3\n"
        struct.pack_into("<QQQQQQ", footer, 8, index_offset, len(self.blocks),
                         self.records, self.data_bytes, self.membership_digest,
                         self.metadata_digest)
        struct.pack_into("<Q", footer, 72, crc64(bytes(footer[:72])))
        self.handle.write(footer)
        if self.header_builder is None:
            header, identity = build_output_header(
                self.reader, self.category_id, self.label, self.records,
                self.data_bytes, self.membership_digest, self.metadata_digest)
        else:
            header, identity = self.header_builder(
                self.records, self.data_bytes, self.membership_digest,
                self.metadata_digest)
        if len(header) != self.header_bytes:
            raise PoolError("organizer output header builder returned the wrong size")
        self.handle.seek(0)
        self.handle.write(header)
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()
        self.closed = True
        identity.update({
            "path": self.final_path,
            "records": self.records,
            "category_id": self.category_id,
        })
        return identity

    def abort(self) -> None:
        if not self.closed:
            try:
                self.handle.close()
            except Exception:
                pass
            self.closed = True
        try:
            os.unlink(self.temp_path)
        except OSError:
            pass


def _hex_header(reader: BSPoolReader, key: str, default: int = 0) -> int:
    return reader.header.integer(key, 16, required=False, default=default)


def build_output_header(reader: BSPoolReader, category_id: str, label: str,
                        records: int, data_bytes: int, membership: int,
                        metadata: int) -> Tuple[bytes, Dict[str, object]]:
    category_hash = fnv64(category_id.encode("utf-8"))
    source_lineage = reader.lineage_id or hash_fields(
        "lineage-fallback", reader.family_id, reader.criteria_hash, 0, 0)
    family = reader.family_id or hash_fields(
        "family-fallback", reader.catalog_hash, reader.criteria_hash,
        reader.space_index, 0)
    source_stage = _hex_header(reader, "stage_hash", reader.criteria_hash)
    stage = hash_fields("organize-stage", category_hash, source_stage,
                        reader.criteria_hash, 0)
    lineage = hash_fields("organize", source_lineage, stage, reader.snapshot_id, 0)
    segment = hash_fields("segment", lineage, reader.range_start,
                          reader.range_end, reader.space_index)
    derivation = hash_fields("derive-organize", lineage, segment,
                             reader.snapshot_id, category_hash)
    criteria = hash_fields("organize-criteria", reader.criteria_hash,
                           category_hash, reader.snapshot_id, 0)
    snapshot = hash_fields("snapshot", segment, records, data_bytes, membership)
    coverage = int(bool(reader.coverage_complete))
    pool_material = ("%016x%016x%d-%d%s%d%d" % (
        reader.catalog_hash, criteria, reader.range_start, reader.range_end,
        reader.space_name, records, coverage)).encode("ascii")
    pool_id = "%016x" % fnv64(pool_material)
    modelver = reader.modelver
    route = reader.header.one("tag_route", required=False, default="collect")
    if route not in ("collect", "observe"):
        raise PoolError("source tag_route is invalid")
    refilter_depth = reader.header.integer("refilter_depth", required=False, default=0) + 1
    scan_cursor = reader.header.integer("scan_cursor", required=False, default=0)
    parent_segment = reader.segment_id
    lines = [
        "BRAINSTORM_SEED_POOL 3",
        "modelver %d" % modelver,
        "encoding delta-varint-events-v1",
        "header_bytes %d" % reader.header_bytes,
        "charset %s" % reader.charset,
        "seedspace %d" % reader.seedspace,
        "space %s" % reader.space_name,
        "range_start %d" % reader.range_start,
        "range_end %d" % reader.range_end,
        "catalog_hash %016x" % reader.catalog_hash,
        "criteria_hash %016x" % criteria,
        "pool_id %s" % pool_id,
        "family_id %016x" % family,
        "segment_id %016x" % segment,
        "stage_hash %016x" % stage,
        "lineage_id %016x" % lineage,
        "derivation_id %016x" % derivation,
        "snapshot_id %016x" % snapshot,
        "membership_digest %016x" % membership,
        "metadata_digest %016x" % metadata,
        "scan_cursor %d" % scan_cursor,
        "tag_route %s" % route,
        "label %s" % label,
        "organizer_schema 1",
        "organizer_category %s" % category_id,
        "organizer_source_snapshot %s" % reader.snapshot_token,
    ]
    for key, _, raw in reader.header.lines:
        if key in CRITERIA_DIRECTIVES and key != "tag_route":
            lines.append(raw)
    # A category split is a subset of the same composite truth.  Keep the
    # branch dictionary and input provenance contract alongside the copied
    # per-record provenance descriptors; otherwise a later combine would see
    # opaque bytes without knowing which filters they name.
    if reader.is_composite:
        for key, _value, raw in reader.header.lines:
            if key.startswith("composite_"):
                lines.append(raw)
    lines.extend([
        "refilter_depth %d" % refilter_depth,
        "source_criteria_hash %016x" % reader.criteria_hash,
        "source_records %d" % reader.records,
        "source_pool_id %s" % reader.pool_id,
        "source_complete %d" % reader.complete,
        "source_coverage_complete %d" % reader.coverage_complete,
        "input_cursor %d" % reader.records,
        "input_record_start 0",
        "input_record_end %d" % reader.records,
        "parent_snapshot_id %s" % reader.snapshot_token,
        "parent_segment_id %016x" % parent_segment,
        "parent_records %d" % reader.records,
        "parent_data_bytes %d" % reader.data_bytes,
        "parent_coverage_complete %d" % reader.coverage_complete,
        "records %d" % records,
        "data_bytes %d" % data_bytes,
        "complete 1",
        "coverage_complete %d" % coverage,
        "end",
    ])
    encoded = ("\n".join(lines) + "\n").encode("latin-1")
    if len(encoded) > reader.header_bytes:
        raise PoolError("organizer output header exceeds %d bytes" % reader.header_bytes)
    encoded += b"\0" * (reader.header_bytes - len(encoded))
    return encoded, {
        "pool_id": pool_id,
        "family_id": "%016x" % family,
        "lineage_id": "%016x" % lineage,
        "segment_id": "%016x" % segment,
        "snapshot_id": "%016x" % snapshot,
        "coverage_complete": bool(coverage),
    }


def _reader_criteria(reader: BSPoolReader) -> Tuple[str, ...]:
    return tuple(raw for key, _value, raw in reader.header.lines
                 if key in CRITERIA_DIRECTIVES)


def _direct_branch(reader: BSPoolReader) -> CompositeBranch:
    branch_id = hash_fields(
        "combine-branch", reader.catalog_hash, reader.criteria_hash,
        reader.snapshot_id, reader.segment_id)
    label = reader.header.one("label", required=False, default="").strip()
    if not label:
        label = os.path.basename(reader.path)
    return CompositeBranch(
        branch_id, reader.criteria_hash, reader.snapshot_id,
        bool(reader.coverage_complete), reader.pool_id, label,
        _reader_criteria(reader))


def source_branches(reader: BSPoolReader) -> Dict[int, CompositeBranch]:
    if reader.is_composite:
        return dict(reader.composite_branches)
    branch = _direct_branch(reader)
    return {branch.branch_id: branch}


def _reader_operand(reader: BSPoolReader) -> CompositeOperand:
    operand_id = hash_fields(
        "combine-operand", reader.catalog_hash, reader.snapshot_id,
        reader.segment_id, reader.membership_digest)
    filename = os.path.basename(reader.path)
    display = reader.header.one("label", required=False, default="").strip()
    label = filename
    if display and display not in (filename, os.path.splitext(filename)[0]):
        label = "%s [%s]" % (filename, display)
    return CompositeOperand(
        operand_id, reader.snapshot_id, reader.criteria_hash,
        bool(reader.coverage_complete), reader.records, reader.pool_id, label)


def _combine_reader_key(reader: BSPoolReader):
    return (reader.snapshot_id, reader.segment_id, reader.criteria_hash,
            reader.membership_digest, reader.path.lower())


def _canonical_combine_readers(readers: Sequence[BSPoolReader],
                               operation: str) -> Tuple[BSPoolReader, ...]:
    if operation not in COMPOSITE_OPERATIONS:
        raise PoolError("combine operation must be union, intersection, or difference")
    if len(readers) < 2:
        raise PoolError("combine needs at least two source pools")
    if len(readers) > COMPOSITE_MAX_INPUTS:
        raise PoolError("combine accepts at most %d source pools" %
                        COMPOSITE_MAX_INPUTS)
    if operation == "difference":
        return (readers[0],) + tuple(sorted(readers[1:], key=_combine_reader_key))
    return tuple(sorted(readers, key=_combine_reader_key))


def _merge_branch_definition(existing: CompositeBranch,
                             incoming: CompositeBranch) -> CompositeBranch:
    if (existing.branch_id != incoming.branch_id
            or existing.criteria_hash != incoming.criteria_hash
            or existing.snapshot_id != incoming.snapshot_id
            or existing.coverage_complete != incoming.coverage_complete
            or existing.criteria != incoming.criteria):
        raise PoolError("composite branch id collision has conflicting definitions")
    # File copies can give one snapshot a different filename/label. Identity
    # remains the same; choose deterministic display text for the new header.
    pool_id = min((value for value in (existing.pool_id, incoming.pool_id) if value),
                  default="")
    label = min((value for value in (existing.label, incoming.label) if value),
                default="")
    return CompositeBranch(
        existing.branch_id, existing.criteria_hash, existing.snapshot_id,
        existing.coverage_complete, pool_id, label, existing.criteria)


@dataclass(frozen=True)
class CombineContext:
    operation: str
    readers: Tuple[BSPoolReader, ...]
    branches: Tuple[CompositeBranch, ...]
    source_branch_ids: Tuple[Tuple[int, ...], ...]
    operands: Tuple[CompositeOperand, ...]
    range_start: int
    range_end: int
    coverage_complete: bool
    metadata_complete: bool
    branch_hash: int
    input_hash: int
    expression: Dict[str, object]

    def as_dict(self) -> Dict[str, object]:
        return {
            "operation": self.operation,
            "inputs": [source_summary(reader) for reader in self.readers],
            "input_count": len(self.readers),
            "branches": [branch.as_dict() for branch in self.branches],
            "branch_count": len(self.branches),
            "operands": [operand.as_dict() for operand in self.operands],
            "operand_count": len(self.operands),
            "range_start": self.range_start,
            "range_end": self.range_end,
            "coverage_complete": self.coverage_complete,
            "metadata_complete": self.metadata_complete,
            "criteria_differ": len({branch.criteria_hash for branch in self.branches}) > 1,
            "expression": self.expression,
            "expression_text": expression_text(
                self.expression,
                {operand.token: operand.label or operand.pool_id or operand.token[:8]
                 for operand in self.operands}),
        }


def prepare_combine(readers: Sequence[BSPoolReader], operation: str) -> CombineContext:
    ordered = _canonical_combine_readers(tuple(readers), operation)
    first = ordered[0]
    identities = set()
    for reader in ordered:
        identity = (reader.snapshot_id, reader.segment_id, reader.membership_digest,
                    reader.records)
        if identity in identities:
            raise PoolError("the same committed pool snapshot was selected more than once")
        identities.add(identity)
        if reader.modelver != first.modelver:
            raise PoolError("selected pools use different RNG model versions")
        if reader.catalog_hash != first.catalog_hash:
            raise PoolError("selected pools use different catalog/profile snapshots")
        if reader.charset != first.charset or reader.seedspace != first.seedspace:
            raise PoolError("selected pools use different seed spaces")

    merged = {}  # type: Dict[int, CompositeBranch]
    source_ids = []
    for reader in ordered:
        definitions = source_branches(reader)
        source_ids.append(tuple(sorted(definitions)))
        for branch_id, branch in definitions.items():
            if branch_id in merged:
                merged[branch_id] = _merge_branch_definition(merged[branch_id], branch)
            else:
                merged[branch_id] = branch
    branches = tuple(merged[key] for key in sorted(merged))
    if not branches:
        raise PoolError("selected pools do not define any source-filter branches")
    operands = tuple(_reader_operand(reader) for reader in ordered)
    if len({operand.operand_id for operand in operands}) != len(operands):
        raise PoolError("selected pool snapshots resolve to the same operand identity")
    expression = _validate_expression({
        "op": operation,
        "inputs": [_operand_expression(operand.operand_id)
                   for operand in operands],
    }, {operand.operand_id for operand in operands})

    branch_hash = FNV64_OFFSET
    for branch in branches:
        branch_hash = fnv64(("%016x:%016x:%016x\n" % (
            branch.branch_id, branch.criteria_hash,
            branch.snapshot_id)).encode("ascii"), branch_hash)
        for criterion in branch.criteria:
            branch_hash = fnv64(criterion.encode("utf-8") + b"\0", branch_hash)
    input_hash = FNV64_OFFSET
    for reader in ordered:
        input_hash = fnv64(("%016x:%016x:%016x:%d\n" % (
            reader.snapshot_id, reader.segment_id, reader.membership_digest,
            reader.records)).encode("ascii"), input_hash)
    same_range = all((reader.range_start, reader.range_end)
                     == (first.range_start, first.range_end) for reader in ordered)
    coverage = same_range and all(bool(reader.coverage_complete) for reader in ordered)
    return CombineContext(
        operation, ordered, branches, tuple(source_ids), operands,
        min(reader.range_start for reader in ordered),
        max(reader.range_end for reader in ordered), coverage,
        all(reader.occurrence_metadata_complete for reader in ordered),
        branch_hash, input_hash, expression)


def _composite_variable_lines(context: CombineContext) -> List[str]:
    lines = [
        "organizer_schema 2",
        "organizer_category composite:%s" % context.operation,
        "composite_schema %d" % COMPOSITE_SCHEMA,
        "composite_operation %s" % context.operation,
        "composite_route_policy provenance-only",
        "composite_expression %s" % _header_token(json.dumps(
            context.expression, sort_keys=True, separators=(",", ":"))),
        "composite_inputs %d" % len(context.readers),
        "composite_metadata_complete %d" % int(context.metadata_complete),
    ]
    for branch in context.branches:
        lines.append("composite_branch %016x %016x %016x %d %s %s" % (
            branch.branch_id, branch.criteria_hash, branch.snapshot_id,
            int(branch.coverage_complete), _header_token(branch.pool_id),
            _header_token(branch.label)))
        for criterion in branch.criteria:
            lines.append("composite_criterion %016x %s" % (
                branch.branch_id, _header_token(criterion)))
    for operand in context.operands:
        lines.append("composite_operand %016x %016x %016x %d %d %s %s" % (
            operand.operand_id, operand.snapshot_id, operand.criteria_hash,
            int(operand.coverage_complete), operand.records,
            _header_token(operand.pool_id), _header_token(operand.label)))
    for index, reader in enumerate(context.readers):
        lines.append("composite_parent %d %s %016x %d %d %s" % (
            index + 1, reader.snapshot_token, reader.segment_id,
            reader.records, int(reader.coverage_complete),
            _header_token(os.path.basename(reader.path))))
    return lines


def _composite_output_header(context: CombineContext, label: str,
                             header_bytes: int, records: int, data_bytes: int,
                             membership: int,
                             metadata: int) -> Tuple[bytes, Dict[str, object]]:
    first = context.readers[0]
    operation_hash = fnv64(context.operation.encode("ascii"))
    criteria = hash_fields("combine-criteria", first.catalog_hash,
                           context.branch_hash, context.input_hash, operation_hash)
    family = hash_fields("combine-family", first.catalog_hash, first.seedspace,
                         context.branch_hash, operation_hash)
    stage = hash_fields("combine-stage", context.branch_hash,
                        context.input_hash, operation_hash, len(context.readers))
    lineage = hash_fields("combine-lineage", family, stage,
                          context.input_hash, operation_hash)
    segment = hash_fields("segment", lineage, context.range_start,
                          context.range_end, first.space_index)
    derivation = hash_fields("derive-combine", lineage, segment,
                             context.input_hash, operation_hash)
    snapshot = hash_fields("snapshot", segment, records, data_bytes, membership)
    pool_material = ("%016x%016x%d-%d%s%d%d" % (
        first.catalog_hash, criteria, context.range_start, context.range_end,
        first.space_name, records, int(context.coverage_complete))).encode("ascii")
    pool_id = "%016x" % fnv64(pool_material)
    refilter_depth = max(reader.header.integer(
        "refilter_depth", required=False, default=0)
                         for reader in context.readers) + 1
    safe_label = label.replace("\r", " ").replace("\n", " ").replace("\0", " ")
    safe_label = safe_label.encode("ascii", "replace").decode("ascii")[:135]
    lines = [
        "BRAINSTORM_SEED_POOL 3",
        "modelver %d" % first.modelver,
        "encoding delta-varint-events-v1",
        "header_bytes %d" % header_bytes,
        "charset %s" % first.charset,
        "seedspace %d" % first.seedspace,
        "space %s" % first.space_name,
        "range_start %d" % context.range_start,
        "range_end %d" % context.range_end,
        "catalog_hash %016x" % first.catalog_hash,
        "criteria_hash %016x" % criteria,
        "pool_id %s" % pool_id,
        "family_id %016x" % family,
        "segment_id %016x" % segment,
        "stage_hash %016x" % stage,
        "lineage_id %016x" % lineage,
        "derivation_id %016x" % derivation,
        "snapshot_id %016x" % snapshot,
        "membership_digest %016x" % membership,
        "metadata_digest %016x" % metadata,
        "scan_cursor 0",
        "label %s" % safe_label,
    ]
    lines.extend(_composite_variable_lines(context))
    lines.extend([
        "refilter_depth %d" % refilter_depth,
        "records %d" % records,
        "data_bytes %d" % data_bytes,
        "complete 1",
        "coverage_complete %d" % int(context.coverage_complete),
        "end",
    ])
    encoded = ("\n".join(lines) + "\n").encode("latin-1")
    if len(encoded) > header_bytes:
        raise PoolError("composite metadata exceeds the maximum BSP3 header size")
    encoded += b"\0" * (header_bytes - len(encoded))
    return encoded, {
        "pool_id": pool_id,
        "family_id": "%016x" % family,
        "lineage_id": "%016x" % lineage,
        "segment_id": "%016x" % segment,
        "snapshot_id": "%016x" % snapshot,
        "coverage_complete": bool(context.coverage_complete),
        "operation": context.operation,
        "branch_count": len(context.branches),
    }


def composite_header_size(context: CombineContext, label: str) -> int:
    minimum = max([HEADER_EVENTS_BYTES]
                  + [reader.header_bytes for reader in context.readers])
    for size in COMPOSITE_HEADER_SIZES:
        if size < minimum:
            continue
        try:
            header, _summary = _composite_output_header(
                context, label, size, MASK64, MASK64, MASK64, MASK64)
            used = header.find(b"\0")
            if used < 0:
                used = len(header)
            # Leave room for later split/derivation lines.  Composite files
            # can be organized again, and preserving their exact expression
            # should not fail merely because the original header was packed to
            # its final byte.  The maximum size remains a last-resort fit.
            if size == HEADER_MAX_BYTES or size - used >= COMPOSITE_HEADER_SPARE_BYTES:
                return size
        except PoolError:
            continue
    raise PoolError("composite branch/filter metadata will not fit in a BSP3 header")


def _normalized_combine_record(context: CombineContext, source_index: int,
                               record: Record) -> Record:
    reader = context.readers[source_index]
    allowed = set(context.source_branch_ids[source_index])
    raw_items = {item.raw for item in record.occurrences if not item.is_operand}
    provenance = {item.provenance_id for item in record.occurrences
                  if item.is_provenance}
    source_operands = {item.operand_id for item in record.occurrences
                       if item.is_operand}
    if reader.is_composite:
        if not provenance:
            raise PoolError("composite source %s has a seed without provenance" %
                            os.path.basename(reader.path))
        unknown = provenance - allowed
        if unknown:
            raise PoolError("composite source %s references an undeclared branch" %
                            os.path.basename(reader.path))
        if not source_operands or source_operands - set(reader.composite_operands):
            raise PoolError("composite source %s has missing or undeclared operand provenance" %
                            os.path.basename(reader.path))
        if not expression_matches(reader.composite_expression, source_operands):
            raise PoolError("composite source %s has operand provenance that does not satisfy its expression" %
                            os.path.basename(reader.path))
    else:
        if provenance or source_operands:
            raise PoolError("non-composite source contains undeclared provenance metadata")
        branch_id = next(iter(allowed))
        raw_items.add(provenance_descriptor(branch_id))
    return Record(record.rank, tuple(Occurrence.decode(raw) for raw in sorted(raw_items)))


def _iter_combined_records(context: CombineContext,
                           progress=None) -> Iterator[Record]:
    iterators = [iter(reader.iter_records()) for reader in context.readers]
    heap = []
    consumed = 0
    for index, iterator in enumerate(iterators):
        try:
            record = _normalized_combine_record(context, index, next(iterator))
        except StopIteration:
            continue
        heapq.heappush(heap, (record.rank, index, record))
    while heap:
        rank = heap[0][0]
        group = []
        while heap and heap[0][0] == rank:
            _rank, index, record = heapq.heappop(heap)
            group.append((index, record))
            consumed += 1
            try:
                following = _normalized_combine_record(
                    context, index, next(iterators[index]))
            except StopIteration:
                following = None
            if following is not None:
                heapq.heappush(heap, (following.rank, index, following))
        present = {index for index, _record in group}
        include = context.operation == "union" \
            or (context.operation == "intersection"
                and len(present) == len(context.readers)) \
            or (context.operation == "difference"
                and present == {0})
        if include:
            raw_items = {item.raw for _index, item_record in group
                         for item in item_record.occurrences}
            raw_items.update(operand_descriptor(context.operands[index].operand_id)
                             for index in present)
            yield Record(rank, tuple(Occurrence.decode(raw)
                                     for raw in sorted(raw_items)))
        if progress is not None and consumed % 100000 == 0:
            progress(consumed)


def combine_pools(readers: Sequence[BSPoolReader], output_path: str,
                  operation: str = "union", label: str = "Combined seed pool",
                  progress=None) -> Dict[str, object]:
    """Stream a literal set operation and atomically publish one BSP3 pool."""
    context = prepare_combine(readers, operation)
    output_path = os.path.abspath(output_path)
    directory = os.path.dirname(output_path)
    os.makedirs(directory, exist_ok=True)
    if os.path.exists(output_path):
        raise PoolError("output already exists: %s" % output_path)
    for reader in context.readers:
        if os.path.realpath(reader.path) == os.path.realpath(output_path):
            raise PoolError("a combined pool cannot overwrite one of its inputs")
    header_bytes = composite_header_size(context, label)

    def header_builder(records, data_bytes, membership, metadata):
        return _composite_output_header(
            context, label, header_bytes, records, data_bytes,
            membership, metadata)

    writer = BSP3OutputWriter(
        context.readers[0], "composite:%s" % context.operation, label,
        output_path, header_bytes=header_bytes,
        header_builder=header_builder, allow_empty=True)
    published = False
    try:
        for record in _iter_combined_records(context, progress=progress):
            writer.add(record)
        identity = writer.finalize()
        os.link(writer.temp_path, output_path)
        published = True
        os.unlink(writer.temp_path)
        result = context.as_dict()
        result.update(identity)
        result.update({
            "path": output_path,
            "records": writer.records,
            "header_bytes": header_bytes,
            "completed": True,
        })
        return result
    except Exception:
        if published:
            try:
                os.unlink(output_path)
            except OSError:
                pass
        writer.abort()
        raise


def safe_filename(category_id: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._+-]+", "-", category_id).strip("-.")
    stem = stem[:96] or "category"
    return "%s-%016x.bspool" % (stem, fnv64(category_id.encode("utf-8")))


def atomic_json(path: str, value: object) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".organizer-report-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def load_choices(path: Optional[str], reader: BSPoolReader) -> Dict[str, str]:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as exc:
        raise PoolError("cannot read choices file: %s" % exc)
    if not isinstance(value, dict) or not isinstance(value.get("choices"), dict):
        raise PoolError("choices file needs an object-valued 'choices' field")
    snapshot = str(value.get("source_snapshot_id", "")).lower()
    if snapshot != reader.snapshot_token:
        raise PoolError("choices file is for snapshot %s, current committed snapshot is %s" % (
            snapshot or "(missing)", reader.snapshot_token))
    choices = {}
    for key, category in value["choices"].items():
        if not isinstance(key, str) or not isinstance(category, str):
            raise PoolError("choice keys and category ids must be strings")
        # A generated split plan uses blank values as editable placeholders.
        if not category:
            continue
        choices[key] = category
    return choices


def choice_for_record(choices: Dict[str, str], reader: BSPoolReader,
                      record: Record) -> Tuple[Optional[str], Optional[str]]:
    seed = reader.seed(record.rank)
    rank_key = "rank:%d" % record.rank
    if seed in choices and rank_key in choices and choices[seed] != choices[rank_key]:
        raise PoolError("choices disagree for seed %s / rank %d" % (seed, record.rank))
    if seed in choices:
        return choices[seed], seed
    if rank_key in choices:
        return choices[rank_key], rank_key
    return None, None


def split_pool(reader: BSPoolReader, output_dir: str,
               selected_ids: Optional[Sequence[str]], choices_path: Optional[str],
               report_path: Optional[str], remainder_name: Optional[str],
               omit_unmatched: bool) -> Tuple[Dict[str, object], bool]:
    if not reader.metadata_capable:
        raise PoolError("BSP2 has no per-seed occurrence metadata; refilter/rescan into BSP3 before splitting")
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    choices = load_choices(choices_path, reader)
    all_summary = analyze(reader, ambiguity_limit=0)
    available = {item["category_id"]: item for item in all_summary["categories"]}
    if selected_ids:
        unknown = sorted(set(selected_ids) - set(available))
        if unknown:
            raise PoolError("selected category is absent from snapshot: %s" % unknown[0])
        selected = set(selected_ids)
    else:
        selected = set(available)
    if not selected:
        raise PoolError("snapshot has no known tag/Legendary/voucher occurrence categories")

    category_counts = collections.Counter()
    ambiguous = []
    unresolved = 0
    unmatched = 0
    used_choices = set()
    remainder_id = "remainder:%s" % quote(remainder_name, safe="_.-") \
        if remainder_name else None
    for record in reader.iter_records():
        candidates = record_categories(record, selected)
        chosen = None
        choice_key = None
        if len(candidates) > 1:
            chosen, choice_key = choice_for_record(choices, reader, record)
            if choice_key:
                used_choices.add(choice_key)
            if chosen is not None and chosen not in candidates:
                raise PoolError("choice for %s is not one of that seed's candidates" %
                                reader.seed(record.rank))
            if chosen is None:
                unresolved += 1
            ambiguous.append({
                "seed": reader.seed(record.rank),
                "rank": record.rank,
                "candidates": candidates,
                "choice": chosen,
            })
        elif len(candidates) == 1:
            chosen = candidates[0]
            extra_choice, choice_key = choice_for_record(choices, reader, record)
            if extra_choice is not None:
                used_choices.add(choice_key)
                if extra_choice != chosen:
                    raise PoolError("choice for unambiguous seed %s conflicts with its category" %
                                    reader.seed(record.rank))
        else:
            unmatched += 1
            if remainder_id:
                chosen = remainder_id
        if chosen is not None:
            category_counts[chosen] += 1
    unused = sorted(set(choices) - used_choices)
    if unused:
        raise PoolError("choices file contains a seed/rank not used by this split: %s" % unused[0])

    category_rows = []
    for category in sorted(category_counts):
        if category == remainder_id:
            label = remainder_name
        else:
            label = available[category]["label"]
        category_rows.append({
            "category_id": category,
            "label": label,
            "records": category_counts[category],
        })
    report = {
        "organizer_schema": 1,
        "source": source_summary(reader),
        # These two fields intentionally make the generated plan itself a
        # valid --choices template: fill in the blank values and pass it back.
        "source_snapshot_id": reader.snapshot_token,
        "choices": {item["seed"]: (item["choice"] or "") for item in ambiguous},
        "selected_categories": sorted(selected),
        "categories": category_rows,
        "ambiguous_count": len(ambiguous),
        "unresolved_ambiguities": unresolved,
        "ambiguous": ambiguous,
        "unmatched_count": unmatched,
        "unmatched_policy": "remainder" if remainder_id else (
            "omit" if omit_unmatched else "error"),
        "outputs": [],
    }
    if report_path is None:
        base = os.path.basename(reader.path)
        if base.lower().endswith(".bspool"):
            base = base[:-7]
        report_path = os.path.join(output_dir, base + "-split-plan.json")
    else:
        report_path = os.path.abspath(report_path)
    atomic_json(report_path, report)
    if unresolved or (unmatched and not remainder_id and not omit_unmatched):
        return report, False

    destinations = {}
    labels = {}
    for row in category_rows:
        category = row["category_id"]
        destination = os.path.join(output_dir, safe_filename(category))
        if os.path.exists(destination):
            raise PoolError("output already exists: %s" % destination)
        if destination in destinations.values():
            raise PoolError("two categories resolve to the same output filename")
        destinations[category] = destination
        labels[category] = row["label"]
    writers = {}  # type: Dict[str, BSP3OutputWriter]
    linked = []  # type: List[str]
    try:
        for record in reader.iter_records():
            candidates = record_categories(record, selected)
            if len(candidates) > 1:
                category, _ = choice_for_record(choices, reader, record)
            elif len(candidates) == 1:
                category = candidates[0]
            else:
                category = remainder_id
            if category is None:
                continue
            writer = writers.get(category)
            if writer is None:
                writer = BSP3OutputWriter(reader, category, labels[category],
                                          destinations[category])
                writers[category] = writer
            writer.add(record)
        outputs = [writers[category].finalize() for category in sorted(writers)]
        # Hard-linking is a no-overwrite atomic publish on the same filesystem.
        # All files are finalized and fsynced before any becomes visible.
        for category in sorted(writers):
            writer = writers[category]
            os.link(writer.temp_path, writer.final_path)
            linked.append(writer.final_path)
        for writer in writers.values():
            os.unlink(writer.temp_path)
        report["outputs"] = outputs
        atomic_json(report_path, report)
        return report, True
    except Exception:
        for path in linked:
            try:
                os.unlink(path)
            except OSError:
                pass
        for writer in writers.values():
            writer.abort()
        raise


def command_inspect(args) -> int:
    reader = BSPoolReader(args.input)
    limit = None if args.ambiguity_limit < 0 else args.ambiguity_limit
    report = analyze(reader, ambiguity_limit=limit)
    if args.json:
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    source = report["source"]
    state = "complete" if source["complete"] else "incomplete/paused"
    coverage = "complete coverage" if source["coverage_complete"] else "provisional coverage"
    print("%s: BSP%d, %d committed seeds, %s, %s" % (
        source["path"], source["schema"], source["records"], state, coverage))
    if not source["metadata_capable"]:
        print("No position metadata (BSP2); safe to read, but exact-category split is unavailable.")
        return 0
    print("Exact categories: %d; ambiguous seeds: %d; uncategorized seeds: %d" % (
        report["category_count"], report["ambiguous_count"], report["unmatched_count"]))
    for item in report["categories"]:
        print("  %7d  %s" % (item["records"], item["category_id"]))
    for item in report["ambiguous"]:
        print("  AMBIGUOUS %s: %s" % (item["seed"], ", ".join(item["candidates"])))
    if report["ambiguities_truncated"]:
        print("  ... %d more ambiguities (use --ambiguity-limit -1 for all)" %
              report["ambiguities_truncated"])
    return 0


def command_records(args) -> int:
    reader = BSPoolReader(args.input)
    output = sys.stdout if args.output == "-" else open(
        args.output, "w", encoding="utf-8", newline="\n")
    try:
        for record in reader.iter_records():
            value = {
                "seed": reader.seed(record.rank),
                "rank": record.rank,
                "occurrences": [item.as_dict() for item in record.occurrences],
            }
            output.write(json.dumps(value, sort_keys=True) + "\n")
    finally:
        if output is not sys.stdout:
            output.close()
    return 0


def command_split(args) -> int:
    reader = BSPoolReader(args.input)
    report, completed = split_pool(
        reader, args.output_dir, args.category, args.choices, args.report,
        args.remainder, args.omit_unmatched)
    if completed:
        print("Split %d committed seeds into %d pool(s)." % (
            reader.records - report["unmatched_count"] if args.omit_unmatched
            else reader.records, len(report["outputs"])))
        for output in report["outputs"]:
            print("  %7d  %s" % (output["records"], output["path"]))
        return 0
    if report["unresolved_ambiguities"]:
        print("Split stopped: %d ambiguous seed(s) need explicit choices." %
              report["unresolved_ambiguities"], file=sys.stderr)
    if report["unmatched_count"] and not args.remainder and not args.omit_unmatched:
        print("Split stopped: %d seed(s) match no selected category; use --remainder or --omit-unmatched." %
              report["unmatched_count"], file=sys.stderr)
    return 2


def command_combine(args) -> int:
    readers = [BSPoolReader(path) for path in args.inputs]
    result = combine_pools(
        readers, args.output, operation=args.operation,
        label=args.label or os.path.splitext(os.path.basename(args.output))[0])
    print("Created %s of %d committed source pools: %d unique seed(s)." % (
        result["operation"], result["input_count"], result["records"]))
    print("  %s" % result["path"])
    if not result["coverage_complete"]:
        print("  coverage is provisional (the literal recorded sets are still valid)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Inspect, split, and combine committed seed pools without "
                     "rerunning their RNG searches."))
    sub = parser.add_subparsers(dest="command", required=True)
    inspect_parser = sub.add_parser("inspect", help="summarize exact occurrence categories")
    inspect_parser.add_argument("input")
    inspect_parser.add_argument("--json", action="store_true")
    inspect_parser.add_argument("--ambiguity-limit", type=int, default=20,
                                help="ambiguity samples; -1 includes every seed")
    inspect_parser.set_defaults(function=command_inspect)

    records_parser = sub.add_parser("records", help="emit every seed and recorded occurrence as NDJSON")
    records_parser.add_argument("input")
    records_parser.add_argument("--output", default="-")
    records_parser.set_defaults(function=command_records)

    split_parser = sub.add_parser("split", help="write one pool per exact occurrence category")
    split_parser.add_argument("input")
    split_parser.add_argument("output_dir")
    split_parser.add_argument("--category", action="append",
                              help="exact category id from inspect; repeat to select several")
    split_parser.add_argument("--choices", help="JSON choices for ambiguous seeds")
    split_parser.add_argument("--report", help="split plan/result JSON path")
    unmatched = split_parser.add_mutually_exclusive_group()
    unmatched.add_argument("--remainder", help="put seeds matching no selected category in this pool")
    unmatched.add_argument("--omit-unmatched", action="store_true",
                           help="explicitly leave uncategorized seeds out")
    split_parser.set_defaults(function=command_split)

    combine_parser = sub.add_parser(
        "combine", help="union/intersect/subtract committed records from unrelated pools")
    combine_parser.add_argument("output")
    combine_parser.add_argument("inputs", nargs="+")
    combine_parser.add_argument(
        "--operation", choices=COMPOSITE_OPERATIONS, default="union")
    combine_parser.add_argument("--label", help="human-readable output label")
    combine_parser.set_defaults(function=command_combine)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.function(args)
    except (OSError, PoolError) as exc:
        print("organizer error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
