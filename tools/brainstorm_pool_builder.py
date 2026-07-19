#!/usr/bin/env python3
"""Brainstorm Seed Pool Builder -- interactive front end for
native/brainstorm_seed_pool.

A keyboard-driven settings screen in the spirit of the in-game Brainstorm
tab (cyclers + toggles) that writes the pool criteria for you, runs the
count-only estimate, and drives the multi-day exhaustive scan with live
progress, a clean pause (checkpointed; rerun to resume), and pools landing
directly in seed_pools/ where the mod's Seed Pool selector finds them.

Run it:
    python3 tools/brainstorm_pool_builder.py
or double-click "Seed Pool Builder.command" in the mod folder.

Headless modes (used by the test suite; no curses):
    python3 tools/brainstorm_pool_builder.py --headless-estimate
    python3 tools/brainstorm_pool_builder.py --headless-criteria

Only the Python standard library is used (curses ships with macOS python3).
On Windows there is no stdlib curses, so this file doubles as the engine for
the browser UI (tools/pool_builder_web.py) and its TUI politely declines.
"""

try:
    import curses
except ImportError:  # Windows: use tools/pool_builder_web.py instead
    curses = None
import hashlib
import hmac
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque

try:
    from pool_writer_lock import pool_writer_guard as _pool_writer_guard
except ImportError:  # Imported as tools.brainstorm_pool_builder.
    from tools.pool_writer_lock import pool_writer_guard as _pool_writer_guard

IS_FROZEN = bool(getattr(sys, "frozen", False))
if IS_FROZEN:
    # __file__ points into PyInstaller's temporary onefile extraction folder.
    # Keep the app location separate from the mod location: Windows releases
    # put both builder executables in <mod>/Seed Pool Builder/ so the active
    # SMODS folder is less cluttered.
    BUILDER_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BUILDER_DIR = os.path.dirname(os.path.abspath(__file__))


def _looks_like_mod_dir(path):
    return (os.path.isfile(os.path.join(path, "Brainstorm_main.lua"))
            and os.path.isfile(os.path.join(path, "manifest.json")))


def _find_mod_dir():
    """Find the active mod root without depending on its folder name.

    Source runs start in tools/, old frozen releases start in the mod root,
    and current Windows releases start in the Seed Pool Builder/ child.  An
    explicit environment override also lets a separately copied builder point
    at an unusual installation without changing any game files.
    """
    candidates = []
    override = os.environ.get("BRAINSTORM_MOD_DIR", "").strip()
    if override:
        candidates.append(os.path.abspath(os.path.expanduser(override)))
    current = BUILDER_DIR
    for _ in range(4):
        candidates.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for candidate in candidates:
        if _looks_like_mod_dir(candidate):
            return candidate
    # Preserve the old source/frozen fallback so preflight can give its normal
    # missing-file diagnostics rather than failing during module import.
    if IS_FROZEN:
        return BUILDER_DIR
    return os.path.dirname(BUILDER_DIR)


MOD_DIR = _find_mod_dir()
NATIVE_DIR = os.path.join(MOD_DIR, "native")
IS_WINDOWS = os.name == "nt"
_POOL_BINARY_NAME = "brainstorm_seed_pool" + (".exe" if IS_WINDOWS else "")
_POOL_BINARY_CANDIDATES = [
    # Current packaged Windows layout.
    os.path.join(BUILDER_DIR, _POOL_BINARY_NAME),
    # Allow a self-contained builder folder with a native/ child too.
    os.path.join(BUILDER_DIR, "native", _POOL_BINARY_NAME),
    # Source checkouts and Windows releases through win-v9.
    os.path.join(NATIVE_DIR, _POOL_BINARY_NAME),
]
POOL_BIN = next((path for path in _POOL_BINARY_CANDIDATES
                 if os.path.isfile(path)), _POOL_BINARY_CANDIDATES[-1])
SNAPSHOT = os.path.join(MOD_DIR, "native_search.cfg")
POOL_DIR = os.path.join(MOD_DIR, "seed_pools")
POOL_HEADER_PREFIX_BYTES = 1024
POOL_HEADER_MAX_BYTES = 256 * 1024
SEEDSPACE = 1785793904896  # 34^8: seeds the game's generator can deal
# 35^1 + ... + 35^8: every seed vanilla's seed box preserves -- adds O and
# 1-7 character seeds, but excludes 0 because vanilla remaps it to O.
SEEDSPACE_SETTABLE = 2318107019760
# 36^1 + ... + 36^8: all possible set seeds, including 0. Seeds containing 0
# require Brainstorm's Illegal Seed Input support instead of vanilla input.
SEEDSPACE_TOTAL = 2901713047668
SPACES = [
    ("natural", "Natural (34^8 -- seeds the game deals)", SEEDSPACE),
    ("settable", "All vanilla-settable (no 0; adds O + short seeds)",
     SEEDSPACE_SETTABLE),
    ("total", "All possible (includes 0/O + short seeds)", SEEDSPACE_TOTAL),
]
MAX_TAG_RULES = 16
MAX_VOUCHER_RULES = 8
MAX_VOUCHER_EXCLUSIONS = 16
MAX_VOUCHER_ANTE = 8
MAX_ANTE = 39
MODEL_VERSION = 6

