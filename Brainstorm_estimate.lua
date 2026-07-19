-- Main-thread-only search estimates and live telemetry. This intentionally
-- stays out of Brainstorm_reroll.lua because that file is copied into every
-- Lua search worker; workers publish exact counters but need no estimate UI.

-- ===========================================================================
-- Search estimates and live search telemetry
-- ---------------------------------------------------------------------------
-- GreenNeedle popularized a useful presentation: show an expected candidate
-- count and the cumulative chance of having seen a hit. Its estimate is a
-- product of fixed, hand-entered odds. Brainstorm has more exact information
-- available at runtime, so the implementation below derives its inputs from
-- the same eligible pools, booster weights, pack card counts, physical route,
-- and joker windows that passesAllFilters uses.
--
-- This is still an analytical estimate, not a claim that keyed pseudorandom
-- streams or overlapping route filters are statistically independent. The
-- external Seed Pool Builder remains the high-confidence option: its Quick
-- Estimate actually evaluates 2M seeds. Interactive first-hit search cannot
-- obtain that match-rate sample without doing much of the search itself.
-- ===========================================================================

local BS_NATURAL_SEEDSPACE = 1785793904896 -- 34^8; native helper's domain
local BS_ESTIMATE_VERSION = "estimate-v1"

local function bs_clamp_probability(p)
	if not p or p ~= p or p <= 0 then return 0 end
	if p >= 1 then return 1 end
	return p
end

local function bs_estimate_multiply(est, component)
	component = bs_clamp_probability(component)
	est.active = true
	est.probability = bs_clamp_probability(est.probability * component)
end

local function bs_count_eligible_tags(ante, target)
	local count, targetEligible = 0, false
	local pool = G and G.P_CENTER_POOLS and G.P_CENTER_POOLS.Tag
	for _, v in ipairs(pool or {}) do
		if Brainstorm.tag_is_eligible(v, ante) then
			count = count + 1
			if v.key == target then targetEligible = true end
		end
	end
	return count, targetEligible
end

local function bs_count_eligible_jokers(rarity, target)
	local count, targetEligible = 0, false
	local pool = G and G.P_JOKER_RARITY_POOLS and G.P_JOKER_RARITY_POOLS[rarity]
	for _, v in ipairs(pool or {}) do
		if Brainstorm.joker_is_pool_eligible(v) then
			count = count + 1
			if v.key == target then targetEligible = true end
		end
	end
	return count, targetEligible
end

local function bs_tag_estimate(est, ar)
	local route = { small = {}, big = {} }
	local target = ar.searchTag or ""
	if target == "" then return route end

	if not ar.searchTagAnywhere then
		local count, eligible = bs_count_eligible_tags(1, target)
		bs_estimate_multiply(est, eligible and count > 0 and (1 / count) or 0)
		route.small[1] = 1
		return route
	end

	-- Two sequential tag advances per Ante. Besides the total hit chance, keep
	-- the conditional first-hit location distribution: a matching tag removes
	-- that blind's shop, which changes the physical pack opportunities used by
	-- the pack/Legendary estimates below.
	local reach, total = 1, 0
	local rawSmall, rawBig = {}, {}
	for ante = 1, 8 do
		local count, eligible = bs_count_eligible_tags(ante, target)
		local one = eligible and count > 0 and (1 / count) or 0
		rawSmall[ante] = reach * one
		rawBig[ante] = reach * (1 - one) * one
		total = total + rawSmall[ante] + rawBig[ante]
		reach = reach * (1 - one) * (1 - one)
	end
	bs_estimate_multiply(est, total)
	if total > 0 then
		for ante = 1, 8 do
			route.small[ante] = rawSmall[ante] / total
			route.big[ante] = rawBig[ante] / total
		end
	end
	est.approximate = true -- sequential keyed draws are modeled as uniform
	return route
end

