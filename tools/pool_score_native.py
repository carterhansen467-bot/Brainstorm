"""One persistent native exact tag scorer per caller/worker.

The helper protocol accepts only bounded numeric placement records. It never
opens a pool. Native errors, bad replies, and cancellation close the scorer;
there is no implicit switch to a different scoring implementation.
"""

from collections import deque
import json
import math
from pathlib import Path
import queue
import subprocess
import threading
import time

try:
    import tagcalc as model
except ImportError:
    from tools import tagcalc as model


PROTOCOL_VERSION = 1
MODEL_VERSION = "tagcalc-1-exact"
_MAX_REPLY = 128 * 1024
_EOF = object()


class NativeScoreError(RuntimeError):
    pass


class NativeScoreCancelled(NativeScoreError):
    pass


def _integer(value, first, last):
    return type(value) is int and first <= value <= last


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate result field")
        result[name] = value
    return result


def _normalize(baseline, placements):
    if (not isinstance(baseline, (list, tuple)) or len(baseline) != 2
            or not _integer(baseline[0], 1, 40) or not _integer(baseline[1], 0, 2)):
        raise ValueError("Baseline must contain ante 1–40 and blind 0–2.")
    by_location = {}
    for placement in placements:
        if (not isinstance(placement, (list, tuple)) or len(placement) != 3
                or placement[0] not in ("neg", "rare")
                or not _integer(placement[1], 1, 39) or not _integer(placement[2], 0, 1)):
            raise ValueError("A placement must contain neg/rare, ante 1–39, and blind 0–1.")
        kind, ante, blind = placement
        if ante == 39:
            continue
        key = ante, blind
        if key in by_location and by_location[key] != kind:
            raise ValueError("A physical placement cannot be both Negative and Rare.")
        by_location[key] = kind
    return tuple(baseline), tuple((by_location[key], *key) for key in sorted(by_location))


