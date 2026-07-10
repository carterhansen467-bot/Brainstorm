-- ===========================================================================
-- Catalog snapshot generator for the seed-pool harnesses.
-- ---------------------------------------------------------------------------
-- tests/seed_pool_equivalence.sh and tests/pool_search_equivalence.sh need a
-- full native config ("snapshot") for its catalog (tagdef/vouchdef/jokerdef/
-- boostdef) and parity checks. In-game that file is native_search.cfg,
-- written whenever the native search runs; on a machine without the game
-- (CI, fresh checkout) this script produces an equivalent one.
--
-- Same worker bootstrap and synthetic catalog as tests/dump_native_fixtures.lua
-- (keep the pools in sync), except two tags carry their real names so the
-- default pool criteria (tag_rare collected, tag_negative min_ante-gated)
-- resolve: the pool harnesses prove engine-vs-engine equivalence on a shared
-- catalog, so which catalog is shared does not matter. The checks are computed
-- with THIS LuaJIT's math, exactly like the game would.
--
-- usage: luajit tests/dump_pool_snapshot.lua <Brainstorm_reroll.lua> <out.cfg>
-- ===========================================================================

local rerollPath, outPath = arg[1], arg[2]
assert(rerollPath and outPath, "usage: dump_pool_snapshot.lua <reroll.lua> <out.cfg>")

local function readFile(p)
	local f = assert(io.open(p, "rb"))
	local s = f:read("*a")
	f:close()
	return s
end

local fileText = readFile(rerollPath)

