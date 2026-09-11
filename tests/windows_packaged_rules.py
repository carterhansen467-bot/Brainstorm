#!/usr/bin/env python3
"""Smoke both assembled Windows apps through their real HTTP interfaces.

Run after package assembly: python tests/windows_packaged_rules.py
Local source equivalent:    python tests/windows_packaged_rules.py --source

Every pool/profile is synthetic and lives in a temporary mod. Packaged mode
executes the actual PyInstaller apps; it never simulates a frozen interpreter.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import brainstorm_pool_organizer as organizer
from pool_organizer import descriptor, write_custom_bsp3


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def check_port_free(port):
    with socket.socket() as probe:
        if os.name != "nt":
            # The HTTP server also enables address reuse; TIME_WAIT from this
            # test's completed requests must not look like a live listener.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        elif hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(
                "Port %d is occupied; close the existing app before this smoke test. "
                "It will not contact or stop that process." % port) from exc


def request(base, path, data=None, *, text=False, timeout=20):
    payload = None if data is None else json.dumps(data).encode("utf-8")
    call = Request(base + path, data=payload,
                   headers={"Content-Type": "application/json"} if payload else {})
    try:
        with urlopen(call, timeout=timeout) as response:
            result = response.read().decode("utf-8")
    except HTTPError as exc:
        raise RuntimeError("HTTP %d for %s: %s" % (
            exc.code, path, exc.read().decode("utf-8", "replace"))) from exc
    result = result if text else json.loads(result)
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError("%s: %s" % (path, result["error"]))
    return result


def make_mod(directory, source_mode):
    directory.mkdir(parents=True)
    (directory / "Brainstorm_main.lua").write_text("-- isolated packaged smoke fixture\n", encoding="ascii")
    (directory / "manifest.json").write_text('{"id":"Brainstorm-smoke"}\n', encoding="ascii")
    # Enough current profile data to open the Builder. No actual scan is
    # requested, and no game snapshot or user's pool directory is read.
    (directory / "native_search.cfg").write_text("\n".join([
        "modelver 6", "tagdef tag_negative 1 2", "tagdef tag_rare 1 0",
        "specialdef c_soul 1", "boostdef p_arcana_mega_1 1 1 5 A 1",
        "boostdef p_spectral_normal_1 1 1 2 S 1", "end", "",
    ]), encoding="ascii")
    if source_mode:
        scanner = ROOT / "native" / ("brainstorm_seed_pool.exe" if os.name == "nt"
                                      else "brainstorm_seed_pool")
        require(scanner.is_file(), "Build the native helper before running --source: %s" % scanner)
        (directory / "native").mkdir()
        shutil.copy2(scanner, directory / "native" / scanner.name)
    pools = directory / "seed_pools"
    pools.mkdir()
    token = uuid.uuid4().hex[:12]
    first = pools / ("X-L1-%s.bspool" % token)
    second = pools / ("Y-L2-%s.bspool" % token)
    combined = pools / ("Complete-%s.bspool" % token)

    def tag(name, ante, phase):
        return descriptor(1, "tag_" + name, ante, phase, 0, 0, 0)

    events = {
        1: [tag("negative", 3, 1), tag("rare", 5, 2)],
        2: [tag("rare", 3, 1), tag("negative", 3, 2), tag("rare", 6, 1)],
        3: [tag("negative", 4, 1), tag("rare", 4, 2)],
        4: [tag("rare", 4, 2), tag("negative", 5, 1)],
        5: [tag("negative", 5, 1), tag("rare", 7, 2)],
    }
    criteria = ["tag_route observe", "tag tag_negative 3 small 7 big 1",
                "tag tag_rare 3 small 7 big 1"]
    for path, ranks, fingerprint in ((first, [1, 2, 3, 4], "1111111111111111"),
                                     (second, [2, 4, 5], "2222222222222222")):
        write_custom_bsp3(str(path), ranks, [events[rank] for rank in ranks],
                         fingerprint, criteria, range_end=organizer.NATURAL_SEEDSPACE)
    organizer.combine_pools([organizer.BSPoolReader(first), organizer.BSPoolReader(second)],
                            str(combined), "union", "Complete")
    first.unlink()
    second.unlink()
    return pools, combined


def wait_ready(process, base, page, log, seconds=45):
    deadline = time.monotonic() + seconds
    last = "No HTTP response"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("App exited during startup (%s):\n%s" % (
                process.returncode, log.read_text(encoding="utf-8", errors="replace")))
        try:
            html = request(base, page, text=True, timeout=1)
            if 'id="rulesWorkspace"' in html and 'id="ruleConditionEditor"' in html:
                return html
            last = "HTTP page is missing the bundled rules UI"
        except (OSError, URLError, RuntimeError) as exc:
            last = str(exc)
        time.sleep(0.1)
    raise RuntimeError("App did not become ready: %s\n%s" % (
        last, log.read_text(encoding="utf-8", errors="replace")))


def stop_app(process, base, builder):
    if process.poll() is not None:
        return
    # The Builder's existing close action is /api/shutdown. The standalone
    # Organizer currently exposes no close endpoint.
    if builder:
        try:
            request(base, "/api/shutdown", {}, timeout=3)
            process.wait(timeout=8)
            return
        except (OSError, URLError, RuntimeError, subprocess.TimeoutExpired):
            pass
    if os.name == "nt":
        # Onefile PyInstaller starts a child interpreter. Stop the process tree
        # rooted at exactly the Popen PID owned by this test, never by image name.
        stopped = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                 capture_output=True, text=True, timeout=15)
        if stopped.returncode and process.poll() is None:
            raise RuntimeError("Could not stop test app tree: " + stopped.stdout + stopped.stderr)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            raise RuntimeError("Windows test app tree did not exit")
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def output_reader(row, pool_dir):
    path = Path(row["path"]).resolve()
    require(path.parent == pool_dir.resolve(), "App attempted publication outside the isolated pool directory")
    reader = organizer.BSPoolReader(path)
    require(reader.schema == 4 and reader.complete, "Output is not a finished BSP4 pool")
    require(not reader.coverage_complete, "Derived subset incorrectly claims exhaustive search coverage")
    require(reader.occurrence_metadata_complete, "Output lost occurrence metadata")
    require(reader.header.one("workflow_recipe"), "Output lost its saved recipe")
    return reader


def exercise(base, prefix, pools, combined):
    recipe = {"version": 1, "mode": "second_tag", "rule": {
        "version": 1, "name": "Packaged smoke", "range": {"start": "A3S", "end": "A7B"}}}
    validated = request(base, prefix + "/rules/validate", {"document": json.dumps(recipe)})
    require(validated["recipe"] == recipe, "Rules validation changed valid settings")
    description = request(base, prefix + "/rules/describe", {"source": combined.name})
    require(Path(description["source"]["path"]).resolve() == combined.resolve(),
            "App is not using the isolated BRAINSTORM_MOD_DIR")
    require(description["can_separate"] and len(description["direct_inputs"]) == 2,
            "Deleted original inputs were not detected")
    require(description["counts_pending"] and
            all(row["records"] is None for row in description["direct_inputs"]) and
            sorted(row["original_records"] for row in description["direct_inputs"]) == [3, 4],
            "Detected source memberships are incorrect")
    separation = {"version": 1, "mode": "separate_sources", "source_kind": "inputs",
                  "source_ids": [row["id"] for row in description["direct_inputs"]]}
    plan = request(base, prefix + "/rules/preview", {
        "source": combined.name, "recipe": separation, "prefix": "recovered",
        "snapshot": description["source"]["snapshot_id"]})
    require(plan["can_create"] and plan["copied_records"] == 5
            and plan["output_memberships"] == 7 and plan["overlap_records"] == 2,
            "Recovery preview lost overlaps or selected the wrong seeds")
    restored = request(base, prefix + "/rules/publish", {
        "source": combined.name, "planToken": plan["plan_token"]})
    require(restored["completed"] and len(restored["outputs"]) == 2, "Recovery did not publish two pools")
    recovered = [output_reader(row, pools) for row in restored["outputs"]]
    require(sorted([record.rank for record in reader.iter_records()] for reader in recovered)
            == [[1, 2, 3, 4], [2, 4, 5]], "Recovered memberships do not match deleted originals")
    x = next(reader for reader in recovered if reader.records == 4)
    tags = request(base, prefix + "/rules/preview", {
        "source": Path(x.path).name, "recipe": recipe, "prefix": "second-tag"})
    require(tags["can_create"] and tags["copied_records"] == 3 and tags["excluded_records"] == 1,
            "Second-tag preview counts are incorrect")
    require(tags["exclusions"] == {"same_ante_only": 1}, "Same-Ante-only seed was not excluded")
    require({row["key"] for row in tags["outputs"]} == {"a5b-rare", "a6s-rare", "a5s-negative"},
            "Second-tag destinations are incorrect")
    sorted_pools = request(base, prefix + "/rules/publish", {
        "source": Path(x.path).name, "planToken": tags["plan_token"]})
    require(sorted_pools["completed"] and len(sorted_pools["outputs"]) == 3,
            "Second-tag publication did not create three pools")
    ranks = sorted(record.rank for row in sorted_pools["outputs"]
                   for record in output_reader(row, pools).iter_records())
    require(ranks == [1, 2, 4], "Second-tag outputs contain the wrong seeds")
    require(organizer.BSPoolReader(combined).records == 5, "Source pool was changed")


def smoke_app(name, executable, directory, source_mode):
    builder = name == "Builder"
    port = 8917 if builder else 8918
    check_port_free(port)
    pools, combined = make_mod(directory, source_mode)
    environment = os.environ.copy()
    environment["BRAINSTORM_MOD_DIR"] = str(directory)
    environment["PYTHONUNBUFFERED"] = "1"
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    command = ([sys.executable, str(executable)] if source_mode else [str(executable)]) + ["--no-browser"]
    log = directory / "app.log"
    base = "http://127.0.0.1:%d" % port
    with log.open("wb") as output:
        process = subprocess.Popen(command, cwd=directory, env=environment,
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=os.name != "nt",
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        try:
            html = wait_ready(process, base, "/organize" if builder else "/", log)
            require("Separate original pools" in html and "Sort by second tag" in html,
                    "Bundled page is missing the new operations")
            exercise(base, "/organizer/api" if builder else "/api", pools, combined)
            require(process.poll() is None, "App exited during rule operations")
        except BaseException:
            print(log.read_text(encoding="utf-8", errors="replace"), file=sys.stderr)
            raise
        finally:
            stop_app(process, base, builder)
    with socket.socket() as probe:
        probe.settimeout(1)
        require(probe.connect_ex(("127.0.0.1", port)) != 0,
                "The stopped test app left a listening child process on port %d" % port)
    print("%s %s: recovery, second tags, BSP4 coverage, and shutdown PASS" % (
        "Source" if source_mode else "Packaged", name), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="store_true", help="Run real Python scripts instead of packaged executables")
    parser.add_argument("--package-dir", type=Path, default=ROOT / "out" / "Brainstorm-Windows",
                        help="Assembled Windows wrapper directory containing Brainstorm/")
    args = parser.parse_args(argv)
    if not args.source and os.name != "nt":
        parser.error("Packaged mode must run on Windows; use --source for local Python testing.")
    for port in (8917, 8918):
        check_port_free(port)
    apps = [(name, ROOT / "tools" / ("pool_builder_web.py" if name == "Builder" else "pool_organizer_web.py"))
            if args.source else (name, args.package_dir.resolve() / "Brainstorm" / "Seed Pool Builder" / ("Seed Pool %s.exe" % name))
            for name in ("Builder", "Organizer")]
    for _name, executable in apps:
        require(executable.is_file(), "App is missing: %s" % executable)
    with tempfile.TemporaryDirectory(prefix="brainstorm-packaged-rules-") as temporary:
        for name, executable in apps:
            smoke_app(name, executable, Path(temporary) / name, args.source)
    print("SEED POOL APPS RULES SMOKE: ALL PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
