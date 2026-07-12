Brainstorm = {}

-- The mod's own folder under Mods/. Historically hardcoded as
-- "<Mods>/Brainstorm", which crashed the game at boot ("bad argument #1 to
-- 'load'") whenever the folder was named anything else -- GitHub branch ZIPs
-- extract as Brainstorm-<branch>, and users sometimes unzip one level deep.
-- Lovely applies our patches wherever the folder is, so locate it the same
-- way: find the directory that holds Brainstorm_main.lua.
function Brainstorm.locateModDir(root, nativefs)
	local function isMod(p)
		return nativefs.getInfo(p .. "/Brainstorm_main.lua") ~= nil and p or nil
	end
	local found = isMod(root .. "/Brainstorm")
	if found then return found end
	local dirs = nativefs.getDirectoryItems(root) or {}
	for _, d in ipairs(dirs) do
		found = isMod(root .. "/" .. d)
		if found then return found end
	end
	for _, d in ipairs(dirs) do
		for _, d2 in ipairs(nativefs.getDirectoryItems(root .. "/" .. d) or {}) do
			found = isMod(root .. "/" .. d .. "/" .. d2)
			if found then return found end
		end
	end
	return nil
end

-- All mod file paths go through here. MOD_PATH is set by initBrainstorm();
-- the fallback keeps the pre-locator behavior for test bootstraps that load
-- individual files without running init.
function Brainstorm.modPath()
	return Brainstorm.MOD_PATH or (require("lovely").mod_dir .. "/Brainstorm")
end

function initBrainstorm()
	local lovely = require("lovely")
	local nativefs = require("nativefs")
	Brainstorm.MOD_PATH = Brainstorm.locateModDir(lovely.mod_dir, nativefs)
	if not Brainstorm.MOD_PATH then
		error("[Brainstorm] could not find Brainstorm_main.lua anywhere under\n"
			.. lovely.mod_dir .. "\n"
			.. "Reinstall so the mod's files sit directly in Mods/Brainstorm\n"
			.. "(no extra nesting; see the README install steps).")
	end
	local MOD = Brainstorm.MOD_PATH
	assert(load(nativefs.read(MOD .. "/Brainstorm_main.lua")))()
	assert(load(nativefs.read(MOD .. "/Brainstorm_UI.lua")))()
	assert(load(nativefs.read(MOD .. "/Brainstorm_keyhandler.lua")))()
	assert(load(nativefs.read(MOD .. "/Brainstorm_reroll.lua")))()
	-- Full defaults for a fresh install. settings.lua is NOT shipped in the repo
	-- (it's user state, rewritten by the mod on every settings change -- shipping
	-- it made every `git pull` conflict); it's created on first save. A loaded
	-- file replaces this wholesale, and the backfills below patch files written
	-- by older versions.
	Brainstorm.SETTINGS = {
		keybinds = { loadState = "x", saveState = "z", rerollSeed = "t", autoReroll = "a" },
		autoreroll = {
			searchTag = "", searchTagID = 1,
			searchPack = {}, searchPackID = 1,
			searchVoucher = "", searchVoucherID = 1,
			searchForSoul = 0,
			searchLegendary = "", searchLegendaryID = 1,
			seedsPerFrame = 500, seedsPerFrameID = 1,
		},
		debug_mode = false,
	}
	if nativefs.getInfo(MOD .. "/settings.lua") then
		local settings_file = STR_UNPACK(nativefs.read(MOD .. "/settings.lua"))
		if settings_file ~= nil then
			Brainstorm.SETTINGS = settings_file
		end
	end
	if not Brainstorm.SETTINGS.multiAnteSearch then
		Brainstorm.SETTINGS.multiAnteSearch = {}
	end
	if not Brainstorm.SETTINGS.autoreroll.jokerSlotData then
		Brainstorm.SETTINGS.autoreroll.jokerSlotData = {
			{index = 1, key = Brainstorm.SETTINGS.autoreroll.searchJoker or "", requireNegative = false},
			{index = 1, key = "", requireNegative = false},
			{index = 1, key = "", requireNegative = false},
		}
	end
	for i = 1, 3 do
		local s = Brainstorm.SETTINGS.autoreroll.jokerSlotData[i]
		if s and s.requireNegative == nil then s.requireNegative = false end
	end
	if Brainstorm.SETTINGS.autoreroll.searchNegativeLegendary == nil then
		Brainstorm.SETTINGS.autoreroll.searchNegativeLegendary = false
	end
	if Brainstorm.SETTINGS.autoreroll.jokerSearchMatchAny == nil then
		Brainstorm.SETTINGS.autoreroll.jokerSearchMatchAny = false
	end
	if Brainstorm.SETTINGS.autoreroll.searchVoucherAnte == nil then
		Brainstorm.SETTINGS.autoreroll.searchVoucherAnte = 1
		Brainstorm.SETTINGS.autoreroll.searchVoucherAnteID = 1
	end
	-- Run the filtered seed search on a background love.thread so it doesn't stall
	-- the game. Set false to fall back to the old synchronous per-frame search.
	if Brainstorm.SETTINGS.useSearchThread == nil then
		Brainstorm.SETTINGS.useSearchThread = true
	end
	-- Number of parallel search worker threads. 0 = auto (cores - 1).
	if Brainstorm.SETTINGS.autoreroll.searchThreads == nil then
		Brainstorm.SETTINGS.autoreroll.searchThreads = 0
		Brainstorm.SETTINGS.autoreroll.searchThreadsID = 1
	end
	-- Precompute culled joker/voucher pools once per search (hot-path speedup).
	-- Kill switch: set false to force the original inline pool build everywhere.
	if Brainstorm.SETTINGS.useCulledCache == nil then
		Brainstorm.SETTINGS.useCulledCache = true
	end
	-- Where a found seed goes: 0 = overwrite the current run immediately (old
	-- behavior); 1-5 = bank the seed into that save slot and keep playing.
	if Brainstorm.SETTINGS.autoreroll.foundSeedSlot == nil then
		Brainstorm.SETTINGS.autoreroll.foundSeedSlot = 0
		Brainstorm.SETTINGS.autoreroll.foundSeedSlotID = 1
	end
	-- seed -> found-joker map so Ctrl+J survives quitting the game (see
	-- Brainstorm.recordFoundJoker / currentRunJoker).
	if Brainstorm.SETTINGS.foundJokers == nil then
		Brainstorm.SETTINGS.foundJokers = {}
	end
	-- Stake the found run starts at: 1 = White, 4 = Black, 8 = Gold.
	if Brainstorm.SETTINGS.autoreroll.foundSeedStake == nil then
		Brainstorm.SETTINGS.autoreroll.foundSeedStake = 1
		Brainstorm.SETTINGS.autoreroll.foundSeedStakeID = 1
	end
	-- "Anywhere" joker search: one uniform shop depth across antes 1-8 with
	-- packs on, overriding the per-ante Multi-Ante rows while enabled.
	if Brainstorm.SETTINGS.multiAnteSearch.anywhereMode == nil then
		Brainstorm.SETTINGS.multiAnteSearch.anywhereMode = false
	end
	if Brainstorm.SETTINGS.multiAnteSearch.anywhereSlots == nil then
		Brainstorm.SETTINGS.multiAnteSearch.anywhereSlots = 8
		Brainstorm.SETTINGS.multiAnteSearch.anywhereSlotsID = 3
	end
	-- Deep-search phase 2: tag search across both blinds of antes 1-8, and
	-- legendary search across every Arcana/Spectral pack of antes 1-8.
	if Brainstorm.SETTINGS.autoreroll.searchTagAnywhere == nil then
		Brainstorm.SETTINGS.autoreroll.searchTagAnywhere = false
	end
	if Brainstorm.SETTINGS.autoreroll.searchLegendaryAnywhere == nil then
		Brainstorm.SETTINGS.autoreroll.searchLegendaryAnywhere = false
	end
  _RELEASE_MODE = not Brainstorm.SETTINGS.debug_mode
end


