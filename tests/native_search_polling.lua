-- Focused regression for native-search status/heartbeat timing. Production
-- LÖVE supplies a monotonic wall clock; this harness controls it directly so
-- polling behavior is deterministic and independent of the test machine.

local now = 100
local statusText = "P 1\n"
local statusReads, heartbeatWrites, stopWrites = 0, 0, 0

package.loaded.lovely = { mod_dir = "" }
package.loaded.brainstorm_nativefs = {
	read = function(path)
		if path == "status" then
			statusReads = statusReads + 1
			return statusText
		end
		return nil
	end,
	write = function(path)
		if path == "hb" then heartbeatWrites = heartbeatWrites + 1 end
		if path == "stop" then stopWrites = stopWrites + 1 end
		return true
	end,
	getInfo = function() return nil end,
}

love = {
	timer = { getTime = function() return now end },
	system = { getOS = function() return "OS X" end },
}
G = { FUNCS = {} }
Brainstorm = {
	SETTINGS = { autoreroll = {}, multiAnteSearch = {} },
	AUTOREROLL = {},
}
assert(loadfile("Brainstorm_reroll.lua"))()

Brainstorm.nativePaths = function()
	return { status = "status", stop = "stop", hb = "hb" }
end
Brainstorm.effectiveSeedPoolSelection = function() return nil end

local function activate()
	local A = Brainstorm.AUTOREROLL
	A.nativeActive = true
	A.nativeStartedAt = now
	A.nativeLastStatusPollAt = nil
	A.nativeLastHeartbeatAt = now
	A.nativeFailed = nil
end

-- One immediate read, then no more than 10 reads/second regardless of frames.
activate()
assert(Brainstorm.pollNativeSearch() == nil)
assert(statusReads == 1 and Brainstorm.AUTOREROLL.searchTried == 1)
for _ = 1, 240 do
	assert(Brainstorm.pollNativeSearch() == nil)
end
assert(statusReads == 1, "status was read again without elapsed time")
now = now + 0.099
Brainstorm.pollNativeSearch()
assert(statusReads == 1, "status was read before the 100 ms interval")
now = now + 0.002
Brainstorm.pollNativeSearch()
assert(statusReads == 2, "status was not read after the 100 ms interval")

-- Heartbeats likewise follow wall time, not rendered-frame count.
for _ = 1, 240 do Brainstorm.pollNativeSearch() end
assert(heartbeatWrites == 0, "heartbeat was frame-counted")
now = now + 1.89
Brainstorm.pollNativeSearch()
assert(heartbeatWrites == 0, "heartbeat was written before two seconds")
now = now + 0.02
Brainstorm.pollNativeSearch()
assert(heartbeatWrites == 1, "heartbeat was not written after two seconds")

-- A terminal result is surfaced on the next scheduled read (at most 100 ms).
statusText = "P 2\nR TESTSEED Found_at_Ante_2\n"
now = now + 0.079
assert(Brainstorm.pollNativeSearch() == nil)
now = now + 0.002
local result = assert(Brainstorm.pollNativeSearch())
assert(result.seed == "TESTSEED")
assert(result.jokerFoundAt == "Found_at_Ante_2")

-- Errors remain prompt and clear the timing state for the next search.
Brainstorm.stopNativeSearch()
activate()
statusText = "P 3\nE helper failed\n"
Brainstorm.pollNativeSearch()
assert(Brainstorm.AUTOREROLL.nativeFailed)
assert(not Brainstorm.AUTOREROLL.nativeActive)
assert(Brainstorm.AUTOREROLL.nativeLastStatusPollAt == nil)
assert(Brainstorm.AUTOREROLL.nativeLastHeartbeatAt == nil)

-- Missing-status timeout is elapsed-time based and is checked on a scheduled
-- read, independent of how many frames were rendered in between.
activate()
statusText = nil
Brainstorm.pollNativeSearch()
for _ = 1, 240 do Brainstorm.pollNativeSearch() end
assert(Brainstorm.AUTOREROLL.nativeActive)
now = now + 5.01
Brainstorm.pollNativeSearch()
assert(Brainstorm.AUTOREROLL.nativeFailed)
assert(not Brainstorm.AUTOREROLL.nativeActive)

assert(stopWrites == 3)
print("native search polling regression: OK")