# Friendly names, mirroring Brainstorm_UI's SearchTagList (fallback: raw key).
TAG_NAMES = {
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

VOUCHER_NAMES = {
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

SCOPES = [
    ("Entire seed space", 0),
    ("First 10M seeds", 10_000_000),
    ("First 100M seeds", 100_000_000),
    ("First 1B seeds", 1_000_000_000),
    ("First 10B seeds", 10_000_000_000),
]
# Estimates should answer in seconds even for expensive multi-Soul/voucher
# routes.  Two million observations are still a large statistical sample for
# ordinary filters; very rare results are explicitly reported as uncertain by
# the front ends instead of making every estimate scan 100M seeds.
ESTIMATE_COUNT = 2_000_000
ESTIMATE_CHECKPOINT = 262_144
BUILD_CHECKPOINT = 16_777_216
SHARD_COUNTS = (1, 2, 4, 8, 16, 32, 64, 128, 256)


def joker_name(key):
    return key[2:].replace("_", " ").title() if key.startswith("j_") else key


def voucher_name(key):
    if key in VOUCHER_NAMES:
        return VOUCHER_NAMES[key]
    return key[2:].replace("_", " ").title() if key.startswith("v_") else key


class Snapshot:
    """The catalog part of native_search.cfg: what can actually appear."""

    def __init__(self, path):
        self.path = path
        self.modelver = None
        self.has_specialdef = False
        self.has_boostdef = False
        self.bad_boostdef = False
        self.tag_reward_defs = {}
        self.tags = []        # (key, reqOk, minAnte)
        self.legendaries = [] # (key, avail)
        self.vouchers = []    # (key, fresh-run avail, prerequisite key or "")
        self.voucher_owned = set()
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                if parts[0] == "modelver" and len(parts) >= 2:
                    self.modelver = int(parts[1])
                elif parts[0] == "tagdef" and len(parts) >= 4:
                    self.tags.append((parts[1], parts[2] == "1", int(parts[3])))
                elif parts[0] == "jokerdef" and len(parts) >= 4 and parts[1] == "4":
                    self.legendaries.append((parts[2], parts[3] == "1"))
                elif parts[0] == "vouchroute" and len(parts) == 4:
                    self.vouchers.append((parts[1], parts[2] == "1",
                                          "" if parts[3] == "-" else parts[3]))
                elif parts[0] == "vouchowned" and len(parts) == 2:
                    self.voucher_owned.add(parts[1])
                elif parts[0] == "boostdef":
                    self.has_boostdef = True
                    self.bad_boostdef = self.bad_boostdef or len(parts) != 7
                    if len(parts) == 7 and parts[1] in (
                            "p_arcana_mega_1", "p_spectral_normal_1"):
                        try:
                            self.tag_reward_defs[parts[1]] = (int(parts[4]), parts[5])
                        except ValueError:
                            self.bad_boostdef = True
                elif parts[0] == "specialdef" and len(parts) == 3:
                    self.has_specialdef = True

    def usable_tags(self):
        return [(k, ma) for k, ok, ma in self.tags if ok]

    def usable_legendaries(self):
        return [k for k, ok in self.legendaries if ok]

    def usable_vouchers(self):
        # A target is useful only when its complete prerequisite chain can be
        # reached from fresh eligible bases or a deck/challenge starting
        # voucher. Keep catalog order for exact RNG index presentation.
        reachable = set(self.voucher_owned)
        changed = True
        while changed:
            changed = False
            for key, ok, prerequisite in self.vouchers:
                if (ok and key not in reachable
                        and (not prerequisite or prerequisite in reachable)):
                    reachable.add(key)
                    changed = True
        return [(key, prerequisite) for key, ok, prerequisite in self.vouchers
                if ok and key in reachable and key not in self.voucher_owned]

    def current_model_copy(self):
        """Return a current snapshot or refuse stale catalog semantics.

        Model 6 uses the snapshotted Charm/Ethereal reward-pack definitions in
        the chronological Soul route, so changing only the version number can
        fabricate profile data and produce a false pool. Launch one in-game
        native search to refresh native_search.cfg.
        """
        rewards_ok = (self.tag_reward_defs.get("p_arcana_mega_1", (0, ""))[0] > 0
                      and self.tag_reward_defs.get("p_arcana_mega_1", (0, ""))[1] == "A"
                      and self.tag_reward_defs.get("p_spectral_normal_1", (0, ""))[0] > 0
                      and self.tag_reward_defs.get("p_spectral_normal_1", (0, ""))[1] == "S")
        if (self.modelver == MODEL_VERSION and self.has_specialdef and self.has_boostdef
                and not self.bad_boostdef and rewards_ok):
            return self.path
        raise ValueError("native_search.cfg is stale or missing current profile data. "
                         "Launch Balatro, toggle Ctrl+A on and off once, then "
                         "reopen the Seed Pool Builder.")

# Soul search depth in UI order. 1 = the first Soul only; 0 ("any") = up to
# two Souls deep -- the first Soul, or failing that the second. The legacy
# exclusive value 2 (second Soul ONLY) is engine-readable but not offered.
SOUL_DEPTHS = [1, 0]
SOUL_DEPTH_LABELS = ["1 Soul deep (the first Soul)",
                     "2 Souls deep (first or second Soul)"]
LEGENDARY_ROUTES = ["full", "canonical_charm"]
LEGENDARY_ROUTE_LABELS = [
    "Exhaustive routes (Shop + Charm + Omen)",
    "Fast exact (Shop + Charm; skip automatic Omen purchase)",
]
PHASES = ["small", "big", "boss"]
TAG_PHASES = ["small", "big"]
PHASE_LABELS = {"small": "Small", "big": "Big", "boss": "Boss"}
LEGENDARY_SOURCES = ["any", "shop", "charm", "ethereal"]
LEGENDARY_SOURCE_LABELS = ["Any pack source", "Shop packs only",
                           "Charm Tag reward only", "Ethereal Tag reward only"]


def route_position(ante, phase):
    """Chronological human route coordinate: Small -> Big -> Boss."""
    return int(ante) * 3 + {"small": 0, "big": 1, "boss": 2}[phase]


def tag_rule_parts(rule):
    """Accept older four-field UI/test rules while adding blind boundaries."""
    key, lo, hi, count = rule[:4]
    min_phase = rule[4] if len(rule) > 4 else "small"
    max_phase = rule[5] if len(rule) > 5 else "big"
    return key, lo, hi, count, min_phase, max_phase


def tag_location_count(lo, min_phase, hi, max_phase):
    return sum(route_position(lo, min_phase) <= route_position(ante, phase)
               <= route_position(hi, max_phase)
               for ante in range(lo, hi + 1) for phase in TAG_PHASES)


class Criteria:
    """Everything the criteria file expresses, held as UI state."""

    def __init__(self):
        self.legendary = ""      # "" = none
        self.leg_min = 1
        self.leg_min_phase = "small"
        self.leg_max = 8
        self.leg_max_phase = "big"
        self.leg_source = "any"
        self.leg_human_location = True
        self.leg_neg = False
        self.leg_soul_depth = 1  # 1 = first Soul only, 0 = up to 2 Souls deep
        self.leg_routes = "full"  # full or canonical_charm (exact subset)
        self.tag_rules = []      # [key, min, max, count, min_phase, max_phase]
        self.voucher_rules = []  # [key, min_ante, max_ante]
        # Vouchers here may still be offered, but a qualifying route may not
        # purchase them.  This is deliberately separate from voucher_rules so
        # a target can itself be marked "must be found without buying it".
        self.voucher_exclusions = []  # [key]
        self.route_collect = True
        self.threads = 0         # 0 = auto
        self.scope = 0           # index into SCOPES
        self.space = "natural"   # one of the SPACES keys above
        self.shard_total = 1     # exact non-overlapping distributed parts
        self.shard_index = 1     # this machine's 1-based part
        self.name = ""           # "" = auto
        self.name_edited = False

    def seedspace(self):
        return next(limit for key, _label, limit in SPACES if key == self.space)

    def predicates(self):
        return (bool(self.legendary) or bool(self.tag_rules)
                or bool(self.voucher_rules))

    def auto_name(self):
        bits = []
        for rule in self.tag_rules:
            key, lo, hi, cnt, min_phase, max_phase = tag_rule_parts(rule)
            b = key.replace("tag_", "")
            b += "-a%d" % lo if lo == hi else "-a%d-%d" % (lo, hi)
            if min_phase != "small" or max_phase != "big":
                b += "-%s-%s" % (min_phase, max_phase)
            if cnt > 1:
                b += "x%d" % cnt
            bits.append(b)
        for key, lo, hi in self.voucher_rules:
            b = key.replace("v_", "", 1)
            b += "-a%d" % lo if lo == hi else "-a%d-%d" % (lo, hi)
            bits.append(b)
        for key in self.voucher_exclusions:
            bits.append("no-buy-" + key.replace("v_", "", 1))
        if self.legendary:
            b = self.legendary.replace("j_", "")
            if self.leg_neg:
                b = "neg-" + b
            if self.leg_soul_depth == 0:
                b += "-2souls"
            elif self.leg_soul_depth == 2:
                b += "-soul2"
            b += "-a%d" % self.leg_min if self.leg_min == self.leg_max \
                else "-a%d-%d" % (self.leg_min, self.leg_max)
            b += "-%s-%s" % (self.leg_min_phase, self.leg_max_phase)
            if self.leg_source != "any":
                b += "-" + self.leg_source
            if self.leg_routes == "canonical_charm":
                b += "-fast-no-omen"
            bits.append(b)
        if self.space == "settable":
            bits.append("settable")
        elif self.space == "total":
            bits.append("total")
        return re.sub(r"[^A-Za-z0-9._+-]", "-", "_".join(bits)) or "pool"

    def pool_name(self):
        base = self.name if self.name_edited and self.name else self.auto_name()
        if self.shard_total > 1:
            width = len(str(self.shard_total))
            base += "-part-%0*d-of-%d" % (width, self.shard_index, self.shard_total)
        return base

    def shard_bounds(self, count):
        """Half-open [start,end) for this part. Integer floor boundaries
        guarantee every rank appears exactly once across all parts."""
        limit = self.seedspace() if count == 0 else min(count, self.seedspace())
        total = max(1, self.shard_total)
        index = max(1, min(self.shard_index, total))
        start = limit * (index - 1) // total
        end = limit * index // total
        return start, end

    def text(self, fmt, count, apply_shard=True, checkpoint=None):
        start, end = self.shard_bounds(count) if apply_shard else (0, count)
        count_text = "all" if count == 0 and start == 0 and self.shard_total == 1 \
            else str(end - start)
        checkpoint = BUILD_CHECKPOINT if checkpoint is None \
            else max(16_384, int(checkpoint))
        lines = ["# generated by tools/brainstorm_pool_builder.py",
                 "poolver 1",
                 "threads %d" % self.threads,
                 "start %d" % start,
                 "count %s" % count_text,
                 "checkpoint %d" % checkpoint,
                 "chunk 2048",
                 "resume 1",
                 "format %s" % fmt,
                 "tag_route %s" % ("collect" if self.route_collect else "observe")]
        if self.space != "natural":
            lines.append("space %s" % self.space)
        lines.append("label %s" % self.pool_name())
        for rule in self.tag_rules:
            key, lo, hi, cnt, min_phase, max_phase = tag_rule_parts(rule)
            if min_phase == "small" and max_phase == "big":
                lines.append("tag %s %d %d %d" % (key, lo, hi, cnt))
            else:
                lines.append("tag %s %d %s %d %s %d"
                             % (key, lo, min_phase, hi, max_phase, cnt))
        for key, lo, hi in self.voucher_rules:
            lines.append("voucher %s %d %d" % (key, lo, hi))
        for key in self.voucher_exclusions:
            lines.append("voucher_exclude %s" % key)
        if self.legendary:
            # Always make the user's current-stage choice explicit in the
            # criteria file. The native engine deliberately omits default
            # `full` from hashes/headers to preserve existing pool identity.
            lines.append("legendary_routes %s" % self.leg_routes)
            if self.leg_human_location:
                lines.append("legendary %s %d %s %d %s %d %s"
                             % (self.legendary, self.leg_min, self.leg_min_phase,
                                self.leg_max, self.leg_max_phase,
                                1 if self.leg_neg else 0, self.leg_source))
            else:
                lines.append("legendary %s %d %d %d"
                             % (self.legendary, self.leg_min, self.leg_max,
                                1 if self.leg_neg else 0))
            if self.leg_soul_depth == 0:
                lines.append("soul_depth any")
            elif self.leg_soul_depth != 1:
                lines.append("soul_depth %d" % self.leg_soul_depth)
        lines.append("end")
        return "\n".join(lines) + "\n"

    def summary(self):
        parts = []
        for rule in self.tag_rules:
            key, lo, hi, cnt, min_phase, max_phase = tag_rule_parts(rule)
            s = TAG_NAMES.get(key, key)
            s += " x%d" % cnt if cnt > 1 else ""
            s += " (A%d %s through A%d %s)" % (
                lo, PHASE_LABELS[min_phase], hi, PHASE_LABELS[max_phase])
            parts.append(s)
        for key, lo, hi in self.voucher_rules:
            s = voucher_name(key)
            s += " (A%d)" % lo if lo == hi else " (A%d through A%d)" % (lo, hi)
            parts.append(s)
        if self.voucher_exclusions:
            names = ", ".join(voucher_name(key) for key in self.voucher_exclusions)
            parts.append("route cannot purchase %s" % names)
        depth_text = {1: " within 1 Soul (the first)",
                      0: " within 2 Souls (first or second)",
                      2: " second reachable Soul only"}
        if self.legendary:
            s = ("Negative " if self.leg_neg else "") + joker_name(self.legendary)
            s += depth_text.get(self.leg_soul_depth, "")
            s += " (A%d %s through A%d %s" % (
                self.leg_min, PHASE_LABELS[self.leg_min_phase],
                self.leg_max, PHASE_LABELS[self.leg_max_phase])
            if self.leg_source != "any":
                s += ", %s" % LEGENDARY_SOURCE_LABELS[
                    LEGENDARY_SOURCES.index(self.leg_source)].replace(" only", "")
            s += ")"
            if self.leg_routes == "canonical_charm":
                s += " [fast exact; automatic Omen-purchase recovery omitted]"
            parts.append(s)
        out = " + ".join(parts) if parts else "(no criteria yet)"
        if self.space == "settable":
            out += " [all vanilla-settable seeds; no 0]"
        elif self.space == "total":
            out += " [all possible seeds; includes 0]"
        if self.shard_total > 1:
            out += " [part %d of %d]" % (self.shard_index, self.shard_total)
        return out


class Runner:
    """One scanner process: criteria written to disk, stderr streamed."""

    def __init__(self, snapshot_path, criteria_text, output, input_pool=None):
        self.output = output
        self.input_pool = input_pool
        self.criteria_path = output + ".criteria.cfg"
        with open(self.criteria_path, "w", encoding="utf-8") as f:
            f.write(criteria_text)
        self.lines = deque(maxlen=200)
        # Populate the known scan total before starting the reader thread so a
        # fresh job immediately renders as 0 / N instead of appearing inert
        # until the native scanner reaches its first checkpoint.  Refilters
        # replace the configured count with the input pool's record count, so
        # their first native progress line remains authoritative.
        count_match = re.search(r"^count (\d+)$", criteria_text, re.MULTILINE)
        self.scanned = 0
        self.total = int(count_match.group(1)) if count_match and not input_pool else 0
        self.matched = 0
        self.rate = 0.0
        # Windows: a new process group so stop() can deliver CTRL_BREAK_EVENT
        # to the scanner alone (the C side turns it into a checkpointed pause,
        # same contract as SIGINT elsewhere).
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
        command = [POOL_BIN, "scan", snapshot_path, self.criteria_path, output]
        if input_pool:
            if os.path.abspath(input_pool) == os.path.abspath(output):
                raise ValueError("Input and output pool must be different files.")
            command = [POOL_BIN, "refilter", snapshot_path, self.criteria_path,
                       input_pool, output]
        self.proc = subprocess.Popen(
            command,
            stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True,
            creationflags=flags)
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()

    def _pump(self):
        for line in self.proc.stderr:
            line = line.rstrip("\n")
            self.lines.append(line)
            m = re.match(r"scanned=(\d+)/(\d+) matches=(\d+) rate=(\d+)", line)
            if m:
                self.scanned, self.total = int(m.group(1)), int(m.group(2))
                self.matched, self.rate = int(m.group(3)), float(m.group(4))

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(
                signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGINT)

    def done(self):
        return self.proc.poll() is not None

    def returncode(self):
        return self.proc.poll()


class MergeRunner:
    """One native merge process, shaped like Runner for the browser UI."""

    def __init__(self, inputs, output):
        self.output = output
        self.input_pool = None
        self.inputs = tuple(inputs)
        self.lines = deque(maxlen=200)
        self.scanned = 0
        self.total = sum(int(read_pool_header(p).get("records", "0") or 0)
                         for p in inputs)
        self.matched = 0
        self.rate = 0.0
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
        self.proc = subprocess.Popen(
            [POOL_BIN, "merge", output] + list(inputs),
            stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True,
            creationflags=flags)
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()

    def _pump(self):
        for line in self.proc.stderr:
            line = line.rstrip("\n")
            self.lines.append(line)
            m = re.match(r"merged=\d+/\d+ records=(\d+)/(\d+)", line)
            if m:
                self.scanned, self.total = int(m.group(1)), int(m.group(2))
                self.matched = self.scanned

    def stop(self):
        pass  # merges are deliberately non-pausable; inputs remain untouched

    def done(self):
        return self.proc.poll() is not None

    def returncode(self):
        return self.proc.poll()


def read_manifest(path):
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split(None, 1)
                if len(parts) == 2:
                    out[parts[0]] = parts[1].strip()
    except OSError:
        pass
    return out


def read_state(path):
    return read_manifest(path)


def read_pool_header_text(path):
    """Read a bounded .bspool text header without touching its data payload.

    Schemas 1 and 2 have the historical fixed 1 KiB header.  Schema 3 keeps
    ``header_bytes`` in that first KiB so readers can safely discover a larger
    metadata header before issuing a second, still-bounded read.
    """
    try:
        with open(path, "rb") as f:
            prefix = f.read(POOL_HEADER_PREFIX_BYTES)
            raw = prefix
            magic = re.match(
                br"^BRAINSTORM_SEED_POOL[ \t]+([0-9]+)[ \t]*(?:\r?\n|\x00|$)",
                prefix,
            )
            if magic and int(magic.group(1)) == 3:
                size_line = re.search(
                    br"(?:^|\n)header_bytes[ \t]+([0-9]+)[ \t]*(?:\r?\n|\x00|$)",
                    prefix,
                )
                if not size_line:
                    return ""
                header_bytes = int(size_line.group(1))
                if not (POOL_HEADER_PREFIX_BYTES <= header_bytes <= POOL_HEADER_MAX_BYTES):
                    return ""
                if header_bytes > len(prefix):
                    raw += f.read(header_bytes - len(prefix))
                    if len(raw) != header_bytes:
                        return ""
                else:
                    raw = raw[:header_bytes]
        return raw.split(b"\0", 1)[0].decode("latin-1")
    except OSError:
        return ""


def read_pool_header(path):
    out = {}
    raw = read_pool_header_text(path)
    for line in raw.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            out[parts[0]] = parts[1].strip()
        if parts and parts[0] == "end":
            break
    return out


POOL_CRITERIA_FIELDS = (
    "tag", "route_tag", "legendary", "route_legendary", "soul_depth",
    "tag_route", "voucher", "route_voucher", "voucher_exclude",
    "route_voucher_exclude", "legendary_routes", "route_legendary_routes",
)
POOL_IDENTITY_FIELDS = (
    "family_id", "segment_id", "stage_hash", "lineage_id",
    "derivation_id", "snapshot_id", "membership_digest",
    "metadata_digest", "parent_snapshot_id", "parent_segment_id",
    "catalog_hash", "criteria_hash",
)
POOL_INTEGER_FIELDS = (
    "schema", "modelver", "header_bytes", "seedspace", "records",
    "refilter_depth", "range_start",
    "range_end", "merged_parts", "scan_cursor", "input_cursor",
    "parent_records", "parent_data_bytes", "input_record_start",
    "input_record_end", "shard_index", "shard_total",
)


def _pool_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _pool_identity(value):
    """Return a usable identity value, treating native zero IDs as absent."""
    value = str(value or "").strip()
    if not value or value == "-":
        return ""
    compact = value.lower()
    if compact.startswith("0x"):
        compact = compact[2:]
    if compact and all(ch == "0" for ch in compact):
        return ""
    return value


ATTACHMENT_SCHEMA = 1
ATTACHMENT_SIGNATURE_SCHEMA = 1
ATTACHMENT_ROLES = ("accelerator", "authoritative")
ATTACHMENT_PHASES = ("boss", "small", "big")
ATTACHMENT_SOURCES = ("any", "shop", "charm", "ethereal")
ATTACHMENT_ROUTE_POLICIES = ("full", "canonical_charm")
ATTACHMENT_CATALOG_DIRECTIVES = {
    "modelver", "tagdef", "vouchdef", "vouchroute", "vouchowned",
    "jokerdef", "boostdef", "specialdef",
}


def catalog_hash_file(path):
    """Mirror the native helper's immutable profile/catalog fingerprint."""
    value = 1469598103934665603
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            tokens = line.split()
            if not tokens or (tokens[0] not in ATTACHMENT_CATALOG_DIRECTIVES
                              and not tokens[0].startswith("check_")):
                continue
            for token in tokens:
                for byte in token.encode("utf-8") + b"\0":
                    value ^= byte
                    value = (value * 1099511628211) & 0xffffffffffffffff
            value ^= ord("\n")
            value = (value * 1099511628211) & 0xffffffffffffffff
    return "%016x" % value


def _attachment_int(value, field, lo=None, hi=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("invalid %s" % field)
    if lo is not None and number < lo or hi is not None and number > hi:
        raise ValueError("invalid %s" % field)
    return number


def _attachment_phase(value, field, allow_boss=True):
    value = str(value).lower()
    allowed = ATTACHMENT_PHASES if allow_boss else ("small", "big")
    if value not in allowed:
        raise ValueError("invalid %s" % field)
    return value


def pool_attachment_predicates(pool):
    """Translate a pool header's cumulative criteria into canonical tokens.

    These strings are the cross-language contract consumed by the future Lua
    matcher.  They describe semantics rather than the criteria hash, so a
    refiltered pool retains every inherited route predicate explicitly.
    """
    lines = list(pool.get("criteria") or ())
    tag_route = "collect"
    legendary_routes = "full"
    inherited_legendary_routes = "full"
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and parts[0] == "tag_route":
            if parts[1] not in ("collect", "observe"):
                raise ValueError("invalid tag route policy")
            tag_route = parts[1]
        elif len(parts) == 2 and parts[0] == "legendary_routes":
            if parts[1] not in ATTACHMENT_ROUTE_POLICIES:
                raise ValueError("invalid Legendary route policy")
            legendary_routes = parts[1]
        elif len(parts) == 2 and parts[0] == "route_legendary_routes":
            if parts[1] not in ATTACHMENT_ROUTE_POLICIES:
                raise ValueError("invalid inherited Legendary route policy")
            inherited_legendary_routes = parts[1]

    tags = []
    legendaries = []
    vouchers = []
    exclusions = []
    last_current_legendary = None
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]
        if kind == "tag":
            if len(parts) == 5:
                key, lo, hi, count = parts[1:]
                min_phase, max_phase = "small", "big"
            elif len(parts) == 7:
                key, lo, min_phase, hi, max_phase, count = parts[1:]
            else:
                raise ValueError("invalid tag predicate")
            tags.append((tag_route, key,
                         _attachment_int(lo, "tag minimum Ante", 1, MAX_ANTE),
                         _attachment_phase(min_phase, "tag minimum phase", False),
                         _attachment_int(hi, "tag maximum Ante", 1, MAX_ANTE),
                         _attachment_phase(max_phase, "tag maximum phase", False),
                         _attachment_int(count, "tag count", 1)))
        elif kind == "route_tag":
            if len(parts) == 6:
                mode, key, lo, hi, count = parts[1:]
                min_phase, max_phase = "small", "big"
            elif len(parts) == 8:
                mode, key, lo, min_phase, hi, max_phase, count = parts[1:]
            else:
                raise ValueError("invalid inherited tag predicate")
            if mode not in ("collect", "observe"):
                raise ValueError("invalid inherited tag route policy")
            tags.append((mode, key,
                         _attachment_int(lo, "tag minimum Ante", 1, MAX_ANTE),
                         _attachment_phase(min_phase, "tag minimum phase", False),
                         _attachment_int(hi, "tag maximum Ante", 1, MAX_ANTE),
                         _attachment_phase(max_phase, "tag maximum phase", False),
                         _attachment_int(count, "tag count", 1)))
        elif kind in ("legendary", "route_legendary"):
            inherited = kind == "route_legendary"
            if (not inherited and len(parts) == 5) or (inherited and len(parts) == 6):
                key, lo, hi, neg = parts[1:5]
                depth = parts[5] if inherited else "1"
                min_phase, max_phase, source = "boss", "big", "any"
            elif (not inherited and len(parts) == 8) or (inherited and len(parts) == 9):
                key, lo, min_phase, hi, max_phase, neg, source = parts[1:8]
                depth = parts[8] if inherited else "1"
            else:
                raise ValueError("invalid Legendary predicate")
            source = source.lower()
            if source not in ATTACHMENT_SOURCES:
                raise ValueError("invalid Legendary source")
            rule = [key,
                    _attachment_int(lo, "Legendary minimum Ante", 1, MAX_ANTE),
                    _attachment_phase(min_phase, "Legendary minimum phase"),
                    _attachment_int(hi, "Legendary maximum Ante", 1, MAX_ANTE),
                    _attachment_phase(max_phase, "Legendary maximum phase"),
                    _attachment_int(neg, "Legendary Negative flag", 0, 1),
                    source,
                    _attachment_int(depth, "Soul depth", 0, 2),
                    inherited_legendary_routes if inherited else legendary_routes]
            legendaries.append(rule)
            last_current_legendary = None if inherited else rule
        elif kind == "soul_depth":
            if len(parts) != 2 or last_current_legendary is None:
                raise ValueError("Soul depth has no preceding Legendary predicate")
            last_current_legendary[7] = 0 if parts[1] == "any" else \
                _attachment_int(parts[1], "Soul depth", 1, 2)
        elif kind in ("voucher", "route_voucher"):
            if len(parts) != 4:
                raise ValueError("invalid voucher predicate")
            vouchers.append((parts[1],
                             _attachment_int(parts[2], "voucher minimum Ante", 1, MAX_ANTE),
                             _attachment_int(parts[3], "voucher maximum Ante", 1, MAX_ANTE)))
        elif kind in ("voucher_exclude", "route_voucher_exclude"):
            if len(parts) != 2:
                raise ValueError("invalid voucher exclusion")
            exclusions.append(parts[1])

    predicates = []
    for mode, key, lo, min_phase, hi, max_phase, count in tags:
        if route_position(lo, min_phase) > route_position(hi, max_phase):
            raise ValueError("tag route start follows its end")
        if count > tag_location_count(lo, min_phase, hi, max_phase):
            raise ValueError("tag count exceeds its route window")
    for value in legendaries:
        if route_position(value[1], value[2]) > route_position(value[3], value[4]):
            raise ValueError("Legendary route start follows its end")
    for key, lo, hi in vouchers:
        if lo > hi:
            raise ValueError("voucher route start follows its end")
    predicates.extend("tag %s %s %d %s %d %s %d" % value for value in tags)
    predicates.extend("legendary %s %d %s %d %s %d %s %d %s" % tuple(value)
                      for value in legendaries)
    predicates.extend("voucher %s %d %d" % value for value in vouchers)
    predicates.extend("voucher_exclude %s" % value for value in exclusions)
    if not predicates:
        raise ValueError("the pool has no canonical attachment predicates")
    return sorted(predicates)


def pool_attachment_signature(pool):
    predicates = pool_attachment_predicates(pool)
    body = ("signature_schema %d\n" % ATTACHMENT_SIGNATURE_SCHEMA) \
        + "".join("predicate %s\n" % value for value in predicates)
    return {
        "schema": ATTACHMENT_SIGNATURE_SCHEMA,
        "predicates": predicates,
        "hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def pool_attachment_accelerator_blockers(pool, current_catalog_hash=None):
    """Physical blockers shared by every automatic accelerator."""
    blockers = []
    if not pool.get("complete"):
        blockers.append("the pool operation is not complete")
    if not pool.get("coverage_complete"):
        blockers.append("the pool descends from an incomplete source snapshot")
    if pool.get("space", "natural") != "natural" \
            or pool.get("seedspace", 0) != SEEDSPACE:
        blockers.append("automatic search substitution currently requires the natural seed space")
    range_start = pool.get("range_start", -1)
    range_end = pool.get("range_end", -1)
    if not isinstance(range_start, int) or not isinstance(range_end, int) \
            or range_start < 0 or range_start >= range_end or range_end > SEEDSPACE:
        blockers.append("the pool has an invalid natural-space rank range")
    if pool.get("records", 0) <= 0:
        blockers.append("the pool contains no searchable seed records")
    if pool.get("composite"):
        blockers.append("composite boolean membership needs a separate filter matcher")
    if pool.get("modelver", 0) != 6:
        blockers.append("the pool was not built with the current route model")
    if not _pool_identity(pool.get("pool_id")) \
            or not _pool_identity(pool.get("catalog_hash")) \
            or not _pool_identity(pool.get("criteria_hash")) \
            or not _pool_identity(pool.get("snapshot_id")):
        blockers.append("the pool lacks attachment identity metadata")
    if not pool.get("criteria"):
        blockers.append("the pool has no embedded filter criteria")
    else:
        try:
            pool_attachment_signature(pool)
        except ValueError as exc:
            blockers.append("the embedded criteria cannot be attached: %s" % exc)
    if current_catalog_hash and str(pool.get("catalog_hash", "")).lower() \
            != str(current_catalog_hash).lower():
        blockers.append("the pool was built from a different profile/catalog snapshot")
    return blockers


def pool_attachment_authoritative_blockers(pool, current_catalog_hash=None):
    """Additional proof required before pool exhaustion may be definitive."""
    blockers = pool_attachment_accelerator_blockers(pool, current_catalog_hash)
    if pool.get("range_start", -1) != 0 or pool.get("range_end", -1) != SEEDSPACE:
        blockers.append("the pool does not cover every natural-space rank")
    return blockers


def pool_attachment_base_blockers(pool):
    """Compatibility alias for the original preliminary API."""
    return pool_attachment_accelerator_blockers(pool)


class PoolInfo:
    """A browser/TUI-safe view of one pool's header and checkpoint state.

    Older pools deliberately remain valid: fields introduced with schema 3
    are optional, and their absence puts the file in the legacy library group
    instead of hiding it.
    """

    def __init__(self, path):
        self.path = path
        self.header = {}
        self.criteria = []
        self.composite_branches = []
        self.composite_operands = []
        raw = read_pool_header_text(path)
        for line in raw.splitlines():
            parts = line.split(None, 1)
            if not parts:
                continue
            key = parts[0]
            value = parts[1].strip() if len(parts) == 2 else ""
            if key == "BRAINSTORM_SEED_POOL":
                self.header["schema"] = value
            elif key in POOL_CRITERIA_FIELDS:
                self.criteria.append(line)
            elif key == "composite_branch":
                self.composite_branches.append(value)
            elif key == "composite_operand":
                self.composite_operands.append(value)
            elif key != "end":
                self.header[key] = value
            if key == "end":
                break
        self.state = read_state(path + ".state")

    def as_dict(self, include_attachment=True, current_catalog_hash=None):
        h = self.header
        complete = h.get("complete") == "1"
        coverage_complete = h.get("coverage_complete", h.get("complete")) == "1"
        resumable = self.state.get("done") == "0"
        if not complete:
            status = "paused" if resumable else "incomplete"
            status_label = "Paused · resumable" if resumable else "Incomplete snapshot"
        elif not coverage_complete:
            status = "provisional"
            status_label = "Provisional · source snapshot"
        else:
            status = "complete"
            status_label = "Complete"

        try:
            byte_count = os.path.getsize(self.path)
        except OSError:
            byte_count = 0
        pool = {
            "name": os.path.basename(self.path),
            "bytes": byte_count,
            "records": _pool_int(h.get("records")),
            "complete": complete,
            "coverage_complete": coverage_complete,
            "resumable": resumable,
            "status": status,
            "status_label": status_label,
            "criteria": list(self.criteria),
            "composite": bool(self.header.get("composite_schema")),
            "composite_schema": _pool_int(h.get("composite_schema")),
            "composite_operation": h.get("composite_operation", ""),
            "composite_route_policy": h.get("composite_route_policy", ""),
            "composite_branch_count": len(self.composite_branches),
            "composite_operand_count": len(self.composite_operands),
            "composite_metadata_complete": h.get(
                "composite_metadata_complete", "1") == "1",
            "label": h.get("label", ""),
            "pool_id": h.get("pool_id", ""),
            "space": h.get("space", "natural"),
            "source_pool_id": h.get("source_pool_id", ""),
            "encoding": h.get("encoding", ""),
            "parent_name": "",
            "parent_current_records": 0,
            "update_available": False,
            "new_records": 0,
        }
        for key in POOL_INTEGER_FIELDS:
            if key in h:
                pool[key] = _pool_int(h[key])
        for key in POOL_IDENTITY_FIELDS:
            if key in h:
                pool[key] = h[key]
        for key in ("parent_coverage_complete", "source_complete",
                    "source_coverage_complete"):
            if key in h:
                pool[key] = h[key] == "1"
        pool["legacy"] = not (
            _pool_identity(pool.get("family_id"))
            and _pool_identity(pool.get("lineage_id"))
        )
        pool["attachment_accelerator_blockers"] = \
            pool_attachment_accelerator_blockers(pool, current_catalog_hash)
        pool["attachment_accelerator_eligible"] = \
            not pool["attachment_accelerator_blockers"]
        pool["attachment_authoritative_blockers"] = \
            pool_attachment_authoritative_blockers(pool, current_catalog_hash)
        pool["attachment_authoritative_eligible"] = \
            not pool["attachment_authoritative_blockers"]
        # Compatibility names used by the first attachment prototype. "Base"
        # now correctly means accelerator eligibility rather than full-space
        # authority.
        pool["attachment_blockers"] = list(
            pool["attachment_accelerator_blockers"])
        pool["attachment_base_eligible"] = \
            pool["attachment_accelerator_eligible"]
        attachment = read_pool_attachment(
            self.path, pool, current_catalog_hash) \
            if include_attachment else None
        pool["attachment"] = attachment
        pool["attached"] = bool(attachment and attachment.get("valid")
                                and attachment.get("enabled"))
        pool["attachment_role"] = attachment.get("role", "") \
            if attachment else ""
        pool["attachment_invalid_reason"] = "" if not attachment \
            else "; ".join(attachment.get("blockers", ()))
        return pool


def _pool_display_name(pool):
    label = str(pool.get("label") or "").strip()
    if label and label + ".bspool" != pool["name"]:
        return label
    name = pool["name"]
    return name[:-7] if name.lower().endswith(".bspool") else name


def _link_pool_parents(pools):
    """Add best-effort direct-parent and append-update information in place."""
    segment_sources = {}
    pool_id_sources = {}
    for pool in pools:
        family = _pool_identity(pool.get("family_id"))
        segment = _pool_identity(pool.get("segment_id"))
        if family and segment:
            segment_sources.setdefault((family, segment), []).append(pool)
        pool_id = _pool_identity(pool.get("pool_id"))
        if pool_id:
            pool_id_sources.setdefault(pool_id, []).append(pool)

    for pool in pools:
        family = _pool_identity(pool.get("family_id"))
        parent_segment = _pool_identity(pool.get("parent_segment_id"))
        candidates = segment_sources.get((family, parent_segment), []) \
            if family and parent_segment else []
        # A segment is append-stable, so the largest visible snapshot is the
        # useful current parent when several copies of that segment exist.
        candidates = [item for item in candidates if item is not pool]
        if not candidates:
            source_id = _pool_identity(pool.get("source_pool_id"))
            candidates = [item for item in pool_id_sources.get(source_id, [])
                          if item is not pool] if source_id else []
        if not candidates:
            continue
        parent = max(candidates, key=lambda item: (item.get("records", 0), item["name"]))
        pool["parent_name"] = parent["name"]
        pool["parent_current_records"] = parent.get("records", 0)
        pinned = pool.get("parent_records", 0)
        # Incremental updates are safe to advertise only for schema-3 lineage:
        # legacy pool IDs can change as a paused file grows.
        if (family and parent_segment and "parent_records" in pool
                and pinned < parent.get("records", 0)):
            pool["update_available"] = True
            pool["new_records"] = parent["records"] - pinned


def group_pool_library(pools):
    """Group JSON-ready pool dictionaries by family, then lineage."""
    family_buckets = {}
    for pool in pools:
        family_id = _pool_identity(pool.get("family_id"))
        lineage_id = _pool_identity(pool.get("lineage_id"))
        if not family_id or not lineage_id:
            family_id = ""
            lineage_id = ""
        family_key = "family:" + family_id if family_id else "legacy"
        lineage_key = "lineage:" + lineage_id if lineage_id else "legacy"
        pool["family_key"] = family_key
        pool["lineage_key"] = lineage_key
        family = family_buckets.setdefault(family_key, {
            "key": family_key,
            "family_id": family_id,
            "legacy": not bool(family_id),
            "lineages": {},
        })
        family["lineages"].setdefault(lineage_key, {
            "key": lineage_key,
            "lineage_id": lineage_id,
            "pools": [],
        })["pools"].append(pool)

    groups = []
    for family in family_buckets.values():
        lineages = []
        family_pools = []
        for lineage in family["lineages"].values():
            lineage["pools"].sort(
                key=lambda item: (item.get("range_start", 0), item["name"]))
            family_pools.extend(lineage["pools"])
            depth = min((item.get("refilter_depth", 0) for item in lineage["pools"]),
                        default=0)
            has_parent = any(item.get("parent_name") or item.get("parent_segment_id")
                             for item in lineage["pools"])
            if family["legacy"]:
                lineage["label"] = "Older / standalone pools"
            elif has_parent or depth:
                lineage["label"] = "Filter pass %d" % max(depth, 1)
            else:
                lineage["label"] = "Original search"
            lineage["display_name"] = _pool_display_name(lineage["pools"][0])
            lineages.append(lineage)
        lineages.sort(key=lambda item: (
            min((pool.get("refilter_depth", 0) for pool in item["pools"]), default=0),
            item["display_name"], item["key"],
        ))
        family["lineages"] = lineages
        roots = [pool for pool in family_pools
                 if not _pool_identity(pool.get("parent_segment_id"))
                 and not _pool_identity(pool.get("source_pool_id"))]
        representative = sorted(roots or family_pools,
                                key=lambda item: (item.get("refilter_depth", 0),
                                                  item["name"]))[0]
        family["label"] = "Legacy pools" if family["legacy"] \
            else _pool_display_name(representative)
        groups.append(family)
    groups.sort(key=lambda item: (item["legacy"], item["label"].lower(), item["key"]))
    return groups


def read_pool_library(pool_dir=POOL_DIR, current_catalog_hash=None):
    """Return ``(flat_pools, grouped_pools)`` for the standalone builder."""
    pools = []
    if os.path.isdir(pool_dir):
        for name in sorted(os.listdir(pool_dir)):
            if name.endswith(".bspool"):
                pools.append(PoolInfo(os.path.join(pool_dir, name)).as_dict(
                    current_catalog_hash=current_catalog_hash))
    _link_pool_parents(pools)
    return pools, group_pool_library(pools)


# A Builder scan deliberately keeps its criteria and checkpoint metadata next
# to the shareable pool.  An attached-pool marker is reserved for the automatic
# in-game pool-selection work; deleting a pool must not leave that marker
# pointing at a file that no longer exists.  The writer lock is deliberately
# *not* deleted: POSIX flock protects an inode rather than a pathname, so
# unlinking it while a scanner still has the old inode locked would let a
# second scanner create and lock a different inode for the same pool.
POOL_DELETE_SUFFIXES = (
    "", ".state", ".manifest", ".criteria.cfg", ".attached",
)


def _safe_pool_path(name, pool_dir):
    """Resolve one direct child of ``pool_dir`` without accepting traversal."""
    if not isinstance(name, str) or name != os.path.basename(name) \
            or not name.lower().endswith(".bspool") \
            or any(ch in name for ch in ("\0", "\r", "\n")):
        raise ValueError("Select a .bspool file directly from the seed-pool library.")
    root = os.path.abspath(pool_dir)
    path = os.path.abspath(os.path.join(root, name))
    try:
        inside = os.path.commonpath((root, path)) == root
    except ValueError:  # different Windows drives
        inside = False
    if not inside or os.path.dirname(path) != root:
        raise ValueError("The selected pool is outside the seed-pool library.")
    return path


def _pool_is_protected(path, protected_paths):
    protected = {os.path.normcase(os.path.abspath(value))
                 for value in protected_paths if value}
    return os.path.normcase(os.path.abspath(path)) in protected


def _attachment_file_identity(path):
    stat = os.stat(path)
    return {
        "file_size": int(stat.st_size),
        "file_mtime_ns": int(getattr(
            stat, "st_mtime_ns", int(stat.st_mtime * 1000000000))),
    }


def _attachment_marker_hash(signature_schema, predicates):
    body = ("signature_schema %d\n" % signature_schema) \
        + "".join("predicate %s\n" % value for value in predicates)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def read_pool_attachment(path, pool=None, current_catalog_hash=None):
    """Read and validate ``<pool>.attached`` without trusting its claims."""
    marker_path = path + ".attached"
    if not os.path.isfile(marker_path):
        return None
    marker = {"path": marker_path, "predicates": [], "blockers": []}
    try:
        with open(marker_path, "r", encoding="utf-8", errors="strict") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeError) as exc:
        marker["blockers"].append("the attachment marker cannot be read: %s" % exc)
        marker["valid"] = False
        return marker
    if not lines or lines[0] != "BRAINSTORM_POOL_ATTACHMENT %d" % ATTACHMENT_SCHEMA:
        marker["blockers"].append("the attachment marker schema is unsupported")
    saw_end = False
    for line in lines[1:]:
        parts = line.split(None, 1)
        if not parts:
            continue
        key = parts[0]
        value = parts[1].strip() if len(parts) == 2 else ""
        if key == "end":
            saw_end = True
            break
        if key == "predicate":
            marker["predicates"].append(value)
        elif key not in marker:
            marker[key] = value
        else:
            marker["blockers"].append("the attachment marker repeats %s" % key)
    if not saw_end:
        marker["blockers"].append("the attachment marker is truncated")
    marker["enabled"] = marker.get("enabled") == "1"
    if not marker["enabled"]:
        marker["blockers"].append("the attachment marker is disabled")
    marker["role"] = marker.get("role", "")
    if marker["role"] not in ATTACHMENT_ROLES:
        marker["blockers"].append("the attachment role is invalid")
    if marker.get("pool_file") != os.path.basename(path):
        marker["blockers"].append("the attachment marker names a different pool file")
    try:
        signature_schema = int(marker.get("signature_schema", "0"))
    except ValueError:
        signature_schema = 0
    if signature_schema != ATTACHMENT_SIGNATURE_SCHEMA:
        marker["blockers"].append("the attachment signature schema is unsupported")
    expected_marker_hash = _attachment_marker_hash(
        signature_schema, marker["predicates"])
    if marker.get("signature_hash", "").lower() != expected_marker_hash:
        marker["blockers"].append("the attachment signature checksum is stale")

    if pool is None:
        pool = PoolInfo(path).as_dict(include_attachment=False)
    for field in ("pool_id", "catalog_hash", "criteria_hash", "snapshot_id"):
        expected = str(pool.get(field, ""))
        actual = str(marker.get(field, ""))
        if not expected or actual.lower() != expected.lower():
            marker["blockers"].append("the attachment %s no longer matches the pool" % field)
    try:
        identity = _attachment_file_identity(path)
    except OSError as exc:
        marker["blockers"].append("the attached pool cannot be statted: %s" % exc)
        identity = {}
    for field in ("file_size", "file_mtime_ns"):
        if str(marker.get(field, "")) != str(identity.get(field, "")):
            marker["blockers"].append("the attached pool file identity changed")
            break
    try:
        current_signature = pool_attachment_signature(pool)
        if marker["predicates"] != current_signature["predicates"] \
                or marker.get("signature_hash", "").lower() != current_signature["hash"]:
            marker["blockers"].append("the attachment predicates no longer match the pool header")
    except ValueError as exc:
        marker["blockers"].append("the pool criteria are no longer attachable: %s" % exc)
    role_blockers = pool_attachment_authoritative_blockers \
        if marker["role"] == "authoritative" \
        else pool_attachment_accelerator_blockers
    marker["blockers"].extend(role_blockers(pool, current_catalog_hash))
    marker["blockers"] = list(dict.fromkeys(marker["blockers"]))
    marker["valid"] = not marker["blockers"]
    return marker


def attach_completed_pool(name, role, pool_dir=POOL_DIR,
                          current_catalog_hash=None, protected_paths=()):
    """Atomically bind one completed pool to automatic in-game selection."""
    path = _safe_pool_path(name, pool_dir)
    if _pool_is_protected(path, protected_paths):
        raise ValueError("That pool is an active Builder input or output; finish the job first.")
    if role not in ATTACHMENT_ROLES:
        raise ValueError("Choose accelerator or authoritative attachment.")
    with _pool_writer_guard(path):
        if not os.path.isfile(path):
            raise ValueError("The selected seed pool no longer exists.")
        return _attach_completed_pool_locked(
            path, name, role, pool_dir, current_catalog_hash)


def _attach_completed_pool_locked(path, name, role, pool_dir,
                                  current_catalog_hash):
    pool = PoolInfo(path).as_dict()
    blockers = (pool_attachment_authoritative_blockers
                if role == "authoritative"
                else pool_attachment_accelerator_blockers)(
                    pool, current_catalog_hash)
    if blockers:
        raise ValueError("Cannot attach this pool as %s: %s."
                         % (role, "; ".join(blockers)))
    signature = pool_attachment_signature(pool)
    identity = _attachment_file_identity(path)
    lines = [
        "BRAINSTORM_POOL_ATTACHMENT %d" % ATTACHMENT_SCHEMA,
        "enabled 1",
        "role %s" % role,
        "pool_file %s" % name,
        "pool_id %s" % pool["pool_id"],
        "catalog_hash %s" % pool["catalog_hash"],
        "criteria_hash %s" % pool["criteria_hash"],
        "snapshot_id %s" % pool.get("snapshot_id", "-"),
        "signature_schema %d" % signature["schema"],
        "signature_hash %s" % signature["hash"],
        "file_size %d" % identity["file_size"],
        "file_mtime_ns %d" % identity["file_mtime_ns"],
    ]
    lines.extend("predicate %s" % value for value in signature["predicates"])
    lines.extend(("end", ""))
    marker_path = path + ".attached"
    fd, temporary = tempfile.mkstemp(
        prefix=".%s.attached." % os.path.basename(path), dir=pool_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker_path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    marker = read_pool_attachment(path, pool, current_catalog_hash)
    if not marker or not marker.get("valid"):
        raise ValueError("The attachment marker failed validation after writing.")
    return marker


def detach_pool(name, pool_dir=POOL_DIR, protected_paths=()):
    """Remove an attachment policy without changing the pool itself."""
    path = _safe_pool_path(name, pool_dir)
    if _pool_is_protected(path, protected_paths):
        raise ValueError("That pool is an active Builder input or output; finish the job first.")
    with _pool_writer_guard(path):
        marker_path = path + ".attached"
        if not os.path.lexists(marker_path):
            return {"name": name, "detached": False}
        os.unlink(marker_path)
        return {"name": name, "detached": True}


def pool_delete_plan(name, pool_dir=POOL_DIR, protected_paths=()):
    """Return a snapshot-bound deletion plan for one completed pool.

    The token covers every currently existing related file and its lstat
    identity.  The caller must recompute the plan immediately before deleting;
    this turns a stale browser confirmation into a refusal instead of deleting
    a newly resumed/replaced pool.
    """
    path = _safe_pool_path(name, pool_dir)
    if not os.path.isfile(path):
        raise ValueError("The selected seed pool no longer exists.")
    header = read_pool_header(path)
    if header.get("complete") != "1":
        raise ValueError("Only completed seed pools can be deleted from the Builder.")

    protected = {os.path.normcase(os.path.abspath(value))
                 for value in protected_paths if value}
    related = [path + suffix for suffix in POOL_DELETE_SUFFIXES
               if os.path.lexists(path + suffix)]
    blocked = [value for value in related
               if os.path.normcase(os.path.abspath(value)) in protected]
    if blocked:
        raise ValueError("That pool is an active Builder input or output; stop or finish the job first.")

    digest = hashlib.sha256()
    entries = []
    total_bytes = 0
    for value in related:
        stat = os.lstat(value)
        size = int(stat.st_size)
        total_bytes += size
        entries.append({"name": os.path.basename(value), "path": value,
                        "bytes": size})
        digest.update(os.path.normcase(os.path.abspath(value)).encode("utf-8"))
        digest.update(("\0%d\0%d\0%d\0%d\n" % (
            int(stat.st_dev), int(stat.st_ino), size,
            int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1000000000))),
        )).encode("ascii"))
    return {"name": name, "path": path, "files": entries,
            "bytes": total_bytes, "token": digest.hexdigest()}


