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
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque

if getattr(sys, "frozen", False):
    # PyInstaller bundle ("Seed Pool Builder.exe" in the mod root): __file__
    # would point into the onefile extraction dir, not the mod folder.
    MOD_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    MOD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NATIVE_DIR = os.path.join(MOD_DIR, "native")
IS_WINDOWS = os.name == "nt"
POOL_BIN = os.path.join(NATIVE_DIR,
                        "brainstorm_seed_pool" + (".exe" if IS_WINDOWS else ""))
SNAPSHOT = os.path.join(MOD_DIR, "native_search.cfg")
POOL_DIR = os.path.join(MOD_DIR, "seed_pools")
SEEDSPACE = 1785793904896  # 34^8: seeds the game's generator can deal
# 36^1 + ... + 36^8: every seed the game ACCEPTS typed in -- adds 0/O and
# 1-7 character seeds. Only reachable by typing, never by rerolling.
SEEDSPACE_TOTAL = 2901713047668
SPACES = [
    ("natural", "Natural (34^8 -- seeds the game deals)", SEEDSPACE),
    ("total", "All typeable (adds 0/O + short seeds)", SEEDSPACE_TOTAL),
]
MAX_TAG_RULES = 16
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

SCOPES = [
    ("Entire seed space", 0),
    ("First 10M seeds", 10_000_000),
    ("First 100M seeds", 100_000_000),
    ("First 1B seeds", 1_000_000_000),
    ("First 10B seeds", 10_000_000_000),
]
ESTIMATE_COUNT = 100_000_000
SHARD_COUNTS = (1, 2, 4, 8, 16, 32, 64, 128, 256)


def joker_name(key):
    return key[2:].replace("_", " ").title() if key.startswith("j_") else key


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


class Criteria:
    """Everything the criteria file expresses, held as UI state."""

    def __init__(self):
        self.legendary = ""      # "" = none
        self.leg_min = 1
        self.leg_max = 8
        self.leg_neg = False
        self.leg_second_soul = False  # exact Soul #2; never first-or-second
        self.tag_rules = []      # [key, min, max, count]
        self.route_collect = True
        self.threads = 0         # 0 = auto
        self.scope = 0           # index into SCOPES
        self.space = "natural"   # SPACES key: "natural" or "total"
        self.shard_total = 1     # exact non-overlapping distributed parts
        self.shard_index = 1     # this machine's 1-based part
        self.name = ""           # "" = auto
        self.name_edited = False

    def seedspace(self):
        return SEEDSPACE_TOTAL if self.space == "total" else SEEDSPACE

    def predicates(self):
        return bool(self.legendary) or bool(self.tag_rules)

    def auto_name(self):
        bits = []
        for key, lo, hi, cnt in self.tag_rules:
            b = key.replace("tag_", "")
            b += "-a%d" % lo if lo == hi else "-a%d-%d" % (lo, hi)
            if cnt > 1:
                b += "x%d" % cnt
            bits.append(b)
        if self.legendary:
            b = self.legendary.replace("j_", "")
            if self.leg_neg:
                b = "neg-" + b
            if self.leg_second_soul:
                b += "-soul2"
            b += "-a%d" % self.leg_min if self.leg_min == self.leg_max \
                else "-a%d-%d" % (self.leg_min, self.leg_max)
            bits.append(b)
        if self.space == "total":
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

    def text(self, fmt, count, apply_shard=True):
        start, end = self.shard_bounds(count) if apply_shard else (0, count)
        count_text = "all" if count == 0 and start == 0 and self.shard_total == 1 \
            else str(end - start)
        lines = ["# generated by tools/brainstorm_pool_builder.py",
                 "poolver 1",
                 "threads %d" % self.threads,
                 "start %d" % start,
                 "count %s" % count_text,
                 "checkpoint 16777216",
                 "chunk 16384",
                 "resume 1",
                 "format %s" % fmt,
                 "tag_route %s" % ("collect" if self.route_collect else "observe")]
        if self.space != "natural":
            lines.append("space %s" % self.space)
        lines.append("label %s" % self.pool_name())
        for key, lo, hi, cnt in self.tag_rules:
            lines.append("tag %s %d %d %d" % (key, lo, hi, cnt))
        if self.legendary:
            lines.append("legendary %s %d %d %d"
                         % (self.legendary, self.leg_min, self.leg_max,
                            1 if self.leg_neg else 0))
            if self.leg_second_soul:
                lines.append("soul_depth 2")
        lines.append("end")
        return "\n".join(lines) + "\n"

    def summary(self):
        parts = []
        for key, lo, hi, cnt in self.tag_rules:
            s = TAG_NAMES.get(key, key)
            s += " x%d" % cnt if cnt > 1 else ""
            s += " (antes %d-%d)" % (lo, hi)
            parts.append(s)
        if self.legendary:
            s = ("Negative " if self.leg_neg else "") + joker_name(self.legendary)
            s += " second reachable Soul only" if self.leg_second_soul else " first reachable Soul"
            s += " (antes %d-%d)" % (self.leg_min, self.leg_max)
            parts.append(s)
        out = " + ".join(parts) if parts else "(no criteria yet)"
        if self.space == "total":
            out += " [all typeable seeds]"
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
        # progress
        self.scanned = 0
        self.total = 0
        self.matched = 0
        self.rate = 0.0

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


