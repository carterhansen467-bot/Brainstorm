#!/bin/sh
set -eu

case "$(uname -s)" in
	MINGW*|MSYS*|CYGWIN*) BUILD=native/build_windows.sh ;;
	*) BUILD=native/build.sh ;;
esac

sh "$BUILD"
python3 tests/pool_organizer.py