def delete_completed_pool(name, token, pool_dir=POOL_DIR, protected_paths=()):
    """Delete a still-identical completed pool plan, main file last."""
    path = _safe_pool_path(name, pool_dir)
    with _pool_writer_guard(path):
        plan = pool_delete_plan(name, pool_dir, protected_paths)
        if not isinstance(token, str) \
                or not hmac.compare_digest(plan["token"], token):
            raise ValueError(
                "The pool files changed after confirmation; review the deletion again.")
        paths = [item["path"] for item in plan["files"]]
        # Sidecars first keeps the usable .bspool present if an earlier unlink
        # fails.  A completed pool does not require its sidecars to remain usable.
        main = plan["path"]
        ordered = [value for value in paths if value != main] + [main]
        removed = []
        for value in ordered:
            os.unlink(value)
            removed.append(os.path.basename(value))
        plan["removed"] = removed
        return plan


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def human_secs(s):
    s = int(s)
    if s < 90:
        return "%ds" % s
    if s < 5400:
        return "%dm %02ds" % (s // 60, s % 60)
    if s < 172800:
        return "%dh %02dm" % (s // 3600, (s % 3600) // 60)
    return "%.1f days" % (s / 86400.0)


# ============================================================== curses UI ==

class Field:
    def __init__(self, kind, label, **kw):
        self.kind = kind          # cycle | number | toggle | text | action | rule
        self.label = label
        self.__dict__.update(kw)


class App:
    def __init__(self, stdscr, snap):
        self.scr = stdscr
        self.snap = snap
        self.crit = Criteria()
        self.sel = 0
        self.status = ""
        self.tag_keys = [k for k, _ in snap.usable_tags()]
        self.tag_min_ante = {k: ma for k, ma in snap.usable_tags()}
        self.legendaries = snap.usable_legendaries()
        self.vouchers = [key for key, _prerequisite in snap.usable_vouchers()]
        self.voucher_prerequisite = dict(snap.usable_vouchers())
        self.input_pools = [""]
        if os.path.isdir(POOL_DIR):
            for fn in sorted(os.listdir(POOL_DIR)):
                path = os.path.join(POOL_DIR, fn)
                head = read_pool_header(path) if fn.endswith(".bspool") else {}
                # Committed blocks are independently checksummed and readable
                # before a scan finishes. Refiltering a paused pool is a
                # snapshot operation over only those currently recorded seeds.
                if fn.endswith(".bspool") and int(head.get("records", "0") or 0) > 0:
                    self.input_pools.append(path)
        self.input_idx = 0
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_YELLOW)  # selected
        curses.init_pair(2, curses.COLOR_YELLOW, -1)                  # accents
        curses.init_pair(3, curses.COLOR_GREEN, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
        curses.init_pair(5, curses.COLOR_CYAN, -1)

    # -------------------------------------------------------- field model
    def fields(self):
        c = self.crit
        f = []
        legopts = ["None"] + [joker_name(k) for k in self.legendaries]
        legidx = ([""] + self.legendaries).index(c.legendary)
        f.append(Field("cycle", "Legendary", options=legopts,
                       idx=legidx, set=self._set_leg))
        if c.legendary:
            route_idx = LEGENDARY_ROUTES.index(c.leg_routes) \
                if c.leg_routes in LEGENDARY_ROUTES else 0
            f.append(Field("cycle", "  Route coverage",
                           options=LEGENDARY_ROUTE_LABELS,
                           idx=route_idx, set=self._set_leg_routes))
            depth_idx = SOUL_DEPTHS.index(c.leg_soul_depth) \
                if c.leg_soul_depth in SOUL_DEPTHS else 0
            f.append(Field("cycle", "  Search depth", options=SOUL_DEPTH_LABELS,
                           idx=depth_idx, set=self._set_leg_depth))
            f.append(Field("number", "  Earliest ante", get=lambda: c.leg_min,
                           set=self._set_leg_min, lo=1, hi=MAX_ANTE))
            f.append(Field("cycle", "  Earliest route point",
                           options=[PHASE_LABELS[p] for p in PHASES],
                           idx=PHASES.index(c.leg_min_phase),
                           set=self._set_leg_min_phase))
            f.append(Field("number", "  Latest ante", get=lambda: c.leg_max,
                           set=self._set_leg_max, lo=1, hi=MAX_ANTE))
            f.append(Field("cycle", "  Latest route point",
                           options=[PHASE_LABELS[p] for p in PHASES],
                           idx=PHASES.index(c.leg_max_phase),
                           set=self._set_leg_max_phase))
            f.append(Field("cycle", "  Pack source",
                           options=LEGENDARY_SOURCE_LABELS,
                           idx=LEGENDARY_SOURCES.index(c.leg_source),
                           set=self._set_leg_source))
            f.append(Field("toggle", "  Require Negative edition",
                           get=lambda: c.leg_neg, set=self._set_leg_neg))
        for i, rule in enumerate(c.tag_rules):
            f.append(Field("rule", "Tag rule %d" % (i + 1), rule=rule, ridx=i))
        if len(c.tag_rules) < MAX_TAG_RULES and self.tag_keys:
            f.append(Field("action", "[ Add a tag requirement ]", run=self._add_rule))
        for i, rule in enumerate(c.voucher_rules):
            f.append(Field("voucher_rule", "Voucher target %d" % (i + 1),
                           rule=rule, ridx=i))
        if len(c.voucher_rules) < MAX_VOUCHER_RULES and self.vouchers:
            f.append(Field("action", "[ Add a voucher target ]",
                           run=self._add_voucher_rule))
        for i, key in enumerate(c.voucher_exclusions):
            f.append(Field("voucher_exclusion", "Cannot require purchase %d" % (i + 1),
                           key=key, ridx=i))
        if (c.voucher_rules
                and len(c.voucher_exclusions) < MAX_VOUCHER_EXCLUSIONS
                and len(c.voucher_exclusions) < len(self.vouchers)):
            f.append(Field("action", "[ Add a voucher purchase exclusion ]",
                           run=self._add_voucher_exclusion))
        f.append(Field("toggle", "Tag route",
                       get=lambda: c.route_collect, set=self._set_route,
                       on="collect (skip matched blinds)",
                       off="observe (play those blinds)"))
        f.append(Field("cycle", "Input seeds",
                       options=["Balatro's seed space"] +
                       [os.path.basename(p) for p in self.input_pools[1:]],
                       idx=self.input_idx, set=self._set_input))
        f.append(Field("cycle", "Threads",
                       options=["Auto (all cores)"] + [str(n) for n in range(1, (os.cpu_count() or 8) + 1)],
                       idx=c.threads, set=self._set_threads))
        f.append(Field("cycle", "Seed space", options=[s[1] for s in SPACES],
                       idx=[s[0] for s in SPACES].index(c.space), set=self._set_space))
        f.append(Field("cycle", "Scan scope", options=[s[0] for s in SCOPES],
                       idx=c.scope, set=self._set_scope))
        f.append(Field("cycle", "Distributed parts",
                       options=[str(n) for n in SHARD_COUNTS],
                       idx=SHARD_COUNTS.index(c.shard_total), set=self._set_shard_total))
        if c.shard_total > 1:
            f.append(Field("cycle", "This computer's part",
                           options=[str(n) for n in range(1, c.shard_total + 1)],
                           idx=c.shard_index - 1, set=self._set_shard_index))
        f.append(Field("text", "Pool name", get=c.pool_name, set=self._set_name))
        f.append(Field("action", "[ Run quick estimate  --  sample 2M seeds, project size/time ]",
                       run=self._do_estimate))
        f.append(Field("action", "[ BUILD POOL  --  write .bspool for the mod ]",
                       run=self._do_build))
        f.append(Field("action", "[ Quit ]", run=self._quit))
        return f

    # setters -----------------------------------------------------------
    def _set_leg(self, i):
        self.crit.legendary = ([""] + self.legendaries)[i]

    def _set_leg_min(self, v):
        self.crit.leg_min = v
        self.crit.leg_max = max(self.crit.leg_max, v)
        if route_position(self.crit.leg_min, self.crit.leg_min_phase) > \
                route_position(self.crit.leg_max, self.crit.leg_max_phase):
            self.crit.leg_max_phase = self.crit.leg_min_phase

    def _set_leg_max(self, v):
        self.crit.leg_max = v
        self.crit.leg_min = min(self.crit.leg_min, v)
        if route_position(self.crit.leg_max, self.crit.leg_max_phase) < \
                route_position(self.crit.leg_min, self.crit.leg_min_phase):
            self.crit.leg_min_phase = self.crit.leg_max_phase

    def _set_leg_neg(self, v):
        self.crit.leg_neg = v

    def _set_leg_min_phase(self, i):
        self.crit.leg_min_phase = PHASES[i]
        if route_position(self.crit.leg_min, self.crit.leg_min_phase) > \
                route_position(self.crit.leg_max, self.crit.leg_max_phase):
            self.crit.leg_max = self.crit.leg_min
            self.crit.leg_max_phase = self.crit.leg_min_phase

    def _set_leg_max_phase(self, i):
        self.crit.leg_max_phase = PHASES[i]
        if route_position(self.crit.leg_max, self.crit.leg_max_phase) < \
                route_position(self.crit.leg_min, self.crit.leg_min_phase):
            self.crit.leg_min = self.crit.leg_max
            self.crit.leg_min_phase = self.crit.leg_max_phase

    def _set_leg_source(self, i):
        self.crit.leg_source = LEGENDARY_SOURCES[i]

    def _set_leg_depth(self, i):
        self.crit.leg_soul_depth = SOUL_DEPTHS[i]

    def _set_leg_routes(self, i):
        self.crit.leg_routes = LEGENDARY_ROUTES[i]

    def _set_route(self, v):
        self.crit.route_collect = v

    def _set_threads(self, i):
        self.crit.threads = i

    def _set_input(self, i):
        self.input_idx = i
        if i:
            self.crit.space = read_pool_header(self.input_pools[i]).get("space", "natural")

    def _set_scope(self, i):
        self.crit.scope = i

    def _set_shard_total(self, i):
        self.crit.shard_total = SHARD_COUNTS[i]
        self.crit.shard_index = min(self.crit.shard_index, self.crit.shard_total)

    def _set_shard_index(self, i):
        self.crit.shard_index = i + 1

    def _set_space(self, i):
        if not self.input_idx:
            self.crit.space = SPACES[i][0]

    def _set_name(self, s):
        self.crit.name = s
        self.crit.name_edited = bool(s)

    def _add_rule(self):
        used = {r[0] for r in self.crit.tag_rules}
        key = next((k for k in self.tag_keys if k not in used), self.tag_keys[0])
        self.crit.tag_rules.append(
            [key, max(1, self.tag_min_ante.get(key, 1)), 8, 1, "small", "big"])

    def _add_voucher_rule(self):
        if self.vouchers:
            self.crit.voucher_rules.append([self.vouchers[0], 1, 8])

    def _add_voucher_exclusion(self):
        used = set(self.crit.voucher_exclusions)
        key = next((item for item in self.vouchers if item not in used), None)
        if key:
            self.crit.voucher_exclusions.append(key)

    def _quit(self):
        raise KeyboardInterrupt

    # ------------------------------------------------------------ drawing
    def draw_form(self):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        title = " Brainstorm Seed Pool Builder "
        scr.addnstr(0, max(0, (w - len(title)) // 2), title, w - 1,
                    curses.A_BOLD | curses.color_pair(2))
        scr.addnstr(1, 2, "Criteria: " + self.crit.summary(), w - 4,
                    curses.color_pair(5))
        flds = self.fields()
        self.sel = max(0, min(self.sel, len(flds) - 1))
        top = 3
        for i, fld in enumerate(flds):
            if top + i >= h - 3:
                break
            attr = curses.color_pair(1) if i == self.sel else 0
            val = self.field_value(fld)
            line = "%-34s %s" % (fld.label, val) if val is not None else fld.label
            scr.addnstr(top + i, 2, line.ljust(w - 5), w - 4, attr)
        keys = "up/down move   left/right change   enter edit   a add tag   d delete selected rule   q quit"
        scr.addnstr(h - 2, 2, keys[: w - 4], w - 4, curses.A_DIM)
        if self.status:
            scr.addnstr(h - 3, 2, self.status[: w - 4], w - 4, curses.color_pair(4))
        scr.refresh()

    def field_value(self, fld):
        if fld.kind == "cycle":
            return "< %s >" % fld.options[fld.idx]
        if fld.kind == "number":
            return "< %d >" % fld.get()
        if fld.kind == "toggle":
            on = getattr(fld, "on", "ON")
            off = getattr(fld, "off", "OFF")
            return "< %s >" % (on if fld.get() else off)
        if fld.kind == "text":
            return fld.get() + ".bspool"
        if fld.kind == "rule":
            key, lo, hi, cnt, min_phase, max_phase = tag_rule_parts(fld.rule)
            return "< %s >  A%d %s - A%d %s  count %d" % (
                TAG_NAMES.get(key, key), lo, PHASE_LABELS[min_phase],
                hi, PHASE_LABELS[max_phase], cnt)
        if fld.kind == "voucher_rule":
            key, lo, hi = fld.rule
            prerequisite = self.voucher_prerequisite.get(key, "")
            needs = "  (requires %s)" % voucher_name(prerequisite) \
                if prerequisite else ""
            return "< %s >  A%d - A%d%s" % (voucher_name(key), lo, hi, needs)
        if fld.kind == "voucher_exclusion":
            return "< %s >  (may appear; route cannot buy it)" % voucher_name(fld.key)
        return None

    # ------------------------------------------------------- interaction
    def adjust(self, fld, delta):
        c = self.crit
        if fld.kind == "cycle":
            fld.set((fld.idx + delta) % len(fld.options))
        elif fld.kind == "number":
            fld.set(max(fld.lo, min(fld.hi, fld.get() + delta)))
        elif fld.kind == "toggle":
            fld.set(not fld.get())
        elif fld.kind == "rule":
            # left/right cycles the tag on the rule row; shift the ante
            # window / count on the sub-editor opened with enter.
            key, lo, hi, cnt, _min_phase, _max_phase = tag_rule_parts(fld.rule)
            i = (self.tag_keys.index(key) + delta) % len(self.tag_keys)
            fld.rule[0] = self.tag_keys[i]
            fld.rule[1] = max(lo, self.tag_min_ante.get(fld.rule[0], 1), 1)
            if fld.rule[2] < fld.rule[1]:
                fld.rule[2] = fld.rule[1]
        elif fld.kind == "voucher_rule":
            key = fld.rule[0]
            i = (self.vouchers.index(key) + delta) % len(self.vouchers)
            fld.rule[0] = self.vouchers[i]
        elif fld.kind == "voucher_exclusion":
            i = (self.vouchers.index(fld.key) + delta) % len(self.vouchers)
            self.crit.voucher_exclusions[fld.ridx] = self.vouchers[i]

    def edit_rule(self, fld):
        """Enter on a tag rule: small sub-loop editing antes/count."""
        rule = fld.rule
        while len(rule) < 6:
            rule.append("small" if len(rule) == 4 else "big")
        part = 0  # min ante, min blind, max ante, max blind, count
        labels = ["earliest ante", "earliest blind", "latest ante",
                  "latest blind", "minimum count"]
        while True:
            self.status = "Editing %s -- %s: left/right change, tab next, enter done" \
                % (TAG_NAMES.get(rule[0], rule[0]), labels[part])
            self.draw_form()
            ch = self.scr.getch()
            if ch in (curses.KEY_ENTER, 10, 13, 27):
                break
            if ch == 9:
                part = (part + 1) % 5
            elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT):
                d = 1 if ch == curses.KEY_RIGHT else -1
                if part == 0:
                    rule[1] = max(1, min(MAX_ANTE, rule[1] + d))
                    rule[2] = max(rule[2], rule[1])
                elif part == 1:
                    i = (TAG_PHASES.index(rule[4]) + d) % len(TAG_PHASES)
                    rule[4] = TAG_PHASES[i]
                elif part == 2:
                    rule[2] = max(1, min(MAX_ANTE, rule[2] + d))
                    rule[1] = min(rule[1], rule[2])
                elif part == 3:
                    i = (TAG_PHASES.index(rule[5]) + d) % len(TAG_PHASES)
                    rule[5] = TAG_PHASES[i]
                else:
                    span = tag_location_count(rule[1], rule[4], rule[2], rule[5])
                    rule[3] = max(1, min(span, rule[3] + d))

                if route_position(rule[1], rule[4]) > route_position(rule[2], rule[5]):
                    if part in (0, 1):
                        rule[2], rule[5] = rule[1], rule[4]
                    else:
                        rule[1], rule[4] = rule[2], rule[5]
                rule[3] = min(rule[3], tag_location_count(
                    rule[1], rule[4], rule[2], rule[5]))
        self.status = ""

    def edit_voucher_rule(self, fld):
        """Enter on a voucher target: edit its inclusive Ante window."""
        rule = fld.rule
        part = 0
        labels = ["earliest ante", "latest ante"]
        while True:
            self.status = "Editing %s -- %s: left/right change, tab next, enter done" \
                % (voucher_name(rule[0]), labels[part])
            self.draw_form()
            ch = self.scr.getch()
            if ch in (curses.KEY_ENTER, 10, 13, 27):
                break
            if ch == 9:
                part = (part + 1) % 2
            elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT):
                delta = 1 if ch == curses.KEY_RIGHT else -1
                if part == 0:
                    rule[1] = max(1, min(MAX_VOUCHER_ANTE, rule[1] + delta))
                    rule[2] = max(rule[2], rule[1])
                else:
                    rule[2] = max(1, min(MAX_VOUCHER_ANTE, rule[2] + delta))
                    rule[1] = min(rule[1], rule[2])
        self.status = ""

    def edit_text(self, fld):
        curses.curs_set(1)
        buf = list(self.crit.name if self.crit.name_edited else "")
        while True:
            self.status = "Pool name: %s_  (enter accept, empty = automatic)" % "".join(buf)
            self.draw_form()
            ch = self.scr.getch()
            if ch in (curses.KEY_ENTER, 10, 13):
                break
            if ch == 27:
                buf = list(self.crit.name)
                break
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
            elif 32 <= ch < 127:
                buf.append(chr(ch))
        curses.curs_set(0)
        fld.set(re.sub(r"[^A-Za-z0-9._+-]", "-", "".join(buf)))
        self.status = ""

    # ------------------------------------------------------------- runs
    def validate(self):
        if not self.crit.predicates():
            return "Add a Legendary, tag requirement, or voucher target first."
        if self.crit.voucher_exclusions and not self.crit.voucher_rules:
            return "A voucher purchase exclusion requires at least one voucher target."
        if self.input_idx and self.crit.shard_total > 1:
            return "Distributed parts currently apply to Balatro's seed space, not an input pool."
        if self.crit.legendary and route_position(
                self.crit.leg_min, self.crit.leg_min_phase) > route_position(
                    self.crit.leg_max, self.crit.leg_max_phase):
            return "Legendary route start must come before its route end."
        if self.crit.legendary and self.crit.leg_max == MAX_ANTE \
                and self.crit.leg_max_phase == "boss":
            return "The final supported Ante cannot end at Boss (its shop uses the next RNG Ante)."
        for rule in self.crit.tag_rules:
            key, lo, hi, cnt, min_phase, max_phase = tag_rule_parts(rule)
            pool_min = self.tag_min_ante.get(key, 0)
            if pool_min > hi:
                return "%s cannot appear before ante %d." % (TAG_NAMES.get(key, key), pool_min)
            if route_position(lo, min_phase) > route_position(hi, max_phase):
                return "%s route start must come before its route end." % TAG_NAMES.get(key, key)
            if cnt > tag_location_count(lo, min_phase, hi, max_phase):
                return "%s count exceeds the selected blind window." % TAG_NAMES.get(key, key)
            if (self.crit.voucher_rules and self.crit.route_collect
                    and key in ("tag_voucher", "tag_double")):
                return ("Collected Voucher and Double Tags are not yet supported "
                        "with voucher targets; observe that tag or remove it.")
        for key, lo, hi in self.crit.voucher_rules:
            if key not in self.vouchers:
                return "%s is unavailable in this snapshot." % voucher_name(key)
            if lo < 1 or hi < lo or hi > MAX_VOUCHER_ANTE:
                return "%s has an invalid Ante window." % voucher_name(key)
        if len(set(self.crit.voucher_exclusions)) != len(self.crit.voucher_exclusions):
            return "Each voucher purchase exclusion can only be added once."
        for key in self.crit.voucher_exclusions:
            if key not in self.vouchers:
                return "%s is unavailable in this snapshot." % voucher_name(key)
        return None

    def _do_estimate(self):
        err = self.validate()
        if err:
            self.status = err
            return
        out = os.path.join(tempfile.mkdtemp(prefix="bs_pool_est_"), "estimate")
        text = self.crit.text("count", ESTIMATE_COUNT, apply_shard=False,
                              checkpoint=ESTIMATE_CHECKPOINT)
        input_pool = self.input_pools[self.input_idx] or None
        self.run_screen(Runner(self.snap.current_model_copy(), text, out, input_pool),
                        "Filtering input pool" if input_pool else "Estimating (2M-seed sample)",
                        estimate=True)

    def _do_build(self):
        err = self.validate()
        if err:
            self.status = err
            return
        os.makedirs(POOL_DIR, exist_ok=True)
        out = os.path.join(POOL_DIR, self.crit.pool_name() + ".bspool")
        state = read_state(out + ".state")
        if state.get("done") == "1":
            self.status = "That pool is already complete -- pick a new name (or delete it)."
            return
        text = self.crit.text("binary", SCOPES[self.crit.scope][1])
        input_pool = self.input_pools[self.input_idx] or None
        self.run_screen(Runner(self.snap.current_model_copy(), text, out, input_pool),
                        "Building " + os.path.basename(out), estimate=False)

    def run_screen(self, runner, title, estimate):
        scr = self.scr
        scr.timeout(200)
        started = time.time()
        stopped = False
        try:
            while True:
                h, w = scr.getmaxyx()
                scr.erase()
                scr.addnstr(0, 2, title, w - 4, curses.A_BOLD | curses.color_pair(2))
                scr.addnstr(1, 2, self.crit.summary(), w - 4, curses.color_pair(5))
                total = runner.total or 1
                frac = min(1.0, runner.scanned / total)
                barw = max(10, w - 24)
                bar = "#" * int(frac * barw)
                scr.addnstr(3, 2, "[%s] %5.1f%%" % (bar.ljust(barw), frac * 100), w - 4)
                scr.addnstr(5, 2, "Scanned : %s / %s seeds"
                            % (f"{runner.scanned:,}", f"{runner.total:,}"), w - 4)
                scr.addnstr(6, 2, "Matches : %s" % f"{runner.matched:,}", w - 4,
                            curses.color_pair(3))
                if runner.rate > 0:
                    eta = (total - runner.scanned) / runner.rate
                    scr.addnstr(7, 2, "Rate    : %s seeds/s      ETA %s"
                                % (f"{int(runner.rate):,}", human_secs(eta)), w - 4)
                scr.addnstr(8, 2, "Elapsed : %s" % human_secs(time.time() - started), w - 4)
                y = 10
                for line in list(runner.lines)[-max(1, h - y - 3):]:
                    if y >= h - 2:
                        break
                    scr.addnstr(y, 2, line[: w - 4], w - 4, curses.A_DIM)
                    y += 1
                foot = "s stop (checkpointed -- rerun Build with the same name to resume)"
                scr.addnstr(h - 2, 2, foot[: w - 4], w - 4, curses.A_DIM)
                scr.refresh()
                if runner.done():
                    break
                ch = scr.getch()
                if ch in (ord("s"), ord("S"), ord("q")) and not stopped:
                    runner.stop()
                    stopped = True
        finally:
            scr.timeout(-1)
        rc = runner.returncode()
        manifest = read_manifest(runner.output + ".manifest")
        self.result_screen(runner, rc, manifest, estimate, stopped)

    def result_screen(self, runner, rc, manifest, estimate, stopped):
        scr = self.scr
        h, w = scr.getmaxyx()
        scr.erase()
        lines = []
        if stopped or rc == 130:
            lines.append(("Stopped at a checkpoint. Run BUILD POOL again with the same "
                          "name to resume exactly where it left off.", 2))
        elif rc == 0:
            lines.append(("Done." if not estimate else "Estimate complete.", 3))
        else:
            lines.append(("Scanner exited with an error (code %s):" % rc, 4))
            for line in list(runner.lines)[-6:]:
                lines.append((line, 4))
        if manifest:
            matched = int(manifest.get("matched", "0"))
            scanned = int(manifest.get("scanned", "1")) or 1
            rate = float(manifest.get("seeds_per_second", "0") or 0)
            lines.append(("Scanned %s seeds, matched %s (%.6f%%)"
                          % (f"{scanned:,}", f"{matched:,}", 100.0 * matched / scanned), 0))
            if estimate:
                proj = float(manifest.get("projected_full_matches", "0") or 0)
                projb = float(manifest.get("projected_compressed_bytes",
                                           manifest.get("projected_u64_bytes", "0")) or 0)
                space_total = float(manifest.get("seedspace", "0") or 0) or SEEDSPACE
                space_label = next(label for key, label, _limit in SPACES
                                   if key == self.crit.space)
                scope_name, scope_limit = SCOPES[self.crit.scope]
                scope_start, scope_end = self.crit.shard_bounds(scope_limit)
                scope_count = scope_end - scope_start
                if self.crit.shard_total > 1:
                    scope_name += " -- part %d of %d" % (
                        self.crit.shard_index, self.crit.shard_total)
                if matched == 0:
                    lines.append(("No matches appeared in this quick sample; use a larger "
                                  "test build for a rare filter.", 2))
                elif matched < 25:
                    lines.append(("Only %d matches appeared; the size projection is rough."
                                  % matched, 2))
                if matched:
                    ratio = scope_count / space_total
                    lines.append(("Selected scope (%s; %s seeds): ~%s matches, ~%s on disk"
                                  % (scope_name, f"{scope_count:,}",
                                     f"{int(proj * ratio):,}", human_bytes(projb * ratio)), 0))
                    lines.append(("Complete chosen seed space (%s; %s seeds): ~%s matches, ~%s on disk"
                                  % (space_label, f"{int(space_total):,}",
                                     f"{int(proj):,}", human_bytes(projb)), 0))
                if rate > 0:
                    lines.append(("Selected-scope time at this rate: %s"
                                  % human_secs(scope_count / rate), 0))
                    lines.append(("Complete chosen-seed-space time at this rate: %s"
                                  % human_secs(space_total / rate), 0))
            elif rc == 0:
                lines.append(("Pool file: %s" % runner.output, 3))
                lines.append(("It now appears in the in-game Seed Pool selector; "
                              "share that one file to share the pool.", 0))
        scr.addnstr(0, 2, " Result ", w - 4, curses.A_BOLD | curses.color_pair(2))
        y = 2
        for text, color in lines:
            for start in range(0, len(text), w - 6):
                if y >= h - 2:
                    break
                scr.addnstr(y, 2, text[start:start + w - 6], w - 4,
                            curses.color_pair(color) if color else 0)
                y += 1
        scr.addnstr(h - 2, 2, "press any key", w - 4, curses.A_DIM)
        scr.refresh()
        scr.getch()

    # ---------------------------------------------------------- main loop
    def loop(self):
        while True:
            self.draw_form()
            flds = self.fields()
            fld = flds[self.sel]
            ch = self.scr.getch()
            if ch in (ord("q"), ord("Q")):
                return
            if ch == curses.KEY_UP:
                self.sel = max(0, self.sel - 1)
            elif ch == curses.KEY_DOWN:
                self.sel = min(len(flds) - 1, self.sel + 1)
            elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT):
                self.status = ""
                self.adjust(fld, 1 if ch == curses.KEY_RIGHT else -1)
            elif ch in (ord("a"), ord("A")):
                if len(self.crit.tag_rules) < MAX_TAG_RULES and self.tag_keys:
                    self._add_rule()
            elif ch in (ord("d"), ord("D")) and fld.kind == "rule":
                del self.crit.tag_rules[fld.ridx]
            elif ch in (ord("d"), ord("D")) and fld.kind == "voucher_rule":
                del self.crit.voucher_rules[fld.ridx]
            elif ch in (ord("d"), ord("D")) and fld.kind == "voucher_exclusion":
                del self.crit.voucher_exclusions[fld.ridx]
            elif ch in (curses.KEY_ENTER, 10, 13):
                self.status = ""
                if fld.kind == "action":
                    fld.run()
                elif fld.kind == "rule":
                    self.edit_rule(fld)
                elif fld.kind == "voucher_rule":
                    self.edit_voucher_rule(fld)
                elif fld.kind == "text":
                    self.edit_text(fld)
                elif fld.kind == "toggle":
                    fld.set(not fld.get())


