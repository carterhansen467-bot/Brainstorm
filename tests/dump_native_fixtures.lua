-- ===========================================================================
-- Fixture dumper for the native searcher.
-- ---------------------------------------------------------------------------
-- Boots Brainstorm_reroll.lua exactly like a search worker (same synthetic
-- pool snapshot as tests/search_equivalence.lua -- keep the CASES/pools in
-- sync with that file), then for each filter case writes:
--   case<i>.cfg       via the PRODUCTION serializer Brainstorm.buildNativeConfigText
--   case<i>.seeds     deterministic candidate list
--   case<i>.expected  "<seed> <0|1> <jokerFoundAt|->" from the Lua oracle
-- tests/native_equivalence.sh then diffs the C helper's output against it.
--
-- usage: luajit tests/dump_native_fixtures.lua <Brainstorm_reroll.lua> <outdir> [nseeds]
-- ===========================================================================

local rerollPath, outdir, nseeds = arg[1], arg[2], tonumber(arg[3]) or 30000
assert(rerollPath and outdir, "usage: dump_native_fixtures.lua <reroll.lua> <outdir> [nseeds]")

local function readFile(p)
	local f = assert(io.open(p, "rb"))
	local s = f:read("*a")
	f:close()
	return s
end
local function writeFile(p, s)
	local f = assert(io.open(p, "wb"))
	f:write(s)
	f:close()
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

-- ---- synthetic snapshot + cases (mirror of tests/search_equivalence.lua) ----
local function lcg(x) return (x * 16807) % 2147483647 end

