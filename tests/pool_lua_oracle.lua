-- Independent production-Lua oracle for .bspool embedded criteria.
-- usage: luajit tests/pool_lua_oracle.lua Brainstorm_reroll.lua snapshot.cfg pool.bspool seeds.txt

local rerollPath, snapshotPath, poolPath, seedsPath, overlayTag =
	arg[1], arg[2], arg[3], arg[4], arg[5]
assert(seedsPath, "usage: pool_lua_oracle.lua <reroll.lua> <snapshot.cfg> <pool.bspool> <seeds.txt>")

local function read(path, mode)
	local f = assert(io.open(path, mode or "rb"))
	local s = f:read("*a"); f:close(); return s
end

local function serialize(v)
	local t = type(v)
	if t == "string" then return string.format("%q", v)
	elseif t == "number" then return string.format("%.17g", v)
	elseif t == "boolean" then return tostring(v)
	elseif t == "table" then
		local out = {}
		for k, value in pairs(v) do
			local key = type(k) == "number" and ("[" .. k .. "]")
				or ("[" .. string.format("%q", k) .. "]")
			out[#out + 1] = key .. "=" .. serialize(value)
		end
		return "{" .. table.concat(out, ",") .. "}"
	end
	return "nil"
end

local fileText = read(rerollPath)
package.loaded.lovely = { mod_dir = "" }
package.loaded.brainstorm_nativefs = {
	write = function() end, read = function() return "" end,
	getInfo = function() return nil end,
}
G = { FUNCS = {} }
Brainstorm = { SETTINGS = { autoreroll = {}, multiAnteSearch = {} }, AUTOREROLL = {}, random_state = {} }
assert(load(fileText, "bootstrap"))()
local workerSrc = assert(Brainstorm.SEARCH_WORKER_SRC)

local snap = {
	session = 999, entropy = 0, jokerPools = { {}, {}, {}, {} },
	boosterPool = {}, tagPool = {}, voucherPool = {},
	game = { banned_keys = {}, pool_flags = {} },
	autoreroll = {
		searchTag = "", searchPack = {}, searchVoucher = "", searchVoucherAnte = 1,
		searchLegendary = "", searchForSoul = 0, jokerSlotData = {
			{ key = "", index = 1 }, { key = "", index = 2 }, { key = "", index = 3 },
		}, searchThreads = 1,
	},
	multiAnteSearch = {}, useCulledCache = true,
}
local voucherByKey, startingVouchers = {}, {}
for line in read(snapshotPath):gmatch("[^\r\n]+") do
	local p = {}; for word in line:gmatch("%S+") do p[#p + 1] = word end
	if p[1] == "tagdef" then
		snap.tagPool[#snap.tagPool + 1] = {
			key = p[2], requiresOk = p[3] == "1", min_ante = tonumber(p[4]) or 0,
		}
	elseif p[1] == "vouchdef" then
		local center = { key = p[2], unlocked = p[3] == "1" }
		snap.voucherPool[#snap.voucherPool + 1] = center
		voucherByKey[p[2]] = center
	elseif p[1] == "vouchroute" then
		local center = voucherByKey[p[2]]
		if center then
			center.unlocked = p[3] == "1"
			center.requires = p[4] ~= "-" and { p[4] } or nil
		end
	elseif p[1] == "vouchowned" then
		startingVouchers[#startingVouchers + 1] = p[2]
	elseif p[1] == "jokerdef" then
		local rarity = tonumber(p[2]); snap.jokerPools[rarity][#snap.jokerPools[rarity] + 1] = {
			key = p[3], rarity = rarity, unlocked = p[4] == "1",
		}
		if p[4] == "0" then snap.game.banned_keys[p[3]] = true end
	elseif p[1] == "boostdef" then
		snap.boosterPool[#snap.boosterPool + 1] = {
			key = p[2], weight = tonumber(p[3]),
			kind = p[6] == "A" and "Arcana" or p[6] == "S" and "Spectral"
				or p[4] == "1" and "Buffoon" or "Other",
			cards = tonumber(p[5]),
		}
		if p[7] == "0" then snap.game.banned_keys[p[2]] = true end
	elseif p[1] == "specialdef" then
		if p[2] == "0" then snap.game.banned_keys.c_soul = true end
		if p[3] == "0" then snap.game.banned_keys.c_black_hole = true end
	end
end

local channels = {}
local function getChannel(name)
	channels[name] = channels[name] or {
		items = {}, push = function(self, v) self.items[#self.items + 1] = v end,
		pop = function(self) return table.remove(self.items, 1) end,
		peek = function(self) return self.items[1] end, clear = function(self) self.items = {} end,
	}
	return channels[name]
end
love = { thread = { getChannel = getChannel } }
package.loaded["love.thread"] = true
assert(load(workerSrc, "worker"))(serialize(snap), fileText, 0, 1)
G.GAME.selected_back = { effect = { config = { vouchers = startingVouchers } } }

package.loaded.brainstorm_nativefs.read = function(path, bytes)
	local f = io.open(path, "rb")
	if not f then return nil end
	local value = f:read(bytes or "*a"); f:close(); return value
end
package.loaded.brainstorm_nativefs.getInfo = function(path)
	local f = io.open(path, "rb")
	if not f then return nil end
	f:close(); return { type = "file" }
end
local header = assert(Brainstorm.readPoolHeader(poolPath),
	"production Lua rejected the pool header")

for seed in read(seedsPath):gmatch("[^\r\n]+") do
	Brainstorm.random_state = { hashed_seed = pseudohash(seed) }
	local overlaySm, overlayBig, overlayRewardSm, overlayRewardBig, overlayOK =
		nil, nil, nil, nil, not overlayTag
	if overlayTag then
		for ante = 1, 8 do
			if Brainstorm.rollTag(seed, ante) == overlayTag then
				overlaySm, overlayOK = { [ante] = true }, true
				if Brainstorm.tagSoulRewardKey(overlayTag) then
					overlayRewardSm = { [ante] = overlayTag }
				end
				break
			end
			if Brainstorm.rollTag(seed, ante) == overlayTag then
				overlayBig, overlayOK = { [ante] = true }, true
				if Brainstorm.tagSoulRewardKey(overlayTag) then
					overlayRewardBig = { [ante] = overlayTag }
				end
				break
			end
		end
		Brainstorm.random_state = { hashed_seed = pseudohash(seed) }
	end
	local ok = overlayOK and Brainstorm.evaluatePoolCriteria(
		seed, header, overlaySm, overlayBig, overlayRewardSm, overlayRewardBig)
	print(seed .. " " .. (ok and "1" or "0"))
end
