#!/bin/bash
# Double-click me: opens the Brainstorm Seed Pool Builder in your browser.
# Leave the Terminal window this creates open while a scan is running;
# closing it pauses the scan safely (press Build again later to resume).
cd "$(dirname "$0")"
exec /usr/bin/python3 tools/pool_builder_web.py
