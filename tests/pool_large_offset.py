#!/usr/bin/env python3
"""Compile and run the sparse BSP3 >2/>4 GiB positioning regression."""

import os
import shlex
import subprocess
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    if os.name != "nt":
        print("SKIP: sparse >4 GiB finalizer regression is Windows-only")
        return
    compiler = shlex.split(os.environ.get("CC", "clang"))
    if not compiler:
        raise SystemExit("CC is empty")
    with tempfile.TemporaryDirectory(
            prefix="brainstorm-large-offset-") as temp:
        binary = os.path.join(
            temp, "pool-large-offset" + (
                ".exe" if os.name == "nt" else ""))
        fixture = os.path.join(temp, "sparse-bsp3.bspool")
        subprocess.run(
            compiler + [
                "-std=c11", "-O1", "-Wall", "-Wno-unused-function",
                "-ffp-contract=off", "-pthread",
                "-o", binary, os.path.join(
                    ROOT, "tests", "pool_large_offset.c"), "-lm",
            ],
            cwd=ROOT, check=True)
        subprocess.run([binary, fixture], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
