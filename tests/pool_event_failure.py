#!/usr/bin/env python3
"""Compile and run the ordered-publication allocation-failure regression."""

import os
import shlex
import subprocess
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    compiler = shlex.split(os.environ.get("CC", "clang"))
    if not compiler:
        raise SystemExit("CC is empty")
    with tempfile.TemporaryDirectory(
            prefix="brainstorm-event-failure-") as temp:
        binary = os.path.join(
            temp, "pool-event-failure" + (
                ".exe" if os.name == "nt" else ""))
        subprocess.run(
            compiler + [
                "-std=c11", "-O1", "-Wall", "-Wno-unused-function",
                "-ffp-contract=off", "-pthread",
                "-o", binary, os.path.join(
                    ROOT, "tests", "pool_event_failure.c"), "-lm",
            ],
            cwd=ROOT, check=True)
        subprocess.run([binary], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
