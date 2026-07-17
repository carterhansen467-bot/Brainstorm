#!/bin/bash
# Double-click me: opens the Brainstorm Seed Pool Organizer in your browser.
# Leave the Terminal window open while using the organizer.
cd "$(dirname "$0")"
exec /usr/bin/python3 tools/pool_organizer_web.py