def read_pool_header(path):
    out = {}
    try:
        with open(path, "rb") as f:
            raw = f.read(1024).split(b"\0", 1)[0].decode("latin-1")
        for line in raw.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                out[parts[0]] = parts[1].strip()
            if parts and parts[0] == "end":
                break
    except OSError:
        pass
    return out


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
        self.input_pools = [""]
        if os.path.isdir(POOL_DIR):
            for fn in sorted(os.listdir(POOL_DIR)):
                path = os.path.join(POOL_DIR, fn)
                if fn.endswith(".bspool") and read_pool_header(path).get("complete") == "1":
                    self.input_pools.append(path)
        self.input_idx = 0
        if "j_perkeo" in self.legendaries:
            self.crit.legendary = "j_perkeo"
        elif self.legendaries:
            self.crit.legendary = self.legendaries[0]
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
            f.append(Field("toggle", "  Second Soul only (exclusive)",
                           get=lambda: c.leg_second_soul,
                           set=self._set_leg_second_soul,
                           on="ON -- target must be from Soul #2",
                           off="OFF -- target must be from the first reachable Soul"))
            f.append(Field("number", "  Earliest ante", get=lambda: c.leg_min,
                           set=self._set_leg_min, lo=1, hi=MAX_ANTE))
            f.append(Field("number", "  Latest ante", get=lambda: c.leg_max,
                           set=self._set_leg_max, lo=1, hi=MAX_ANTE))
            f.append(Field("toggle", "  Require Negative edition",
                           get=lambda: c.leg_neg, set=self._set_leg_neg))
        for i, rule in enumerate(c.tag_rules):
            f.append(Field("rule", "Tag rule %d" % (i + 1), rule=rule, ridx=i))
        if len(c.tag_rules) < MAX_TAG_RULES and self.tag_keys:
            f.append(Field("action", "[ Add a tag requirement ]", run=self._add_rule))
        f.append(Field("toggle", "Tag route",
                       get=lambda: c.route_collect, set=self._set_route,
                       on="collect (skip matched blinds)",
                       off="observe (play those blinds)"))
        f.append(Field("cycle", "Input seeds",
                       options=["Balatro's seed space"] +
                       [os.path.basename(p) for p in self.input_pools[1:]],
                       idx=self.input_idx, set=self._set_input))
        f.append(Field("cycle", "Threads",
                       options=["Auto (cores-1)"] + [str(n) for n in range(1, (os.cpu_count() or 8) + 1)],
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
        f.append(Field("action", "[ Run estimate  --  sample 100M seeds, project size/time ]",
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

    def _set_leg_max(self, v):
        self.crit.leg_max = v
        self.crit.leg_min = min(self.crit.leg_min, v)

    def _set_leg_neg(self, v):
        self.crit.leg_neg = v

    def _set_leg_second_soul(self, v):
        self.crit.leg_second_soul = v

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
        self.crit.tag_rules.append([key, max(1, self.tag_min_ante.get(key, 1)), 8, 1])

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
        keys = "up/down move   left/right change   enter select   a add tag   d delete tag   q quit"
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
            key, lo, hi, cnt = fld.rule
            return "< %s >  antes %d-%d  count %d" % (TAG_NAMES.get(key, key), lo, hi, cnt)
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
            key, lo, hi, cnt = fld.rule
            i = (self.tag_keys.index(key) + delta) % len(self.tag_keys)
            fld.rule[0] = self.tag_keys[i]
            fld.rule[1] = max(lo, self.tag_min_ante.get(fld.rule[0], 1), 1)
            if fld.rule[2] < fld.rule[1]:
                fld.rule[2] = fld.rule[1]

    def edit_rule(self, fld):
        """Enter on a tag rule: small sub-loop editing antes/count."""
        rule = fld.rule
        part = 0  # 0 min ante, 1 max ante, 2 count
        labels = ["earliest ante", "latest ante", "minimum count"]
        while True:
            self.status = "Editing %s -- %s: left/right change, tab next, enter done" \
                % (TAG_NAMES.get(rule[0], rule[0]), labels[part])
            self.draw_form()
            ch = self.scr.getch()
            if ch in (curses.KEY_ENTER, 10, 13, 27):
                break
            if ch == 9:
                part = (part + 1) % 3
            elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT):
                d = 1 if ch == curses.KEY_RIGHT else -1
                if part == 0:
                    rule[1] = max(1, min(MAX_ANTE, rule[1] + d))
                    rule[2] = max(rule[2], rule[1])
                elif part == 1:
                    rule[2] = max(1, min(MAX_ANTE, rule[2] + d))
                    rule[1] = min(rule[1], rule[2])
                else:
                    span = 2 * (rule[2] - rule[1] + 1)
                    rule[3] = max(1, min(span, rule[3] + d))
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
            return "Add a legendary or at least one tag requirement first."
        if self.input_idx and self.crit.shard_total > 1:
            return "Distributed parts currently apply to Balatro's seed space, not an input pool."
        for key, lo, hi, cnt in self.crit.tag_rules:
            pool_min = self.tag_min_ante.get(key, 0)
            if pool_min > hi:
                return "%s cannot appear before ante %d." % (TAG_NAMES.get(key, key), pool_min)
        return None

    def _do_estimate(self):
        err = self.validate()
        if err:
            self.status = err
            return
        out = os.path.join(tempfile.mkdtemp(prefix="bs_pool_est_"), "estimate")
        text = self.crit.text("count", ESTIMATE_COUNT, apply_shard=False)
        input_pool = self.input_pools[self.input_idx] or None
        self.run_screen(Runner(self.snap.current_model_copy(), text, out, input_pool),
                        "Filtering input pool" if input_pool else "Estimating (100M-seed sample)",
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
                lines.append(("Projected over the full seed space: ~%s seeds, ~%s on disk"
                              % (f"{int(proj):,}", human_bytes(projb)), 0))
                if rate > 0:
                    space_total = float(manifest.get("seedspace", "0") or 0) or SEEDSPACE
                    lines.append(("Projected full-scan time at this rate: %s"
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
            elif ch in (curses.KEY_ENTER, 10, 13):
                self.status = ""
                if fld.kind == "action":
                    fld.run()
                elif fld.kind == "rule":
                    self.edit_rule(fld)
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
                "native\\brainstorm_seed_pool.exe not found. Download the "
                "Brainstorm Windows release zip and copy both .exe files "
                "into the Mods\\Brainstorm\\native\\ folder, then run this "
                "again. (Building from source instead: see "
                "native/build_windows.sh.)")
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
        c.leg_second_soul = True
        c.leg_min, c.leg_max = 7, 7
        c.tag_rules.append(["tag_rare", 1, 8, 1])
        sys.stdout.write(c.text("binary", 0))
        print("# auto name: %s" % c.pool_name(), file=sys.stderr)
        return 0
    problems = preflight()
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    snap = Snapshot(SNAPSHOT)
    if not snap.usable_legendaries() and not snap.usable_tags():
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
