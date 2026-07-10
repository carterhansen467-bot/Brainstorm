-- ===========================================================================
-- Windows spawn-path check (CI, windows-latest only).
-- ---------------------------------------------------------------------------
-- Boots Brainstorm_reroll.lua with the same fakes as the fixture dumper but
-- with a love.system reporting "Windows", then drives the PRODUCTION spawn
-- path (Brainstorm.spawnHelperDetached -> ffi CreateProcessA with
-- CREATE_NO_WINDOW) against the real helper .exe and a filter-less config:
-- the helper must start detached, hit its first candidate, and commit a
-- final status file with an R line. This is the closest a headless runner
-- gets to the in-game Ctrl+A launch; the one thing it cannot see is an
-- actual window flash, which the ffi path avoids by construction.
--
-- usage: luajit tests/windows_spawn_check.lua <Brainstorm_reroll.lua> <helper.exe> <cfg> <outdir>
-- ===========================================================================

local rerollPath, helperBin, cfgPath, outdir = arg[1], arg[2], arg[3], arg[4]
assert(rerollPath and helperBin and cfgPath and outdir,
	"usage: windows_spawn_check.lua <reroll.lua> <helper.exe> <cfg> <outdir>")

local f = assert(io.open(rerollPath, "rb"))
local fileText = f:read("*a")
f:close()

package.loaded["lovely"] = { mod_dir = "" }
package.loaded["nativefs"] = {
	write = function() end, read = function() return "" end, getInfo = function() return nil end,
}
G = { FUNCS = {} }
Brainstorm = { SETTINGS = { autoreroll = {}, multiAnteSearch = {} }, AUTOREROLL = {}, random_state = {} }
love = { system = { getOS = function() return "Windows" end } }
assert(load(fileText, "bootstrap"))()

assert(Brainstorm.isWindows(), "isWindows() must be true under this bootstrap")
assert(Brainstorm.nativePaths().bin:sub(-4) == ".exe", "nativePaths().bin must end in .exe on Windows")

local status = outdir .. "/spawn_check.status"
local stop = outdir .. "/spawn_check.stop"
local hb = outdir .. "/spawn_check.hb"
os.remove(status)
os.remove(stop)
local h = assert(io.open(hb, "wb"))
h:write(tostring(os.time()))
h:close()

assert(Brainstorm.spawnHelperDetached(helperBin, { "search", cfgPath, status, stop, hb }),
	"spawnHelperDetached returned false")

-- A filter-less config accepts its first candidate, so the final status
-- (R + D lines) lands within a second or two; allow 30 for a slow runner.
local deadline = os.time() + 30
local txt
repeat
	local sf = io.open(status, "rb")
	if sf then
		txt = sf:read("*a")
		sf:close()
		if txt and txt:find("\nD\n", 1, true) then break end
	end
until os.time() > deadline

assert(txt, "helper never wrote a status file")
local seed = txt:match("R (%S+)")
assert(seed, "helper status has no R line:\n" .. txt)
assert(txt:find("\nD\n", 1, true), "helper status never reached D:\n" .. txt)
print("WINDOWS SPAWN CHECK: PASS (detached helper found seed " .. seed .. ")")
