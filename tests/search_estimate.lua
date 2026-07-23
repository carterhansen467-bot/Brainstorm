-- Regression checks for Brainstorm's interactive analytical search estimate.
-- This uses a deliberately tiny synthetic catalog so the expected values are
-- closed-form and independent of a player's profile/unlock state.

package.loaded.lovely = {mod_dir = ""}
package.loaded.brainstorm_nativefs = {
	read = function() return nil end,
	write = function() return true end,
	getInfo = function() return nil end,
}

STR_PACK = function() return "" end
local now = 10
love = {
	system = {getProcessorCount = function() return 4 end},
	timer = {getTime = function() return now end},
}

local function default_autoreroll()
	return {
		searchTag = "", searchPack = {}, searchVoucher = "", searchVoucherAnte = 1,
		searchForSoul = 0, searchLegendary = "", searchNegativeLegendary = false,
		searchTagAnywhere = false, searchLegendaryAnywhere = false,
		jokerSearchMatchAny = false, seedPoolFile = "", searchThreads = 1,
		seedsPerFrame = 500,
		jokerSlotData = {{key = ""}, {key = ""}, {key = ""}},
	}
end

local function default_multi_ante()
	local value = {}
	for ante = 1, 4 do
		value["ante" .. ante .. "Slots"] = 0
		value["ante" .. ante .. "Packs"] = false
	end
	return value
end

Brainstorm = {
	SETTINGS = {
		autoreroll = default_autoreroll(),
		multiAnteSearch = default_multi_ante(),
		useNativeSearch = false,
		useSearchThread = false,
	},
	AUTOREROLL = {},
	SearchTagList = {}, SearchPackList = {}, seedsPerFrame = {},
	searchThreadsValues = {},
	modPath = function() return "/tmp" end,
}

G = {
	FUNCS = {},
	C = {
		GREEN = {0, 1, 0, 1}, YELLOW = {1, 1, 0, 1},
		ORANGE = {1, 0.5, 0, 1}, RED = {1, 0, 0, 1},
	},
	GAME = {banned_keys = {}, pool_flags = {}},
	P_CENTERS = {},
	P_CENTER_POOLS = {
		Tag = {
			{key = "tag_charm"}, {key = "tag_double"},
			{key = "tag_rare"}, {key = "tag_coupon"},
		},
		Voucher = {
			{key = "v_one"}, {key = "v_two"}, {key = "v_three"}, {key = "v_four"},
			{key = "v_upgrade", requires = true},
		},
		Booster = {
			{key = "p_arcana_mega_1", kind = "Arcana", weight = 1, cards = 5},
			{key = "p_buffoon_normal_1", kind = "Buffoon", weight = 1, cards = 2},
			{key = "p_spectral_normal_1", kind = "Spectral", weight = 1, cards = 2},
			{key = "p_celestial_normal_1", kind = "Celestial", weight = 1, cards = 2},
		},
	},
	P_JOKER_RARITY_POOLS = {
		{{key = "j_c1", rarity = 1}, {key = "j_c2", rarity = 1}},
		{{key = "j_u1", rarity = 2}, {key = "j_u2", rarity = 2}},
		{{key = "j_r1", rarity = 3}, {key = "j_r2", rarity = 3}},
		{
			{key = "j_caino", rarity = 4}, {key = "j_triboulet", rarity = 4},
			{key = "j_yorick", rarity = 4}, {key = "j_chicot", rarity = 4},
			{key = "j_perkeo", rarity = 4},
		},
	},
}

assert(loadfile("Brainstorm_reroll.lua"))()
assert(loadfile("Brainstorm_estimate.lua"))()

local checks = 0
local function close(actual, expected, label, tolerance)
	tolerance = tolerance or 1e-10
	checks = checks + 1
	assert(math.abs(actual - expected) <= tolerance * math.max(1, math.abs(expected)),
		string.format("%s: got %.17g, expected %.17g", label, actual, expected))
end

local function reset()
	Brainstorm.SETTINGS.autoreroll = default_autoreroll()
	Brainstorm.SETTINGS.multiAnteSearch = default_multi_ante()
end

reset()
local est = Brainstorm.estimateSearch()
close(est.expectedSeeds, 1, "no filters")

