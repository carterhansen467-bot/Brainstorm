#!/usr/bin/env python3
"""Inspect and splice Brainstorm BSP2/BSP3/BSP4 pools without replaying RNG.

The organizer treats the header's ``records`` and ``data_bytes`` fields as the
last committed checkpoint.  This means a paused pool can be inspected and
split safely while a later, uncommitted tail is ignored.  Schema-3 occurrence
BSP3/BSP4 occurrence metadata is copied with each selected seed; schema-2
pools remain inspectable, but cannot be split by position because they predate
occurrence metadata. New split/combine publications use adaptive BSP4.

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
import functools
import heapq
import json
import operator
import os
import re
import struct
import sys
import tempfile
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import (Callable, Dict, Iterable, Iterator, List, NamedTuple,
                    Optional, Sequence, Tuple)
from urllib.parse import quote

try:
    # Keep bounded extended-header discovery identical to the standalone
    # builder for both event-pool schemas.
    from brainstorm_pool_builder import read_pool_header_text
    from pool_writer_lock import pool_writer_guard
except ImportError:  # Imported as tools.brainstorm_pool_organizer in tests.
    from tools.brainstorm_pool_builder import read_pool_header_text
    from tools.pool_writer_lock import pool_writer_guard


HEADER_PREFIX_BYTES = 1024
HEADER_EVENTS_BYTES = 8192
HEADER_MAX_BYTES = 256 * 1024
BLOCK_MAX_RECORDS = 8192
BSP3_WRITE_RECORDS = 1024
BSP4_WRITE_RECORDS = 4096
# Retain the old name for callers that treated it as the BSP3 layout
# constant. New adaptive output uses BSP4_WRITE_RECORDS.
EVENT_WRITE_RECORDS = BSP3_WRITE_RECORDS
BLOCK2_HEADER_BYTES = 32
BLOCK3_HEADER_BYTES = 48
BLOCK4_HEADER_BYTES = 64
BSP3_HEADER_PREFIX = b"BSP3" + bytes((BLOCK3_HEADER_BYTES, 0, 0, 0))
INDEX2_ENTRY_BYTES = 24
INDEX3_ENTRY_BYTES = 32
INDEX4_ENTRY_BYTES = 56
FOOTER2_BYTES = 40
FOOTER3_BYTES = 80
FOOTER4_BYTES = 96
MAX_METADATA_BYTES = 16 * 1024 * 1024
SUPPORTED_POOL_SCHEMAS = (2, 3, 4)
EVENT_POOL_SCHEMAS = (3, 4)
POOL_ENCODINGS = {
    2: "delta-varint-blocks-v1",
    3: "delta-varint-events-v1",
    4: "adaptive-events-v1",
}

BSP4_RANK_POSITIVE = 0
BSP4_RANK_COMPLEMENT = 1
BSP4_RANK_BITMAP = 2
BSP4_RANK_RICE = 3
BSP4_RICE_MAX_K = 41
BSP4_METADATA_ADAPTIVE = 1
BSP4_META_POSITIVE = 0
BSP4_META_COMPLEMENT = 1
BSP4_META_BITMAP = 2
BSP4_META_RUNS = 3
BSP4_MEMBERSHIP_DOMAIN = b"BSP4MEM1"
BSP4_METADATA_DOMAIN = b"BSP4META1"

# BSP4 codec payloads are deliberately self-delimiting from block counts:
# rank 0 = schema-3 positive deltas after the first rank;
# rank 1 = deltas of absent ranks in [first,last], first relative to first;
# rank 2 = little-endian-per-byte membership bits for [first,last];
# rank 3 = one k byte then Rice-coded (delta - 1) values, LSB-first.
# Each metadata descriptor remains ``length, raw, match_count`` followed by:
# 0 = positive record-index deltas; 1 = absent-index deltas; 2 = one bit per
# block record; 3 = ``run_count`` then ``gap_from_prior_end, run_length``.
# All integers outside bitmaps are canonical unsigned LEB128 values.

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
PHASE_SORT_ORDER = {"small": 0, "big": 1, "boss": 2}
SOURCE_SORT_ORDER = {"none": 0, "shop": 1, "charm": 2, "ethereal": 3}

# Inspect is intentionally catalog-key tolerant: known vanilla keys receive
# their familiar names, while modded keys still get a readable deterministic
# fallback instead of disappearing from the Organizer.
TAG_DISPLAY_NAMES = {
    "tag_uncommon": "Uncommon Tag", "tag_rare": "Rare Tag",
    "tag_negative": "Negative Tag", "tag_foil": "Foil Tag",
    "tag_holo": "Holographic Tag", "tag_polychrome": "Polychrome Tag",
    "tag_investment": "Investment Tag", "tag_voucher": "Voucher Tag",
    "tag_boss": "Boss Tag", "tag_standard": "Standard Tag",
    "tag_charm": "Charm Tag", "tag_meteor": "Meteor Tag",
    "tag_buffoon": "Buffoon Tag", "tag_handy": "Handy Tag",
    "tag_garbage": "Garbage Tag", "tag_ethereal": "Ethereal Tag",
    "tag_coupon": "Coupon Tag", "tag_double": "Double Tag",
    "tag_juggle": "Juggle Tag", "tag_d_six": "D6 Tag",
    "tag_top_up": "Top-up Tag", "tag_skip": "Skip Tag",
    "tag_orbital": "Orbital Tag", "tag_economy": "Economy Tag",
}
VOUCHER_DISPLAY_NAMES = {
    "v_overstock_norm": "Overstock", "v_overstock_plus": "Overstock Plus",
    "v_clearance_sale": "Clearance Sale", "v_liquidation": "Liquidation",
    "v_hone": "Hone", "v_glow_up": "Glow Up",
    "v_reroll_surplus": "Reroll Surplus", "v_reroll_glut": "Reroll Glut",
    "v_crystal_ball": "Crystal Ball", "v_omen_globe": "Omen Globe",
    "v_telescope": "Telescope", "v_observatory": "Observatory",
    "v_grabber": "Grabber", "v_nacho_tong": "Nacho Tong",
    "v_wasteful": "Wasteful", "v_recyclomancy": "Recyclomancy",
    "v_tarot_merchant": "Tarot Merchant", "v_tarot_tycoon": "Tarot Tycoon",
    "v_planet_merchant": "Planet Merchant", "v_planet_tycoon": "Planet Tycoon",
    "v_seed_money": "Seed Money", "v_money_tree": "Money Tree",
    "v_blank": "Blank", "v_antimatter": "Antimatter",
    "v_magic_trick": "Magic Trick", "v_illusion": "Illusion",
    "v_hieroglyph": "Hieroglyph", "v_petroglyph": "Petroglyph",
    "v_directors_cut": "Director's Cut", "v_retcon": "Retcon",
    "v_paint_brush": "Paint Brush", "v_palette": "Palette",
}

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


def _safe_split_output_limit() -> int:
    """Bound simultaneous writer locks/staging streams for this process."""
    absolute_limit = 256
    if os.name == "nt":
        # Lock files use Win32 HANDLEs while staging pools use CRT streams.
        # 256 writers remain below the common 512-stream CRT ceiling.
        return absolute_limit
    try:
        import resource
        soft_limit, _hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft_limit == resource.RLIM_INFINITY:
            return absolute_limit
        soft_limit = int(soft_limit)
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        return 96
    # A POSIX split holds approximately one lock descriptor and one staging
    # stream per output. Reserve descriptors for sources, reports and runtime.
    return max(1, min(absolute_limit, (soft_limit - 32) // 2))


MAX_SPLIT_OUTPUTS = _safe_split_output_limit()
COMPOSITE_HEADER_SIZES = (HEADER_EVENTS_BYTES, 16 * 1024, 32 * 1024,
                          64 * 1024, 128 * 1024, HEADER_MAX_BYTES)
COMPOSITE_HEADER_SPARE_BYTES = 4 * 1024
SORT_CACHE_BYTES = 64 * 1024 * 1024
# Conservative decoded-cache charge for one one-occurrence record, including
# the record object, rank integer, occurrence tuple, and container references.
DECODED_RECORD_CACHE_BYTES = 228
CANCEL_CHECK_RECORDS = 8192

CRITERIA_DIRECTIVES = {
    "tag_route", "tag", "route_tag", "legendary", "route_legendary",
    "soul_depth", "voucher", "route_voucher", "voucher_exclude",
    "route_voucher_exclude", "legendary_routes", "route_legendary_routes",
}


class PoolError(ValueError):
    """Raised when a pool cannot be trusted or organized safely."""


@dataclass(frozen=True)
class _SourceIdentity:
    """Stable attributes used to reject a changed/replaced pool pathname."""

    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    birthtime_ns: int


def _source_identity(status) -> _SourceIdentity:
    def nanoseconds(name):
        direct = getattr(status, name + "_ns", None)
        if direct is not None:
            return int(direct)
        return int(float(getattr(status, name, 0.0)) * 1000000000)

    birthtime_ns = nanoseconds("st_birthtime")
    ctime_ns = nanoseconds("st_ctime")
    if os.name == "nt" and birthtime_ns:
        # CPython's Windows path stat still maps deprecated st_ctime to the
        # creation time, while handle fstat can expose the metadata-change
        # time.  Use the explicit creation timestamp so the two views of one
        # unchanged file have a comparable identity.  File replacement and
        # mutation remain covered by volume/file ID, size, and mtime, and
        # traversal handles additionally deny write/delete sharing.
        ctime_ns = birthtime_ns
    return _SourceIdentity(
        int(status.st_dev), int(status.st_ino), int(status.st_mode),
        int(status.st_size), nanoseconds("st_mtime"), ctime_ns,
        birthtime_ns)


def _check_cancel(cancel_check: Optional[Callable[[], bool]]) -> None:
    """Raise the organizer's stable cancellation error when requested."""
    if cancel_check is not None and cancel_check():
        raise PoolError("operation cancelled")


