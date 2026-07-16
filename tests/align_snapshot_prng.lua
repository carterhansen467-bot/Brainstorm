-- Copy a native snapshot to stdout while recomputing its PRNG parity values
-- with this LuaJIT binary. A regression oracle may not use the same LuaJIT
-- build (and floating-point contraction mode) as Balatro's embedded runtime.
local path = assert(arg[1], "usage: align_snapshot_prng.lua <snapshot.cfg>")
local input = assert(io.open(path, "rb"))
local sawProbe = {}

local function g17(x) return string.format("%.17g", x) end
local function randomValue(seed, n)
	math.randomseed(seed)
	return n and math.random(n) or math.random()
end

for line in input:lines() do
	local seed = line:match("^check_pr ([^ ]+) [^ ]+$")
	if seed then
		local numericSeed = assert(tonumber(seed))
		print("check_pr " .. seed .. " " .. g17(randomValue(numericSeed)))
		sawProbe[seed] = true
	else
		local n
		seed, n = line:match("^check_prn ([^ ]+) ([^ ]+) [^ ]+$")
		if seed then
			local numericSeed, numericN = assert(tonumber(seed)), assert(tonumber(n))
			print("check_prn " .. seed .. " " .. n .. " " .. g17(randomValue(numericSeed, numericN)))
		elseif line == "end" then
			for _, probe in ipairs({ 0.6051828282731726, 0.39349437354872258 }) do
				local probeText = g17(probe)
				if not sawProbe[probeText] then
					print("check_pr " .. probeText .. " " .. g17(randomValue(probe)))
				end
			end
			print(line)
		else
			print(line)
		end
	end
end
input:close()