reset()
Brainstorm.SETTINGS.autoreroll.searchTag = "tag_charm"
est = Brainstorm.estimateSearch()
close(est.expectedSeeds, 4, "one Ante-1 tag")

reset()
Brainstorm.SETTINGS.autoreroll.searchTag = "tag_charm"
Brainstorm.SETTINGS.autoreroll.searchTagAnywhere = true
est = Brainstorm.estimateSearch()
close(est.probability, 1 - (3 / 4) ^ 16, "tag anywhere")

reset()
Brainstorm.SETTINGS.autoreroll.searchVoucher = "v_two"
est = Brainstorm.estimateSearch()
close(est.expectedSeeds, 4, "one voucher Ante")
Brainstorm.SETTINGS.autoreroll.searchVoucherAnte = 0
est = Brainstorm.estimateSearch()
close(est.probability, 1 - (3 / 4) ^ 4, "voucher any Antes 1-4")

reset()
Brainstorm.SETTINGS.autoreroll.searchForSoul = 1
est = Brainstorm.estimateSearch()
local soulProbability = 1 - 0.997 ^ 5
close(est.probability, soulProbability, "classic Soul")

reset()
Brainstorm.SETTINGS.autoreroll.searchLegendary = "j_perkeo"
est = Brainstorm.estimateSearch()
close(est.probability, soulProbability / 5, "specific Legendary")
Brainstorm.SETTINGS.autoreroll.searchNegativeLegendary = true
est = Brainstorm.estimateSearch()
close(est.probability, soulProbability / 5 * 0.003, "Negative Legendary")

reset()
Brainstorm.SETTINGS.autoreroll.searchTag = "tag_missing"
est = Brainstorm.estimateSearch()
close(est.probability, 0, "unavailable target")
assert(est.expectedSeeds == math.huge, "unavailable target should be unbounded")

close(Brainstorm.searchHitLikelihood(4, 0.25), 1 - 0.75 ^ 4,
	"exact geometric likelihood")

reset()
Brainstorm.SETTINGS.multiAnteSearch.ante1Slots = 1
Brainstorm.SETTINGS.autoreroll.jokerSlotData[1] = {key = "j_c1"}
local oneJoker = Brainstorm.estimateSearch().probability
Brainstorm.SETTINGS.autoreroll.jokerSlotData[2] = {key = "j_c1"}
close(Brainstorm.estimateSearch().probability, oneJoker,
	"duplicate joker slots share one occurrence")

-- For duplicate keys, Match Any keeps the looser edition condition while
-- Match All keeps the stricter one.
Brainstorm.SETTINGS.autoreroll.jokerSearchMatchAny = true
Brainstorm.SETTINGS.autoreroll.jokerSlotData[1].requireNegative = true
Brainstorm.SETTINGS.autoreroll.jokerSlotData[2].requireNegative = false
close(Brainstorm.estimateSearch().probability, oneJoker,
	"duplicate ANY keeps ordinary occurrence")
Brainstorm.SETTINGS.autoreroll.jokerSearchMatchAny = false
close(Brainstorm.estimateSearch().probability, oneJoker * 0.003,
	"duplicate ALL keeps Negative occurrence")

reset()
Brainstorm.SETTINGS.multiAnteSearch.ante1Slots = 2
Brainstorm.SETTINGS.autoreroll.jokerSlotData[1] = {
	key = "j_c1", requireNegative = true,
}
-- Common target card chance per shop slot is (20/28) * (0.70/2) = 0.25.
-- The specific-joker matcher tests the edition of the first copy only.
close(Brainstorm.estimateSearch().probability,
	0.003 * (1 - (1 - 0.25) ^ 2),
	"specific Negative uses first occurrence")

reset()
Brainstorm.SETTINGS.multiAnteSearch.ante1Slots = 2
Brainstorm.SETTINGS.autoreroll.jokerSlotData[1] = {
	key = "*common", requireNegative = true,
}
-- A wildcard can use either Negative common, so every slot is an opportunity.
close(Brainstorm.estimateSearch().probability,
	1 - (1 - (20 / 28) * 0.70 * 0.003) ^ 2,
	"Negative wildcard uses any occurrence")

reset()
Brainstorm.SETTINGS.autoreroll.searchPack = {"p_buffoon_normal_1"}
close(Brainstorm.estimateSearch().probability, 1, "forced first Buffoon")