local function bs_classic_soul_estimate(est, ar)
	local legendaryAnywhere = ar.searchLegendaryAnywhere
		and ar.searchLegendary and ar.searchLegendary ~= ""
	local active = not legendaryAnywhere and
		(((ar.searchForSoul or 0) > 0)
			or (ar.searchLegendary and ar.searchLegendary ~= ""))
	if not active then return false end
	if G.GAME.banned_keys and G.GAME.banned_keys.c_soul then
		bs_estimate_multiply(est, 0)
		return true
	end

	local charm = Brainstorm.tagSoulRewardCenter("tag_charm")
	local cards = charm and Brainstorm.packCardCount(charm) or 0
	local onePack = cards > 0 and (1 - 0.997 ^ cards) or 0
	local needed = math.max(ar.searchForSoul or 0, 1)
	bs_estimate_multiply(est, onePack ^ needed)

	if ar.searchLegendary and ar.searchLegendary ~= "" then
		local count, eligible = bs_count_eligible_jokers(4, ar.searchLegendary)
		bs_estimate_multiply(est, eligible and count > 0 and (1 / count) or 0)
		if ar.searchNegativeLegendary then bs_estimate_multiply(est, 0.003) end
	end
	return true
end

local function bs_booster_totals()
	local total, selected = 0, 0
	local selectedKeys = {}
	local ar = Brainstorm.SETTINGS.autoreroll
	for _, key in ipairs(ar.searchPack or {}) do selectedKeys[key] = true end
	for _, v in ipairs((G.P_CENTER_POOLS and G.P_CENTER_POOLS.Booster) or {}) do
		if not (G.GAME.banned_keys and G.GAME.banned_keys[v.key]) then
			local weight = v.weight or 1
			total = total + weight
			if selectedKeys[v.key] then selected = selected + weight end
		end
	end
	return total, selected, selectedKeys
end

local function bs_route_pack_slots(ante, cap, route, classicSoul)
	local baseShops = ante >= 2 and 3 or 2
	local smallSkip = route.small[ante] or 0
	local bigSkip = route.big[ante] or 0
	if classicSoul and ante == 1 then smallSkip = 1 end
	local slots = 2 * math.max(0, baseShops - smallSkip - bigSkip)
	return math.min(cap or slots, slots)
end

local function bs_pack_filter_estimate(est, ar, route, classicSoul)
	if not ar.searchPack or #ar.searchPack == 0 then return end
	local total, selected, selectedKeys = bs_booster_totals()
	local slots = bs_route_pack_slots(1, 6, route, classicSoul)
	local forced = not (G.GAME.banned_keys
		and G.GAME.banned_keys.p_buffoon_normal_1) and slots > 0
	if forced and (selectedKeys.p_buffoon_normal_1
			or selectedKeys.p_buffoon_normal_2) then
		bs_estimate_multiply(est, 1)
		return
	end
	local randomSlots = math.max(0, slots - (forced and 1 or 0))
	local one = total > 0 and selected / total or 0
	bs_estimate_multiply(est, 1 - (1 - one) ^ randomSlots)
	if route.small[1] or route.big[1] then est.approximate = true end
end

local function bs_spectral_miss(cards)
	local soul, miss = 0.003, 0.997
	local blackHoleAllowed = not (G.GAME.banned_keys
		and G.GAME.banned_keys.c_black_hole)
	if not blackHoleAllowed then
		-- Each black-hole hit still overwrites a same-card Soul, but a banned
		-- Black Hole never sets the per-pack "already used" gate.
		return (1 - soul * miss) ^ cards
	end
	-- Failure-state Markov chain: a = no BH yet, b = BH already created.
	-- With no BH, a second 0.003 draw can overwrite the Soul on that card;
	-- after a BH, later cards only perform the first Soul draw.
	local a, b = 1, 0
	for _ = 1, cards do
		a, b = a * miss * miss, b * miss + a * soul
	end
	return a + b
end

local function bs_pack_soul_miss(center)
	local cards = Brainstorm.packCardCount(center)
	if center.kind == "Arcana" then return 0.997 ^ cards end
	if center.kind == "Spectral" then return bs_spectral_miss(cards) end
	return 1
end

local function bs_weighted_soul_miss()
	local weighted, total = 0, 0
	for _, v in ipairs((G.P_CENTER_POOLS and G.P_CENTER_POOLS.Booster) or {}) do
		if not (G.GAME.banned_keys and G.GAME.banned_keys[v.key]) then
			local weight = v.weight or 1
			total = total + weight
			weighted = weighted + weight * bs_pack_soul_miss(v)
		end
	end
	return total > 0 and weighted / total or 1
end

