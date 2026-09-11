#!/bin/sh
set -eu

python3 tests/pool_organizer_web.py
python3 tests/pool_metadata_performance.py
python3 tests/pool_tag_rules.py
python3 tests/pool_rule_workflow.py
python3 tests/pool_rule_native.py
python3 tests/pool_source_preview.py
python3 tests/pool_tag_recording.py
python3 tests/pool_tag_recording_native.py
python3 tests/pool_rules_web.py
python3 tests/pool_builder_ui.py
python3 tests/pool_rules_native_refilter.py