local function serialize(v)
	local t = type(v)
	if t == "string" then return string.format("%q", v)
	elseif t == "number" then return string.format("%.17g", v)
	elseif t == "boolean" then return tostring(v)
	elseif t == "table" then
		local parts = {}
		for k, val in pairs(v) do
			local keyStr
			if type(k) == "number" then keyStr = "[" .. string.format("%.17g", k) .. "]"
			else keyStr = "[" .. string.format("%q", k) .. "]" end
			parts[#parts + 1] = keyStr .. "=" .. serialize(val)
		end
		return "{" .. table.concat(parts, ",") .. "}"
	end
	return "nil"
end

-- ---- synthetic snapshot (mirror of tests/dump_native_fixtures.lua) ----------
local function buildSnapshot(session)
	local snap = { session = session, entropy = 0 }
	snap.jokerPools = {}
	local counts = { 60, 40, 25 }
	for r = 1, 3 do
		local arr = {}
		for i = 1, counts[r] do
			local e = { key = "j_r" .. r .. "_" .. i, rarity = r }
			if i % 11 == 0 then e.unlocked = false end
			if i % 13 == 0 then e.enhancement_gate = "m_gold" end
			if i % 17 == 0 then e.no_pool_flag = "flag_never_set" end
			if i % 19 == 0 then e.yes_pool_flag = "flag_missing" end
			arr[i] = e
		end
		snap.jokerPools[r] = arr
	end
	snap.jokerPools[4] = {
		{ key = "j_caino", rarity = 4 }, { key = "j_triboulet", rarity = 4 },
		{ key = "j_yorick", rarity = 4 }, { key = "j_chicot", rarity = 4 },
		{ key = "j_perkeo", rarity = 4 },
	}
	snap.boosterPool = {
		{ key = "p_arcana_normal_1", weight = 1, kind = "Arcana", cards = 3 },
		{ key = "p_arcana_normal_2", weight = 1, kind = "Arcana", cards = 3 },
		{ key = "p_arcana_jumbo_1", weight = 0.5, kind = "Arcana", cards = 5 },
		{ key = "p_arcana_mega_1", weight = 0.25, kind = "Arcana", cards = 5 },
		{ key = "p_celestial_normal_1", weight = 1, kind = "Celestial", cards = 3 },
		{ key = "p_celestial_jumbo_1", weight = 0.5, kind = "Celestial", cards = 5 },
		{ key = "p_standard_normal_1", weight = 2, kind = "Standard", cards = 3 },
		{ key = "p_standard_jumbo_1", weight = 1, kind = "Standard", cards = 5 },
		{ key = "p_buffoon_normal_1", weight = 1.2, kind = "Buffoon", cards = 2 },
		{ key = "p_buffoon_jumbo_1", weight = 0.6, kind = "Buffoon", cards = 4 },
		{ key = "p_buffoon_mega_1", weight = 0.15, kind = "Buffoon", cards = 4 },
		{ key = "p_spectral_normal_1", weight = 0.3, kind = "Spectral", cards = 2 },
		{ key = "p_spectral_mega_1", weight = 0.07, kind = "Spectral", cards = 4 },
	}
	-- Same shape as the fixture catalog: index 3 ante-gated, index 7
	-- requires-undiscovered. tag_rare / tag_negative get their real names so
	-- the pool builder's stock criteria resolve against this catalog.
	snap.tagPool = {}
	for i = 1, 24 do
		snap.tagPool[i] = { key = "tag_" .. i, sort_id = i, requiresOk = true }
	end
	snap.tagPool[3].key = "tag_negative"
	snap.tagPool[3].min_ante = 2
	snap.tagPool[7].requiresOk = false
	snap.tagPool[11].key = "tag_charm"
	snap.tagPool[15].key = "tag_rare"
	snap.voucherPool = {}
	for i = 1, 16 do
		local base = { key = "v_base_" .. i }
		if i == 9 then base.unlocked = false end
		snap.voucherPool[#snap.voucherPool + 1] = base
		snap.voucherPool[#snap.voucherPool + 1] = { key = "v_upg_" .. i, requires = true }
	end
	snap.game = { banned_keys = { j_r1_5 = true }, pool_flags = {} }
	snap.autoreroll = {
		searchTag = "", searchTagID = 1,
		searchPack = {}, searchPackID = 1,
		searchVoucher = "", searchVoucherID = 1, searchVoucherAnte = 1,
		searchLegendary = "", searchNegativeLegendary = false,
		searchForSoul = 0, jokerSearchMatchAny = false,
		jokerSlotData = {
			{ key = "", index = 1 }, { key = "", index = 2 }, { key = "", index = 3 },
		},
		seedsPerFrame = 1000, searchThreads = 1, foundSeedSlot = 0, foundSeedStake = 1,
	}
	local ma = {}
	for a = 1, 4 do
		ma["ante" .. a .. "Slots"] = 0
		ma["ante" .. a .. "Packs"] = false
	end
	snap.multiAnteSearch = ma
	return snap
end

-- ---- worker-style bootstrap (fake love.thread, stale session => no loop) ----
package.loaded["lovely"] = { mod_dir = "" }
package.loaded["nativefs"] = {
	write = function() end, read = function() return "" end, getInfo = function() return nil end,
}
G = { FUNCS = {} }
Brainstorm = { SETTINGS = { autoreroll = {}, multiAnteSearch = {} }, AUTOREROLL = {}, random_state = {} }
assert(load(fileText, "bootstrap"))()
local workerSrc = assert(Brainstorm.SEARCH_WORKER_SRC)

local channels = {}
local function getChannel(name)
	local c = channels[name]
	if not c then
		c = { items = {} }
		c.push = function(self, v) self.items[#self.items + 1] = v end
		c.pop = function(self) return table.remove(self.items, 1) end
		c.peek = function(self) return self.items[1] end
		c.clear = function(self) self.items = {} end
		channels[name] = c
	end
	return c
end
love = { thread = { getChannel = getChannel } }
package.loaded["love.thread"] = true
assert(load(workerSrc, "worker"))(serialize(buildSnapshot(1)), fileText, 0, 1)

local cfg = assert(Brainstorm.buildNativeConfigText(1), "config serialize failed")
local f = assert(io.open(outPath, "wb"))
f:write(cfg)
f:close()
print("snapshot written to " .. outPath)