local function bs_expected_charm_rolls()
	local expected = 0
	for ante = 1, 8 do
		local count, eligible = bs_count_eligible_tags(ante, "tag_charm")
		if eligible and count > 0 then expected = expected + 2 / count end
	end
	return expected
end

local function bs_legendary_anywhere_estimate(est, ar, route)
	if not (ar.searchLegendaryAnywhere and ar.searchLegendary
			and ar.searchLegendary ~= "") then return end
	if G.GAME.banned_keys and G.GAME.banned_keys.c_soul then
		bs_estimate_multiply(est, 0)
		return
	end

	local physicalSlots = 0
	for ante = 1, 8 do
		physicalSlots = physicalSlots + bs_route_pack_slots(ante, 6, route, false)
	end
	local forced = not (G.GAME.banned_keys
		and G.GAME.banned_keys.p_buffoon_normal_1) and physicalSlots > 0
	local noSoul = bs_weighted_soul_miss()
		^ math.max(0, physicalSlots - (forced and 1 or 0))

	-- A selected Charm/Ethereal tag replaces one shop with its fixed reward.
	local reward = Brainstorm.tagSoulRewardCenter(ar.searchTag or "")
	if reward then noSoul = noSoul * bs_pack_soul_miss(reward) end

	-- passesAllFilters also tries each actually rolled Charm tag as a branch if
	-- the canonical route misses. Model their expected count using the exact
	-- per-Ante eligible tag pools. This is an expectation (branches can share
	-- streams), hence the explicit approximate marker.
	local charm = Brainstorm.tagSoulRewardCenter("tag_charm")
	if charm then
		noSoul = noSoul * bs_pack_soul_miss(charm) ^ bs_expected_charm_rolls()
	end
	bs_estimate_multiply(est, 1 - noSoul)

	local count, eligible = bs_count_eligible_jokers(4, ar.searchLegendary)
	bs_estimate_multiply(est, eligible and count > 0 and (1 / count) or 0)
	if ar.searchNegativeLegendary then bs_estimate_multiply(est, 0.003) end
	est.approximate = true
end

local function bs_voucher_estimate(est, ar)
	local target = ar.searchVoucher or ""
	if target == "" then return end
	local count, eligible = 0, false
	for _, key in ipairs(Brainstorm.getVoucherCulledPool()) do
		if key ~= "UNAVAILABLE" then
			count = count + 1
			if key == target then eligible = true end
		end
	end
	local one = eligible and count > 0 and (1 / count) or 0
	local mode = ar.searchVoucherAnte or 1
	local visits = mode == 0 and 4 or mode == -1 and 8 or 1
	bs_estimate_multiply(est, 1 - (1 - one) ^ visits)
	if visits > 1 then est.approximate = true end
end

local BS_RARITY_CHANCE = {0.70, 0.25, 0.05}

local function bs_joker_base_probability(target)
	local wild = Brainstorm.WILDCARD_RARITY[target.key]
	if wild ~= nil then
		return wild == 0 and 1 or (BS_RARITY_CHANCE[wild] or 0)
	end
	local rarity = Brainstorm.getJokerRarity(target.key)
	if not rarity then return 0 end
	local count, eligible = bs_count_eligible_jokers(rarity, target.key)
	if not eligible or count == 0 then return 0 end
	return BS_RARITY_CHANCE[rarity] * (1 / count)
end

local function bs_random_pack_target_miss(cardProbability)
	local weighted, total = 0, 0
	for _, v in ipairs((G.P_CENTER_POOLS and G.P_CENTER_POOLS.Booster) or {}) do
		if not (G.GAME.banned_keys and G.GAME.banned_keys[v.key]) then
			local weight = v.weight or 1
			local miss = 1
			if v.kind == "Buffoon" then
				miss = (1 - cardProbability) ^ Brainstorm.packCardCount(v)
			end
			total = total + weight
			weighted = weighted + weight * miss
		end
	end
	return total > 0 and weighted / total or 1
end