local function defaultAutoreroll()
	return {
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
end

local function defaultMultiAnte()
	local m = {}
	for a = 1, 4 do
		m["ante" .. a .. "Slots"] = 0
		m["ante" .. a .. "Packs"] = false
	end
	return m
end

local function buildSnapshot(case, session, entropy)
	local snap = { session = session, entropy = entropy }
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
	-- Tags: index 3 is ante-gated (min_ante = 2, like the real tag_negative)
	-- and index 7 is requires-undiscovered, so the culled resample path runs.
	snap.tagPool = {}
	for i = 1, 24 do
		snap.tagPool[i] = { key = "tag_" .. i, sort_id = i, requiresOk = true }
	end
	snap.tagPool[3].min_ante = 2
	snap.tagPool[7].requiresOk = false
	snap.tagPool[11].key = "tag_charm"
	snap.tagPool[12].key = "tag_ethereal"
	snap.tagPool[12].min_ante = 2
	snap.voucherPool = {}
	for i = 1, 16 do
		local base = { key = "v_base_" .. i }
		if i == 9 then base.unlocked = false end
		snap.voucherPool[#snap.voucherPool + 1] = base
		snap.voucherPool[#snap.voucherPool + 1] = { key = "v_upg_" .. i, requires = true }
	end
	-- Exercise model-5 availability: one booster is absent and a banned Black
	-- Hole still consumes/overwrites its Spectral roll without suppressing the
	-- next card's Black Hole roll.
	snap.game = { banned_keys = {
		j_r1_5 = true, p_arcana_normal_1 = true, c_black_hole = true,
	}, pool_flags = {} }
	local ar = defaultAutoreroll()
	for k, v in pairs(case.ar or {}) do ar[k] = v end
	local ma = defaultMultiAnte()
	for k, v in pairs(case.ma or {}) do ma[k] = v end
	snap.autoreroll = ar
	snap.multiAnteSearch = ma
	snap.useCulledCache = case.useCulledCache
	return snap
end

local CASES = {
	{ name = "no filters (accept first)" },
	{ name = "tag only", ar = { searchTag = "tag_charm" } },
	{ name = "tag missing from pool", ar = { searchTag = "tag_no_such" } },
	{ name = "tag + voucher A1", ar = { searchTag = "tag_charm", searchVoucher = "v_base_4", searchVoucherAnte = 1 } },
	{ name = "voucher any ante", ar = { searchVoucher = "v_base_7", searchVoucherAnte = 0 } },
	{ name = "pack", ar = { searchPack = { "p_arcana_mega_1", "p_buffoon_mega_1" } } },
	{ name = "soul x1", ar = { searchForSoul = 1 } },
	{ name = "soul x2", ar = { searchForSoul = 2 } },
	{ name = "legendary", ar = { searchLegendary = "j_perkeo" } },
	{ name = "legendary negative", ar = { searchLegendary = "j_perkeo", searchNegativeLegendary = true } },
	{ name = "jokers ALL shop+packs", ar = { jokerSlotData = {
			{ key = "j_r1_3", index = 1 }, { key = "j_r2_7", index = 2 }, { key = "", index = 3 } } },
		ma = { ante1Slots = 4, ante2Slots = 8, ante2Packs = true, ante3Slots = 8, ante3Packs = true } },
	{ name = "jokers ANY + negative", ar = { jokerSearchMatchAny = true, jokerSlotData = {
			{ key = "j_r3_2", index = 1, requireNegative = true }, { key = "j_r1_9", index = 2 }, { key = "", index = 3 } } },
		ma = { ante1Slots = 8, ante1Packs = true, ante2Slots = 12 } },
	{ name = "unknown joker key", ar = { jokerSlotData = {
			{ key = "j_modded_unknown", index = 1 }, { key = "", index = 2 }, { key = "", index = 3 } } },
		ma = { ante1Slots = 6, ante1Packs = true } },
	{ name = "jokers packs only + pack filter", ar = { searchPack = { "p_buffoon_normal_1" }, jokerSlotData = {
			{ key = "j_r1_3", index = 1 }, { key = "", index = 2 }, { key = "", index = 3 } } },
		ma = { ante1Packs = true, ante2Packs = true } },
	{ name = "stacked (rare hit)", ar = { searchTag = "tag_charm", searchVoucher = "v_base_4", searchVoucherAnte = 1,
			searchLegendary = "j_perkeo", jokerSlotData = {
			{ key = "j_r2_7", index = 1 }, { key = "", index = 2 }, { key = "", index = 3 } } },
		ma = { ante2Slots = 8, ante2Packs = true, ante3Slots = 8, ante3Packs = true, ante4Slots = 12, ante4Packs = true } },
	-- Phase-1 deep search: anywhere mode (antes 1-8), wildcards, voucher any-8
	{ name = "anywhere wildcard neg rare", ar = { jokerSlotData = {
			{ key = "*rare", index = 1, requireNegative = true }, { key = "", index = 2 }, { key = "", index = 3 } } },
		ma = { anywhereMode = true, anywhereSlots = 8 } },
	{ name = "anywhere specific joker", ar = { jokerSlotData = {
			{ key = "j_r2_7", index = 1 }, { key = "", index = 2 }, { key = "", index = 3 } } },
		ma = { anywhereMode = true, anywhereSlots = 4 } },
	{ name = "anywhere wild-any neg + matchANY", ar = { jokerSearchMatchAny = true, jokerSlotData = {
			{ key = "*any", index = 1, requireNegative = true }, { key = "j_r1_3", index = 2 }, { key = "", index = 3 } } },
		ma = { anywhereMode = true, anywhereSlots = 6 } },
	{ name = "voucher any 1-8", ar = { searchVoucher = "v_base_7", searchVoucherAnte = -1 } },
	{ name = "voucher exact ante 7", ar = { searchVoucher = "v_base_2", searchVoucherAnte = 7 } },
	{ name = "wildcard mixed per-ante rows", ar = { jokerSlotData = {
			{ key = "*uncommon", index = 1, requireNegative = true }, { key = "j_r1_9", index = 2 }, { key = "", index = 3 } } },
		ma = { ante1Slots = 8, ante1Packs = true, ante2Slots = 8, ante2Packs = true } },
	{ name = "anywhere stacked deep", ar = { searchTag = "tag_charm", searchVoucher = "v_base_4", searchVoucherAnte = -1,
			jokerSlotData = {
			{ key = "*rare", index = 1, requireNegative = true }, { key = "", index = 2 }, { key = "", index = 3 } } },
		ma = { anywhereMode = true, anywhereSlots = 8 } },
	-- Phase-2: tag anywhere / culled tag / legendary anywhere / forced buffoon
	{ name = "tag culled (min_ante gate)", ar = { searchTag = "tag_3" } },
	{ name = "tag anywhere both blinds", ar = { searchTag = "tag_charm", searchTagAnywhere = true } },
	{ name = "tag anywhere gated tag", ar = { searchTag = "tag_3", searchTagAnywhere = true } },
	{ name = "legendary anywhere", ar = { searchLegendary = "j_perkeo", searchLegendaryAnywhere = true } },
	{ name = "legendary anywhere negative", ar = { searchLegendary = "j_triboulet", searchLegendaryAnywhere = true,
			searchNegativeLegendary = true } },
	-- Model 6: taking these tags replaces the skipped blind's missing shop
	-- with an immediate Soul-capable reward pack. The legendary scan must see
	-- that reward at its true chronological position, not only shop packs.
	{ name = "leg anywhere + collected Charm reward", ar = { searchTag = "tag_charm", searchTagAnywhere = true,
			searchLegendary = "j_perkeo", searchLegendaryAnywhere = true } },
	{ name = "leg anywhere + collected Ethereal reward", ar = { searchTag = "tag_ethereal", searchTagAnywhere = true,
			searchLegendary = "j_perkeo", searchLegendaryAnywhere = true } },
	{ name = "leg anywhere + tag anywhere + jokers", ar = { searchTag = "tag_charm", searchTagAnywhere = true,
			searchLegendary = "j_perkeo", searchLegendaryAnywhere = true, jokerSearchMatchAny = true,
			jokerSlotData = {
			{ key = "*rare", index = 1 }, { key = "j_r1_9", index = 2 }, { key = "", index = 3 } } },
		ma = { anywhereMode = true, anywhereSlots = 6 } },
	{ name = "pack filter forced buffoon", ar = { searchPack = { "p_buffoon_normal_1", "p_buffoon_normal_2" } } },
	{ name = "pack jokers mega count", ar = { jokerSlotData = {
			{ key = "j_r3_2", index = 1 }, { key = "", index = 2 }, { key = "", index = 3 } } },
		ma = { ante1Packs = true, ante2Packs = true, ante3Packs = true } },
	-- Model-3: skip-aware physical shop layout (ease_ante fires before the
	-- boss shop; a filtered tag/soul implies skipping that blind's shop)
	{ name = "tag + pack (skip Small A1)", ar = { searchTag = "tag_charm", searchPack = { "p_spectral_mega_1" } } },
	{ name = "soul + pack (charm-skip A1)", ar = { searchForSoul = 1, searchPack = { "p_arcana_mega_1" } } },
	{ name = "tag anywhere + anywhere packs", ar = { searchTag = "tag_3", searchTagAnywhere = true, jokerSlotData = {
			{ key = "j_r1_3", index = 1 }, { key = "", index = 2 }, { key = "", index = 3 } } },
		ma = { anywhereMode = true, anywhereSlots = 4 } },
	{ name = "pack alone (4-slot ante 1)", ar = { searchPack = { "p_standard_jumbo_1" } } },
	{ name = "tagAny + legAny + pack jokers", ar = { searchTag = "tag_charm", searchTagAnywhere = true,
			searchLegendary = "j_perkeo", searchLegendaryAnywhere = true, jokerSearchMatchAny = true,
			jokerSlotData = {
			{ key = "j_r2_7", index = 1 }, { key = "j_r1_9", index = 2 }, { key = "", index = 3 } } },
		ma = { ante1Packs = true, ante2Packs = true, ante2Slots = 4 } },
	-- soul-skip (Sm A1) + anywhere tag that CAN also match A1 Big: seeds where
	-- both ante-1 blinds are skipped exercise the forced-Buffoon cascade to
	-- ante 2's entry shop (forcedAnte = 2)
	{ name = "soul + tagAny (forced cascade)", ar = { searchForSoul = 1, searchTag = "tag_charm",
			searchTagAnywhere = true, searchPack = { "p_buffoon_normal_1" }, jokerSlotData = {
			{ key = "j_r1_3", index = 1 }, { key = "", index = 2 }, { key = "", index = 3 } } },
		ma = { ante1Packs = true, ante2Packs = true } },
}

-- ---- worker-style bootstrap (fake love.thread, stale session => no loop) ----
local function captureWorkerSrc()
	package.loaded["lovely"] = { mod_dir = "" }
	package.loaded["nativefs"] = {
		write = function() end, read = function() return "" end, getInfo = function() return nil end,
	}
	G = { FUNCS = {} }
	Brainstorm = { SETTINGS = { autoreroll = {}, multiAnteSearch = {} }, AUTOREROLL = {}, random_state = {} }
	assert(load(fileText, "bootstrap"))()
	return assert(Brainstorm.SEARCH_WORKER_SRC)
end

local workerSrc = captureWorkerSrc()

local function bootstrapCase(case, session)
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
	local snap = buildSnapshot(case, session, 0)
	assert(load(workerSrc, "worker"))(serialize(snap), fileText, 0, 1)
end

local CHARSET = "123456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
-- Typed-style seeds: the seed box also accepts 0/O and 1-7 character seeds
-- even though the game never generates them, and total-space .bspool pools
-- contain them -- the helper must agree on those too. Four random seeds of
-- every length over the full 36-symbol charset, plus fixed edge cases.
local CHARSET_TYPED = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
local function appendTypedSeeds(list, salt)
	local x = 987654321 + (salt or 0) * 104729
	for len = 1, 8 do
		for _ = 1, 4 do
			local chars = {}
			for j = 1, len do
				x = lcg(x)
				local idx = (x % 36) + 1
				chars[j] = CHARSET_TYPED:sub(idx, idx)
			end
			list[#list + 1] = table.concat(chars)
		end
	end
	list[#list + 1] = "1"
	list[#list + 1] = "0"
	list[#list + 1] = "O"
	list[#list + 1] = "0O0O0O0O"
end

local function makeSeedList(n, salt)
	local x = 123456789 + (salt or 0) * 7919
	local list = {}
	for i = 1, n do
		local chars = {}
		for j = 1, 8 do
			x = lcg(x)
			local idx = (x % 34) + 1
			chars[j] = CHARSET:sub(idx, idx)
		end
		list[i] = table.concat(chars)
	end
	appendTypedSeeds(list, salt)
	return list
end

-- The production Lua oracle still exposes its historical P<n> shop-pack
-- label, where the number is a flattened cursor into the shops that remain
-- after selected skips.  Native Model 6 now reports the physical collection
-- point instead.  Normalize only the fixture output so equivalence continues
-- to compare the same route using the public, human-readable label:
--
--   RNG ante 1: Small, Big
--   RNG ante 2+: previous Boss, current Small, current Big
--
-- Skipped Small/Big shops do not consume a P<n> pair, so this must consult the
-- oracle's route rather than mechanically mapping P3/P4 to Small.
local function canonicalLegendaryLocation(label, seed)
	if type(label) ~= "string" then return label end
	return (label:gsub("LegA(%d+)P(%d+)", function(streamAnteText, slotText)
		local streamAnte, slot = tonumber(streamAnteText), tonumber(slotText)
		local sim = assert(Brainstorm._packSim,
			"legacy Legendary label was produced without a pack route")
		assert(sim.seed == seed, "Legendary label route belongs to a different seed")

		local shopPairs = {}
		if streamAnte >= 2 then
			shopPairs[#shopPairs + 1] = { ante = streamAnte - 1, phase = "Boss" }
		end
		if not (sim.skipSm and sim.skipSm[streamAnte]) then
			shopPairs[#shopPairs + 1] = { ante = streamAnte, phase = "Sm" }
		end
		if not (sim.skipBig and sim.skipBig[streamAnte]) then
			shopPairs[#shopPairs + 1] = { ante = streamAnte, phase = "Big" }
		end

		local pair = shopPairs[math.floor((slot - 1) / 2) + 1]
		assert(pair, string.format("unmapped Legendary shop slot A%dP%d", streamAnte, slot))
		return "LegA" .. pair.ante .. "Shop" .. pair.phase
	end))
end

for ci, case in ipairs(CASES) do
	bootstrapCase(case, ci)
	local cfg = assert(Brainstorm.buildNativeConfigText(ci), "config serialize failed: " .. case.name)
	writeFile(outdir .. "/case" .. ci .. ".cfg", cfg)
	local seeds = makeSeedList(nseeds, ci)
	local exp, acc = {}, 0
	for i = 1, #seeds do
		local ok = Brainstorm.passesAllFilters(seeds[i])
		if ok then acc = acc + 1 end
		local foundAt = ok and canonicalLegendaryLocation(
			Brainstorm.AUTOREROLL.jokerFoundAt, seeds[i]) or nil
		exp[i] = seeds[i] .. " " .. (ok and "1" or "0") .. " "
			.. (foundAt or "-")
	end
	writeFile(outdir .. "/case" .. ci .. ".seeds", table.concat(seeds, "\n") .. "\n")
	writeFile(outdir .. "/case" .. ci .. ".expected", table.concat(exp, "\n") .. "\n")
	print(string.format("case %-2d %-34s accepts %d/%d", ci, case.name, acc, #seeds))
end
print("fixtures written to " .. outdir)