def _open_windows_read_snapshot(path: str, *, _ctypes=None, _msvcrt=None,
                                _fdopen=None, _close_fd=None):
    """Open one Windows reader that excludes concurrent writes/replacement.

    Ownership moves from the raw Win32 HANDLE to the CRT descriptor and then
    to the returned Python stream. Each failure path closes only the resource
    whose ownership was successfully acquired at that point.

    The private dependency hooks keep those ownership transitions testable on
    non-Windows CI hosts; normal callers should pass only ``path``.
    """
    if _ctypes is None:
        import ctypes as _ctypes
    if _msvcrt is None:
        import msvcrt as _msvcrt
    if _fdopen is None:
        _fdopen = os.fdopen
    if _close_fd is None:
        _close_fd = os.close

    kernel32 = _ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        _ctypes.c_wchar_p, _ctypes.c_uint32, _ctypes.c_uint32,
        _ctypes.c_void_p, _ctypes.c_uint32, _ctypes.c_uint32,
        _ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = _ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [_ctypes.c_void_p]
    kernel32.CloseHandle.restype = _ctypes.c_int

    # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL.
    # Omitting FILE_SHARE_WRITE and FILE_SHARE_DELETE pins the inspected object
    # against both mutation and pathname replacement for the full traversal.
    raw_handle = kernel32.CreateFileW(
        os.fspath(path), 0x80000000, 0x00000001, None, 3, 0x00000080, None)
    handle = getattr(raw_handle, "value", raw_handle)
    invalid_handle = _ctypes.c_void_p(-1).value
    if handle is None or handle == invalid_handle:
        raise _ctypes.WinError(_ctypes.get_last_error())

    try:
        descriptor = _msvcrt.open_osfhandle(
            handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except BaseException:
        try:
            kernel32.CloseHandle(handle)
        finally:
            raise

    try:
        return _fdopen(descriptor, "rb", closefd=True)
    except BaseException:
        try:
            _close_fd(descriptor)
        finally:
            raise


def _open_source_snapshot_handle(path: str):
    """Open a traversal stream, adding a Windows deny-write/delete share."""
    if os.name == "nt":
        return _open_windows_read_snapshot(path)
    return open(path, "rb")


def fsync_directory(path: str) -> None:
    """Durably record directory-entry publication where the platform allows."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = None
    try:
        descriptor = os.open(os.path.abspath(path), flags)
        os.fsync(descriptor)
    except OSError:
        # Some network/virtual filesystems reject directory fsync even though
        # file fsync and atomic link/replace are supported.
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


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


@functools.lru_cache(maxsize=4096)
def _ambiguity_rule_key_cached(candidates: Tuple[str, ...]) -> str:
    payload = "\0".join(candidates).encode("utf-8")
    return "%016x" % fnv64(b"ambiguity-rule\0" + payload)


def ambiguity_rule_key(candidates: Iterable[str]) -> str:
    """Stable token for one exact set of ambiguity destinations."""
    return _ambiguity_rule_key_cached(tuple(sorted(candidates)))


def fnv32(data: bytes, value: int = FNV32_OFFSET) -> int:
    for byte in data:
        value = ((value ^ byte) * FNV32_PRIME) & 0xFFFFFFFF
    return value


def _crc64_table() -> Tuple[int, ...]:
    table = []
    for byte in range(256):
        value = byte << 56
        for _ in range(8):
            value = ((value << 1) ^ 0x42F0E1EBA9EA3693) & MASK64 \
                if value & (1 << 63) else (value << 1) & MASK64
        table.append(value)
    return tuple(table)


CRC64_TABLE = _crc64_table()


def crc64(data: bytes, value: int = 0) -> int:
    """Table-driven CRC64-ECMA-182, matching the native pool readers."""
    for byte in data:
        value = CRC64_TABLE[((value >> 56) ^ byte) & 0xFF] \
            ^ ((value << 8) & MASK64)
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


def _fallback_item_name(key: str, prefix: str) -> str:
    value = key[len(prefix):] if key.startswith(prefix) else key
    return value.replace("_", " ").strip().title() or key


def occurrence_item_name(kind: int, key: str) -> str:
    if kind == 1:
        return TAG_DISPLAY_NAMES.get(
            key, "%s Tag" % _fallback_item_name(key, "tag_"))
    if kind == 2:
        return _fallback_item_name(key, "j_")
    if kind == 3:
        return VOUCHER_DISPLAY_NAMES.get(
            key, _fallback_item_name(key, "v_"))
    return _fallback_item_name(key, "")


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

    @functools.cached_property
    def category_id(self) -> Optional[str]:
        if not self.known:
            return None
        return "%s:%s:A%d:%s:%s:o%d:%s" % (
            KIND_NAMES[self.kind], quote(self.key, safe="_.-"), self.ante,
            _phase_text(self.phase), _source_text(self.source), self.ordinal,
            _flag_text(self.flags),
        )

    @functools.cached_property
    def filter_id(self) -> Optional[str]:
        if not self.known:
            return None
        return "%s:%s" % (
            KIND_NAMES[self.kind], quote(self.key, safe="_.-"))

    @functools.cached_property
    def location_id(self) -> Optional[str]:
        if not self.known:
            return None
        return "%s:A%d:%s" % (
            self.filter_id, self.ante, _phase_text(self.phase))

    @functools.cached_property
    def item_name(self) -> str:
        return occurrence_item_name(self.kind, self.key) \
            if self.known else "Unknown"

    @functools.cached_property
    def location_label(self) -> str:
        if not self.known:
            return "Unknown location"
        return "%s Ante %d %s" % (
            self.item_name, self.ante, _phase_text(self.phase).title())

    @property
    def location_sort_key(self) -> Tuple[object, ...]:
        phase = _phase_text(self.phase) if self.known else ""
        source = _source_text(self.source) if self.known else ""
        return (
            self.ante if self.known else MASK64,
            PHASE_SORT_ORDER.get(phase, 1000 + (self.phase or 0)),
            self.item_name.casefold(),
            SOURCE_SORT_ORDER.get(source, 1000 + (self.source or 0)),
            self.ordinal or 0,
            self.flags or 0,
            self.kind or 0,
            self.key or "",
            self.category_id or self.raw.hex(),
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
            "filter_id": self.filter_id,
            "filter_label": self.item_name,
            "location_id": self.location_id,
            "location_label": self.location_label,
            "location_sort": [
                self.ante,
                PHASE_SORT_ORDER.get(
                    _phase_text(self.phase), 1000 + self.phase),
                SOURCE_SORT_ORDER.get(
                    _source_text(self.source), 1000 + self.source),
                self.ordinal,
                self.flags,
            ],
            "kind": KIND_NAMES[self.kind],
            "key": self.key,
            "ante": self.ante,
            "phase": _phase_text(self.phase),
            "source": _source_text(self.source),
            "ordinal": self.ordinal,
            "flags": _flag_text(self.flags),
        }

    def location_dict(self) -> Dict[str, object]:
        if not self.known:
            return {"known": False, "raw_hex": self.raw.hex()}
        return {
            "known": True,
            "category_id": self.location_id,
            "location_id": self.location_id,
            "label": self.location_label,
            "location_label": self.location_label,
            "filter_id": self.filter_id,
            "filter_label": self.item_name,
            "kind": KIND_NAMES[self.kind],
            "key": self.key,
            "ante": self.ante,
            "phase": _phase_text(self.phase),
        }


@dataclass(frozen=True)
class Record:
    __slots__ = ("rank", "occurrences")

    rank: int
    occurrences: Tuple[Occurrence, ...]


def _positive_rank_payload(ranks: Sequence[int]) -> bytes:
    """Return the schema-3 canonical positive-delta representation."""
    return b"".join(
        encode_varint(ranks[index] - ranks[index - 1])
        for index in range(1, len(ranks)))


def _complement_rank_payload(ranks: Sequence[int],
                             byte_limit: Optional[int] = None
                             ) -> Optional[bytes]:
    """Encode absent ranks in the inclusive first/last block universe.

    The first and last ranks are necessarily present.  Each absent rank is
    encoded as a positive delta from the previous absent rank, with the first
    delta measured from ``first_rank``.  ``byte_limit`` lets the adaptive
    encoder avoid constructing a candidate that cannot beat the positive
    representation.
    """
    span = ranks[-1] - ranks[0] + 1
    missing = span - len(ranks)
    if missing < 0:
        raise PoolError("rank block has an invalid inclusive span")
    if byte_limit is not None and missing >= byte_limit:
        return None
    output = bytearray()
    prior_missing = ranks[0]
    encoded_missing = 0
    for left, right in zip(ranks, ranks[1:]):
        for value in range(left + 1, right):
            output.extend(encode_varint(value - prior_missing))
            prior_missing = value
            encoded_missing += 1
            if byte_limit is not None and len(output) >= byte_limit:
                return None
    if encoded_missing != missing:
        raise PoolError("rank complement accounting failed")
    return bytes(output)


def _bitmap_rank_payload(ranks: Sequence[int]) -> bytes:
    span = ranks[-1] - ranks[0] + 1
    output = bytearray((span + 7) // 8)
    first = ranks[0]
    for rank in ranks:
        bit = rank - first
        output[bit >> 3] |= 1 << (bit & 7)
    return bytes(output)


def _rice_rank_payload_bytes(ranks: Sequence[int], k: int) -> int:
    if k < 0 or k > BSP4_RICE_MAX_K:
        raise PoolError("BSP4 Rice parameter is outside 0..41")
    bits = sum(
        ((ranks[index] - ranks[index - 1] - 1) >> k) + 1 + k
        for index in range(1, len(ranks)))
    return 1 + (bits + 7) // 8


def _rice_rank_payload(ranks: Sequence[int], k: int) -> bytes:
    """Encode rank gaps with unary quotients and LSB-first remainders."""
    size = _rice_rank_payload_bytes(ranks, k)
    output = bytearray(size)
    output[0] = k
    bit_at = 0
    remainder_mask = (1 << k) - 1
    for index in range(1, len(ranks)):
        value = ranks[index] - ranks[index - 1] - 1
        quotient = value >> k
        bit_at += quotient  # Unary quotient zero bits are already clear.
        output[1 + (bit_at >> 3)] |= 1 << (bit_at & 7)
        bit_at += 1
        remainder = value & remainder_mask
        for bit in range(k):
            if remainder & (1 << bit):
                output[1 + (bit_at >> 3)] |= 1 << (bit_at & 7)
            bit_at += 1
    return bytes(output)


def _encode_rice_ranks(ranks: Sequence[int]) -> Tuple[int, bytes]:
    """Choose Rice k in one gap pass, then encode the exact winning stream.

    For ``S[k] = sum(x >> k)``, the recurrence
    ``S[k + 1] = (S[k] - ones[k]) // 2`` avoids rescanning every gap for all
    42 legal parameters. ``ones[k]`` is the count of gaps with bit ``k`` set.
    This is the same fixed-width O(n + 42) selection used by the native writer.
    """
    ones = [0] * 64
    shifted_sum = 0
    if ranks and (ranks[0] < 0 or ranks[0] > MASK64):
        raise PoolError("rank is outside uint64")
    for index in range(1, len(ranks)):
        if ranks[index] < 0 or ranks[index] > MASK64:
            raise PoolError("rank is outside uint64")
        value = ranks[index] - ranks[index - 1] - 1
        if value < 0:
            raise PoolError("rank block is not strictly ordered")
        shifted_sum += value
        bits = value
        while bits:
            lowest = bits & -bits
            ones[lowest.bit_length() - 1] += 1
            bits ^= lowest
    gaps = max(0, len(ranks) - 1)
    best_size = None
    best_k = 0
    for k in range(BSP4_RICE_MAX_K + 1):
        bit_count = gaps * (k + 1) + shifted_sum
        size = 1 + (bit_count + 7) // 8
        if best_size is None or size < best_size:
            best_size = size
            best_k = k
        shifted_sum = (shifted_sum - ones[k]) // 2
    return best_k, _rice_rank_payload(ranks, best_k)


def _encode_rank_codec(ranks: Sequence[int], codec: int) -> bytes:
    if not ranks:
        raise PoolError("cannot encode an empty rank block")
    if any(rank < 0 or rank > MASK64 for rank in ranks):
        raise PoolError("rank is outside uint64")
    if any(ranks[index - 1] >= ranks[index]
           for index in range(1, len(ranks))):
        raise PoolError("rank block is not strictly ordered")
    if codec == BSP4_RANK_POSITIVE:
        return _positive_rank_payload(ranks)
    if codec == BSP4_RANK_COMPLEMENT:
        payload = _complement_rank_payload(ranks)
        if payload is None:  # No limit was supplied, so this is unreachable.
            raise PoolError("rank complement could not be encoded")
        return payload
    if codec == BSP4_RANK_BITMAP:
        return _bitmap_rank_payload(ranks)
    if codec == BSP4_RANK_RICE:
        _k, payload = _encode_rice_ranks(ranks)
        return payload
    raise PoolError("unknown BSP4 rank codec")


def _encode_adaptive_ranks_and_canonical(
        ranks: Sequence[int]) -> Tuple[int, bytes, bytes]:
    """Choose the smallest valid BSP4 rank representation.

    Ties deliberately prefer the lower codec number, making organizer output
    deterministic while readers remain able to validate any semantically
    correct representation.
    """
    positive = _encode_rank_codec(ranks, BSP4_RANK_POSITIVE)
    candidates = [(len(positive), BSP4_RANK_POSITIVE, positive)]
    complement = _complement_rank_payload(ranks, len(positive))
    if complement is not None:
        candidates.append(
            (len(complement), BSP4_RANK_COMPLEMENT, complement))
    span = ranks[-1] - ranks[0] + 1
    bitmap_bytes = (span + 7) // 8
    if bitmap_bytes < len(positive):
        bitmap = _bitmap_rank_payload(ranks)
        candidates.append((len(bitmap), BSP4_RANK_BITMAP, bitmap))
    size, codec, payload = min(candidates, key=lambda item: (item[0], item[1]))
    _rice_k, rice = _encode_rice_ranks(ranks)
    if len(rice) < size:
        return BSP4_RANK_RICE, rice, positive
    return codec, payload, positive


def _encode_adaptive_ranks(ranks: Sequence[int]) -> Tuple[int, bytes]:
    codec, payload, _canonical = _encode_adaptive_ranks_and_canonical(ranks)
    return codec, payload


def _decode_rank_codec(payload: bytes, count: int, first: int, last: int,
                       codec: int) -> List[int]:
    if not count or first > last:
        raise PoolError("BSP4 rank bounds are invalid")
    span = last - first + 1
    if count > span:
        raise PoolError("BSP4 rank count exceeds its inclusive span")
    if codec == BSP4_RANK_POSITIVE:
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
    if codec == BSP4_RANK_COMPLEMENT:
        missing_count = span - count
        # Every absent rank needs at least one byte.  Reject implausible spans
        # before any loop or allocation.
        if missing_count > len(payload):
            raise PoolError("rank complement payload is too short")
        missing = []
        prior = first
        at = 0
        for _ in range(missing_count):
            delta, at = decode_varint(payload, at)
            if not delta or prior > MASK64 - delta:
                raise PoolError("invalid rank complement delta")
            prior += delta
            if prior <= first or prior >= last:
                raise PoolError("rank complement lies outside block bounds")
            missing.append(prior)
        if at != len(payload):
            raise PoolError("rank complement has trailing bytes")
        missing_set = set(missing)
        ranks = [rank for rank in range(first, last + 1)
                 if rank not in missing_set]
        if len(ranks) != count or ranks[0] != first or ranks[-1] != last:
            raise PoolError("rank complement does not match its block header")
        return ranks
    if codec == BSP4_RANK_BITMAP:
        expected = (span + 7) // 8
        if len(payload) != expected:
            raise PoolError("rank bitmap length differs from its block span")
        remainder = span & 7
        if remainder and payload[-1] & ~((1 << remainder) - 1):
            raise PoolError("rank bitmap has nonzero padding bits")
        ranks = [
            first + bit for bit in range(span)
            if payload[bit >> 3] & (1 << (bit & 7))
        ]
        if (len(ranks) != count or not ranks
                or ranks[0] != first or ranks[-1] != last):
            raise PoolError("rank bitmap does not match its block header")
        return ranks
    if codec == BSP4_RANK_RICE:
        if not payload:
            raise PoolError("BSP4 Rice payload is missing its parameter")
        k = payload[0]
        if k > BSP4_RICE_MAX_K:
            raise PoolError("BSP4 Rice parameter is outside 0..41")
        encoded = payload[1:]
        total_bits = len(encoded) * 8
        bit_at = 0
        ranks = [first]
        for _ in range(1, count):
            quotient = 0
            while True:
                if bit_at >= total_bits:
                    raise PoolError("BSP4 Rice unary quotient is truncated")
                bit = (encoded[bit_at >> 3] >> (bit_at & 7)) & 1
                bit_at += 1
                if bit:
                    break
                quotient += 1
            remainder = 0
            for shift in range(k):
                if bit_at >= total_bits:
                    raise PoolError("BSP4 Rice remainder is truncated")
                remainder |= (
                    (encoded[bit_at >> 3] >> (bit_at & 7)) & 1
                ) << shift
                bit_at += 1
            value = (quotient << k) | remainder
            delta = value + 1
            if ranks[-1] > MASK64 - delta:
                raise PoolError("invalid BSP4 Rice rank delta")
            ranks.append(ranks[-1] + delta)
        used_bytes = (bit_at + 7) // 8
        if used_bytes != len(encoded):
            raise PoolError("BSP4 Rice payload has trailing bytes")
        remainder_bits = bit_at & 7
        if (remainder_bits
                and encoded[-1] & ~((1 << remainder_bits) - 1)):
            raise PoolError("BSP4 Rice payload has nonzero padding bits")
        if ranks[-1] != last:
            raise PoolError("BSP4 Rice payload does not match its block header")
        return ranks
    raise PoolError("unknown BSP4 rank codec")


def _descriptor_indexes(
        per_record: Sequence[Sequence[Occurrence]]) -> Dict[bytes, List[int]]:
    descriptors = collections.defaultdict(list)  # type: Dict[bytes, List[int]]
    for index, occurrences in enumerate(per_record):
        seen = set()
        for occurrence in occurrences:
            if occurrence.raw in seen:
                continue
            seen.add(occurrence.raw)
            descriptors[occurrence.raw].append(index)
    return descriptors


def _positive_index_payload(indexes: Sequence[int]) -> bytes:
    output = bytearray(encode_varint(indexes[0]))
    for index in range(1, len(indexes)):
        output.extend(encode_varint(indexes[index] - indexes[index - 1]))
    return bytes(output)


def _complement_index_payload(indexes: Sequence[int], records: int) -> bytes:
    included = set(indexes)
    complement = [index for index in range(records) if index not in included]
    if not complement:
        return b""
    return _positive_index_payload(complement)


def _bitmap_index_payload(indexes: Sequence[int], records: int) -> bytes:
    output = bytearray((records + 7) // 8)
    for index in indexes:
        output[index >> 3] |= 1 << (index & 7)
    return bytes(output)


def _run_index_payload(indexes: Sequence[int]) -> bytes:
    runs = []
    start = prior = indexes[0]
    for index in indexes[1:]:
        if index == prior + 1:
            prior = index
            continue
        runs.append((start, prior - start + 1))
        start = prior = index
    runs.append((start, prior - start + 1))
    output = bytearray(encode_varint(len(runs)))
    prior_end = 0
    for start, length in runs:
        output.extend(encode_varint(start - prior_end))
        output.extend(encode_varint(length))
        prior_end = start + length
    return bytes(output)


def _encode_adaptive_indexes(
        indexes: Sequence[int], records: int,
        positive_payload: Optional[bytes] = None) -> Tuple[int, bytes]:
    if (not indexes or len(indexes) > records
            or any(index < 0 or index >= records for index in indexes)
            or any(indexes[index - 1] >= indexes[index]
                   for index in range(1, len(indexes)))):
        raise PoolError("metadata indexes are invalid")
    candidates = [
        (BSP4_META_POSITIVE, positive_payload
         if positive_payload is not None else _positive_index_payload(indexes)),
        (BSP4_META_COMPLEMENT, _complement_index_payload(indexes, records)),
        (BSP4_META_BITMAP, _bitmap_index_payload(indexes, records)),
        (BSP4_META_RUNS, _run_index_payload(indexes)),
    ]
    codec, payload = min(candidates, key=lambda item: (len(item[1]), item[0]))
    return codec, payload


def _encode_bsp3_metadata(
        per_record: Sequence[Sequence[Occurrence]]) -> Tuple[bytes, int]:
    """Canonical descriptor/list bytes used by BSP3 and BSP4 digests."""
    descriptors = _descriptor_indexes(per_record)
    output = bytearray(encode_varint(len(descriptors)))
    associations = 0
    for raw in sorted(descriptors):
        indexes = descriptors[raw]
        output.extend(encode_varint(len(raw)))
        output.extend(raw)
        output.extend(encode_varint(len(indexes)))
        output.extend(_positive_index_payload(indexes))
        associations += len(indexes)
    return bytes(output), associations


def _encode_bsp4_metadata(
        per_record: Sequence[Sequence[Occurrence]]) -> Tuple[bytes, int]:
    adaptive, _canonical, associations = \
        _encode_bsp4_metadata_and_canonical(per_record)
    return adaptive, associations


def _encode_bsp4_metadata_and_canonical(
        per_record: Sequence[Sequence[Occurrence]]
        ) -> Tuple[bytes, bytes, int]:
    descriptors = _descriptor_indexes(per_record)
    output = bytearray(encode_varint(len(descriptors)))
    canonical = bytearray(encode_varint(len(descriptors)))
    associations = 0
    records = len(per_record)
    for raw in sorted(descriptors):
        indexes = descriptors[raw]
        positive = _positive_index_payload(indexes)
        codec, payload = _encode_adaptive_indexes(
            indexes, records, positive_payload=positive)
        common = (
            encode_varint(len(raw)) + raw + encode_varint(len(indexes)))
        canonical.extend(common)
        canonical.extend(positive)
        output.extend(common)
        output.append(codec)
        output.extend(payload)
        associations += len(indexes)
    return bytes(output), bytes(canonical), associations


def _decode_index_list(payload: bytes, at: int, indexes: int,
                       records: int) -> Tuple[List[int], int]:
    output = []
    prior = None  # type: Optional[int]
    for number in range(indexes):
        value, at = decode_varint(payload, at)
        if number:
            if not value:
                raise PoolError("metadata record delta must be positive")
            value += prior
        if value >= records:
            raise PoolError("metadata association is outside its block")
        output.append(value)
        prior = value
    return output, at


def _decode_bsp4_indexes(payload: bytes, at: int, codec: int, matches: int,
                         records: int) -> Tuple[List[int], int]:
    if codec == BSP4_META_POSITIVE:
        return _decode_index_list(payload, at, matches, records)
    if codec == BSP4_META_COMPLEMENT:
        excluded, at = _decode_index_list(
            payload, at, records - matches, records)
        excluded_set = set(excluded)
        return ([index for index in range(records)
                 if index not in excluded_set], at)
    if codec == BSP4_META_BITMAP:
        size = (records + 7) // 8
        if size > len(payload) - at:
            raise PoolError("metadata bitmap is truncated")
        bitmap = payload[at:at + size]
        at += size
        remainder = records & 7
        if remainder and bitmap[-1] & ~((1 << remainder) - 1):
            raise PoolError("metadata bitmap has nonzero padding bits")
        indexes = [
            index for index in range(records)
            if bitmap[index >> 3] & (1 << (index & 7))
        ]
        if len(indexes) != matches:
            raise PoolError("metadata bitmap match count differs")
        return indexes, at
    if codec == BSP4_META_RUNS:
        run_count, at = decode_varint(payload, at)
        if not run_count or run_count > matches:
            raise PoolError("metadata run count is invalid")
        indexes = []
        prior_end = 0
        for run in range(run_count):
            gap, at = decode_varint(payload, at)
            length, at = decode_varint(payload, at)
            if not length or (run and not gap):
                raise PoolError("metadata runs are empty or not coalesced")
            start = prior_end + gap
            end = start + length
            if end > records:
                raise PoolError("metadata run lies outside its block")
            indexes.extend(range(start, end))
            prior_end = end
        if len(indexes) != matches:
            raise PoolError("metadata run match count differs")
        return indexes, at
    raise PoolError("unknown BSP4 metadata descriptor codec")


def _bsp4_membership_start() -> int:
    return fnv64(BSP4_MEMBERSHIP_DOMAIN)


def _bsp4_metadata_start() -> int:
    return fnv64(BSP4_METADATA_DOMAIN)


def _bsp4_update_membership_digest(value: int,
                                   ranks: Sequence[int],
                                   canonical: Optional[bytes] = None) -> int:
    """Add one logical rank block to the BSP4 membership digest.

    The exact canonical stream is ``BSP4MEM1`` once, followed for every block
    by ``<IQQI`` (record count, first rank, last rank, canonical byte count)
    and the BSP3 positive-delta rank bytes.  Physical rank codec bytes and all
    event metadata are intentionally excluded.
    """
    if canonical is None:
        canonical = _positive_rank_payload(ranks)
    frame = struct.pack(
        "<IQQI", len(ranks), ranks[0], ranks[-1], len(canonical))
    return fnv64(canonical, fnv64(frame, value))


def _bsp4_update_metadata_digest(
        value: int, per_record: Sequence[Sequence[Occurrence]],
        canonical: Optional[bytes] = None,
        associations: Optional[int] = None) -> int:
    """Add one logical event block to the BSP4 metadata digest.

    The exact canonical stream is ``BSP4META1`` once, followed for every block
    by ``<III`` (record count, association count, canonical byte count) and
    canonical BSP3 descriptor/positive-index-list metadata bytes.  Adaptive
    descriptor codecs therefore cannot change the digest.
    """
    if canonical is None or associations is None:
        canonical, associations = _encode_bsp3_metadata(per_record)
    frame = struct.pack(
        "<III", len(per_record), associations, len(canonical))
    return fnv64(canonical, fnv64(frame, value))


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


class Block(NamedTuple):
    """Immutable public view of one committed pool block."""

    offset: int
    first_record: int
    count: int
    rank_bytes: int
    metadata_bytes: int
    associations: int
    first_rank: int
    last_rank: int
    header_bytes: int
    rank_codec: int
    metadata_encoding: int
    flags: int

    @property
    def payload_bytes(self) -> int:
        return self.rank_bytes + self.metadata_bytes


_PACKED_BLOCK = struct.Struct("<QQIIIIQQIBBBx")


class PackedBlockSequence(Sequence[Block]):
    """Compact random-access block descriptors for read-only pool readers.

    A large legacy BSP3 can contain hundreds of thousands of blocks. Keeping
    twelve Python integers plus one object per block costs several hundred
    bytes per entry. This sequence retains the same immutable fields in one
    56-byte packed buffer and materializes a ``Block`` only while a caller is
    accessing it. Integer indexing, slicing, iteration, ``len`` and truth
    testing retain the former list-like reader API.
    """

    __slots__ = ("_data",)

    def __init__(self) -> None:
        self._data = bytearray()

    def __len__(self) -> int:
        return len(self._data) // _PACKED_BLOCK.size

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(len(self)))]
        item = operator.index(index)
        if item < 0:
            item += len(self)
        if item < 0 or item >= len(self):
            raise IndexError("block index out of range")
        return Block(*_PACKED_BLOCK.unpack_from(
            self._data, item * _PACKED_BLOCK.size))

    def __iter__(self) -> Iterator[Block]:
        for fields in _PACKED_BLOCK.iter_unpack(self._data):
            yield Block(*fields)

    def physical_rank_order(self) -> Tuple[bool, bool]:
        """Return (rank-ordered, disjoint) without materializing Block views."""
        fields = iter(_PACKED_BLOCK.iter_unpack(self._data))
        try:
            prior = next(fields)
        except StopIteration:
            return True, True
        rank_ordered = True
        disjoint = True
        for current in fields:
            if (prior[6], prior[7]) > (current[6], current[7]):
                rank_ordered = False
            if prior[7] >= current[6]:
                disjoint = False
            prior = current
        return rank_ordered, disjoint

    def __sizeof__(self) -> int:
        # Include the owned bytearray storage in cache/memory accounting.
        return object.__sizeof__(self) + sys.getsizeof(self._data)

    @property
    def packed_bytes(self) -> int:
        """Exact descriptor payload bytes retained by this sequence."""
        return len(self._data)

    def append(self, block: Block) -> None:
        self.append_fields(*block)

    def append_fields(
            self, offset: int, first_record: int, count: int,
            rank_bytes: int, metadata_bytes: int, associations: int,
            first_rank: int, last_rank: int, header_bytes: int,
            rank_codec: int, metadata_encoding: int, flags: int) -> None:
        self._data.extend(_PACKED_BLOCK.pack(
            offset, first_record, count, rank_bytes, metadata_bytes,
            associations, first_rank, last_rank, header_bytes, rank_codec,
            metadata_encoding, flags))


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
    """Verified view of exactly one committed BSP2/BSP3/BSP4 snapshot."""

    def __init__(self, path: str,
                 cancel_check: Optional[Callable[[], bool]] = None,
                 verify_payloads: bool = True):
        _check_cancel(cancel_check)
        self.cancel_check = cancel_check
        self.path = os.path.abspath(path)
        try:
            source_handle = open(self.path, "rb")
        except OSError as exc:
            raise PoolError("cannot open Brainstorm pool: %s" % exc)
        try:
            self._source_identity = _source_identity(
                os.fstat(source_handle.fileno()))
            text = _read_pool_header_text_from_handle(source_handle)
            self._assert_source_unchanged(source_handle)
        finally:
            source_handle.close()
        if not text:
            raise PoolError("cannot read a bounded Brainstorm pool header")
        self.header = PoolHeader(text)
        magic = self.header.one("BRAINSTORM_SEED_POOL")
        try:
            self.schema = int(magic)
        except ValueError:
            raise PoolError("invalid Brainstorm pool schema")
        if self.schema not in SUPPORTED_POOL_SCHEMAS:
            raise PoolError(
                "organizer supports committed BSP2/BSP3/BSP4 pools "
                "(got BSP%d)" % self.schema)
        self.header_bytes = self.header.integer("header_bytes")
        expected_header = HEADER_EVENTS_BYTES \
            if self.schema in EVENT_POOL_SCHEMAS else HEADER_PREFIX_BYTES
        if self.header_bytes != expected_header:
            # Event formats permit bounded extended headers. Output preserves
            # their size, while BSP2 remains the fixed historical 1 KiB.
            if (self.schema not in EVENT_POOL_SCHEMAS
                    or not (HEADER_PREFIX_BYTES <= self.header_bytes
                            <= HEADER_MAX_BYTES)):
                raise PoolError("pool header_bytes is invalid")
        self.encoding = self.header.one("encoding")
        expected_encoding = POOL_ENCODINGS[self.schema]
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
        self.file_bytes = self._source_identity.size
        self.data_end = self.header_bytes + self.data_bytes
        if self.file_bytes < self.data_end:
            raise PoolError("pool is shorter than its committed data boundary")
        self.blocks = PackedBlockSequence()
        self._trusted_bsp3_index = b""
        self._trusted_bsp3_recovery_identity = False
        self._repaired_bsp3_headers = set()  # type: set
        self._payload_verified = False
        self._verification_lock = threading.RLock()
        self._declared_membership_digest = self.header.integer(
            "membership_digest", 16, required=False, default=0)
        self._declared_metadata_digest = self.header.integer(
            "metadata_digest", 16, required=False, default=0) \
            if self.schema in EVENT_POOL_SCHEMAS else 0
        self._footer_membership_digest = 0
        self._footer_metadata_digest = 0
        self.membership_digest = _bsp4_membership_start() \
            if self.schema == 4 else FNV64_OFFSET
        self.metadata_digest = _bsp4_metadata_start() \
            if self.schema == 4 else (FNV64_OFFSET if self.schema == 3 else 0)
        index_loaded = (
            not verify_payloads and self.complete and self.schema == 4)
        if index_loaded:
            self._load_complete_bsp4_index()
        else:
            if self.complete and self.schema == 3:
                self._load_trusted_complete_bsp3_index()
            self._scan_committed_blocks()
            # The trusted index is needed only while resolving physical BSP3
            # block boundaries. Do not retain tens of megabytes of duplicate
            # index bytes for very large completed pools.
            self._trusted_bsp3_index = b""
        _check_cancel(cancel_check)
        if verify_payloads or self._repaired_bsp3_headers:
            self._verify_all_payloads()
        if self.complete and not index_loaded:
            self._validate_final_index()
        if not verify_payloads:
            # Identity consumers can use the committed header/footer values
            # while payload verification is deferred to the first traversal.
            self.membership_digest = (
                self._declared_membership_digest
                or self._footer_membership_digest
                or self.membership_digest)
            self.metadata_digest = (
                self._declared_metadata_digest
                or self._footer_metadata_digest
                or self.metadata_digest)
            if not self.blocks:
                self._finish_payload_verification(
                    _bsp4_membership_start()
                    if self.schema == 4 else FNV64_OFFSET,
                    _bsp4_metadata_start()
                    if self.schema == 4
                    else (FNV64_OFFSET if self.schema == 3 else 0))
        _check_cancel(cancel_check)

    def _source_changed_error(self) -> PoolError:
        return PoolError(
            "pool source changed or was replaced after inspection began; "
            "inspect the current file again: %s" % self.path)

    def _assert_source_unchanged(self, handle) -> None:
        try:
            opened_identity = _source_identity(os.fstat(handle.fileno()))
            path_identity = _source_identity(os.stat(self.path))
        except OSError:
            raise self._source_changed_error()
        if (opened_identity != self._source_identity
                or path_identity != self._source_identity):
            raise self._source_changed_error()

    @contextmanager
    def _open_source_snapshot(
            self,
            cancel_check: Optional[Callable[[], bool]] = None):
        """Pin one traversal handle and reject pathname/object changes."""
        _check_cancel(cancel_check)
        try:
            handle = _open_source_snapshot_handle(self.path)
        except OSError:
            raise self._source_changed_error()
        try:
            self._assert_source_unchanged(handle)
            yield handle
            _check_cancel(cancel_check)
            self._assert_source_unchanged(handle)
        finally:
            handle.close()

    @property
    def metadata_capable(self) -> bool:
        return self.schema in EVENT_POOL_SCHEMAS

    @property
    def occurrence_metadata_complete(self) -> bool:
        if self.schema not in EVENT_POOL_SCHEMAS:
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
        if self.schema not in EVENT_POOL_SCHEMAS:
            raise PoolError("composite provenance requires a BSP3/BSP4 pool")
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

    def _load_complete_bsp4_index(self) -> None:
        """Build lazy BSP4 block descriptors from its checked final index.

        Complete BSP4 indexes repeat every structural block field. Reading
        them once avoids one physical-header seek per block; the header itself
        is still compared byte-for-byte with the descriptor when that payload
        is CRC/canonical-validated.
        """
        footer_bytes = FOOTER4_BYTES
        if self.file_bytes < self.data_end + footer_bytes:
            raise PoolError("complete pool has a truncated BSP4 index")
        with self._open_source_snapshot(self.cancel_check) as handle:
            footer = handle_read(
                handle, self.file_bytes - footer_bytes, footer_bytes)
            if footer[:8] != b"BSPIDX4\n":
                raise PoolError("complete pool index footer is missing")
            if (any(footer[56:88])
                    or crc64(footer[:88])
                    != struct.unpack_from("<Q", footer, 88)[0]):
                raise PoolError("complete BSP4 footer checksum differs")
            index_offset, block_count, records, data_bytes = \
                struct.unpack_from("<QQQQ", footer, 8)
            if (index_offset != self.data_end or records != self.records
                    or data_bytes != self.data_bytes):
                raise PoolError(
                    "complete pool index footer disagrees with committed data")
            expected_size = (
                self.data_end + block_count * INDEX4_ENTRY_BYTES
                + footer_bytes)
            if self.file_bytes != expected_size:
                raise PoolError(
                    "complete pool has trailing or missing index bytes")
            member, metadata = struct.unpack_from("<QQ", footer, 40)
            self._footer_membership_digest = member
            self._footer_metadata_digest = metadata
            if ((self._declared_membership_digest
                 and member != self._declared_membership_digest)
                    or (self._declared_metadata_digest
                        and metadata != self._declared_metadata_digest)):
                raise PoolError(
                    "complete pool footer digest differs from header")
            raw_index = handle_read(
                handle, index_offset, block_count * INDEX4_ENTRY_BYTES)

        expected_offset = self.header_bytes
        expected_first_record = 0
        entry_format = "<QQQQIIIIBBBBI"
        for number, fields in enumerate(
                struct.iter_unpack(entry_format, raw_index)):
            if number % CANCEL_CHECK_RECORDS == 0:
                _check_cancel(self.cancel_check)
            offset, first_record, first_rank, last_rank, count, rank_bytes, \
                metadata_bytes, associations, rank_codec, \
                metadata_encoding, flags, reserved8, reserved32 = fields
            if (reserved8 or reserved32 or flags
                    or rank_codec not in (
                        BSP4_RANK_POSITIVE, BSP4_RANK_COMPLEMENT,
                        BSP4_RANK_BITMAP, BSP4_RANK_RICE)
                    or metadata_encoding != BSP4_METADATA_ADAPTIVE):
                raise PoolError(
                    "complete BSP4 index entry %d is malformed" % number)
            if (offset != expected_offset
                    or first_record != expected_first_record):
                raise PoolError(
                    "complete BSP4 index entry %d is not contiguous" %
                    number)
            if (not count or count > BLOCK_MAX_RECORDS
                    or first_rank > last_rank
                    or count > last_rank - first_rank + 1
                    or first_rank < self.range_start
                    or last_rank >= self.range_end
                    or rank_bytes > (count - 1) * 6
                    or not metadata_bytes
                    or metadata_bytes > MAX_METADATA_BYTES):
                raise PoolError(
                    "complete BSP4 index entry %d has invalid bounds" %
                    number)
            payload_bytes = rank_bytes + metadata_bytes
            if offset + BLOCK4_HEADER_BYTES + payload_bytes > self.data_end:
                raise PoolError(
                    "complete BSP4 index entry %d exceeds committed data" %
                    number)
            self.blocks.append_fields(
                offset, first_record, count, rank_bytes, metadata_bytes,
                associations, first_rank, last_rank, BLOCK4_HEADER_BYTES,
                rank_codec, metadata_encoding, flags)
            expected_offset += BLOCK4_HEADER_BYTES + payload_bytes
            expected_first_record += count
        _check_cancel(self.cancel_check)
        if (len(self.blocks) != block_count
                or expected_offset != self.data_end
                or expected_first_record != self.records
                or bool(self.records) != bool(self.blocks)):
            raise PoolError(
                "complete BSP4 index does not cover its committed snapshot")

    def _load_trusted_complete_bsp3_index(self) -> None:
        """Preflight the final BSP3 index used for narrow header recovery.

        A completed BSP3 footer authenticates its snapshot digests and fixes
        the location and size of the final index.  The index then fixes every
        physical block boundary and the structural fields duplicated after
        the first eight header bytes.  Keep the raw index only after all of
        those bounds are self-consistent; payload CRCs, decoded ranks and
        metadata, and the whole-pool digests are still checked separately.
        """
        if self.file_bytes < self.data_end + FOOTER3_BYTES:
            raise PoolError("complete pool has a truncated BSP3 index")
        with self._open_source_snapshot(self.cancel_check) as handle:
            footer = handle_read(
                handle, self.file_bytes - FOOTER3_BYTES, FOOTER3_BYTES)
            if footer[:8] != b"BSPIDX3\n":
                raise PoolError("complete pool index footer is missing")
            if (any(footer[56:72])
                    or crc64(footer[:72])
                    != struct.unpack_from("<Q", footer, 72)[0]):
                raise PoolError("complete BSP3 footer checksum differs")
            index_offset, block_count, records, data_bytes = \
                struct.unpack_from("<QQQQ", footer, 8)
            if (index_offset != self.data_end or records != self.records
                    or data_bytes != self.data_bytes):
                raise PoolError(
                    "complete pool index footer disagrees with committed data")
            if (block_count > records
                    or bool(records) != bool(block_count)):
                raise PoolError(
                    "complete BSP3 index has an invalid block count")
            expected_size = (
                self.data_end + block_count * INDEX3_ENTRY_BYTES
                + FOOTER3_BYTES)
            if self.file_bytes != expected_size:
                raise PoolError(
                    "complete pool has trailing or missing index bytes")
            member, metadata = struct.unpack_from("<QQ", footer, 40)
            if ((self._declared_membership_digest
                 and member != self._declared_membership_digest)
                    or (self._declared_metadata_digest
                        and metadata != self._declared_metadata_digest)):
                raise PoolError(
                    "complete pool footer digest differs from header")
            raw_index = handle_read(
                handle, index_offset,
                block_count * INDEX3_ENTRY_BYTES)

        segment = self.header.integer(
            "segment_id", 16, required=False, default=0)
        snapshot = self.header.integer(
            "snapshot_id", 16, required=False, default=0)
        expected_snapshot = hash_fields(
            "snapshot", segment, self.records, self.data_bytes, member)
        self._trusted_bsp3_recovery_identity = bool(
            member and metadata and segment and snapshot
            and self._declared_membership_digest == member
            and self._declared_metadata_digest == metadata
            and snapshot == expected_snapshot)

        expected_offset = self.header_bytes
        expected_first_record = 0
        for number, fields in enumerate(
                struct.iter_unpack("<QQIIII", raw_index)):
            if number % CANCEL_CHECK_RECORDS == 0:
                _check_cancel(self.cancel_check)
            offset, first_record, count, rank_bytes, metadata_bytes, \
                _associations = fields
            if (offset != expected_offset
                    or first_record != expected_first_record):
                raise PoolError(
                    "complete BSP3 index entry %d is not contiguous" %
                    number)
            if (not count or count > BLOCK_MAX_RECORDS
                    or rank_bytes > (count - 1) * 6
                    or not metadata_bytes
                    or metadata_bytes > MAX_METADATA_BYTES):
                raise PoolError(
                    "complete BSP3 index entry %d has invalid bounds" %
                    number)
            expected_offset += (
                BLOCK3_HEADER_BYTES + rank_bytes + metadata_bytes)
            expected_first_record += count
            if expected_offset > self.data_end \
                    or expected_first_record > self.records:
                raise PoolError(
                    "complete BSP3 index entry %d exceeds committed data" %
                    number)
        _check_cancel(self.cancel_check)
        if (expected_offset != self.data_end
                or expected_first_record != self.records):
            raise PoolError(
                "complete BSP3 index does not cover its committed snapshot")
        self._footer_membership_digest = member
        self._footer_metadata_digest = metadata
        self._trusted_bsp3_index = raw_index

    def _trusted_bsp3_entry(
            self, number: int
            ) -> Optional[Tuple[int, int, int, int, int, int]]:
        at = number * INDEX3_ENTRY_BYTES
        if at + INDEX3_ENTRY_BYTES > len(self._trusted_bsp3_index):
            return None
        return struct.unpack_from(
            "<QQIIII", self._trusted_bsp3_index, at)

    def _scan_committed_blocks(self) -> None:
        total_records = 0
        offset = self.header_bytes
        with self._open_source_snapshot(self.cancel_check) as handle:
            while offset < self.data_end:
                _check_cancel(self.cancel_check)
                rank_codec = BSP4_RANK_POSITIVE
                metadata_encoding = 0
                flags = 0
                if self.schema == 4:
                    header_bytes = handle_read(
                        handle, offset, BLOCK4_HEADER_BYTES)
                    fields = struct.unpack(
                        "<4sBBBBIIIIQQQQQ", header_bytes)
                    magic, size, rank_codec, metadata_encoding, flags, count, \
                        rank_bytes, meta_bytes, associations, first, last, \
                        rank_checksum, metadata_checksum, reserved = fields
                    if (magic != b"BSP4" or size != BLOCK4_HEADER_BYTES
                            or rank_codec not in (
                                BSP4_RANK_POSITIVE,
                                BSP4_RANK_COMPLEMENT,
                                BSP4_RANK_BITMAP,
                                BSP4_RANK_RICE)
                            or metadata_encoding != BSP4_METADATA_ADAPTIVE
                            or flags or reserved):
                        raise PoolError(
                            "malformed committed BSP4 block at byte %d" %
                            offset)
                    if not meta_bytes or meta_bytes > MAX_METADATA_BYTES:
                        raise PoolError("BSP4 block metadata size is invalid")
                    block_header_bytes = BLOCK4_HEADER_BYTES
                elif self.schema == 3:
                    header_bytes = handle_read(
                        handle, offset, BLOCK3_HEADER_BYTES)
                    fields = struct.unpack("<4sBBBBIIIIQQQ", header_bytes)
                    magic, size, z1, z2, z3, count, rank_bytes, meta_bytes, \
                        associations, first, last, checksum = fields
                    if (magic != b"BSP3"
                            or size != BLOCK3_HEADER_BYTES
                            or z1 or z2 or z3):
                        number = len(self.blocks)
                        index_entry = self._trusted_bsp3_entry(number)
                        reconstructed = (
                            BSP3_HEADER_PREFIX + header_bytes[8:])
                        fields = struct.unpack(
                            "<4sBBBBIIIIQQQ", reconstructed)
                        magic, size, z1, z2, z3, count, rank_bytes, \
                            meta_bytes, associations, first, last, checksum = \
                        fields
                        if (index_entry is None
                                or not self._trusted_bsp3_recovery_identity
                                or index_entry != (
                                    offset, total_records, count, rank_bytes,
                                    meta_bytes, associations)):
                            raise PoolError(
                                "source pool is damaged: BSP3 block %d at "
                                "byte %d has an invalid header (first 8 "
                                "bytes: %s); its committed index and digests "
                                "cannot safely reconstruct it" % (
                                    number, offset,
                                    header_bytes[:8].hex()))
                        if self._repaired_bsp3_headers:
                            raise PoolError(
                                "source pool is damaged: BSP3 block %d at "
                                "byte %d has a second invalid header prefix; "
                                "automatic recovery is limited to one block" %
                                (number, offset))
                        header_bytes = reconstructed
                        self._repaired_bsp3_headers.add(offset)
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
                self.blocks.append_fields(
                    offset, total_records, count, rank_bytes, meta_bytes,
                    associations, first, last, block_header_bytes, rank_codec,
                    metadata_encoding, flags)
                total_records += count
                offset += block_header_bytes + payload_bytes
        _check_cancel(self.cancel_check)
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
        if (self._declared_membership_digest
                and self._declared_membership_digest
                != self.membership_digest):
            raise PoolError("membership_digest differs from committed pool bytes")
        if (self.schema in EVENT_POOL_SCHEMAS
                and self._declared_metadata_digest
                and self._declared_metadata_digest != self.metadata_digest):
            raise PoolError(
                "metadata_digest differs from committed event metadata")
        if (self._footer_membership_digest
                and self._footer_membership_digest
                != self.membership_digest):
            raise PoolError("complete pool footer digest differs")
        if (self._footer_metadata_digest
                and self._footer_metadata_digest != self.metadata_digest):
            raise PoolError("complete pool footer digest differs")

    def _validate_final_index(self) -> None:
        entry_bytes = {
            2: INDEX2_ENTRY_BYTES,
            3: INDEX3_ENTRY_BYTES,
            4: INDEX4_ENTRY_BYTES,
        }[self.schema]
        footer_bytes = {
            2: FOOTER2_BYTES,
            3: FOOTER3_BYTES,
            4: FOOTER4_BYTES,
        }[self.schema]
        expected_size = self.data_end + len(self.blocks) * entry_bytes + footer_bytes
        if self.file_bytes != expected_size:
            raise PoolError("complete pool has trailing or missing index bytes")
        with self._open_source_snapshot(self.cancel_check) as handle:
            footer = handle_read(handle, self.file_bytes - footer_bytes, footer_bytes)
            magic = {
                2: b"BSPIDX2\n",
                3: b"BSPIDX3\n",
                4: b"BSPIDX4\n",
            }[self.schema]
            if footer[:8] != magic:
                raise PoolError("complete pool index footer is missing")
            index_offset, blocks, records, data_bytes = struct.unpack_from("<QQQQ", footer, 8)
            if (index_offset != self.data_end or blocks != len(self.blocks)
                    or records != self.records or data_bytes != self.data_bytes):
                raise PoolError("complete pool index footer disagrees with committed data")
            if self.schema in EVENT_POOL_SCHEMAS:
                member, metadata = struct.unpack_from("<QQ", footer, 40)
                self._footer_membership_digest = member
                self._footer_metadata_digest = metadata
                if self._payload_verified:
                    if (member != self.membership_digest
                            or metadata != self.metadata_digest):
                        raise PoolError("complete pool footer digest differs")
                elif ((self._declared_membership_digest
                       and member != self._declared_membership_digest)
                      or (self._declared_metadata_digest
                          and metadata != self._declared_metadata_digest)):
                    raise PoolError(
                        "complete pool footer digest differs from header")
            if self.schema == 3:
                if (any(footer[56:72])
                        or crc64(footer[:72])
                        != struct.unpack_from("<Q", footer, 72)[0]):
                    raise PoolError("complete BSP3 footer checksum differs")
            elif self.schema == 4:
                if (any(footer[56:88])
                        or crc64(footer[:88])
                        != struct.unpack_from("<Q", footer, 88)[0]):
                    raise PoolError("complete BSP4 footer checksum differs")
            raw_index = handle_read(handle, index_offset, len(self.blocks) * entry_bytes)
        for number, block in enumerate(self.blocks):
            at = number * entry_bytes
            if self.schema == 4:
                fields = struct.unpack_from(
                    "<QQQQIIIIBBBBI", raw_index, at)
                offset, first_record, first_rank, last_rank, count, \
                    rank_bytes, metadata_bytes, associations, rank_codec, \
                    metadata_encoding, flags, reserved8, reserved32 = fields
                if (offset, first_record, first_rank, last_rank, count,
                        rank_bytes, metadata_bytes, associations, rank_codec,
                        metadata_encoding, flags) != (
                        block.offset, block.first_record, block.first_rank,
                        block.last_rank, block.count, block.rank_bytes,
                        block.metadata_bytes, block.associations,
                        block.rank_codec, block.metadata_encoding,
                        block.flags):
                    raise PoolError(
                        "complete BSP4 index entry %d differs" % number)
                if reserved8 or reserved32:
                    raise PoolError(
                        "complete BSP4 index entry %d has reserved data" %
                        number)
                continue
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

    @staticmethod
    def _decode_metadata4(payload: bytes, records: int,
                          expected_associations: int
                          ) -> List[Tuple[Occurrence, ...]]:
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
            if at >= len(payload):
                raise PoolError("metadata descriptor codec is truncated")
            codec = payload[at]
            at += 1
            indexes, at = _decode_bsp4_indexes(
                payload, at, codec, matches, records)
            if len(indexes) != matches:
                raise PoolError("metadata descriptor match count differs")
            for index in indexes:
                per_record[index].append(occurrence)
            associations += matches
        if at != len(payload) or associations != expected_associations:
            raise PoolError("metadata byte/association count differs")
        return [tuple(items) for items in per_record]

    def _decode_block_payload(
            self, payload: bytes, block: Block
            ) -> Tuple[List[int], Sequence[Tuple[Occurrence, ...]]]:
        if self.schema == 4:
            ranks = _decode_rank_codec(
                payload[:block.rank_bytes], block.count, block.first_rank,
                block.last_rank, block.rank_codec)
            occurrences = self._decode_metadata4(
                payload[block.rank_bytes:], block.count, block.associations)
        else:
            ranks = self._decode_ranks(
                payload[:block.rank_bytes], block.count, block.first_rank,
                block.last_rank)
        if self.schema == 3:
            occurrences = self._decode_metadata(
                payload[block.rank_bytes:], block.count, block.associations)
        elif self.schema == 2:
            occurrences = [tuple() for _ in range(block.count)]
        return ranks, occurrences

    def _read_block_records(self, handle, block: Block) -> Tuple[Record, ...]:
        payload = handle_read(handle, block.offset + block.header_bytes,
                              block.payload_bytes)
        ranks, occurrences = self._decode_block_payload(payload, block)
        return tuple(Record(rank, items)
                     for rank, items in zip(ranks, occurrences))

    def _read_validated_block_records(
            self, handle, block: Block, membership: int, metadata: int
            ) -> Tuple[Tuple[Record, ...], int, int]:
        header = handle_read(handle, block.offset, block.header_bytes)
        if (self.schema == 3
                and block.offset in self._repaired_bsp3_headers):
            header = BSP3_HEADER_PREFIX + header[8:]
        payload = handle_read(
            handle, block.offset + block.header_bytes, block.payload_bytes)
        if self.schema == 4:
            fields = struct.unpack("<4sBBBBIIIIQQQQQ", header)
            magic, size, rank_codec, metadata_encoding, flags, count, \
                rank_bytes, metadata_bytes, associations, first, last, \
                rank_checksum, metadata_checksum, reserved = fields
            if ((magic, size, rank_codec, metadata_encoding, flags, count,
                 rank_bytes, metadata_bytes, associations, first, last,
                 reserved)
                    != (b"BSP4", block.header_bytes, block.rank_codec,
                        block.metadata_encoding, block.flags, block.count,
                        block.rank_bytes, block.metadata_bytes,
                        block.associations, block.first_rank, block.last_rank,
                        0)):
                raise PoolError(
                    "committed BSP4 block header changed at byte %d" %
                    block.offset)
            rank_payload = payload[:block.rank_bytes]
            metadata_payload = payload[block.rank_bytes:]
            calculated = crc64(
                header[4:6] + header[8:16] + header[24:40])
            calculated = crc64(rank_payload, calculated)
            if calculated != rank_checksum:
                raise PoolError(
                    "BSP4 rank checksum differs at byte %d" % block.offset)
            calculated = crc64(
                header[4:5] + header[6:7]
                + header[8:12] + header[16:24])
            calculated = crc64(metadata_payload, calculated)
            if calculated != metadata_checksum:
                raise PoolError(
                    "BSP4 metadata checksum differs at byte %d" %
                    block.offset)
        elif self.schema == 3:
            fields = struct.unpack("<4sBBBBIIIIQQQ", header)
            magic, size, zero1, zero2, zero3, count, rank_bytes, \
                metadata_bytes, associations, first, last, checksum = fields
            if ((magic, size, zero1, zero2, zero3, count, rank_bytes,
                 metadata_bytes, associations, first, last)
                    != (b"BSP3", block.header_bytes, 0, 0, 0, block.count,
                        block.rank_bytes, block.metadata_bytes,
                        block.associations, block.first_rank,
                        block.last_rank)):
                raise PoolError(
                    "committed BSP3 block header changed at byte %d" %
                    block.offset)
            calculated = crc64(header[4:40])
            calculated = crc64(payload, calculated)
            if calculated != checksum:
                raise PoolError(
                    "BSP3 block checksum differs at byte %d" % block.offset)
        else:
            magic, count, rank_bytes, checksum, first, last = struct.unpack(
                "<4sIIIQQ", header)
            if ((magic, count, rank_bytes, first, last)
                    != (b"BSP2", block.count, block.rank_bytes,
                        block.first_rank, block.last_rank)):
                raise PoolError(
                    "committed BSP2 block header changed at byte %d" %
                    block.offset)
            if fnv32(payload) != checksum:
                raise PoolError(
                    "BSP2 block checksum differs at byte %d" % block.offset)

        ranks, occurrences = self._decode_block_payload(payload, block)
        for rank in ranks:
            if rank < self.range_start or rank >= self.range_end:
                raise PoolError(
                    "pool contains a rank outside its declared range")
        if self.is_composite:
            self._validate_composite_metadata(occurrences)
        if self.schema == 4:
            membership = _bsp4_update_membership_digest(membership, ranks)
            metadata = _bsp4_update_metadata_digest(metadata, occurrences)
        else:
            membership = fnv64(header, membership)
            membership = fnv64(payload, membership)
            if self.schema == 3:
                metadata = fnv64(payload[block.rank_bytes:], metadata)
        records = tuple(Record(rank, items)
                        for rank, items in zip(ranks, occurrences))
        return records, membership, metadata

    def _digest_starts(self) -> Tuple[int, int]:
        return (
            _bsp4_membership_start()
            if self.schema == 4 else FNV64_OFFSET,
            _bsp4_metadata_start()
            if self.schema == 4
            else (FNV64_OFFSET if self.schema == 3 else 0),
        )

    def _finish_payload_verification(
            self, membership: int, metadata: int) -> None:
        self.membership_digest = membership
        self.metadata_digest = metadata
        self._validate_header_digests()
        self._payload_verified = True

    def _verify_all_payloads(
            self,
            cancel_check: Optional[Callable[[], bool]] = None) -> None:
        current_cancel = cancel_check \
            if cancel_check is not None else self.cancel_check
        with self._verification_lock:
            if self._payload_verified:
                return
            membership, metadata = self._digest_starts()
            with self._open_source_snapshot(current_cancel) as handle:
                for block in self.blocks:
                    _check_cancel(current_cancel)
                    _records, membership, metadata = \
                        self._read_validated_block_records(
                            handle, block, membership, metadata)
            _check_cancel(current_cancel)
            self._finish_payload_verification(membership, metadata)

    def accept_native_verification(
            self, membership: int, metadata: int) -> None:
        """Accept digests from a native full-payload validation pass."""
        with self._verification_lock:
            self._finish_payload_verification(membership, metadata)

    def iter_records(
            self,
            cancel_check: Optional[Callable[[], bool]] = None
            ) -> Iterator[Record]:
        """Yield records in rank order, independent of physical block order.

        Native multi-threaded writers commit each block atomically, but worker
        completion order is intentionally nondeterministic. Every individual
        block is sorted; the file as a whole need not be. Disjoint block
        intervals can simply be traversed by first rank, keeping one decoded
        block in memory. Only overlapping intervals need the record-level heap
        and bounded decoded-block cache.
        """
        if not self.blocks:
            return
        current_cancel = cancel_check \
            if cancel_check is not None else self.cancel_check
        physically_rank_ordered, disjoint_intervals = \
            self.blocks.physical_rank_order()
        if physically_rank_ordered:
            ordered_blocks = self.blocks
        else:
            # Historical multi-threaded writers may have committed otherwise
            # valid blocks out of rank order. Materialize only that uncommon
            # case because sorting necessarily needs random descriptor access.
            ordered_blocks = sorted(
                self.blocks,
                key=lambda block: (block.first_rank, block.last_rank))
            disjoint_intervals = all(
                ordered_blocks[index - 1].last_rank
                < ordered_blocks[index].first_rank
                for index in range(1, len(ordered_blocks)))
        if not self._payload_verified:
            with self._verification_lock:
                if not self._payload_verified:
                    # Logical BSP4/BSP3 digests follow physical block order.
                    # A physically rank-ordered file can therefore validate
                    # and yield each payload exactly once. Shuffled/overlapping
                    # files take the conservative physical verification pass
                    # first, then use the bounded ordered traversal below.
                    if disjoint_intervals and physically_rank_ordered:
                        membership, metadata = self._digest_starts()
                        prior = None
                        with self._open_source_snapshot(
                                current_cancel) as handle:
                            for block in self.blocks:
                                _check_cancel(current_cancel)
                                records, membership, metadata = \
                                    self._read_validated_block_records(
                                        handle, block, membership, metadata)
                                for record in records:
                                    if (prior is not None
                                            and record.rank <= prior):
                                        raise PoolError(
                                            "pool repeats or misorders a rank "
                                            "across blocks")
                                    prior = record.rank
                                    yield record
                        _check_cancel(current_cancel)
                        self._finish_payload_verification(
                            membership, metadata)
                        return
                    self._verify_all_payloads(current_cancel)
        with self._open_source_snapshot(current_cancel) as handle:
            if disjoint_intervals:
                prior = None
                for block in ordered_blocks:
                    _check_cancel(current_cancel)
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
                # Charge the measured one-occurrence case while always
                # retaining the one block currently being consumed.
                weight = max(
                    block.payload_bytes * 4,
                    block.count * DECODED_RECORD_CACHE_BYTES)
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
            processed = 0
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
                processed += 1
                if processed % CANCEL_CHECK_RECORDS == 0:
                    _check_cancel(current_cancel)
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


def _read_pool_header_text_from_handle(handle) -> str:
    """Read the builder-compatible bounded header from one pinned file."""
    prefix = handle_read(handle, 0, HEADER_PREFIX_BYTES)
    raw = prefix
    magic = re.match(
        br"^BRAINSTORM_SEED_POOL[ \t]+([0-9]+)[ \t]*(?:\r?\n|\x00|$)",
        prefix)
    schema = int(magic.group(1)) if magic else 0
    if schema in EVENT_POOL_SCHEMAS:
        size_line = re.search(
            br"(?:^|\n)header_bytes[ \t]+([0-9]+)[ \t]*(?:\r?\n|\x00|$)",
            prefix)
        if not size_line:
            return ""
        header_bytes = int(size_line.group(1))
        if not (HEADER_PREFIX_BYTES <= header_bytes <= HEADER_MAX_BYTES):
            return ""
        if header_bytes > len(prefix):
            raw += handle_read(
                handle, len(prefix), header_bytes - len(prefix))
        else:
            raw = raw[:header_bytes]
    text = raw.split(b"\0", 1)[0].decode("latin-1")
    if schema == 4 and not re.search(
            r"(?:^|\n)encoding[ \t]+adaptive-events-v1"
            r"[ \t]*(?:\r?\n|$)", text):
        return ""
    return text


def record_categories(record: Record, selected: Optional[set] = None) -> List[str]:
    categories = {item.category_id for item in record.occurrences if item.known}
    categories.discard(None)
    if selected is not None:
        categories.intersection_update(selected)
    return sorted(categories)


def record_locations(record: Record, filter_id: str,
                     selected: Optional[set] = None) -> List[str]:
    locations = {}
    for item in record.occurrences:
        if (not item.known or item.filter_id != filter_id
                or (selected is not None
                    and item.location_id not in selected)):
            continue
        prior = locations.get(item.location_id)
        if prior is None or item.location_sort_key < prior:
            locations[item.location_id] = item.location_sort_key
    return sorted(locations, key=lambda location: (
        locations[location], location))


def _category_row_sort_key(item: Dict[str, object]) -> Tuple[object, ...]:
    phase = str(item.get("phase", ""))
    source = str(item.get("source", ""))
    return (
        str(item.get("filter_label", "")).casefold(),
        int(item.get("ante", MASK64)),
        PHASE_SORT_ORDER.get(phase, 1000),
        SOURCE_SORT_ORDER.get(source, 1000),
        int(item.get("ordinal", 0)),
        str(item.get("flags", "")),
        str(item.get("kind", "")),
        str(item.get("key", "")),
        str(item.get("category_id", "")),
    )


def build_filter_report(
        category_rows: Sequence[Dict[str, object]],
        location_counts: Dict[str, int],
        filter_covered: Dict[str, int],
        filter_multiple: Dict[str, int],
        filter_associations: Dict[str, int],
        total_records: int,
        composite: bool = False
        ) -> Tuple[List[Dict[str, object]], str]:
    """Build the small user-facing filter/location index for Inspect.

    Counts are per-record unions, not sums of exact descriptor counts. A seed
    may carry several source/ordinal/flag variants for one visible location,
    so callers must compute these counters while traversing records (or with
    the native summary's equivalent transpose).
    """
    location_details = {}
    location_categories = collections.defaultdict(set)
    for row in category_rows:
        filter_id = row.get("filter_id")
        location_id = row.get("location_id")
        category_id = row.get("category_id")
        if not all(isinstance(value, str) and value
                   for value in (filter_id, location_id, category_id)):
            continue
        location_categories[location_id].add(category_id)
        if location_id not in location_details:
            location_details[location_id] = {
                "location_id": location_id,
                "label": row.get("location_label") or row.get("label"),
                "filter_id": filter_id,
                "filter_label": row.get("filter_label") or filter_id,
                "kind": row.get("kind"),
                "key": row.get("key"),
                "ante": row.get("ante"),
                "phase": row.get("phase"),
            }
    filters = {}
    for location_id, count in location_counts.items():
        details = location_details.get(location_id)
        if details is None:
            continue
        filter_id = details["filter_id"]
        location = dict(details)
        location["records"] = int(count)
        location["exact_category_ids"] = sorted(
            location_categories.get(location_id, ()))
        filters.setdefault(filter_id, {
            "filter_id": filter_id,
            "label": details["filter_label"],
            "kind": details["kind"],
            "key": details["key"],
            "locations": [],
        })["locations"].append(location)
    rows = []
    for filter_id, item in filters.items():
        item["locations"].sort(key=lambda location: (
            int(location.get("ante", MASK64)),
            PHASE_SORT_ORDER.get(str(location.get("phase", "")), 1000),
            str(location.get("location_id", "")),
        ))
        covered = int(filter_covered.get(filter_id, 0))
        associations = int(filter_associations.get(filter_id, 0))
        item.update({
            "covered_records": covered,
            "unmatched_records": max(0, int(total_records) - covered),
            "multiple_location_records": int(
                filter_multiple.get(filter_id, 0)),
            "location_associations": associations,
            "extra_location_associations": max(0, associations - covered),
            "location_count": len(item["locations"]),
        })
        rows.append(item)
    rows.sort(key=lambda item: (
        item["label"].casefold(), item["filter_id"]))
    recommended = ""
    if rows and not composite:
        recommended = min(rows, key=lambda item: (
            item["covered_records"] != total_records,
            item["unmatched_records"],
            item["multiple_location_records"],
            item["extra_location_associations"],
            item["location_count"],
            item["label"].casefold(),
            item["filter_id"],
        ))["filter_id"]
    return rows, recommended


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
        "legendary_routes": reader.header.one(
            "legendary_routes", required=False, default="full"),
        "route_legendary_routes": reader.header.one(
            "route_legendary_routes", required=False, default="full"),
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
            ambiguity_limit: Optional[int] = None,
            cancel_check: Optional[Callable[[], bool]] = None) -> Dict[str, object]:
    _check_cancel(cancel_check)
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
    location_counts = collections.Counter()
    filter_covered = collections.Counter()
    filter_multiple = collections.Counter()
    filter_associations = collections.Counter()
    # Descriptor text is block-local but normally repeats for hundreds of
    # thousands (or millions) of records.  Resolve each raw descriptor once.
    # Besides avoiding repeated formatting, do not use
    # ``details.setdefault(key, item.as_dict())`` here: Python evaluates the
    # default eagerly, which previously rebuilt the full label/dictionary for
    # every occurrence even after the category was already known.
    category_ids = {}
    processed = 0
    for record in reader.iter_records(cancel_check=cancel_check):
        categories_set = set()
        filter_locations = collections.defaultdict(set)
        record_provenance = set()
        record_operands = set()
        for item in record.occurrences:
            if item.known:
                category = category_ids.get(item.raw)
                if category is None:
                    category = item.category_id
                    category_ids[item.raw] = category
                if selected is None or category in selected:
                    categories_set.add(category)
                    if category not in details:
                        details[category] = item.as_dict()
                filter_locations[item.filter_id].add(item.location_id)
                continue
            provenance_id = item.provenance_id
            if provenance_id is not None:
                record_provenance.add(provenance_id)
                continue
            operand_id = item.operand_id
            if operand_id is not None:
                record_operands.add(operand_id)
            else:
                opaque_associations += 1
        categories = sorted(categories_set)
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
        for filter_id, locations in filter_locations.items():
            filter_covered[filter_id] += 1
            filter_associations[filter_id] += len(locations)
            if len(locations) > 1:
                filter_multiple[filter_id] += 1
            for location in locations:
                location_counts[location] += 1
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
        processed += 1
        if processed % CANCEL_CHECK_RECORDS == 0:
            _check_cancel(cancel_check)
    _check_cancel(cancel_check)
    categories = []
    for category in counts:
        item = dict(details[category])
        item["records"] = counts[category]
        categories.append(item)
    categories.sort(key=_category_row_sort_key)
    filters, recommended = build_filter_report(
        categories, location_counts, filter_covered, filter_multiple,
        filter_associations, reader.records, reader.is_composite)
    return {
        "organizer_schema": 1,
        "source": source_summary(reader),
        "categories": categories,
        "filters": filters,
        "recommended_filter_id": recommended,
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
    write_records = BSP3_WRITE_RECORDS

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
        if len(self.buffer) >= self.write_records:
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
            BSP4_RANK_POSITIVE, 0, 0,
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


class BSP4OutputWriter(BSP3OutputWriter):
    """Adaptive event-pool writer with the BSP3 add/abort lifecycle."""

    write_records = BSP4_WRITE_RECORDS

    def __init__(self, reader: BSPoolReader, category_id: str, label: str,
                 final_path: str, header_bytes: Optional[int] = None,
                 header_builder=None, allow_empty: bool = False):
        super().__init__(
            reader, category_id, label, final_path, header_bytes,
            header_builder, allow_empty)
        self.membership_digest = _bsp4_membership_start()
        self.metadata_digest = _bsp4_metadata_start()

    def _flush(self) -> None:
        if not self.buffer:
            return
        records = sorted(self.buffer, key=lambda item: item.rank)
        if any(records[index - 1].rank >= records[index].rank
               for index in range(1, len(records))):
            raise PoolError("output category contains duplicate ranks")
        ranks = [record.rank for record in records]
        per_record = [record.occurrences for record in records]
        rank_codec, rank_payload, canonical_ranks = \
            _encode_adaptive_ranks_and_canonical(ranks)
        metadata, canonical_metadata, associations = \
            _encode_bsp4_metadata_and_canonical(per_record)
        if not metadata or len(metadata) > MAX_METADATA_BYTES:
            raise PoolError("output metadata block is too large")

        offset = self.handle.tell()
        header = bytearray(BLOCK4_HEADER_BYTES)
        header[:4] = b"BSP4"
        header[4] = BLOCK4_HEADER_BYTES
        header[5] = rank_codec
        header[6] = BSP4_METADATA_ADAPTIVE
        # Byte 7 and the final u64 are reserved and remain zero.
        struct.pack_into(
            "<IIIIQQ", header, 8, len(records), len(rank_payload),
            len(metadata), associations, ranks[0], ranks[-1])
        rank_checksum = crc64(
            bytes(header[4:6] + header[8:16] + header[24:40]))
        rank_checksum = crc64(rank_payload, rank_checksum)
        metadata_checksum = crc64(
            bytes(header[4:5] + header[6:7]
                  + header[8:12] + header[16:24]))
        metadata_checksum = crc64(metadata, metadata_checksum)
        struct.pack_into("<QQ", header, 40, rank_checksum, metadata_checksum)

        self.handle.write(header)
        self.handle.write(rank_payload)
        self.handle.write(metadata)
        self.membership_digest = _bsp4_update_membership_digest(
            self.membership_digest, ranks, canonical=canonical_ranks)
        self.metadata_digest = _bsp4_update_metadata_digest(
            self.metadata_digest, per_record, canonical=canonical_metadata,
            associations=associations)
        self.blocks.append(Block(
            offset, self.records, len(records), len(rank_payload),
            len(metadata), associations, ranks[0], ranks[-1],
            BLOCK4_HEADER_BYTES, rank_codec, BSP4_METADATA_ADAPTIVE, 0,
        ))
        self.records += len(records)
        self.data_bytes += (
            BLOCK4_HEADER_BYTES + len(rank_payload) + len(metadata))
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
                "<QQQQIIIIBBBBI", block.offset, block.first_record,
                block.first_rank, block.last_rank, block.count,
                block.rank_bytes, block.metadata_bytes, block.associations,
                block.rank_codec, block.metadata_encoding, block.flags, 0, 0))
        footer = bytearray(FOOTER4_BYTES)
        footer[:8] = b"BSPIDX4\n"
        struct.pack_into(
            "<QQQQQQ", footer, 8, index_offset, len(self.blocks),
            self.records, self.data_bytes, self.membership_digest,
            self.metadata_digest)
        struct.pack_into("<Q", footer, 88, crc64(bytes(footer[:88])))
        self.handle.write(footer)
        if self.header_builder is None:
            header, identity = build_output_header(
                self.reader, self.category_id, self.label, self.records,
                self.data_bytes, self.membership_digest, self.metadata_digest,
                schema=4)
        else:
            header, identity = self.header_builder(
                self.records, self.data_bytes, self.membership_digest,
                self.metadata_digest)
        if len(header) != self.header_bytes:
            raise PoolError(
                "organizer output header builder returned the wrong size")
        if not header.startswith(b"BRAINSTORM_SEED_POOL 4"):
            raise PoolError("BSP4 output header builder returned another schema")
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


def _hex_header(reader: BSPoolReader, key: str, default: int = 0) -> int:
    return reader.header.integer(key, 16, required=False, default=default)


def build_output_header(reader: BSPoolReader, category_id: str, label: str,
                        records: int, data_bytes: int, membership: int,
                        metadata: int, schema: int = 3
                        ) -> Tuple[bytes, Dict[str, object]]:
    if schema not in EVENT_POOL_SCHEMAS:
        raise PoolError("organizer event output schema is invalid")
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
        "BRAINSTORM_SEED_POOL %d" % schema,
        "modelver %d" % modelver,
        "encoding %s" % POOL_ENCODINGS[schema],
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
        "BRAINSTORM_SEED_POOL 4",
        "modelver %d" % first.modelver,
        "encoding adaptive-events-v1",
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
        raise PoolError("composite metadata exceeds the maximum BSP4 header size")
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
    raise PoolError("composite branch/filter metadata will not fit in a BSP4 header")


class _CombineRecordNormalizer:
    """Validate provenance while retaining already decoded descriptors."""

    __slots__ = (
        "context", "allowed_sets", "declared_operand_sets",
        "direct_provenance", "output_operands")

    def __init__(self, context: CombineContext):
        self.context = context
        self.allowed_sets = tuple(
            frozenset(values) for values in context.source_branch_ids)
        self.declared_operand_sets = tuple(
            frozenset(reader.composite_operands)
            for reader in context.readers)
        self.direct_provenance = tuple(
            None if reader.is_composite else Occurrence.decode(
                provenance_descriptor(next(iter(self.allowed_sets[index]))))
            for index, reader in enumerate(context.readers))
        self.output_operands = tuple(
            Occurrence.decode(operand_descriptor(operand.operand_id))
            for operand in context.operands)

    def normalize(self, source_index: int, record: Record) -> Record:
        reader = self.context.readers[source_index]
        by_raw = {}
        provenance = set()
        source_operands = set()
        for item in record.occurrences:
            operand_id = item.operand_id
            if operand_id is not None:
                source_operands.add(operand_id)
                continue
            by_raw.setdefault(item.raw, item)
            provenance_id = item.provenance_id
            if provenance_id is not None:
                provenance.add(provenance_id)
        if reader.is_composite:
            if not provenance:
                raise PoolError(
                    "composite source %s has a seed without provenance" %
                    os.path.basename(reader.path))
            if provenance - self.allowed_sets[source_index]:
                raise PoolError(
                    "composite source %s references an undeclared branch" %
                    os.path.basename(reader.path))
            if (not source_operands
                    or source_operands
                    - self.declared_operand_sets[source_index]):
                raise PoolError(
                    "composite source %s has missing or undeclared operand provenance" %
                    os.path.basename(reader.path))
            if not expression_matches(
                    reader.composite_expression, source_operands):
                raise PoolError(
                    "composite source %s has operand provenance that does not satisfy its expression" %
                    os.path.basename(reader.path))
        else:
            if provenance or source_operands:
                raise PoolError(
                    "non-composite source contains undeclared provenance metadata")
            item = self.direct_provenance[source_index]
            by_raw[item.raw] = item
        return Record(
            record.rank, tuple(by_raw[raw] for raw in sorted(by_raw)))

    def merged(self, rank: int,
               group: Sequence[Tuple[int, Record]], present: set) -> Record:
        by_raw = {
            item.raw: item
            for _index, record in group
            for item in record.occurrences
        }
        for index in present:
            item = self.output_operands[index]
            by_raw[item.raw] = item
        return Record(rank, tuple(by_raw[raw] for raw in sorted(by_raw)))


def _normalized_combine_record(context: CombineContext, source_index: int,
                               record: Record,
                               normalizer: Optional[
                                   _CombineRecordNormalizer] = None) -> Record:
    return (normalizer or _CombineRecordNormalizer(context)).normalize(
        source_index, record)


def _iter_combined_records(context: CombineContext,
                           progress=None,
                           cancel_check: Optional[Callable[[], bool]] = None
                           ) -> Iterator[Record]:
    _check_cancel(cancel_check)
    iterators = [
        iter(reader.iter_records(cancel_check=cancel_check))
        for reader in context.readers
    ]
    normalizer = _CombineRecordNormalizer(context)
    heap = []
    consumed = 0
    next_cancel_check = CANCEL_CHECK_RECORDS
    for index, iterator in enumerate(iterators):
        try:
            record = _normalized_combine_record(
                context, index, next(iterator), normalizer)
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
                    context, index, next(iterators[index]), normalizer)
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
            yield normalizer.merged(rank, group, present)
        if consumed >= next_cancel_check:
            _check_cancel(cancel_check)
            next_cancel_check = consumed + CANCEL_CHECK_RECORDS
        if progress is not None and consumed % 100000 == 0:
            progress(consumed)
    _check_cancel(cancel_check)


def combine_pools(readers: Sequence[BSPoolReader], output_path: str,
                  operation: str = "union", label: str = "Combined seed pool",
                  progress=None,
                  cancel_check: Optional[Callable[[], bool]] = None
                  ) -> Dict[str, object]:
    """Stream a literal set operation and atomically publish one BSP4 pool."""
    _check_cancel(cancel_check)
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with pool_writer_guard(output_path):
        return _combine_pools_locked(
            readers, output_path, operation, label, progress, cancel_check)


def _combine_pools_locked(readers: Sequence[BSPoolReader], output_path: str,
                          operation: str, label: str,
                          progress=None,
                          cancel_check: Optional[Callable[[], bool]] = None
                          ) -> Dict[str, object]:
    _check_cancel(cancel_check)
    context = prepare_combine(readers, operation)
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

    writer = BSP4OutputWriter(
        context.readers[0], "composite:%s" % context.operation, label,
        output_path, header_bytes=header_bytes,
        header_builder=header_builder, allow_empty=True)
    published = False
    try:
        for record in _iter_combined_records(
                context, progress=progress, cancel_check=cancel_check):
            writer.add(record)
        _check_cancel(cancel_check)
        identity = writer.finalize()
        _check_cancel(cancel_check)
        os.link(writer.temp_path, output_path)
        published = True
        os.unlink(writer.temp_path)
        fsync_directory(directory)
        result = context.as_dict()
        result.update(identity)
        result.update({
            "path": output_path,
            "records": writer.records,
            "header_bytes": header_bytes,
            "completed": True,
        })
        return result
    except BaseException:
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


def _check_split_output_limit(output_count: int) -> None:
    if output_count > MAX_SPLIT_OUTPUTS:
        raise PoolError(
            "split would create %d non-empty pools; choose fewer categories "
            "or run separate splits (maximum %d outputs per publication)" %
            (output_count, MAX_SPLIT_OUTPUTS))


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
        fsync_directory(directory)
    except BaseException:
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
    if not choices:
        return None, None
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
               omit_unmatched: bool,
               cancel_check: Optional[Callable[[], bool]] = None
               ) -> Tuple[Dict[str, object], bool]:
    _check_cancel(cancel_check)
    if not reader.metadata_capable:
        raise PoolError("BSP2 has no per-seed occurrence metadata; refilter/rescan into BSP4 before splitting")
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    choices = load_choices(choices_path, reader)
    all_summary = analyze(
        reader, ambiguity_limit=0, cancel_check=cancel_check)
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
    processed = 0
    for record in reader.iter_records(cancel_check=cancel_check):
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
        processed += 1
        if processed % CANCEL_CHECK_RECORDS == 0:
            _check_cancel(cancel_check)
    _check_cancel(cancel_check)
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
    _check_split_output_limit(len(category_rows))
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
    _check_cancel(cancel_check)
    atomic_json(report_path, report)
    if unresolved or (unmatched and not remainder_id and not omit_unmatched):
        return report, False

    return write_prepared_split(
        reader, output_dir, sorted(selected), choices, category_rows, report,
        report_path, remainder_id=remainder_id, cancel_check=cancel_check,
        ambiguity_rules=None)


def write_prepared_split(
        reader: BSPoolReader, output_dir: str, selected_ids: Sequence[str],
        choices: Dict[str, str], category_rows: Sequence[Dict[str, object]],
        report: Dict[str, object], report_path: str,
        remainder_id: Optional[str] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        ambiguity_rules: Optional[Dict[str, str]] = None,
        group_by_filter: Optional[str] = None
        ) -> Tuple[Dict[str, object], bool]:
    """Publish one already validated, snapshot-pinned split plan.

    ``category_rows`` supplies identities, labels, and exact output counts only.
    Output paths are always derived locally from those identities; caller-owned
    ``path`` or ``name`` fields are deliberately ignored.
    """
    _check_cancel(cancel_check)
    if not reader.metadata_capable:
        raise PoolError(
            "BSP2 has no per-seed occurrence metadata; refilter/rescan into BSP4 before splitting")
    if (isinstance(selected_ids, (str, bytes))
            or not isinstance(selected_ids, Sequence)):
        raise PoolError("prepared split categories must be a sequence")

    def checked_category_id(value, remainder=False):
        if (not isinstance(value, str) or not value or len(value) > 4096
                or any(ch.isspace() or ord(ch) < 33 or ord(ch) > 126
                       for ch in value)
                or "/" in value or "\\" in value):
            raise PoolError("prepared split contains an invalid category id")
        prefixes = ("remainder:",) if remainder else tuple(
            "%s:" % name for name in KIND_NAMES.values())
        if not value.startswith(prefixes):
            raise PoolError("prepared split contains an invalid category id")
        return value

    selected_values = [checked_category_id(value) for value in selected_ids]
    if not selected_values:
        raise PoolError("prepared split has no selected categories")
    if len(set(selected_values)) != len(selected_values):
        raise PoolError("prepared split repeats a selected category")
    selected = set(selected_values)
    if group_by_filter is not None:
        if (not isinstance(group_by_filter, str) or not group_by_filter
                or len(group_by_filter) > 4096
                or any(ch.isspace() or ord(ch) < 33 or ord(ch) > 126
                       for ch in group_by_filter)
                or "/" in group_by_filter or "\\" in group_by_filter
                or not group_by_filter.startswith(tuple(
                    "%s:" % name for name in KIND_NAMES.values()))):
            raise PoolError("prepared split has an invalid organizing filter")
        prefix = group_by_filter + ":A"
        if any(not category.startswith(prefix) for category in selected):
            raise PoolError(
                "prepared split location does not belong to its organizing filter")

    if remainder_id is not None:
        remainder_id = checked_category_id(remainder_id, remainder=True)
        if remainder_id in selected:
            raise PoolError("prepared split remainder duplicates a selected category")

    if not isinstance(choices, dict):
        raise PoolError("prepared split choices must be an object")
    checked_choices = {}
    for choice_index, (key, category) in enumerate(choices.items()):
        if choice_index % CANCEL_CHECK_RECORDS == 0:
            _check_cancel(cancel_check)
        if not isinstance(key, str) or not key or not isinstance(category, str):
            raise PoolError("prepared split choice keys and categories must be strings")
        category = checked_category_id(category)
        if category not in selected:
            raise PoolError("prepared split choice names an unselected category")
        checked_choices[key] = category

    if ambiguity_rules is None:
        checked_ambiguity_rules = {}
    elif not isinstance(ambiguity_rules, dict):
        raise PoolError("prepared split ambiguity rules must be an object")
    else:
        checked_ambiguity_rules = {}
        for rule_index, (key, category) in enumerate(ambiguity_rules.items()):
            if rule_index % CANCEL_CHECK_RECORDS == 0:
                _check_cancel(cancel_check)
            if (not isinstance(key, str)
                    or not re.fullmatch(r"[0-9a-f]{16}", key)
                    or not isinstance(category, str)):
                raise PoolError(
                    "prepared split contains an invalid ambiguity rule")
            category = checked_category_id(category)
            if category not in selected:
                raise PoolError(
                    "prepared split ambiguity rule names an unselected category")
            checked_ambiguity_rules[key] = category

    if (isinstance(category_rows, (str, bytes))
            or not isinstance(category_rows, Sequence)):
        raise PoolError("prepared split outputs must be a sequence")
    destinations = {}
    labels = {}
    expected_counts = {}
    clean_rows = []
    total_records = 0
    output_dir = os.path.abspath(os.fspath(output_dir))
    os.makedirs(output_dir, exist_ok=True)
    for row_index, row in enumerate(category_rows):
        if row_index % CANCEL_CHECK_RECORDS == 0:
            _check_cancel(cancel_check)
        if not isinstance(row, dict):
            raise PoolError("prepared split output must be an object")
        category = checked_category_id(
            row.get("category_id"), remainder=row.get("category_id") == remainder_id)
        if category not in selected and category != remainder_id:
            raise PoolError("prepared split output names an unselected category")
        if category in destinations:
            raise PoolError("prepared split repeats an output category")
        label = row.get("label")
        if (not isinstance(label, str) or not label.strip()
                or len(label) > 4096):
            raise PoolError("prepared split output has an invalid label")
        records = row.get("records")
        if (isinstance(records, bool) or not isinstance(records, int)
                or records <= 0 or records > reader.records):
            raise PoolError("prepared split output has an invalid record count")
        total_records += records
        if total_records > reader.records:
            raise PoolError("prepared split output counts exceed the source snapshot")
        filename = safe_filename(category)
        destination = os.path.abspath(os.path.join(output_dir, filename))
        if (filename != os.path.basename(filename)
                or os.path.commonpath((output_dir, destination)) != output_dir):
            raise PoolError("prepared split output escapes its destination folder")
        if destination in destinations.values():
            raise PoolError("two categories resolve to the same output filename")
        destinations[category] = destination
        labels[category] = label
        expected_counts[category] = records
        clean_rows.append({
            "category_id": category,
            "label": label,
            "records": records,
        })
    if not destinations:
        raise PoolError("prepared split has no non-empty outputs")
    _check_split_output_limit(len(destinations))

    if not isinstance(report, dict):
        raise PoolError("prepared split report must be an object")
    if str(report.get("source_snapshot_id", "")).lower() != reader.snapshot_token:
        raise PoolError("prepared split report is for a different source snapshot")
    if str(report.get("group_by_filter", "") or "") != (
            group_by_filter or ""):
        raise PoolError(
            "prepared split report organizing filter does not match its plan")
    declared_selected = report.get("selected_categories")
    if (not isinstance(declared_selected, list)
            or len(declared_selected) != len(selected_values)
            or set(declared_selected) != selected):
        raise PoolError("prepared split report categories do not match its plan")
    if report.get("unresolved_ambiguities", 0) != 0:
        raise PoolError("prepared split still has unresolved ambiguities")
    declared_rules = report.get("ambiguity_rules", {})
    if (not isinstance(declared_rules, dict)
            or declared_rules != checked_ambiguity_rules):
        raise PoolError(
            "prepared split report ambiguity rules do not match its plan")
    report["source"] = source_summary(reader)
    report["source_snapshot_id"] = reader.snapshot_token
    report["group_by_filter"] = group_by_filter or ""
    report["selected_categories"] = sorted(selected)
    report["categories"] = clean_rows
    report["outputs"] = []
    if ambiguity_rules is not None:
        report["ambiguity_rules"] = dict(checked_ambiguity_rules)

    report_path = os.path.abspath(os.fspath(report_path))
    forbidden_paths = {os.path.normcase(os.path.realpath(reader.path))}
    for destination in destinations.values():
        forbidden_paths.add(os.path.normcase(os.path.realpath(destination)))
        forbidden_paths.add(os.path.normcase(os.path.realpath(
            destination + ".writer.lock")))
    if os.path.normcase(os.path.realpath(report_path)) in forbidden_paths:
        raise PoolError("prepared split report path conflicts with a pool file")

    with ExitStack() as output_locks:
        _check_cancel(cancel_check)
        for destination in sorted(destinations.values()):
            output_locks.enter_context(pool_writer_guard(destination))
            if os.path.exists(destination):
                raise PoolError("output already exists: %s" % destination)
        return _write_split_outputs(
            reader, selected, checked_choices, remainder_id, destinations,
            labels, report, report_path, cancel_check,
            expected_counts=expected_counts,
            ambiguity_rules=checked_ambiguity_rules,
            group_by_filter=group_by_filter)


def _write_split_outputs(reader: BSPoolReader, selected: Sequence[str],
                         choices: Dict[str, str], remainder_id: Optional[str],
                         destinations: Dict[str, str], labels: Dict[str, str],
                         report: Dict[str, object], report_path: str,
                         cancel_check: Optional[Callable[[], bool]] = None,
                         expected_counts: Optional[Dict[str, int]] = None,
                         ambiguity_rules: Optional[Dict[str, str]] = None,
                         group_by_filter: Optional[str] = None):
    """Write and publish split outputs while the caller holds every lock."""
    _check_cancel(cancel_check)
    writers = {}  # type: Dict[str, BSP4OutputWriter]
    linked = []  # type: List[str]
    rules = ambiguity_rules or {}
    used_rules = set()
    try:
        processed = 0
        for record in reader.iter_records(cancel_check=cancel_check):
            candidates = record_locations(
                record, group_by_filter, selected) if group_by_filter \
                else record_categories(record, selected)
            if len(candidates) > 1:
                category, choice_key = choice_for_record(
                    choices, reader, record)
                rule_key = ambiguity_rule_key(candidates)
                if rule_key in rules:
                    used_rules.add(rule_key)
                    rule_destination = rules[rule_key]
                    if rule_destination not in candidates:
                        raise PoolError(
                            "prepared split ambiguity rule is not one of its candidates")
                    if choice_key is None:
                        category = rule_destination
                if category is None:
                    raise PoolError(
                        "prepared split still has an unresolved ambiguity")
                if category not in candidates:
                    raise PoolError(
                        "prepared split choice is not one of its candidates")
            elif len(candidates) == 1:
                category = candidates[0]
            else:
                category = remainder_id
            processed += 1
            if processed % CANCEL_CHECK_RECORDS == 0:
                _check_cancel(cancel_check)
            if category is None:
                continue
            if category not in destinations or category not in labels:
                raise PoolError(
                    "prepared split produced an unplanned output category")
            writer = writers.get(category)
            if writer is None:
                writer = BSP4OutputWriter(reader, category, labels[category],
                                          destinations[category])
                writers[category] = writer
            writer.add(record)
        unused_rules = sorted(set(rules) - used_rules)
        if unused_rules:
            raise PoolError(
                "prepared split contains an ambiguity rule not used by this split: %s" %
                unused_rules[0])
        outputs = []
        for category in sorted(writers):
            _check_cancel(cancel_check)
            outputs.append(writers[category].finalize())
        if expected_counts is not None:
            actual_counts = {
                output["category_id"]: output["records"] for output in outputs
            }
            if actual_counts != expected_counts:
                raise PoolError(
                    "prepared split output counts do not match its plan")
        # Hard-linking is a no-overwrite atomic publish on the same filesystem.
        # All files are finalized and fsynced before any becomes visible.
        _check_cancel(cancel_check)
        for category in sorted(writers):
            writer = writers[category]
            _check_cancel(cancel_check)
            os.link(writer.temp_path, writer.final_path)
            linked.append(writer.final_path)
        for writer in writers.values():
            os.unlink(writer.temp_path)
        output_directory = os.path.dirname(next(iter(destinations.values())))
        fsync_directory(output_directory)
        report["outputs"] = outputs
        _check_cancel(cancel_check)
        atomic_json(report_path, report)
        return report, True
    except BaseException:
        for path in linked:
            try:
                os.unlink(path)
            except OSError:
                pass
        if linked:
            fsync_directory(os.path.dirname(linked[0]))
        for writer in writers.values():
            writer.abort()
        raise


def command_inspect(args) -> int:
    reader = BSPoolReader(args.input, verify_payloads=False)
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
    reader = BSPoolReader(args.input, verify_payloads=False)

    def write_records(output) -> None:
        for record in reader.iter_records():
            value = {
                "seed": reader.seed(record.rank),
                "rank": record.rank,
                "occurrences": [
                    item.as_dict() for item in record.occurrences],
            }
            output.write(json.dumps(value, sort_keys=True) + "\n")

    if args.output == "-":
        # Standard output intentionally remains streaming: callers may pipe a
        # very large export without first staging another full-sized copy.
        write_records(sys.stdout)
        return 0

    destination = os.path.abspath(args.output)
    try:
        replaces_source = os.path.samefile(destination, reader.path)
    except FileNotFoundError:
        # The destination does not exist yet, so inode identity is
        # unavailable.  This fallback still catches a literal source path and
        # Windows case aliases without weakening the existing-file check.
        replaces_source = (
            os.path.normcase(os.path.realpath(destination))
            == os.path.normcase(os.path.realpath(reader.path)))
    except OSError:
        # Be conservative if the platform cannot stat an existing output.
        replaces_source = (
            os.path.normcase(os.path.realpath(destination))
            == os.path.normcase(os.path.realpath(reader.path)))
    if replaces_source:
        raise PoolError("records output cannot replace its source pool")
    directory = os.path.dirname(destination)
    fd, staged = tempfile.mkstemp(
        prefix=".organizer-records-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            write_records(output)
            # Deferred verification completes only when iter_records() is
            # exhausted. Sync the validated staging file before publishing it.
            output.flush()
            os.fsync(output.fileno())
        os.replace(staged, destination)
        staged = ""
        fsync_directory(directory)
    except BaseException:
        if staged:
            try:
                os.unlink(staged)
            except OSError:
                pass
        raise
    return 0


def command_split(args) -> int:
    # Split still needs a planning traversal and a staged-output traversal;
    # defer validation so its initial inspect traversal validates while it
    # consumes instead of adding a separate constructor pass.
    reader = BSPoolReader(args.input, verify_payloads=False)
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
    readers = [
        BSPoolReader(path, verify_payloads=False) for path in args.inputs]
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