local function bs_joker_sequence_miss(target, baseCardProbability, cards)
	if cards <= 0 then return 1 end
	if not target.requireNegative then
		return (1 - baseCardProbability) ^ cards
	end
	if Brainstorm.WILDCARD_RARITY[target.key] ~= nil then
		-- Wildcards accept any qualifying Negative occurrence in the sequence.
		return (1 - baseCardProbability * 0.003) ^ cards
	end
	-- Specific jokers deliberately inspect only their first occurrence in each
	-- simulated shop/pack sequence. A later copy cannot rescue a non-Negative
	-- first copy, so apply the edition chance to P(at least one occurrence).
	return 1 - 0.003 * (1 - (1 - baseCardProbability) ^ cards)
end

local function bs_joker_pack_sequence_miss(target, baseCardProbability,
		physical, forced)
	local randomSlots = math.max(0, physical - (forced and 1 or 0))
	local occurrenceMiss = forced and
		((1 - baseCardProbability)
			^ Brainstorm.packCardCount(Brainstorm.FORCED_BUFFOON)) or 1
	occurrenceMiss = occurrenceMiss
		* bs_random_pack_target_miss(baseCardProbability) ^ randomSlots
	if target.requireNegative
			and Brainstorm.WILDCARD_RARITY[target.key] == nil then
		return 1 - 0.003 * (1 - occurrenceMiss)
	end
	if not target.requireNegative then return occurrenceMiss end

	-- A wildcard may use any qualifying Negative card, so edition probability
	-- belongs on every Buffoon card rather than only on the first occurrence.
	local matchProbability = baseCardProbability * 0.003
	local miss = forced and
		((1 - matchProbability)
			^ Brainstorm.packCardCount(Brainstorm.FORCED_BUFFOON)) or 1
	return miss * bs_random_pack_target_miss(matchProbability) ^ randomSlots
end

local function bs_one_joker_target_probability(target, anteSlots, antePacks,
		maxAnte, packSlots, route, classicSoul)
	local baseCardProbability = bs_joker_base_probability(target)
	if baseCardProbability <= 0 then return 0 end
	local shopMiss, packMiss = 1, 1
	for ante = 1, maxAnte do
		local slots = anteSlots[ante] or 0
		if slots > 0 then
			-- Each shop card is a Joker with probability 20/(20+4+4).
			shopMiss = shopMiss * bs_joker_sequence_miss(target,
				(20 / 28) * baseCardProbability, slots)
		end
		if antePacks[ante] then
			local physical = bs_route_pack_slots(ante, packSlots, route, classicSoul)
			local forced = ante == 1 and physical > 0
				and not (G.GAME.banned_keys
					and G.GAME.banned_keys.p_buffoon_normal_1)
			packMiss = packMiss * bs_joker_pack_sequence_miss(target,
				baseCardProbability, physical, forced)
		end
	end
	return bs_clamp_probability(1 - shopMiss * packMiss)
end

