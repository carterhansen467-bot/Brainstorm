-- Regression for the background Lua worker's compact, throttled progress
-- protocol. This exercises the main-thread parser without needing LÖVE.

package.loaded.lovely = { mod_dir = "" }
package.loaded.brainstorm_nativefs = {
	read = function() return nil end,
	write = function() return true end,
	getInfo = function() return nil end,
}

local queues = {}
local function channel(name)
	if queues[name] then return queues[name] end
	local value = { items = {} }
	function value:push(item) self.items[#self.items + 1] = item end
	function value:pop() return table.remove(self.items, 1) end
	function value:peek() return self.items[1] end
	function value:clear() self.items = {} end
	queues[name] = value
	return value
end

love = {
	thread = { getChannel = channel },
	system = { getProcessorCount = function() return 4 end },
	timer = { getTime = function() return 1 end },
}
G = { FUNCS = {} }
Brainstorm = {
	SETTINGS = { autoreroll = {}, multiAnteSearch = {} },
	AUTOREROLL = {},
}
assert(loadfile("Brainstorm_reroll.lua"))()

local worker = { getError = function() return nil end }
Brainstorm.AUTOREROLL.searchThreads = { worker, worker }
Brainstorm.AUTOREROLL.searchThreadCount = 2
Brainstorm.AUTOREROLL.searchProgress = {}
Brainstorm.AUTOREROLL.searchSession = 9

local progress = channel(Brainstorm.SEARCH_CHANNELS.progress)
progress:push("0:100")
progress:push("1:75")
progress:push("0:125")
progress:push("malformed")
progress:push("2:999") -- outside this session's worker count

local originalLoad = load
local dynamicLoads = 0
load = function(...)
	dynamicLoads = dynamicLoads + 1
	return originalLoad(...)
end
assert(Brainstorm.pollSearchThread() == nil)
load = originalLoad

assert(dynamicLoads == 0, "compact progress unexpectedly invoked load()")
assert(Brainstorm.AUTOREROLL.searchProgress[0] == 125)
assert(Brainstorm.AUTOREROLL.searchProgress[1] == 75)
assert(Brainstorm.AUTOREROLL.searchProgress[2] == nil)
assert(Brainstorm.AUTOREROLL.searchTried == 200)

-- The embedded worker retains the 250-seed cancellation boundary but
-- rate-limits observable progress and always publishes a terminal sample.
local source = assert(Brainstorm.SEARCH_WORKER_SRC)
assert(source:find("for _ = 1, 250 do", 1, true))
assert(source:find("now - lastProgressAt >= 0.1", 1, true))
assert(source:find("publishProgress(tried, true)", 1, true))
assert(not source:find("serializeValue({ i = threadIndex", 1, true))

print("Lua search progress regression: OK")
