-- ===========================================================================
-- CI test for win_spawn.lua with a bare luajit.exe (no love, no Balatro).
--   1. Quoting rules (all platforms).
--   2. Windows only: spawn native/brainstorm_native_search.exe in `search`
--      mode through the SAME CreateProcessA path the game uses, prove the
--      status file protocol comes alive, then stop-file it and prove a clean
--      final status. This is the in-game launch minus Balatro itself.
-- usage: luajit tests/win_spawn_test.lua <full-search-cfg>   (from repo root)
-- ===========================================================================

local spawner = dofile("win_spawn.lua")

local q = spawner.quote_arg
assert(q("plain.exe") == "plain.exe")
assert(q([[C:\Program Files\x.exe]]) == [["C:\Program Files\x.exe"]])
assert(q([[a"b]]) == [["a\"b"]])
assert(q([[has space\]]) == [["has space\\"]])
assert(q("") == [[""]])
print("PASS quoting rules")

local isWindows = package.config:sub(1, 1) == "\\"
if not isWindows then
	print("WIN SPAWN TEST: quoting only (not Windows); spawn part skipped")
	return
end

local cfg = arg[1]
assert(cfg, "usage: luajit tests/win_spawn_test.lua <search-cfg>")

local ffi = require("ffi")
pcall(ffi.cdef, "void Sleep(uint32_t ms);")
local function sleep_ms(ms) ffi.C.Sleep(ms) end

local function write_file(path, text)
	local f = assert(io.open(path, "wb"))
	f:write(text)
	f:close()
end

local function read_file(path)
	local f = io.open(path, "rb")
	if not f then return nil end
	local s = f:read("*a")
	f:close()
	return s
end

local base = "win_spawn_test_run"
local status, stop, hb = base .. ".status", base .. ".stop", base .. ".hb"
os.remove(status)
os.remove(stop)
write_file(hb, tostring(os.time()))

local ok, err = spawner.spawn({
	"native\\brainstorm_native_search.exe", "search", cfg, status, stop, hb,
})
assert(ok, "spawn failed: " .. tostring(err))
print("PASS CreateProcessA spawn")

-- The helper writes its first status within a second of calibrating.
local txt
for _ = 1, 100 do
	sleep_ms(200)
	txt = read_file(status)
	if txt and txt:find("P ") then break end
end
assert(txt and txt:find("P "), "no progress status appeared; helper did not run")
assert(not txt:find("E "), "helper reported an error:\n" .. tostring(txt))
print("PASS status file protocol alive")

write_file(stop, "stop")
local finished
for _ = 1, 150 do
	sleep_ms(200)
	txt = read_file(status) or ""
	if txt:find("\nD\n") or txt:match("^D\n") or txt:find("R ") then
		finished = true
		break
	end
end
assert(finished, "helper did not write a final status after the stop file:\n" .. tostring(txt))
print("PASS stop file honored, final status written")

os.remove(status)
os.remove(stop)
os.remove(hb)
print("WIN SPAWN TEST: ALL PASS")
