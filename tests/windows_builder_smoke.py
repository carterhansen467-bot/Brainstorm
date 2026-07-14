"""Windows CI smoke test for the pool builder engine.

Drives tools/brainstorm_pool_builder.py's Runner exactly the way the web UI
does, on Windows: start a binary-format scan, pause it through the Windows
path (CREATE_NEW_PROCESS_GROUP + CTRL_BREAK_EVENT), assert the checkpointed
rc-130 pause with a resumable state, then resume the same scan to completion.

usage: python tests/windows_builder_smoke.py <snapshot.cfg>
(snapshot must contain tag_charm -- the synthetic fixture snapshots do)
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "tools"))
import brainstorm_pool_builder as core

SCAN_COUNT = 120_000_000  # several default checkpoints worth of work, so the
                          # pause always lands mid-scan even on a fast machine


def wait(runner, seconds, until=None):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if runner.done():
            return True
        if until and until(runner):
            return True
        time.sleep(0.2)
    return False


def main():
    snapshot_src = sys.argv[1]
    snap = core.Snapshot(snapshot_src)
    crit = core.Criteria()
    crit.tag_rules.append(["tag_charm", 1, 8, 1])
    out = os.path.join(tempfile.mkdtemp(prefix="bs_win_smoke_"), "smoke.bspool")
    text = crit.text("binary", SCAN_COUNT)

    r = core.Runner(snap.current_model_copy(), text, out)
    assert wait(r, 120, until=lambda r: r.scanned > 0), \
        "scanner never reported progress: %r" % list(r.lines)
    r.stop()  # Windows: CTRL_BREAK_EVENT -> checkpointed pause
    assert wait(r, 120), "scanner did not stop after CTRL_BREAK"
    rc = r.returncode()
    state = core.read_state(out + ".state")
    print("pause rc=%s cursor=%s done=%s" % (rc, state.get("cursor"), state.get("done")))
    assert rc == 130, "expected checkpointed-pause rc 130, got %s: %r" % (rc, list(r.lines))
    assert state.get("done") == "0", "state should be resumable"
    assert int(state.get("cursor", "0")) > 0, "no checkpoint was committed"

    r2 = core.Runner(snap.current_model_copy(), text, out)
    assert wait(r2, 600), "resumed scan did not finish"
    state = core.read_state(out + ".state")
    manifest = core.read_manifest(out + ".manifest")
    print("resume rc=%s done=%s matched=%s" % (
        r2.returncode(), state.get("done"), manifest.get("matched")))
    assert r2.returncode() == 0, "resume failed: %r" % list(r2.lines)
    assert state.get("done") == "1", "scan should be complete"
    assert any("resuming at rank" in ln for ln in r2.lines), \
        "second run did not actually resume: %r" % list(r2.lines)
    print("WINDOWS BUILDER SMOKE: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