def preflight():
    problems = []
    if not os.path.exists(POOL_BIN):
        if IS_WINDOWS:
            # No compiler assumed on Windows: the prebuilt exes ship in the
            # release zip (see README's Windows section).
            problems.append(
                "brainstorm_seed_pool.exe not found. Re-run the Windows "
                "Brainstorm installer so 'Seed Pool Builder\\' contains both "
                "the app and scanner. Older/source layouts may instead put "
                "the scanner in Mods\\Brainstorm\\native\\. (Building from "
                "source instead: see native/build_windows.sh.)")
        else:
            build = os.path.join(NATIVE_DIR, "build.sh")
            print("Building native helpers (first run)...")
            r = subprocess.run(["sh", build], cwd=MOD_DIR)
            if r.returncode != 0 or not os.path.exists(POOL_BIN):
                problems.append("Could not build native/brainstorm_seed_pool -- "
                                "run 'sh native/build.sh' and check for errors.")
    if not os.path.exists(SNAPSHOT):
        problems.append(
            "native_search.cfg not found. Launch Balatro and briefly toggle an\n"
            "auto-reroll (Ctrl+A on, then off) so Brainstorm writes the snapshot\n"
            "of YOUR unlocks; then run this again.")
    else:
        try:
            Snapshot(SNAPSHOT).current_model_copy()
        except (OSError, ValueError, TypeError) as e:
            problems.append(str(e))
    return problems