reset()
Brainstorm.SETTINGS.autoreroll.seedPoolFile = "tiny.bspool"
Brainstorm.seedPoolDir = function() return "/tmp" end
Brainstorm.readPoolHeader = function() return {records = 123} end
est = Brainstorm.estimateSearch()
close(est.expectedSeeds, 1, "pool with no overlay filters")
close(est.domain, 123, "pool candidate domain")

Brainstorm.readPoolHeader = function() return {records = 0} end
est = Brainstorm.estimateSearch()
close(est.probability, 0, "empty pool cannot match")
assert(est.poolError, "empty pool should explain why it cannot match")

Brainstorm.readPoolHeader = function() return nil end
est = Brainstorm.estimateSearch()
close(est.probability, 0, "missing pool cannot match")
assert(est.poolError, "missing pool should explain why it cannot match")

-- Search-wide progress remains monotonic when a failed backend hands off to
-- another backend whose local counter starts again at zero.
reset()
Brainstorm.SETTINGS.autoreroll.searchTag = "tag_charm"
Brainstorm.AUTOREROLL = {}
now = 100
Brainstorm.beginSearchStats()
Brainstorm.startSearchBackendCounter("native-2")
Brainstorm.AUTOREROLL.searchTried = 40
now = 102
Brainstorm.updateSearchStats()
close(Brainstorm.AUTOREROLL.searchTotalTried, 40, "native progress total")
close(Brainstorm.AUTOREROLL.searchLiveRate, 20, "measured wall-clock rate")
Brainstorm.AUTOREROLL.searchTried = 41
now = 102.05
Brainstorm.updateSearchStats()
close(Brainstorm.AUTOREROLL.searchTotalTried, 40,
	"per-frame progress formatting is throttled")
now = 102.11
Brainstorm.updateSearchStats()
close(Brainstorm.AUTOREROLL.searchTotalTried, 41,
	"throttled progress refreshes at 10 Hz")
Brainstorm.startSearchBackendCounter("lua-2")
Brainstorm.AUTOREROLL.searchTried = 10
now = 103
Brainstorm.updateSearchStats()
close(Brainstorm.AUTOREROLL.searchTotalTried, 51, "backend handoff total")
close(Brainstorm.AUTOREROLL.searchLikelihood, 1 - (3 / 4) ^ 51,
	"live likelihood uses total progress", 1e-9)

-- Attached-pool candidates are enriched, not random full-space samples. The
-- chance is unavailable during that phase and restarts at zero only for the
-- unrestricted suffix after an accelerator hands off.
Brainstorm.setAttachedPoolEstimateMode(true)
assert(Brainstorm.AUTOREROLL.searchEstimateUnavailable)
assert(Brainstorm.AUTOREROLL.searchProbability == nil)
Brainstorm.AUTOREROLL.searchTried = 30
now = 104
Brainstorm.updateSearchStats()
assert(Brainstorm.AUTOREROLL.searchLikelihood == nil)
local attachedLines = table.concat(Brainstorm.liveSearchTextLines(), "\n")
assert(attachedLines:find("Chance estimate unavailable", 1, true))
Brainstorm.setAttachedPoolEstimateMode(false)
Brainstorm.startSearchBackendCounter("native-2")
Brainstorm.AUTOREROLL.searchTried = 4
now = 105
Brainstorm.updateSearchStats()
close(Brainstorm.AUTOREROLL.searchTotalTried, 75,
	"attached fallback retains total progress")
close(Brainstorm.AUTOREROLL.searchLikelihood, 1 - (3 / 4) ^ 4,
	"attached fallback likelihood is rebased", 1e-9)

-- The settings text is useful before a search even when no matching-history
-- rate exists; after a backend rate has been learned it includes a time.
Brainstorm.SETTINGS.searchRateFallback = { ["sync-500"] = {rate = 4, updated = 1} }
Brainstorm.AUTOREROLL.nativeFailed = true
Brainstorm.refreshSearchEstimateDisplay(true)
assert(Brainstorm.SEARCH_ESTIMATE_DISPLAY.text:find("~1s", 1, true),
	"settings estimate should include learned backend time")

print(string.format("SEARCH ESTIMATE: %d CHECKS PASSED", checks))
