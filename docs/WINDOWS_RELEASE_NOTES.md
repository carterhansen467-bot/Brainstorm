Windows native helpers for the Brainstorm `joker-search-experiment` branch:
the fast native seed search, in-game `.bspool` seed pools, and the Seed Pool
Builder app, prebuilt -- no compiler or Python install needed.

**Install:** unzip, copy `native\*.exe` into
`%AppData%\Balatro\Mods\Brainstorm\native\`, and (optionally) `Seed Pool
Builder.exe` into the mod folder. Full steps in the zip's `INSTALL.txt`.

The exes are unsigned; if SmartScreen objects, pick "More info → Run anyway"
or right-click → Properties → Unblock.

Bit-exactness is proven in CI on `windows-latest`: the Lua filter oracle
generates its fixtures with that machine's own LuaJIT build and the helpers
must reproduce every verdict byte-for-byte, plus the full pool
build → restricted search → exhaustion flow. Pools built on any OS work on
every other one (`.bspool` is platform-independent).