local function bs_joker_estimate(est, ar, route, classicSoul)
	local configured = ar.jokerSlotData or {}
	local anteSlots, antePacks, maxAnte, packSlots = Brainstorm.effectiveMultiAnte()
	local anyWindow = false
	for ante = 1, maxAnte do
		if (anteSlots[ante] or 0) > 0 or antePacks[ante] then
			anyWindow = true
			break
		end
	end
	if not anyWindow then return end

	-- Duplicate slot conditions can be fulfilled by the same occurrence. Merge
	-- them rather than squaring their probability: AND retains the stricter
	-- Negative requirement, while OR retains the looser requirement.
	local targets, byKey = {}, {}
	for _, slot in ipairs(configured) do
		if slot.key and slot.key ~= "" then
			local requireNegative = slot.requireNegative and true or false
			local target = byKey[slot.key]
			if not target then
				target = { key = slot.key, requireNegative = requireNegative }
				byKey[slot.key] = target
				targets[#targets + 1] = target
			elseif ar.jokerSearchMatchAny then
				-- Either duplicate can satisfy an OR search, so one ordinary copy
				-- makes the Negative-only duplicate irrelevant.
				target.requireNegative = target.requireNegative and requireNegative
			elseif requireNegative then
				-- Both duplicates must pass an AND search; the stricter occurrence
				-- condition therefore governs the merged target.
				target.requireNegative = true
			end
		end
	end
	if #targets == 0 then return end

	local combined
	if ar.jokerSearchMatchAny then
		local miss = 1
		for _, target in ipairs(targets) do
			miss = miss * (1 - bs_one_joker_target_probability(target,
				anteSlots, antePacks, maxAnte, packSlots, route, classicSoul))
		end
		combined = 1 - miss
	else
		combined = 1
		for _, target in ipairs(targets) do
			combined = combined * bs_one_joker_target_probability(target,
				anteSlots, antePacks, maxAnte, packSlots, route, classicSoul)
		end
	end
	bs_estimate_multiply(est, combined)
	est.approximate = true
end

function Brainstorm.estimateSearch()
	local ar = Brainstorm.SETTINGS.autoreroll
	local est = {
		probability = 1,
		active = false,
		approximate = false,
		domain = BS_NATURAL_SEEDSPACE,
	}
	if not (G and G.GAME and G.P_CENTER_POOLS) then
		est.probability, est.expectedSeeds = 0, math.huge
		return est
	end

	local poolName = ar.seedPoolFile or ""
	if poolName ~= "" then
		local header
		if Brainstorm.readPoolHeader and Brainstorm.seedPoolDir then
			header = Brainstorm.readPoolHeader(Brainstorm.seedPoolDir() .. "/" .. poolName)
		end
		if not header or tonumber(header.records) == nil then
			est.poolError = "Selected seed pool is missing or unreadable."
			est.probability, est.expectedSeeds = 0, math.huge
			return est
		end
		est.poolRecords = math.max(0, tonumber(header.records))
		est.domain = est.poolRecords
		if est.poolRecords == 0 then
			est.poolError = "Selected seed pool contains no recorded seeds."
			est.probability, est.expectedSeeds = 0, math.huge
			return est
		end
	end

	local route = bs_tag_estimate(est, ar)
	local classicSoul = bs_classic_soul_estimate(est, ar)
	bs_legendary_anywhere_estimate(est, ar, route)
	bs_pack_filter_estimate(est, ar, route, classicSoul)
	bs_voucher_estimate(est, ar)
	bs_joker_estimate(est, ar, route, classicSoul)

	-- A pool's embedded criteria are already true for every record. With no
	-- additional active filters, the first decoded record is therefore an exact
	-- one-candidate estimate rather than a projection from the pool's original
	-- whole-space match rate.
	if not est.active then est.probability = 1 end
	if est.probability > 0 then
		est.expectedSeeds = math.max(1, 1 / est.probability)
	else
		est.expectedSeeds = math.huge
	end
	if est.poolRecords and est.probability > 0 then
		est.poolHitChance = Brainstorm.searchHitLikelihood(est.poolRecords,
			est.probability)
	end
	return est
end

function Brainstorm.formatSeedEstimate(n)
	if not n or n == math.huge or n ~= n then return "unbounded" end
	if n >= 1e15 then
		local exponent = math.floor(math.log(n) / math.log(10))
		return string.format("%.1fe%d", n / (10 ^ exponent), exponent)
	elseif n >= 1e12 then return string.format("%.2fT", n / 1e12)
	elseif n >= 1e9 then return string.format("%.2fB", n / 1e9)
	elseif n >= 1e6 then return string.format("%.2fM", n / 1e6)
	elseif n >= 1e3 then return string.format("%.1fK", n / 1e3)
	end
	return tostring(math.max(0, math.floor(n + 0.5)))
end

function Brainstorm.formatSearchDuration(seconds)
	if not seconds or seconds ~= seconds or seconds == math.huge then return "unknown" end
	if seconds < 1 then return "<1s" end
	seconds = math.floor(seconds + 0.5)
	if seconds < 60 then return tostring(seconds) .. "s" end
	if seconds < 3600 then
		return string.format("%dm %02ds", math.floor(seconds / 60), seconds % 60)
	end
	if seconds < 86400 then
		return string.format("%dh %02dm", math.floor(seconds / 3600),
			math.floor(seconds / 60) % 60)
	end
	return string.format("%dd %02dh", math.floor(seconds / 86400),
		math.floor(seconds / 3600) % 24)
end

function Brainstorm.searchHitLikelihood(tried, probability)
	tried = math.max(0, tonumber(tried) or 0)
	probability = bs_clamp_probability(probability)
	if tried <= 0 or probability <= 0 then return 0 end
	if probability >= 1 then return 1 end
	-- log(1-p) loses all information for very small p on some LuaJIT builds;
	-- the first three terms retain an accurate geometric calculation there.
	local logMiss
	if probability < 1e-4 then
		logMiss = -probability - probability * probability / 2
			- probability * probability * probability / 3
	else
		logMiss = math.log(1 - probability)
	end
	return bs_clamp_probability(1 - math.exp(tried * logMiss))
end

function Brainstorm.formatSearchPercent(chance)
	local pct = bs_clamp_probability(chance) * 100
	if pct > 0 and pct < 0.001 then return "<0.001%" end
	if pct < 1 then return string.format("%.3f%%", pct) end
	if pct < 99 then return string.format("%.1f%%", pct) end
	return string.format("%.2f%%", pct)
end

function Brainstorm.nativeSearchBackendKey(threads)
	local pool = Brainstorm.SETTINGS.autoreroll.seedPoolFile or ""
	return "native-" .. tostring(threads)
		.. (pool ~= "" and "-pool" or "-space")
end

function Brainstorm.luaSearchBackendKey(threads)
	return "lua-" .. tostring(threads) .. "-cache"
		.. (Brainstorm.SETTINGS.useCulledCache == false and "0" or "1")
end

function Brainstorm.searchBackendKey()
	local A = Brainstorm.AUTOREROLL
	local threads = Brainstorm.getSearchThreadCount()
	if Brainstorm.nativeAvailable and Brainstorm.nativeAvailable()
			and not A.nativeFailed then
		return Brainstorm.nativeSearchBackendKey(threads)
	end
	if Brainstorm.SETTINGS.useSearchThread ~= false and love and love.thread
			and not A.searchThreadFailed then
		return Brainstorm.luaSearchBackendKey(threads)
	end
	return "sync-" .. tostring(Brainstorm.SETTINGS.autoreroll.seedsPerFrame or 500)
end

function Brainstorm.searchCriteriaSignature()
	local ar = Brainstorm.SETTINGS.autoreroll
	local ma = Brainstorm.SETTINGS.multiAnteSearch or {}
	local parts = {BS_ESTIMATE_VERSION}
	local function add(value) parts[#parts + 1] = tostring(value == nil and "-" or value) end
	add(ar.searchForSoul or 0)
	add(ar.searchLegendary or "")
	add(ar.searchNegativeLegendary and 1 or 0)
	add(ar.searchLegendaryAnywhere and 1 or 0)
	add(ar.searchTag or "")
	add(ar.searchTagAnywhere and 1 or 0)
	add(ar.searchVoucher or "")
	add(ar.searchVoucherAnte or 1)
	add(ar.seedPoolFile or "")
	for _, key in ipairs(ar.searchPack or {}) do add(key) end
	add("packs-end")
	for i = 1, 3 do
		local slot = ar.jokerSlotData and ar.jokerSlotData[i]
		add(slot and slot.key or "")
		add(slot and slot.requireNegative and 1 or 0)
	end
	add(ar.jokerSearchMatchAny and 1 or 0)
	add(ma.anywhereMode and 1 or 0)
	add(ma.anywhereSlots or 0)
	for ante = 1, 4 do
		add(ma["ante" .. ante .. "Slots"] or 0)
		add(ma["ante" .. ante .. "Packs"] and 1 or 0)
	end
	return table.concat(parts, "|")
end

local function bs_rate_history_lookup(signature, backend)
	local settings = Brainstorm.SETTINGS
	local key = signature .. "||" .. backend
	local exact = settings.searchRateHistory and settings.searchRateHistory[key]
	if exact and tonumber(exact.rate) and exact.rate > 0 then
		return exact.rate, true
	end
	local fallback = settings.searchRateFallback and settings.searchRateFallback[backend]
	if fallback and tonumber(fallback.rate) and fallback.rate > 0 then
		return fallback.rate, false
	end
	return nil, false
end

function Brainstorm.refreshSearchEstimateDisplay(force)
	Brainstorm.SEARCH_ESTIMATE_DISPLAY = Brainstorm.SEARCH_ESTIMATE_DISPLAY
		or { text = "", note = "", colour = {1, 1, 1, 1} }
	local display = Brainstorm.SEARCH_ESTIMATE_DISPLAY
	local signature = Brainstorm.searchCriteriaSignature()
	local backend = Brainstorm.searchBackendKey()
	local cacheKey = signature .. "||" .. backend
	if not force and display.cacheKey == cacheKey then return end
	display.cacheKey = cacheKey

	local est = Brainstorm.estimateSearch()
	display.estimate = est
	local rate, exactRate = bs_rate_history_lookup(signature, backend)
	if est.poolError then
		display.text = "Estimated search: seed pool unavailable"
		display.note = est.poolError
	elseif est.probability <= 0 then
		display.text = "Estimated search: no eligible match with these settings"
		display.note = "Check banned/unavailable targets or broaden the search."
	elseif est.expectedSeeds <= 1.0000001 then
		display.text = "Estimated search: ~1 seed"
		display.note = est.poolRecords and "Every selected pool record already satisfies its embedded filters."
			or "No active filter reduces the candidate set."
	else
		display.text = "Estimated search: ~1 in "
			.. Brainstorm.formatSeedEstimate(est.expectedSeeds) .. " seeds"
		if rate then
			display.text = display.text .. "  |  ~"
				.. Brainstorm.formatSearchDuration(est.expectedSeeds / rate)
		end
		local notes = {}
		if est.poolRecords then
			notes[#notes + 1] = "Analytical pool overlay: "
				.. Brainstorm.formatSearchPercent(est.poolHitChance) .. " chance / "
				.. Brainstorm.formatSeedEstimate(est.poolRecords) .. " records"
			notes[#notes + 1] = "overlap may make it conservative"
		else
			notes[#notes + 1] = "Analytical route estimate"
			if rate then
				notes[#notes + 1] = exactRate and "matching-run speed"
					or "recent backend speed"
			else
				notes[#notes + 1] = "speed calibrates during first search"
			end
		end
		if not est.poolRecords and est.expectedSeeds > est.domain then
			notes[#notes + 1] = "fewer than one expected match in the natural seed space"
		end
		display.note = table.concat(notes, "  |  ")
	end

	local c = display.colour
	local n = est.expectedSeeds
	local source = n <= 1e5 and G.C.GREEN
		or n <= 1e8 and G.C.YELLOW
		or n <= 1e11 and G.C.ORANGE
		or G.C.RED
	if est.probability <= 0 then source = G.C.RED end
	c[1], c[2], c[3], c[4] = source[1], source[2], source[3], source[4] or 1
end

local function bs_monotonic_time()
	return love and love.timer and love.timer.getTime and love.timer.getTime()
		or os.clock()
end

function Brainstorm.beginSearchStats()
	local A = Brainstorm.AUTOREROLL
	local est = Brainstorm.estimateSearch()
	A.searchStatsActive = true
	A.searchStartedAt = bs_monotonic_time()
	A.searchProbability = est.probability
	A.searchExpectedSeeds = est.expectedSeeds
	A.searchPoolRecords = est.poolRecords
	A.searchEstimateSignature = Brainstorm.searchCriteriaSignature()
	A.searchCounterBackend = nil
	A.searchBackendSwitches = 0
	A.searchBackendBase = 0
	A.searchTried = 0
	A.searchTotalTried = 0
	A.searchLiveRate = nil
	A.searchLikelihood = 0
	A.searchHeadline = A.searchHeadline or { value = "" }
	A.searchHeadline.value = "Searching... (0 / ~"
		.. Brainstorm.formatSeedEstimate(est.expectedSeeds) .. ")"
end

-- Start a fresh backend-local progress counter while retaining work performed
-- by a failed/relaunched backend in the search-wide total.
function Brainstorm.startSearchBackendCounter(backend)
	local A = Brainstorm.AUTOREROLL
	local previous = (A.searchBackendBase or 0) + (A.searchTried or 0)
	if A.searchCounterBackend and A.searchCounterBackend ~= backend then
		A.searchBackendSwitches = (A.searchBackendSwitches or 0) + 1
	end
	A.searchBackendBase = previous
	A.searchTried = 0
	A.searchCounterBackend = backend
	A.searchTotalTried = previous
end

function Brainstorm.updateSearchStats()
	local A = Brainstorm.AUTOREROLL
	if not A.searchStatsActive then return end
	local now = bs_monotonic_time()
	local elapsed = math.max(0, now - (A.searchStartedAt or now))
	local tried = (A.searchBackendBase or 0) + (A.searchTried or 0)
	A.searchTotalTried = tried
	A.searchWallElapsed = elapsed
	if elapsed >= 0.25 and tried > 0 then A.searchLiveRate = tried / elapsed end
	A.searchLikelihood = Brainstorm.searchHitLikelihood(tried,
		A.searchProbability or 0)
	local expected = A.searchExpectedSeeds or math.huge
	local suffix = expected < math.huge and
		(" / ~" .. Brainstorm.formatSeedEstimate(expected)) or ""
	A.searchHeadline = A.searchHeadline or { value = "" }
	A.searchHeadline.value = "Searching... ("
		.. Brainstorm.formatSeedEstimate(tried) .. suffix .. ")"
end

local function bs_trim_rate_history(history, maximum)
	local count = 0
	for _ in pairs(history) do count = count + 1 end
	while count > maximum do
		local oldestKey, oldestTime
		for key, value in pairs(history) do
			local updated = tonumber(value.updated) or 0
			if not oldestTime or updated < oldestTime then
				oldestKey, oldestTime = key, updated
			end
		end
		if not oldestKey then break end
		history[oldestKey] = nil
		count = count - 1
	end
end

function Brainstorm.finishSearchStats()
	local A = Brainstorm.AUTOREROLL
	if not A.searchStatsActive then return end
	Brainstorm.updateSearchStats()
	local elapsed = A.searchWallElapsed or 0
	local tried = A.searchTotalTried or 0
	local backend = A.searchCounterBackend
	if backend and (A.searchBackendSwitches or 0) == 0
			and elapsed >= 0.75 and tried >= 1000 then
		local rate = tried / elapsed
		local updated = os.time()
		Brainstorm.SETTINGS.searchRateHistory =
			Brainstorm.SETTINGS.searchRateHistory or {}
		Brainstorm.SETTINGS.searchRateFallback =
			Brainstorm.SETTINGS.searchRateFallback or {}
		local entry = { rate = rate, updated = updated }
		Brainstorm.SETTINGS.searchRateHistory[
			(A.searchEstimateSignature or "") .. "||" .. backend] = entry
		Brainstorm.SETTINGS.searchRateFallback[backend] = entry
		bs_trim_rate_history(Brainstorm.SETTINGS.searchRateHistory, 24)
		pcall(function()
			local nativefs = require("nativefs")
			nativefs.write(Brainstorm.modPath() .. "/settings.lua",
				STR_PACK(Brainstorm.SETTINGS))
		end)
	end
	A.searchStatsActive = false
	if Brainstorm.SEARCH_ESTIMATE_DISPLAY then
		Brainstorm.SEARCH_ESTIMATE_DISPLAY.cacheKey = nil
	end
end

function Brainstorm.liveSearchTextLines()
	local A = Brainstorm.AUTOREROLL
	local tried = A.searchTotalTried or 0
	local expected = A.searchExpectedSeeds or math.huge
	local line1 = Brainstorm.formatSeedEstimate(tried) .. " checked"
	if expected < math.huge then
		line1 = line1 .. "  /  ~" .. Brainstorm.formatSeedEstimate(expected)
	end
	local elapsed = Brainstorm.formatSearchDuration(A.searchWallElapsed or 0)
	local line2 = "Elapsed " .. elapsed
	if A.searchLiveRate and A.searchLiveRate > 0 and expected < math.huge then
		-- A geometric search is memoryless: after any number of misses, the
		-- expected additional trials remain 1/p. Label this average explicitly
		-- instead of presenting a misleading countdown that reaches zero.
		line2 = line2 .. "  |  Avg. remaining ~"
			.. Brainstorm.formatSearchDuration(expected / A.searchLiveRate)
	end
	local line3 = "Chance by now "
		.. Brainstorm.formatSearchPercent(A.searchLikelihood or 0)
	if A.searchLiveRate and A.searchLiveRate > 0 then
		line3 = line3 .. "  |  "
			.. Brainstorm.formatSeedEstimate(A.searchLiveRate) .. " seeds/s"
	else
		line3 = line3 .. "  |  measuring speed..."
	end
	return {line1, line2, line3}
end
