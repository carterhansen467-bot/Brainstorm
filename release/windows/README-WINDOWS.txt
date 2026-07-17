Brainstorm for Windows x64
==========================

Normal install or update (recommended)
--------------------------------------
1. Quit Balatro and close the Seed Pool Builder or Organizer.
2. Extract the entire release ZIP. Do not run files from inside the ZIP view.
3. Double-click "Install or Update Brainstorm.bat".
4. Start Balatro. Briefly toggle Ctrl+A on and then off once so this version
   writes a fresh native_search.cfg for your profile.

The updater installs to:
  %AppData%\Balatro\Mods\Brainstorm

It replaces only files shipped by Brainstorm. It preserves seed_pools,
settings.lua, native_search.cfg, scan checkpoints, and other runtime state.
It also removes the two obsolete duplicate executable locations used through
win-v9, after installing their new copies.

Manual fallback
---------------
Copy this package's Brainstorm folder into %AppData%\Balatro\Mods\ and choose
Replace for matching files. Keep the active install's seed_pools folder,
settings.lua, native_search.cfg, and *.state files. Then delete these two old
duplicates if they still exist:
  Brainstorm\Seed Pool Builder.exe
  Brainstorm\native\brainstorm_seed_pool.exe

Do not delete Brainstorm\native\brainstorm_native_search.exe; Balatro uses it.
The root "Seed Pool Builder.bat" and "Seed Pool Organizer.bat" files are the
stable shortcuts. Both apps and the exhaustive scanner live together under
Brainstorm\Seed Pool Builder\.

Windows security
----------------
If SmartScreen blocks an executable, right-click it, choose Properties, check
Unblock, and apply; or choose More info -> Run anyway. The package manifest is
checked before the updater copies files.

Sharing pools
-------------
Share the single .bspool file from Brainstorm\seed_pools\. The recipient must
use a Brainstorm release that supports that pool's schema/model. Updating with
the full package first is the safest way to keep the Lua mod, in-game helper,
builder, and scanner on one tested version.