def main():
    if "--headless-criteria" in sys.argv:
        c = Criteria()
        c.legendary = "j_perkeo"
        c.leg_soul_depth = 2  # legacy exclusive value stays writable
        c.leg_min, c.leg_max = 7, 7
        c.tag_rules.append(["tag_rare", 1, 8, 1])
        sys.stdout.write(c.text("binary", 0))
        print("# auto name: %s" % c.pool_name(), file=sys.stderr)
        c2 = Criteria()
        c2.legendary = "j_perkeo"
        c2.leg_soul_depth = 0
        assert "soul_depth any" in c2.text("binary", 0)
        assert "2souls" in c2.pool_name()
        sys.stdout.write(c2.text("binary", 0))
        print("# auto name: %s" % c2.pool_name(), file=sys.stderr)
        return 0
    problems = preflight()
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    snap = Snapshot(SNAPSHOT)
    if (not snap.usable_legendaries() and not snap.usable_tags()
            and not snap.usable_vouchers()):
        print("The snapshot has no usable pools; regenerate native_search.cfg in-game.",
              file=sys.stderr)
        return 1
    if "--headless-estimate" in sys.argv:
        c = Criteria()
        c.legendary = "j_perkeo"
        c.tag_rules.append(["tag_rare", 1, 8, 1])
        out = os.path.join(tempfile.mkdtemp(prefix="bs_pool_est_"), "estimate")
        r = Runner(snap.current_model_copy(), c.text("count", 3_000_000), out)
        while not r.done():
            time.sleep(0.1)
        m = read_manifest(out + ".manifest")
        print("rc=%d matched=%s scanned=%s" % (r.returncode(),
                                               m.get("matched"), m.get("scanned")))
        return 0 if r.returncode() == 0 else 1
    if curses is None:
        print("This terminal UI needs curses, which Windows Python does not\n"
              "ship. Use the browser UI instead: double-click\n"
              "'Seed Pool Builder.bat' (or run: python tools\\pool_builder_web.py).",
              file=sys.stderr)
        return 1
    try:
        curses.wrapper(lambda scr: App(scr, snap).loop())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