def _validate_result(value, request, baseline, placements, details):
    if (not isinstance(value, dict) or set(value) != {"id", "score", "fillers", "route"}
            or type(value["id"]) is not int or value["id"] != request
            or not _number(value["score"]) or not (value["score"] == -1 or value["score"] > 0)):
        raise ValueError("Invalid result identity or score")
    fillers, route = value["fillers"], value["route"]
    first = min(baseline[0] + 1, 39)
    if (not isinstance(fillers, list) or len(fillers) != 2
            or any(not _integer(x, first, 39) for x in fillers) or fillers != sorted(fillers)
            or not isinstance(route, list) or len(route) > len(placements) // 2):
        raise ValueError("Invalid filler or route structure")
    if value["score"] == -1:
        if route or fillers != [first, first]:
            raise ValueError("Invalid no-route result")
    elif details:
        if not route:
            raise ValueError("Missing route details")
        # Replay only the returned route (not the optimization). This catches
        # corrupt indexes, strings, shop timing, and scores without repeating
        # the expensive search in Python.
        tags = [{"kind": kind, "real_ante": ante, "blind": blind,
                 "label": "%s A%d%s" % (kind.upper(), ante, model.BLIND_SHORT[blind])}
                for kind, ante, blind in placements]
        events = model.build_events(tags, fillers, 1, 40)
        pairs = model.make_candidate_pairs(events)
        previous_shop = model.first_copy_index(events, baseline)
        T, r, previous_pair = 1.0, 7.0 / 17.0, -1
        for index, actual in enumerate(route):
            if (not isinstance(actual, dict) or not _integer(actual.get("pair_index"), 0, len(pairs) - 1)
                    or type(actual.get("final")) is not bool):
                raise ValueError("Invalid route pair")
            pi = actual["pair_index"]
            pair = pairs[pi]
            if pair["first_idx"] <= previous_shop:
                raise ValueError("Overlapping route pairs")
            final = index == len(route) - 1
            k = model.copy_count_between(events, previous_shop - 1, pair["first_idx"], set())
            g = model.copy_count_between(events, pair["first_idx"], pair["shop_idx"],
                                         {pair["first_idx"], pair["second_idx"]})
            M = (model.final_redeem_multiplier if final else model.normal_redeem_multiplier)(r, k, g)
            F = model.copy_count_from(events, pair["shop_idx"], set()) if final else 0
            after = T * M
            new_r = 0.5 if final else model.next_r_after_normal_redeem(r, k, g, M)
            expected = model.make_choice(previous_pair, pi, pair, final,
                                         after * F if final else after, after, new_r, F,
                                         k, g, M, 1.0 - r)
            if set(actual) != set(expected):
                raise ValueError("Missing or unknown route fields")
            for name, expected_value in expected.items():
                found = actual[name]
                if type(expected_value) is float:
                    if not _number(found) or not math.isclose(found, expected_value, rel_tol=1e-12, abs_tol=1e-12):
                        raise ValueError("Inconsistent route " + name)
                elif type(found) is not type(expected_value) or found != expected_value:
                    raise ValueError("Inconsistent route " + name)
            previous_pair, previous_shop, T, r = pi, pair["shop_idx"], after, new_r
        if not math.isclose(value["score"], route[-1]["score"], rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("Inconsistent final score")
    elif route:
        raise ValueError("Unexpected route details")
    return value["score"], route, tuple(fillers)


class NativeScorer:
    """Use once per worker; concurrent score calls on one instance are rejected."""

    def __init__(self, binary, cancel_check=None):
        self.binary = str(Path(binary).resolve())
        self.cancel_check = cancel_check
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._replies = queue.Queue(maxsize=2)
        self._stderr = deque(maxlen=4)
        self._process = None
        self._threads = []
        self._request = 0
        try:
            self._check_cancel()
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}
            self._process = subprocess.Popen([self.binary, "score-tags"], stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
            self._threads = [threading.Thread(target=self._read_stdout, daemon=True),
                             threading.Thread(target=self._read_stderr, daemon=True)]
            for thread in self._threads:
                thread.start()
            if self._read_reply(deadline=time.monotonic() + 15) != b"BRAINSTORM_TAG_SCORE 1\n":
                raise NativeScoreError("Native scorer protocol is unsupported; use the matching current helper.")
        except BaseException:
            self.close()
            raise

    def _check_cancel(self):
        if self.cancel_check is not None and self.cancel_check():
            raise NativeScoreCancelled("Native scoring cancelled.")

    def _put_reply(self, value):
        while not self._closed.is_set():
            try:
                self._replies.put(value, timeout=0.05)
                return
            except queue.Full:
                pass

    def _read_stdout(self):
        try:
            while not self._closed.is_set():
                line = self._process.stdout.readline(_MAX_REPLY + 1)
                if not line:
                    self._put_reply(_EOF)
                    return
                if len(line) > _MAX_REPLY or not line.endswith(b"\n"):
                    self._put_reply(NativeScoreError("Native scorer returned an oversized or incomplete reply."))
                    return
                # C stdio uses CRLF on Windows; normalize only that terminator.
                if line.endswith(b"\r\n"):
                    line = line[:-2] + b"\n"
                self._put_reply(line)
        except (OSError, ValueError) as error:
            self._put_reply(NativeScoreError("Cannot read native scorer: " + str(error)))

    def _read_stderr(self):
        try:
            while not self._closed.is_set():
                data = self._process.stderr.read(4096)
                if not data:
                    return
                self._stderr.append(data)
        except (OSError, ValueError):
            pass

    def _read_reply(self, deadline=None):
        while True:
            self._check_cancel()
            if self._closed.is_set():
                raise NativeScoreError("Native scorer is closed.")
            if deadline is not None and time.monotonic() >= deadline:
                raise NativeScoreError("Native scorer did not complete its protocol handshake.")
            try:
                value = self._replies.get(timeout=0.05)
            except queue.Empty:
                continue
            if value is _EOF:
                diagnostic = b"".join(self._stderr).decode("utf-8", "replace").strip()
                raise NativeScoreError("Native scorer stopped without a complete result." +
                                       (" " + diagnostic if diagnostic else ""))
            if isinstance(value, BaseException):
                raise value
            return value

    def score(self, baseline, placements, details=True):
        baseline, placements = _normalize(baseline, placements)
        if type(details) is not bool:
            raise ValueError("details must be true or false.")
        if not self._lock.acquire(blocking=False):
            raise NativeScoreError("One native scorer cannot handle concurrent score requests.")
        try:
            self._check_cancel()
            if self._closed.is_set():
                raise NativeScoreError("Native scorer is closed.")
            self._request += 1
            if self._request > (1 << 64) - 1:
                raise NativeScoreError("Native scorer request identity exhausted.")
            numbers = [self._request, *baseline, int(details), len(placements)]
            for kind, ante, blind in placements:
                numbers.extend((0 if kind == "neg" else 1, ante, blind))
            try:
                self._process.stdin.write((" ".join(map(str, numbers)) + "\n").encode("ascii"))
                self._process.stdin.flush()
            except (OSError, ValueError) as error:
                # The helper can exit, or another thread can close this
                # scorer, after the closed check above. Keep transport
                # failures in the same API as failed reads and bad replies.
                self._check_cancel()
                if self._closed.is_set():
                    raise NativeScoreError("Native scorer is closed.") from error
                raise NativeScoreError("Cannot send native scorer request: " + str(error)) from error
            raw = self._read_reply()
            try:
                result = json.loads(raw, object_pairs_hook=_object,
                                    parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Invalid numeric result")))
                return _validate_result(result, self._request, baseline, placements, details)
            except (ValueError, TypeError, KeyError, OverflowError) as error:
                raise NativeScoreError("Native scorer returned an invalid result: " + str(error)) from error
        except BaseException:
            self.close()
            raise
        finally:
            self._lock.release()

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            except OSError:
                pass
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=0.2)

    def __enter__(self):
        if self._closed.is_set():
            raise NativeScoreError("Native scorer is closed.")
        return self

    def __exit__(self, *unused):
        self.close()
