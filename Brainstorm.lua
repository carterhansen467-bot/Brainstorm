Brainstorm = {}
function initBrainstorm()
	local lovely = require("lovely")
	local nativefs = require("nativefs")
	assert(load(nativefs.read(lovely.mod_dir .. "/Brainstorm/Brainstorm_main.lua")))()
	assert(load(nativefs.read(lovely.mod_dir .. "/Brainstorm/Brainstorm_UI.lua")))()
	assert(load(nativefs.read(lovely.mod_dir .. "/Brainstorm/Brainstorm_keyhandler.lua")))()
	assert(load(nativefs.read(lovely.mod_dir .. "/Brainstorm/Brainstorm_reroll.lua")))()
	if nativefs.getInfo(lovely.mod_dir .. "/Brainstorm/settings.lua") then
		local settings_file = STR_UNPACK(nativefs.read((lovely.mod_dir .. "/Brainstorm/settings.lua")))
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
  _RELEASE_MODE = not Brainstorm.SETTINGS.debug_mode
end


