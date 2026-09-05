local nativefs = require("brainstorm_nativefs")

-- In-game this is already defined (Brainstorm.lua locates MOD_PATH before
-- loading us); the fallback covers standalone loads -- test harnesses and
-- search worker threads -- which fake lovely with mod_dir = "".
Brainstorm.modPath = Brainstorm.modPath or function()
	return Brainstorm.MOD_PATH or (require("lovely").mod_dir .. "/Brainstorm")
end

Brainstorm.AUTOREROLL = {}

G.FUNCS.change_search_tag = function(x)
	Brainstorm.SETTINGS.autoreroll.searchTagID = x.to_key
	Brainstorm.SETTINGS.autoreroll.searchTag = Brainstorm.SearchTagList[x.to_val]
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

G.FUNCS.change_search_pack = function(x)
	Brainstorm.SETTINGS.autoreroll.searchPackID = x.to_key
	Brainstorm.SETTINGS.autoreroll.searchPack = Brainstorm.SearchPackList[x.to_val]
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

G.FUNCS.change_search_soul_count = function(x)
	Brainstorm.SETTINGS.autoreroll.searchForSoul = x.to_val
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

G.FUNCS.change_seeds_per_frame = function(x)
	Brainstorm.SETTINGS.autoreroll.seedsPerFrameID = x.to_key
	Brainstorm.SETTINGS.autoreroll.seedsPerFrame = Brainstorm.seedsPerFrame[x.to_val]
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

G.FUNCS.change_search_threads = function(x)
	Brainstorm.SETTINGS.autoreroll.searchThreadsID = x.to_key
	Brainstorm.SETTINGS.autoreroll.searchThreads = Brainstorm.searchThreadsValues[x.to_val] or 0
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

Brainstorm.AUTOREROLL.autoRerollActive = false
Brainstorm.AUTOREROLL.rerollInterval = 0.01 -- Time interval between rerolls (in seconds)
Brainstorm.AUTOREROLL.rerollTimer = 0

function FastReroll()
	G.GAME.viewed_back = nil
	G.run_setup_seed = G.GAME.seeded
	G.challenge_tab = G.GAME and G.GAME.challenge and G.GAME.challenge_tab or nil
	G.forced_seed, G.setup_seed = nil, nil
	if G.GAME.seeded then
		G.forced_seed = G.GAME.pseudorandom.seed
	end
	local current_stake = G.GAME.stake
	local _seed = G.run_setup_seed and G.setup_seed or G.forced_seed or nil
	local _challenge = G.challenge_tab
	if not G.challenge_tab then
		_stake = current_stake or G.PROFILES[G.SETTINGS.profile].MEMORY.stake or 1
	else
		_stake = 1
	end
	G:delete_run()
	G:start_run({ stake = _stake, seed = _seed, challenge = _challenge })
end

-- Runs every configured filter against a candidate seed. Returns true only if
-- the seed passes all active filters. Extracted verbatim from the old inline
-- auto_reroll body so the EXACT same logic can also run on a background
-- love.thread worker (see Brainstorm.SEARCH_WORKER_SRC). Any RNG fix here is
-- automatically shared with the worker because the worker loads this file.
-- Filters run CHEAPEST + MOST SELECTIVE FIRST. Reordering is safe because every
-- filter reads its own independent RNG stream (each pseudoseed key hashes
-- key..seed on first use; no filter reads another's stream), so the accept set
-- is order-independent -- verified by an old-vs-new equivalence test over
-- synthetic pools. Why this order matters: the soul/legendary check costs ~6
-- rolls and rejects ~98.5% (soul) to ~99.7% (specific legendary) of seeds,
-- while the multi-ante joker search costs ~70-200 pool picks -- running soul
-- first means the expensive joker walk only ever runs on the tiny fraction of
-- seeds that already have the legendary start.
function Brainstorm.passesAllFilters(seed_found)
	-- Hoisted once per candidate: this function is the per-seed hot loop, and
	-- each Brainstorm.SETTINGS.autoreroll.x read below costs three table hops.
	local ar = Brainstorm.SETTINGS.autoreroll
	Brainstorm.random_state = {
		hashed_seed = pseudohash(seed_found),
	}
	local legAnywhere = ar.searchLegendaryAnywhere and ar.searchLegendary and ar.searchLegendary ~= ""
	-- 1) Soul / legendary (cheapest per rejection by far). With the Anywhere
	-- toggle on, this ante-1 charm-tag-convention block is replaced by the
	-- pack scan in step 2.5 (order swap is safe: independent streams;
	-- searchForSoul is ignored in Anywhere mode).
	if not legAnywhere and ((ar.searchForSoul and ar.searchForSoul > 0) or (ar.searchLegendary and ar.searchLegendary ~= "")) then
		if G.GAME.banned_keys and G.GAME.banned_keys.c_soul then return false end
		local charmPack = Brainstorm.tagSoulRewardCenter("tag_charm")
		if not charmPack then return false end
		local charmCards = Brainstorm.packCardCount(charmPack)
		local needed = math.max(ar.searchForSoul or 0, 1)
		local last_soul_found = false
		for i = 1, needed do
			local soul_found = false
			for j = 1, charmCards do
				if pseudorandom(Brainstorm.pseudoseed("soul_Tarot1" .. seed_found)) > 0.997 then
					soul_found = true
				end
			end
			last_soul_found = soul_found
			if not soul_found then
				return false
			end
		end

		if ar.searchLegendary and ar.searchLegendary ~= "" then
			if not last_soul_found then
				return false
			else
				local filtered = Brainstorm.getJokerCulledPool(4)
				local chosen_key = pseudorandom_element(filtered, Brainstorm.pseudoseed("Joker4" .. seed_found))
				local it = 1
				while chosen_key == 'UNAVAILABLE' do
					it = it + 1
					chosen_key = pseudorandom_element(filtered, Brainstorm.pseudoseed("Joker4" .. '_resample' .. it .. seed_found))
				end
				if chosen_key ~= ar.searchLegendary then
					return false
				elseif ar.searchNegativeLegendary then
					local edition_poll = pseudorandom(Brainstorm.pseudoseed("edisou1" .. seed_found))
					if edition_poll <= 0.997 then return false end
				end
			end
		end
	end
	-- 2) Tag. SOURCE-VERIFIED FIX (get_next_tag_key + get_current_pool 'Tag'):
	-- the game picks from an index-preserving CULLED string array (requires-
	-- center undiscovered / min_ante > ante / banned => 'UNAVAILABLE') and
	-- resamples with 'Tag'..ante..'_resample'..it. The old raw-pool pick
	-- diverged whenever the roll landed on a culled tag (e.g. tag_negative has
	-- min_ante=2, so ~1/24 of ante-1 rolls). searchTagAnywhere checks BOTH
	-- blinds (Small rolls first, then Big -- game.lua reset_blinds) across
	-- antes 1-8; obtain the tag by skipping that blind.
	local tagLoc = nil
	if ar.searchTag ~= "" then
		if ar.searchTagAnywhere then
			for ante = 1, 8 do
				if Brainstorm.rollTag(seed_found, ante) == ar.searchTag then
					tagLoc = "TagA" .. ante .. "Sm"
					break
				end
				if Brainstorm.rollTag(seed_found, ante) == ar.searchTag then
					tagLoc = "TagA" .. ante .. "Big"
					break
				end
			end
			if not tagLoc then return false end
		else
			if Brainstorm.rollTag(seed_found, 1) ~= ar.searchTag then
				return false
			end
		end
	end
	-- 2.4) Install this seed's blind-skip assumption BEFORE any pack consumer
	-- (legendary-anywhere scan, pack filter, joker-in-pack matcher). Taking a
	-- filtered tag/soul means SKIPPING that blind, and a skipped blind's shop
	-- never opens (skip_blind goes straight to the next blind select), so its
	-- two get_pack picks are never drawn. See skipsFromFilters.
	local filterSkipSm, filterSkipBig, filterRewardSm, filterRewardBig =
		Brainstorm.skipsFromFilters(tagLoc)
	local finalSkipSm, finalSkipBig = filterSkipSm, filterSkipBig
	local finalRewardSm, finalRewardBig = filterRewardSm, filterRewardBig
	local finalVoucherRoute = nil
	local effectivePool = ((ar.seedPoolFile and ar.seedPoolFile ~= "")
		or Brainstorm.AUTOREROLL.autoPoolSelection)
		and Brainstorm.effectiveSeedPoolSelection
		and Brainstorm.effectiveSeedPoolSelection() or nil
	if effectivePool and Brainstorm.readPoolHeader then
		local poolPath = effectivePool.path
		local poolHeader = effectivePool.header
			or (poolPath and Brainstorm.readPoolHeader(poolPath))
		if not poolHeader then return false end
		local poolOK
		-- Active tag/classic-Soul predicates were just read on their own pass.
		-- Rewind so the pool's embedded rules replay the same per-blind rolls
		-- from the beginning, then rewind once more for downstream overlays.
		Brainstorm.random_state = { hashed_seed = pseudohash(seed_found) }
		poolOK, finalSkipSm, finalSkipBig, finalRewardSm, finalRewardBig,
			finalVoucherRoute =
			Brainstorm.evaluatePoolCriteria(seed_found, poolHeader,
				filterSkipSm, filterSkipBig, filterRewardSm, filterRewardBig)
		if not poolOK then return false end
		-- The pool oracle and active filters share a route but each starts its
		-- independent RNG streams at advance one.
		Brainstorm.random_state = { hashed_seed = pseudohash(seed_found) }
	end
	Brainstorm.setPackSkipAssumption(seed_found, finalSkipSm, finalSkipBig,
		finalRewardSm, finalRewardBig)
	-- 2.5) Legendary ANYWHERE (antes 1-8): scan each ante's spawned Arcana /
	-- Spectral packs for the run's FIRST Soul (see checkLegendaryAnywhere).
	local legLoc = nil
	if legAnywhere then
		local ok, loc = Brainstorm.checkLegendaryAnywhere(seed_found,
			ar.searchLegendary, ar.searchNegativeLegendary, finalVoucherRoute)
		if not ok then
			ok, loc, finalSkipSm, finalSkipBig, finalRewardSm, finalRewardBig =
				Brainstorm.tryTargetedCharmLegendary(seed_found, ar.searchLegendary,
					ar.searchNegativeLegendary, finalSkipSm, finalSkipBig,
					finalRewardSm, finalRewardBig, finalVoucherRoute)
		end
		if not ok then return false end
		legLoc = loc
	end
	-- 3) Pack: scan ALL of ante 1's PHYSICAL slots. SOURCE-VERIFIED SHOP MODEL
	-- (ease_ante fires on boss death, before its shop): ante 1 has only the
	-- Small- and Big-blind shops -- the post-boss shop already draws from
	-- 'shop_pack2' -- so at most [forced buffoon, adv1 | adv2, adv3] exist,
	-- minus 2 per assumed-skipped blind. Slots that can never spawn must not
	-- match (the old fixed 1-3 window let phantom picks pass the filter).
	-- The forced normal Buffoon truthfully always-matches buffoon targets.
	if ar.searchPack and #ar.searchPack > 0 then
		local packs = Brainstorm.getSimulatedPacks(seed_found, 1, 6)
		local list = ar.searchPack
		local pack_found = false
		for slot = 1, #packs do
			local center = packs[slot]
			if center then
				if center.forced then
					for i = 1, #list do
						if list[i] == "p_buffoon_normal_1" or list[i] == "p_buffoon_normal_2" then pack_found = true; break end
					end
				else
					for i = 1, #list do
						if list[i] == center.key then pack_found = true; break end
					end
				end
			end
			if pack_found then break end
		end
		if not pack_found then
			return false
		end
	end
	-- 4) Voucher
	if ar.searchVoucher and ar.searchVoucher ~= "" then
		local ante_mode = ar.searchVoucherAnte or 1
		if not Brainstorm.checkVoucherSearch(seed_found, ar.searchVoucher, ante_mode) then
			return false
		end
	end
	-- 5) Multi-ante jokers (the expensive walk) last
	if not Brainstorm.checkMultiAnteJokerSearch(seed_found) then
		return false
	end
	-- Compose the found-at label: joker parts (set by the walk above), then
	-- legendary pack location, then tag blind location. C helper mirrors this
	-- exact order (fixtures compare labels byte-for-byte).
	if tagLoc or legLoc then
		local parts = {}
		if Brainstorm.AUTOREROLL.jokerFoundAt then parts[#parts + 1] = Brainstorm.AUTOREROLL.jokerFoundAt end
		if legLoc then parts[#parts + 1] = legLoc end
		if tagLoc then parts[#parts + 1] = tagLoc end
		Brainstorm.AUTOREROLL.jokerFoundAt = table.concat(parts, " ")
	end
	return true
end

-- Starts a fresh run on the winning seed, overwriting the current run. MUST run
-- on the main thread (touches G). `stake` is optional; when nil it inherits the
-- current run's stake (the live "overwrite current run" path). The banked-seed
-- load path passes the stake stored at bank time.
function Brainstorm.applyFoundSeed(seed_found, stake)
	_stake = stake or (G.GAME and G.GAME.stake) or 1
	G:delete_run()
	G:start_run({
		stake = _stake,
		seed = seed_found,
		challenge = G.GAME and G.GAME.challenge and G.GAME.challenge_tab,
	})
	G.GAME.seeded = false
end

-- Writes the found seed into a save slot as a lightweight marker. A real save
-- blob can't be fabricated for an unplayed seed (save_run serializes the live G),
-- so we store the seed + stake instead; loading the slot starts a fresh run on it
-- (see the banked-seed branch of the load handler in Brainstorm_keyhandler.lua).
-- Never touches the current run, so the player keeps playing uninterrupted.
function Brainstorm.bankFoundSeed(seed_found, slot, jokerFoundAt)
	local marker = {
		brainstorm_found_seed = seed_found,
		-- Stake comes from the "Found Stake" setting (White/Black/Gold). Stake is
		-- safe to choose freely: per the real source (common_events.lua:2138) the
		-- stake modifiers only gate what the etperpoll/ssjr roll VALUES do, not
		-- whether the streams the filters read are rolled, so the same seed gives
		-- identical tags/packs/vouchers/jokers/negatives at any stake.
		stake = Brainstorm.SETTINGS.autoreroll.foundSeedStake or (G.GAME and G.GAME.stake) or 1,
		joker = jokerFoundAt,
		ts = os.time(),
	}
	compress_and_save(G.SETTINGS.profile .. "/" .. "saveState" .. slot .. ".jkr", marker)
end

-- Persist the found joker keyed by seed, so Ctrl+J still resolves after quitting
-- (lastJokerFoundAt is in-memory only). Survives restart + resume-via-Continue,
-- not just re-loading a banked slot. Stored in settings so it loads at launch.
function Brainstorm.recordFoundJoker(seed, joker)
	if not seed or not joker then return end
	Brainstorm.SETTINGS.foundJokers = Brainstorm.SETTINGS.foundJokers or {}
	Brainstorm.SETTINGS.foundJokers[string.upper(seed)] = joker
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

-- What Ctrl+J should show: the joker recorded for the seed the player is CURRENTLY
-- on (authoritative across restarts), falling back to the in-session last find.
function Brainstorm.currentRunJoker()
	local seed = G.GAME and G.GAME.pseudorandom and G.GAME.pseudorandom.seed
	if seed and Brainstorm.SETTINGS.foundJokers then
		local j = Brainstorm.SETTINGS.foundJokers[string.upper(seed)]
		if j then return j end
	end
	return Brainstorm.AUTOREROLL.lastJokerFoundAt
end

-- Synchronous (main-thread) search. Still used as a fallback when the search
-- thread is disabled (Brainstorm.SETTINGS.useSearchThread == false) or when
-- love.thread is unavailable. Returns the seed WITHOUT acting on it; the caller
-- (updateAutoReroll) decides whether to overwrite the current run or bank it.
function Brainstorm.auto_reroll()
	local rerollsThisFrame = 0
	-- This part is meant to mimic how Balatro rerolls for Gold Stake
	local extra_num = -0.561892350821
	local seed_found = nil
	while not seed_found and rerollsThisFrame < Brainstorm.SETTINGS.autoreroll.seedsPerFrame do
		rerollsThisFrame = rerollsThisFrame + 1
		extra_num = extra_num + 0.561892350821
		seed_found = random_string(
			8,
			extra_num
				+ G.CONTROLLER.cursor_hover.T.x * 0.33411983
				+ G.CONTROLLER.cursor_hover.T.y * 0.874146
				+ 0.412311010 * G.CONTROLLER.cursor_hover.time
		)
		if not Brainstorm.passesAllFilters(seed_found) then
			seed_found = nil
		end
	end
	-- The background and native paths publish authoritative progress of their
	-- own. Keep the synchronous fallback on the same counter contract so the
	-- live estimate UI never has to infer work from frames or configured batch
	-- size (the final batch may stop early on a hit).
	Brainstorm.AUTOREROLL.searchTried =
		(Brainstorm.AUTOREROLL.searchTried or 0) + rerollsThisFrame
	return seed_found
end

-- Roll the shop voucher for each ante 1..max_ante and return {[ante] = key}.
-- VERIFIED EMPIRICALLY against a live run (seed BFXJ42PE, ante-1 voucher
-- v_seed_money; the game's own G.GAME.pseudorandom held keys Voucher1 and
-- Voucher1_resample2..5):
--   * Each ante uses its OWN independent RNG key 'Voucher'..ante (ante 1 = key
--     "Voucher1"), first advance only -- NOT a single advancing 'Voucher' stream.
--     (The extracted source's get_current_pool showed just 'Voucher', but the
--     shipped game clearly appends the ante, same as the joker keys.) Resamples
--     use 'Voucher'..ante..'_resample'..it.
--   * Pool = base vouchers only; upgraded vouchers (v.requires) stay UNAVAILABLE
--     (prereq not in used_vouchers). ~half the 32-entry pool is UNAVAILABLE, so
--     resamples are common (ante 1 above resampled 4x before landing).
--   * An unredeemed voucher remains eligible on the next normal roll. The old
--     shop and its voucher CardArea are destroyed before the Boss transition
--     calls get_next_voucher_key(), so G.shop_vouchers no longer excludes the
--     previous offer. Only redeemed/starting-owned vouchers are removed.
function Brainstorm.rollVoucherSequence(seed_found, max_ante)
	local base = Brainstorm.getVoucherCulledPool()
	local out = {}
	for ante = 1, max_ante do
		local key = 'Voucher' .. ante
		local chosen = pseudorandom_element(base, Brainstorm.pseudoseed(key .. seed_found))
		local it = 1
		while chosen == 'UNAVAILABLE' do
			it = it + 1
			chosen = pseudorandom_element(base, Brainstorm.pseudoseed(key .. '_resample' .. it .. seed_found))
		end
		out[ante] = chosen
	end
	return out
end

-- ante_mode: 1-8 requires the target voucher at exactly that ante; 0 = any of
-- 1-4; -1 = any of 1-8. Deeper antes use the same per-ante 'Voucher'..ante
-- keys. Skipping an offer does not remove it from later pools.
function Brainstorm.checkVoucherSearch(seed_found, target_key, ante_mode)
	ante_mode = ante_mode or 1
	local any_to = (ante_mode == 0 and 4) or (ante_mode == -1 and 8) or nil
	local max_ante = any_to or ante_mode
	if any_to then
		-- Each Ante has an independent stream, so later rolls cannot change an
		-- ANY match that has already succeeded.
		local base = Brainstorm.getVoucherCulledPool()
		for ante = 1, any_to do
			local key = 'Voucher' .. ante
			local chosen = pseudorandom_element(base, Brainstorm.pseudoseed(key .. seed_found))
			local it = 1
			while chosen == 'UNAVAILABLE' do
				it = it + 1
				chosen = pseudorandom_element(base,
					Brainstorm.pseudoseed(key .. '_resample' .. it .. seed_found))
			end
			if chosen == target_key then return true end
		end
		return false
	end
	local seq = Brainstorm.rollVoucherSequence(seed_found, max_ante)
	return seq[ante_mode] == target_key
end

-- Main-thread self-test (Ctrl+B). Predicts the CURRENT run's voucher sequence
-- from its seed and compares seq[current_ante] against the live
-- G.GAME.current_round.voucher. On a fresh ante-1 run where nothing was bought,
-- "Predicted A1" MUST equal "Live current_round.voucher" -- if it doesn't, the
-- core RNG replication is wrong (independent of the worker/apply path). Writes to
-- debug_predict.txt.
function Brainstorm.debugPredictVoucher()
	local seed = G.GAME and G.GAME.pseudorandom and G.GAME.pseudorandom.seed
	local lines = {}
	if not seed then
		lines[1] = "No active seed (start a run first)."
	else
		Brainstorm.random_state = { hashed_seed = pseudohash(seed) }
		local seq = Brainstorm.rollVoucherSequence(seed, 4)
		local ante = (G.GAME.round_resets and G.GAME.round_resets.ante) or "?"
		local live = (G.GAME.current_round and G.GAME.current_round.voucher) or "(none)"
		lines[#lines + 1] = "Seed: " .. tostring(seed)
		lines[#lines + 1] = "Current ante: " .. tostring(ante)
		lines[#lines + 1] = "Live current_round.voucher: " .. tostring(live)
		lines[#lines + 1] = "shop_vouchers.cards count: "
			.. tostring((G.shop_vouchers and G.shop_vouchers.cards and #G.shop_vouchers.cards) or 0)
		lines[#lines + 1] = ""
		for a = 1, 4 do
			local mark = (tostring(a) == tostring(ante)) and "   <-- current ante" or ""
			lines[#lines + 1] = "Predicted A" .. a .. ": " .. tostring(seq[a]) .. mark
		end
		if type(ante) == "number" and seq[ante] then
			lines[#lines + 1] = ""
			lines[#lines + 1] = "MATCH seq[ante]==live: " .. tostring(seq[ante] == live)
		end
		local avail = {}
		for k, v in ipairs(G.P_CENTER_POOLS['Voucher']) do
			if v.unlocked ~= false and not v.requires then avail[#avail + 1] = k .. ":" .. v.key end
		end
		lines[#lines + 1] = ""
		lines[#lines + 1] = "Pool size " .. #G.P_CENTER_POOLS['Voucher'] .. ", available " .. #avail
		lines[#lines + 1] = "Available: " .. table.concat(avail, ", ")
	end
	local nativefs = require("brainstorm_nativefs")
	nativefs.write(Brainstorm.modPath() .. "/debug_predict.txt", table.concat(lines, "\n"))
end

-- Main-thread self-test (Ctrl+P) for the physical pack model. Predicts the
-- CURRENT run's shops per ante from its seed and lists the live shop's packs
-- for comparison. Layout is shop-by-shop: ENTRY is the shop right after the
-- previous boss (ease_ante has already ticked, so it draws from THIS ante's
-- streams and shows this ante on the HUD); a skipped blind's shop never opens.
-- The printed layout uses the same skip assumption the seed search uses.
-- Compare each shop you enter against its bracket, in order; on the current
-- run's found seed they must match exactly. Writes to debug_predict.txt.
function Brainstorm.debugPredictPacks()
	local seed = G.GAME and G.GAME.pseudorandom and G.GAME.pseudorandom.seed
	local lines = {}
	if not seed then
		lines[1] = "No active seed (start a run first)."
	else
		Brainstorm.random_state = { hashed_seed = pseudohash(seed) }
		local sm, big = Brainstorm.installSkipAssumptionFresh(seed)
		local ante = (G.GAME.round_resets and G.GAME.round_resets.ante) or "?"
		lines[#lines + 1] = "Seed: " .. tostring(seed)
		lines[#lines + 1] = "Current ante: " .. tostring(ante)
		local live = {}
		if G.shop_booster and G.shop_booster.cards then
			for i, c in ipairs(G.shop_booster.cards) do
				live[#live + 1] = (c.config and c.config.center and c.config.center.key) or "?"
			end
		end
		lines[#lines + 1] = "Live shop packs: " .. (next(live) and table.concat(live, ", ") or "(none visible)")
		lines[#lines + 1] = ""
		lines[#lines + 1] = "(ENTRY = the shop right after the previous boss -- the ante has already"
		lines[#lines + 1] = " ticked up, so it belongs to THIS row. Slot 1 of the run's first shop is"
		lines[#lines + 1] = " the game's FORCED normal Buffoon. 'skip' = that blind's shop never opens;"
		lines[#lines + 1] = " assumed from your filters: tag/soul filters mean you skip for the reward.)"
		local skipNote = {}
		for a = 1, 8 do
			if sm and sm[a] then skipNote[#skipNote + 1] = "A" .. a .. " Small" end
			if big and big[a] then skipNote[#skipNote + 1] = "A" .. a .. " Big" end
		end
		lines[#lines + 1] = "Assumed skips: " .. (next(skipNote) and table.concat(skipNote, ", ") or "(none)")
		local maxA = (Brainstorm.SETTINGS.multiAnteSearch and Brainstorm.SETTINGS.multiAnteSearch.anywhereMode) and 8 or 4
		for a = 1, maxA do
			local packs = Brainstorm.getSimulatedPacks(seed, a, 6)
			local shopNames = {}
			if a >= 2 then shopNames[#shopNames + 1] = "ENTRY" end
			if not (sm and sm[a]) then shopNames[#shopNames + 1] = "SMALL" end
			if not (big and big[a]) then shopNames[#shopNames + 1] = "BIG" end
			local parts, s = {}, 1
			for _, name in ipairs(shopNames) do
				local two = {}
				for i = 1, 2 do
					local c = packs[s]
					two[i] = c and (c.forced and "FORCED-buffoon-normal" or c.key) or "?"
					s = s + 1
				end
				parts[#parts + 1] = name .. "[" .. table.concat(two, ", ") .. "]"
			end
			local skipped = {}
			if sm and sm[a] then skipped[#skipped + 1] = "SMALL skipped" end
			if big and big[a] then skipped[#skipped + 1] = "BIG skipped" end
			local note = next(skipped) and ("  (" .. table.concat(skipped, ", ") .. ")") or ""
			local mark = (tostring(a) == tostring(ante)) and "   <-- compare with live" or ""
			lines[#lines + 1] = "Predicted A" .. a .. ": " .. table.concat(parts, " ") .. note .. mark
		end
	end
	local nativefs = require("brainstorm_nativefs")
	nativefs.write(Brainstorm.modPath() .. "/debug_predict.txt", table.concat(lines, "\n"))
end

-- ===========================================================================
-- Diagnostics dump (Ctrl+D) + worker/main mismatch logger
-- ---------------------------------------------------------------------------
-- buildDiagnosticsText(seed) predicts every filter for `seed` on the MAIN thread
-- (trusted inline pools) and lays out: the exact settings that were enabled, the
-- per-filter prediction, the live game values where the seed matches the current
-- run, and the overall pass/fail. This is the file to send for troubleshooting a
-- "the filter didn't match my settings" report -- it shows both what you asked for
-- and what the search actually computed for that seed.
-- ===========================================================================
function Brainstorm.buildDiagnosticsText(seed)
	local ar = Brainstorm.SETTINGS.autoreroll or {}
	local ma = Brainstorm.SETTINGS.multiAnteSearch or {}
	local liveSeed = G.GAME and G.GAME.pseudorandom and G.GAME.pseudorandom.seed
	local sameRun = (seed == liveSeed)
	local L = {}
	local function add(s) L[#L + 1] = s end

	add("=== Brainstorm diagnostics ===")
	add("time: " .. os.date("%Y-%m-%d %H:%M:%S"))
	add("version: " .. tostring(Brainstorm.VER))
	add("seed: " .. tostring(seed))
	add("is current run's seed: " .. tostring(sameRun))
	if sameRun then
		add("current ante: " .. tostring(G.GAME.round_resets and G.GAME.round_resets.ante))
		add("stake: " .. tostring(G.GAME.stake))
	end

	add("")
	add("--- enabled filters (settings) ---")
	add("Tag: " .. (ar.searchTag ~= "" and tostring(ar.searchTag) or "(off)"))
	add("Pack: " .. ((ar.searchPack and #ar.searchPack > 0) and table.concat(ar.searchPack, ",") or "(off)"))
	add("Voucher: " .. (ar.searchVoucher ~= "" and tostring(ar.searchVoucher) or "(off)")
		.. "  ante=" .. tostring(ar.searchVoucherAnte == 0 and "Any(1-4)" or ar.searchVoucherAnte))
	add("Legendary: " .. (ar.searchLegendary ~= "" and tostring(ar.searchLegendary) or "(off)")
		.. "  negative=" .. tostring(ar.searchNegativeLegendary and true or false))
	add("Souls required: " .. tostring(ar.searchForSoul or 0))
	add("Joker match mode: " .. (ar.jokerSearchMatchAny and "ANY of the 3" or "ALL of the 3"))
	for i = 1, 3 do
		local s = ar.jokerSlotData and ar.jokerSlotData[i]
		if s and s.key and s.key ~= "" then
			add("Joker slot " .. i .. ": " .. s.key .. "  negative=" .. tostring(s.requireNegative and true or false))
		end
	end
	local anteBits = {}
	for a = 1, 4 do
		anteBits[#anteBits + 1] = "A" .. a .. "(slots=" .. tostring(ma["ante" .. a .. "Slots"] or 0)
			.. ",packs=" .. tostring(ma["ante" .. a .. "Packs"] and true or false) .. ")"
	end
	add("Multi-ante: " .. table.concat(anteBits, " "))
	add("Threads: " .. tostring(ar.searchThreads == 0 and "Auto" or ar.searchThreads)
		.. "  useSearchThread=" .. tostring(Brainstorm.SETTINGS.useSearchThread ~= false)
		.. "  useCulledCache=" .. tostring(Brainstorm.SETTINGS.useCulledCache ~= false))

	add("")
	add("--- predictions for this seed (trusted main-thread path) ---")

	-- Tag (culled model: Small rolls first, then Big, per ante)
	if ar.searchTag ~= "" then
		Brainstorm.random_state = { hashed_seed = pseudohash(seed) }
		for a = 1, 2 do
			local small = Brainstorm.rollTag(seed, a)
			local big = Brainstorm.rollTag(seed, a)
			add("Tag A" .. a .. ": predicted Small=" .. tostring(small) .. " Big=" .. tostring(big))
		end
		add("     want " .. tostring(ar.searchTag) .. "  anywhere=" .. tostring(ar.searchTagAnywhere and true or false))
		if sameRun and G.GAME.round_resets and G.GAME.round_resets.blind_tags then
			add("     live current-ante tags: Small=" .. tostring(G.GAME.round_resets.blind_tags.Small)
				.. " Big=" .. tostring(G.GAME.round_resets.blind_tags.Big))
		end
	end

	-- Voucher
	if ar.searchVoucher ~= "" then
		Brainstorm.random_state = { hashed_seed = pseudohash(seed) }
		local seq = Brainstorm.rollVoucherSequence(seed, 4)
		add("Voucher: predicted A1=" .. tostring(seq[1]) .. " A2=" .. tostring(seq[2])
			.. " A3=" .. tostring(seq[3]) .. " A4=" .. tostring(seq[4]))
		add("     want " .. tostring(ar.searchVoucher) .. " at ante " .. tostring(ar.searchVoucherAnte))
		if sameRun and G.GAME.current_round then
			add("     live current_round.voucher: " .. tostring(G.GAME.current_round.voucher))
		end
	end

	-- Jokers: per-ante shop prediction + overall find result
	local hasJoker = false
	for i = 1, 3 do
		local s = ar.jokerSlotData and ar.jokerSlotData[i]
		if s and s.key and s.key ~= "" then hasJoker = true end
	end
	if hasJoker then
		local function predictShop(ante, num_slots)
			Brainstorm.random_state = { hashed_seed = pseudohash(seed) }
			local total = 28 -- SHOP_RATES_ANTE1: joker 20 + tarot 4 + planet 4
			local rname = { "Common", "Uncommon", "Rare" }
			for slot = 1, num_slots do
				local ctr = pseudorandom(Brainstorm.pseudoseed("cdt" .. ante .. seed)) * total
				if ctr < 20 then
					local rr = pseudorandom(Brainstorm.pseudoseed("rarity" .. ante .. "sho" .. seed))
					local rarity = rr > 0.95 and 3 or rr > 0.7 and 2 or 1
					local rkey = "Joker" .. rarity .. "sho" .. ante
					local pool = Brainstorm.getJokerCulledPool(rarity)
					local chosen = pseudorandom_element(pool, Brainstorm.pseudoseed(rkey .. seed))
					local it = 1
					while chosen == 'UNAVAILABLE' do
						it = it + 1
						chosen = pseudorandom_element(pool, Brainstorm.pseudoseed(rkey .. '_resample' .. it .. seed))
					end
					add("     A" .. ante .. " shop slot " .. slot .. ": " .. rname[rarity] .. " -> " .. chosen)
				else
					add("     A" .. ante .. " shop slot " .. slot .. ": (non-joker)")
				end
			end
		end
		add("Jokers: shop predictions for active antes --")
		for ante = 1, 4 do
			local n = ma["ante" .. ante .. "Slots"] or 0
			if n > 0 then predictShop(ante, n) end
		end
		Brainstorm.random_state = { hashed_seed = pseudohash(seed) }
		Brainstorm.installSkipAssumptionFresh(seed)
		local found = Brainstorm.checkMultiAnteJokerSearch(seed)
		add("     multi-ante joker result: pass=" .. tostring(found)
			.. "  foundAt=" .. tostring(Brainstorm.AUTOREROLL.jokerFoundAt or "(none)"))
	end

	-- Legendary / soul
	if ar.searchLegendary ~= "" or (ar.searchForSoul or 0) > 0 then
		Brainstorm.random_state = { hashed_seed = pseudohash(seed) }
		local soul = false
		local charmPack = Brainstorm.tagSoulRewardCenter("tag_charm")
		for j = 1, charmPack and Brainstorm.packCardCount(charmPack) or 0 do
			if pseudorandom(Brainstorm.pseudoseed("soul_Tarot1" .. seed)) > 0.997 then soul = true end
		end
		add("Soul present in Arcana pack: " .. tostring(soul))
		if ar.searchLegendary ~= "" then
			Brainstorm.random_state = { hashed_seed = pseudohash(seed) }
			local filtered = Brainstorm.getJokerCulledPool(4)
			local chosen = pseudorandom_element(filtered, Brainstorm.pseudoseed("Joker4" .. seed))
			local it = 1
			while chosen == 'UNAVAILABLE' do
				it = it + 1
				chosen = pseudorandom_element(filtered, Brainstorm.pseudoseed("Joker4" .. '_resample' .. it .. seed))
			end
			add("Legendary: predicted " .. tostring(chosen) .. " | want " .. tostring(ar.searchLegendary)
				.. " | match " .. tostring(chosen == ar.searchLegendary))
		end
		if ar.searchLegendaryAnywhere and ar.searchLegendary ~= "" then
			Brainstorm.random_state = { hashed_seed = pseudohash(seed) }
			Brainstorm.installSkipAssumptionFresh(seed)
			local ok, loc = Brainstorm.checkLegendaryAnywhere(seed, ar.searchLegendary, ar.searchNegativeLegendary)
			add("Legendary ANYWHERE: pass=" .. tostring(ok) .. "  at=" .. tostring(loc or "(none)"))
		end
	end

	add("")
	Brainstorm.random_state = { hashed_seed = pseudohash(seed) }
	add("OVERALL passesAllFilters(seed) = " .. tostring(Brainstorm.passesAllFilters(seed)))
	return table.concat(L, "\n")
end

-- Ctrl+D: dump diagnostics for the seed of the run you're currently in.
function Brainstorm.dumpDiagnostics()
	local seed = G.GAME and G.GAME.pseudorandom and G.GAME.pseudorandom.seed
	local text = seed and Brainstorm.buildDiagnosticsText(seed) or "No active run/seed."
	local nativefs = require("brainstorm_nativefs")
	nativefs.write(Brainstorm.modPath() .. "/brainstorm_diagnostics.txt", text)
end

-- Safety-rail logger: a worker hit failed main-thread re-verification. Records the
-- worker's claim + the trusted main-thread breakdown so the divergence can be
-- diagnosed. Appends (timestamped) so repeated mismatches accumulate.
function Brainstorm.logSeedMismatch(res)
	local seed = res and res.seed or "(nil)"
	local parts = {
		"### WORKER/MAIN MISMATCH @ " .. os.date("%Y-%m-%d %H:%M:%S"),
		"worker claimed: seed=" .. tostring(seed) .. " jokerFoundAt=" .. tostring(res and res.jokerFoundAt),
		Brainstorm.buildDiagnosticsText(seed),
		"",
	}
	local nativefs = require("brainstorm_nativefs")
	local path = Brainstorm.modPath() .. "/brainstorm_mismatch.txt"
	local prior = nativefs.read(path) or ""
	nativefs.write(path, prior .. table.concat(parts, "\n") .. "\n")
	print("[Brainstorm] SEED MISMATCH logged for " .. tostring(seed) .. " -> brainstorm_mismatch.txt")
end

function Brainstorm.searchParametersMet()
	--note: this appears to be deprecated, so I didn't update it
	if not G or not G.GAME or not G.GAME.round_resets or not G.GAME.round_resets.blind_tags then
		print("One or more variables are nil or undefined")
		return false
	end

	local _tag = G.GAME.round_resets.blind_tags.Small
	if not _tag then
		print("Value of _tag is nil or undefined")
		return false
	end

	if _tag == Brainstorm.SETTINGS.autoreroll.searchTag then
		if Brainstorm.SETTINGS.autoreroll.searchForSoul then
			return true
		end
		-- Check if arcana pack from skip has The Soul
		Brainstorm.random_state = copy_table(G.GAME.pseudorandom)
		for i = 1, 5 do
			if pseudorandom(Brainstorm.pseudoseed("soul_Tarot1")) > 0.997 then
				return true
			end
		end
		return false
	else
		return false
	end
end

function wait(seconds)
	local start = os.clock()
	while os.clock() - start < seconds do
		-- Busy wait
	end
end

-- Bit-identical, string-free replacement for the game's per-advance
-- math.abs(tonumber(string.format("%.13f", x))) round-trip. x is always in
-- [0,1) here (it comes out of `% 1`), and that sprintf+strtod pair is the
-- hottest single cost in pseudoseed -- it runs on EVERY RNG advance of every
-- candidate seed. Rounding x to 13 decimal places has an all-arithmetic
-- answer whenever frac(x*1e13) is clearly on one side of 0.5: both n and 1e13
-- are exact doubles, and IEEE division rounds correctly, so n/1e13 is the
-- same double strtod produces for the 13-digit decimal the string path would
-- print. fl(x*1e13) carries at most ~0.001 absolute error (half an ulp at
-- 2^44), so only the band |frac - 0.5| <= 0.0015 is ambiguous; those rare
-- calls (~0.3%) defer to the original string round-trip, keeping every result
-- bit-identical (fuzz-verified against the string path by
-- tests/search_equivalence.lua).
local math_floor, string_format = math.floor, string.format
local function round13(x)
	local q = x * 1e13
	local n = math_floor(q)
	local f = q - n
	if f > 0.5015 then
		n = n + 1
	elseif f > 0.4985 then
		return math.abs(tonumber(string_format("%.13f", x)))
	end
	return n / 1e13
end
Brainstorm.round13 = round13 -- exported for the test harness's fuzz check

function Brainstorm.pseudoseed(key, predict_seed)
	if key == "seed" then
		return math.random()
	end

	if predict_seed then
		local _pseed = pseudohash(key .. (predict_seed or ""))
		_pseed = round13((2.134453429141 + _pseed * 1.72431234) % 1)
		return (_pseed + (pseudohash(predict_seed) or 0)) / 2
	end

	if not Brainstorm.random_state[key] then
		Brainstorm.random_state[key] = pseudohash(key .. (Brainstorm.random_state.seed or ""))
	end

	Brainstorm.random_state[key] = round13((2.134453429141 + Brainstorm.random_state[key] * 1.72431234) % 1)
	return (Brainstorm.random_state[key] + (Brainstorm.random_state.hashed_seed or 0)) / 2
end

--Used for reroll UI
--Based on Balatro's attention_text
function Brainstorm.attention_text(args)
    args = args or {}
    args.text = args.text or 'test'
    args.scale = args.scale or 1
    args.colour = copy_table(args.colour or G.C.WHITE)
    args.hold = (args.hold or 0) + 0.1*(G.SPEEDFACTOR)
    args.pos = args.pos or {x = 0, y = 0}
    args.align = args.align or 'cm'
    args.emboss = args.emboss or nil

    args.fade = 1

    if args.cover then
      args.cover_colour = copy_table(args.cover_colour or G.C.RED)
      args.cover_colour_l = copy_table(lighten(args.cover_colour, 0.2))
      args.cover_colour_d = copy_table(darken(args.cover_colour, 0.2))
    else
      args.cover_colour = copy_table(G.C.CLEAR)
    end

    args.uibox_config = {
      align = args.align or 'cm',
      offset = args.offset or {x=0,y=0}, 
      major = args.cover or args.major or nil,
    }

    G.E_MANAGER:add_event(Event({
      trigger = 'after',
      delay = 0,
      blockable = false,
      blocking = false,
      func = function()
          args.AT = UIBox{
            T = {args.pos.x,args.pos.y,0,0},
            definition = 
              {n=G.UIT.ROOT, config = {align = args.cover_align or 'cm', minw = (args.cover and args.cover.T.w or 0.001) + (args.cover_padding or 0), minh = (args.cover and args.cover.T.h or 0.001) + (args.cover_padding or 0), padding = 0.03, r = 0.1, emboss = args.emboss, colour = args.cover_colour}, nodes={
                {n=G.UIT.O, config={draw_layer = 1, object = DynaText({scale = args.scale, string = args.text, maxw = args.maxw, colours = {args.colour},float = true, shadow = true, silent = not args.noisy, args.scale, pop_in = 0, pop_in_rate = 6, rotate = args.rotate or nil})}},
              }}, 
            config = args.uibox_config
          }
          args.AT.attention_text = true

          args.text = args.AT.UIRoot.children[1].config.object
          args.text:pulse(0.5)

          if args.cover then
            Particles(args.pos.x,args.pos.y, 0,0, {
              timer_type = 'TOTAL',
              timer = 0.01,
              pulse_max = 15,
              max = 0,
              scale = 0.3,
              vel_variation = 0.2,
              padding = 0.1,
              fill=true,
              lifespan = 0.5,
              speed = 2.5,
              attach = args.AT.UIRoot,
              colours = {args.cover_colour, args.cover_colour_l, args.cover_colour_d},
          })
          end
          if args.backdrop_colour then
            args.backdrop_colour = copy_table(args.backdrop_colour)
            Particles(args.pos.x,args.pos.y,0,0,{
              -- Defaults preserve the original persistent glow. Callers can pass a
              -- 'REAL' timer_type + short timer/lifespan for a quick one-shot flash
              -- that isn't stretched/compressed by the game-speed multiplier.
              timer_type = args.backdrop_timer_type or 'TOTAL',
              timer = args.backdrop_timer or 5,
              scale = 2.4*(args.backdrop_scale or 1),
              lifespan = args.backdrop_lifespan or 5,
              speed = 0,
              attach = args.AT,
              colours = {args.backdrop_colour}
            })
          end
          return true
      end
      }))
      return args
end

function Brainstorm.remove_attention_text(args)
    G.E_MANAGER:add_event(Event({
        trigger = 'after',
        delay = 0,
        blockable = false,
        blocking = false,
        func = function()
          if not args.start_time then
            args.start_time = G.TIMERS.TOTAL
            args.text:pop_out(3)
          else
            --args.AT:align_to_attach()
            args.fade = math.max(0, 1 - 3*(G.TIMERS.TOTAL - args.start_time))
            if args.cover_colour then args.cover_colour[4] = math.min(args.cover_colour[4], 2*args.fade) end
            if args.cover_colour_l then args.cover_colour_l[4] = math.min(args.cover_colour_l[4], args.fade) end
            if args.cover_colour_d then args.cover_colour_d[4] = math.min(args.cover_colour_d[4], args.fade) end
            if args.backdrop_colour then args.backdrop_colour[4] = math.min(args.backdrop_colour[4], args.fade) end
            args.colour[4] = math.min(args.colour[4], args.fade)
            if args.fade <= 0 then
              args.AT:remove()
              return true
            end
          end
        end
      }))
end

function Brainstorm.debugPredictShop(seed_found, ante, num_slots)
	Brainstorm.random_state = { hashed_seed = pseudohash(seed_found) }
	local r = { joker = 20, tarot = 4, planet = 4, playing_card = 0, spectral = 0 }
	local total = r.joker + r.tarot + r.planet + r.playing_card + r.spectral
	local lines = { "Seed: " .. seed_found, "Ante: " .. ante }

	for slot = 1, num_slots do
		local card_type_roll = pseudorandom(Brainstorm.pseudoseed("cdt" .. ante .. seed_found)) * total
		if card_type_roll < r.joker then
			local rarity_roll = pseudorandom(Brainstorm.pseudoseed("rarity" .. ante .. "sho" .. seed_found))
			local rarity, rname
			if rarity_roll > 0.95 then
				rarity, rname = 3, "Rare"
			elseif rarity_roll > 0.7 then
				rarity, rname = 2, "Uncommon"
			else
				rarity, rname = 1, "Common"
			end

			local rkey = "Joker" .. rarity .. "sho" .. ante

			-- Preserve original index positions; ineligible jokers become 'UNAVAILABLE'
			-- placeholders instead of being removed from the array
			local source_pool = G.P_JOKER_RARITY_POOLS[rarity]
			local pool = {}
			for k, v in ipairs(source_pool) do
				local eligible
				if v.enhancement_gate then
					eligible = false
					if G.playing_cards then
						for kk, vv in pairs(G.playing_cards) do
							if vv.config.center.key == v.enhancement_gate then
								eligible = true
								break
							end
						end
					end
				else
					eligible = not (G.GAME.used_jokers[v.key] and not next(find_joker("Showman")))
						and (v.unlocked ~= false or v.rarity == 4)
				end
				if v.no_pool_flag and G.GAME.pool_flags[v.no_pool_flag] then eligible = false end
				if v.yes_pool_flag and not G.GAME.pool_flags[v.yes_pool_flag] then eligible = false end
				pool[k] = (eligible and not G.GAME.banned_keys[v.key]) and v.key or 'UNAVAILABLE'
			end

			local chosen_key = pseudorandom_element(pool, Brainstorm.pseudoseed(rkey .. seed_found))
			local it = 1
			while chosen_key == 'UNAVAILABLE' do
				it = it + 1
				chosen_key = pseudorandom_element(pool, Brainstorm.pseudoseed(rkey .. '_resample' .. it .. seed_found))
			end
			lines[#lines+1] = "Slot " .. slot .. ": JOKER (" .. rname .. ") -> " .. chosen_key
		else
			lines[#lines+1] = "Slot " .. slot .. ": non-joker (tarot/planet/etc)"
		end
	end

	local nativefs = require("brainstorm_nativefs")
	nativefs.write(Brainstorm.modPath() .. "/debug_predict.txt", table.concat(lines, "\n"))
end

function Brainstorm.joker_is_pool_eligible(v)
	local add
	if v.enhancement_gate then
		add = false -- a freshly started run never has enhanced cards yet
	else
		add = (v.unlocked ~= false or v.rarity == 4)
	end
	if v.no_pool_flag and G.GAME.pool_flags[v.no_pool_flag] then add = false end
	if v.yes_pool_flag and not G.GAME.pool_flags[v.yes_pool_flag] then add = false end
	return add and not G.GAME.banned_keys[v.key]
end

-- ===========================================================================
-- Precomputed culled pools (hot-path optimization + its safety rails)
-- ---------------------------------------------------------------------------
-- The hot joker/voucher filters rebuild an index-preserving, 'UNAVAILABLE'-culled
-- pool array for every slot of every seed. Eligibility is static for a fresh run
-- (banned_keys / pool_flags are fixed for the whole search), so that array is
-- identical every time -- we build it ONCE and reuse it read-only.
--
-- SAFETY RAILS:
--  1. Fallback: the getters build the pool inline whenever the cache is absent,
--     so a missing/disabled cache degrades to the exact original code, never to a
--     wrong answer.
--  2. Worker-only: buildCulledPools() runs only in the worker's fresh Lua state
--     (rebuilt per search from an immutable snapshot), so there is no cross-search
--     staleness. The main thread leaves Brainstorm.CULLED nil, so its
--     passesAllFilters is the untouched inline path -- which is exactly the
--     trusted reference every worker hit is re-verified against (see
--     updateAutoReroll). A cache bug can only ever cost throughput, not correctness.
--  3. Kill switch: Brainstorm.SETTINGS.useCulledCache = false disables it entirely.
--  4. Read-only: callers only pick/resample from the returned array; the voucher
--     per-ante blanking copy-on-writes, so the cache is never mutated.
-- ===========================================================================
function Brainstorm.buildCulledPools()
	if Brainstorm.SETTINGS.useCulledCache == false then Brainstorm.CULLED = nil; return end
	local c = { joker = {} }
	for r = 1, 4 do
		local src = G.P_JOKER_RARITY_POOLS[r]
		if src then
			local arr = {}
			for k, v in ipairs(src) do
				arr[k] = Brainstorm.joker_is_pool_eligible(v) and v.key or 'UNAVAILABLE'
			end
			c.joker[r] = arr
		end
	end
	local vb = {}
	for k, v in ipairs(G.P_CENTER_POOLS['Voucher']) do
		local eligible = v.unlocked ~= false and not v.requires
		vb[k] = (eligible and not (G.GAME.banned_keys and G.GAME.banned_keys[v.key])) and v.key or 'UNAVAILABLE'
	end
	c.voucher = vb
	-- Booster weights are static for a whole search; sum them once so
	-- getSimulatedPacks stops re-walking the pool per candidate seed. Same
	-- ipairs order as its inline fallback => bit-identical float sum.
	local cume = 0
	for _, v in ipairs(G.P_CENTER_POOLS['Booster']) do
		if not (G.GAME.banned_keys and G.GAME.banned_keys[v.key]) then
			cume = cume + (v.weight or 1)
		end
	end
	c.boosterCume = cume
	-- Per-ante culled tag arrays (min_ante makes eligibility ante-dependent).
	c.tag = {}
	for a = 1, 8 do
		local arr = {}
		for k, v in ipairs(G.P_CENTER_POOLS['Tag']) do
			arr[k] = Brainstorm.tag_is_eligible(v, a) and v.key or 'UNAVAILABLE'
		end
		c.tag[a] = arr
	end
	Brainstorm.CULLED = c
end

function Brainstorm.getJokerCulledPool(rarity)
	local c = Brainstorm.CULLED
	if c and c.joker and c.joker[rarity] then return c.joker[rarity] end
	local arr = {}
	for k, v in ipairs(G.P_JOKER_RARITY_POOLS[rarity]) do
		arr[k] = Brainstorm.joker_is_pool_eligible(v) and v.key or 'UNAVAILABLE'
	end
	return arr
end

function Brainstorm.getVoucherCulledPool()
	local c = Brainstorm.CULLED
	if c and c.voucher then return c.voucher end
	local base = {}
	for k, v in ipairs(G.P_CENTER_POOLS['Voucher']) do
		local eligible = v.unlocked ~= false and not v.requires
		base[k] = (eligible and not (G.GAME.banned_keys and G.GAME.banned_keys[v.key])) and v.key or 'UNAVAILABLE'
	end
	return base
end

-- Tag eligibility, verbatim from get_current_pool's 'Tag' branch: requires-
-- center must be DISCOVERED (profile collection state; the snapshot resolves
-- it into requiresOk for the worker) and min_ante <= ante (tag_negative has
-- min_ante = 2, so it is UNAVAILABLE at ante 1).
function Brainstorm.tag_is_eligible(v, ante)
	if v.requiresOk ~= nil then
		if not v.requiresOk then return false end
	elseif v.requires and not (G.P_CENTERS and G.P_CENTERS[v.requires] and G.P_CENTERS[v.requires].discovered) then
		return false
	end
	if v.min_ante and v.min_ante > ante then return false end
	if v.no_pool_flag and G.GAME.pool_flags[v.no_pool_flag] then return false end
	if v.yes_pool_flag and not G.GAME.pool_flags[v.yes_pool_flag] then return false end
	return not (G.GAME.banned_keys and G.GAME.banned_keys[v.key])
end

function Brainstorm.getTagCulledPool(ante)
	local c = Brainstorm.CULLED
	if c and c.tag and c.tag[ante] then return c.tag[ante] end
	local arr = {}
	for k, v in ipairs(G.P_CENTER_POOLS['Tag']) do
		arr[k] = Brainstorm.tag_is_eligible(v, ante) and v.key or 'UNAVAILABLE'
	end
	return arr
end

-- Source-verified tag roll (get_next_tag_key): index pick from the culled
-- string array (fast path -- the game's tag pool is a string array too), with
-- 'Tag'..ante..'_resample'..it on UNAVAILABLE. One call = one blind's tag;
-- Small rolls before Big within an ante (game.lua reset_blinds).
function Brainstorm.rollTag(seed_found, ante)
	local pool = Brainstorm.getTagCulledPool(ante)
	local key = 'Tag' .. ante
	local t = pseudorandom_element(pool, Brainstorm.pseudoseed(key .. seed_found))
	local it = 1
	while t == 'UNAVAILABLE' do
		it = it + 1
		t = pseudorandom_element(pool, Brainstorm.pseudoseed(key .. '_resample' .. it .. seed_found))
	end
	return t
end

-- Cards created when a pack opens = center.config.extra (Card:open uses
-- self.ability.extra). The snapshot carries it as .cards; the name fallback
-- is source-corrected (mega/jumbo Buffoon and Spectral are 4, NOT 6).
function Brainstorm.packCardCount(center)
	return center.cards or (center.config and center.config.extra)
		or (center.key:find("mega") and 4 or center.key:find("jumbo") and 4 or 2)
end

-- ===========================================================================
-- Legendary ANYWHERE (antes 1-8): the run's FIRST Soul, source-verified.
-- ---------------------------------------------------------------------------
-- Per ante, scan opened pack events in chronological order: the post-boss
-- entry shop at antes 2+, then each played Small/Big shop or the immediate
-- reward from a collected Charm/Ethereal tag. Each opened shop has two pack
-- offers and the run's first shop leads with the forced Buffoon. Opening an
-- Arcana pack rolls
-- 'soul_Tarot'..ante once per card (config.extra cards); a Spectral pack
-- rolls 'soul_Spectral'..ante TWICE per card -- soul roll then black-hole
-- roll -- and a black-hole hit OVERWRITES a soul hit on the same card
-- (create_card sets forced_key twice). After a black hole exists, later
-- spectral cards skip the second roll (used_jokers gate). The first Soul's
-- legendary is the FIRST 'Joker4' advance (no ante appended -- source:
-- get_current_pool appends the ante to every pool key EXCEPT legendary), and
-- its edition rolls 'edisou'..ante (create_card key_append 'sou').
-- CONVENTION for collecting the find: follow the assumed play/skip route,
-- opening eligible packs in event order. A P<n> label is a flattened shop
-- offer; CharmSm/Big or EtherealSm/Big is the immediate reward at that blind.
-- Use the first Soul encountered on that route.
-- ===========================================================================
function Brainstorm.checkLegendaryAnywhere(seed_found, target_key, require_negative,
		voucherRoute)
	if G.GAME.banned_keys and G.GAME.banned_keys.c_soul then return false end
	for ante = 1, 8 do
		local packs = Brainstorm.getSoulPackEvents(seed_found, ante)
		if not packs then return false end
		for slot = 1, #packs do
			local packEvent = packs[slot]
			local center = packEvent.center
			local kind = center and not center.forced and center.kind or nil
			if kind == 'Arcana' or kind == 'Spectral' then
				local ncards = Brainstorm.packCardCount(center)
				local soul_in_pack, bh_in_pack = false, false
				for card = 1, ncards do
					local contentKind = kind
					if kind == 'Arcana'
							and Brainstorm.omenOwnedForSoulEvent(voucherRoute, packEvent)
							and pseudorandom(Brainstorm.pseudoseed(
								'omen_globe' .. seed_found)) > 0.8 then
						contentKind = 'Spectral'
					end
					local stype = (contentKind == 'Arcana') and 'Tarot' or 'Spectral'
					local soul = false
					if not soul_in_pack then
						soul = pseudorandom(Brainstorm.pseudoseed('soul_' .. stype .. ante .. seed_found)) > 0.997
					end
					if contentKind == 'Spectral' and not bh_in_pack then
						local bh = pseudorandom(Brainstorm.pseudoseed('soul_' .. stype .. ante .. seed_found)) > 0.997
						if bh then
							if not (G.GAME.banned_keys and G.GAME.banned_keys.c_black_hole) then
								bh_in_pack = true
							end
							soul = false -- black hole overwrites the soul on this card
						end
					end
					if soul then
						soul_in_pack = true
						local filtered = Brainstorm.getJokerCulledPool(4)
						local chosen = pseudorandom_element(filtered, Brainstorm.pseudoseed("Joker4" .. seed_found))
						local it = 1
						while chosen == 'UNAVAILABLE' do
							it = it + 1
							chosen = pseudorandom_element(filtered, Brainstorm.pseudoseed("Joker4" .. '_resample' .. it .. seed_found))
						end
						if chosen ~= target_key then return false end
						if require_negative then
							if pseudorandom(Brainstorm.pseudoseed("edisou" .. ante .. seed_found)) <= 0.997 then
								return false
							end
						end
						return true, "LegA" .. ante .. packEvent.location
					end
				end
			end
		end
	end
	return false
end

local SHOP_RATES_ANTE1 = { joker = 20, tarot = 4, planet = 4, playing_card = 0, spectral = 0 }

function Brainstorm.checkShopJokerSearch(seed_found, ante, num_slots, target_key, require_negative)
	local r = SHOP_RATES_ANTE1
	local total = r.joker + r.tarot + r.planet + r.playing_card + r.spectral

	for slot = 1, num_slots do
		local card_type_roll = pseudorandom(Brainstorm.pseudoseed("cdt" .. ante .. seed_found)) * total
		if card_type_roll < r.joker then
			local rarity_roll = pseudorandom(Brainstorm.pseudoseed("rarity" .. ante .. "sho" .. seed_found))
			local rarity
			if rarity_roll > 0.95 then rarity = 3
			elseif rarity_roll > 0.7 then rarity = 2
			else rarity = 1
			end

			local rkey = "Joker" .. rarity .. "sho" .. ante
			local pool = Brainstorm.getJokerCulledPool(rarity)

			local chosen_key = pseudorandom_element(pool, Brainstorm.pseudoseed(rkey .. seed_found))
			local it = 1
			while chosen_key == 'UNAVAILABLE' do
				it = it + 1
				chosen_key = pseudorandom_element(pool, Brainstorm.pseudoseed(rkey .. '_resample' .. it .. seed_found))
			end

			if chosen_key == target_key then
				if require_negative then
					pseudorandom(Brainstorm.pseudoseed("etperpoll" .. ante .. seed_found))
					local edition_poll = pseudorandom(Brainstorm.pseudoseed("edisho" .. ante .. seed_found))
					return edition_poll > 0.997
				end
				return true
			elseif require_negative then
				-- Advance etperpoll and edisho chains past this non-target joker slot
				pseudorandom(Brainstorm.pseudoseed("etperpoll" .. ante .. seed_found))
				pseudorandom(Brainstorm.pseudoseed("edisho" .. ante .. seed_found))
			end
		elseif require_negative then
			-- Non-joker slot: advance etperpoll only (non-jokers don't use the edisho key)
			pseudorandom(Brainstorm.pseudoseed("etperpoll" .. ante .. seed_found))
		end
	end
	return false
end
function Brainstorm.checkAntePacksForJoker(seed_found, ante, target_key, require_negative)
	local cume = 0
	for k, v in ipairs(G.P_CENTER_POOLS['Booster']) do
		cume = cume + (v.weight or 1)
	end
	for slot = 1, 2 do
		local it, center = 0, nil
		local poll = pseudorandom(Brainstorm.pseudoseed("shop_pack" .. slot .. ante .. seed_found)) * cume
		for k, v in ipairs(G.P_CENTER_POOLS['Booster']) do
			it = it + (v.weight or 1)
			if it >= poll and it - (v.weight or 1) <= poll then center = v; break end
		end
		if center and center.kind == 'Buffoon' then
			local num_cards = center.key:find("mega") and 6 or center.key:find("jumbo") and 4 or 2
			for card = 1, num_cards do
				local rarity_roll = pseudorandom(Brainstorm.pseudoseed("rarity" .. ante .. "buf" .. seed_found))
				local rarity = rarity_roll > 0.95 and 3 or rarity_roll > 0.7 and 2 or 1
				local rkey = "Joker" .. rarity .. "buf" .. ante
				local pool = Brainstorm.getJokerCulledPool(rarity)
				local chosen = pseudorandom_element(pool, Brainstorm.pseudoseed(rkey .. seed_found))
				local it2 = 1
				while chosen == 'UNAVAILABLE' do
					it2 = it2 + 1
					chosen = pseudorandom_element(pool, Brainstorm.pseudoseed(rkey .. '_resample' .. it2 .. seed_found))
				end
				if chosen == target_key then
					if require_negative then
						pseudorandom(Brainstorm.pseudoseed("packetper" .. ante .. seed_found))
						local edition_poll = pseudorandom(Brainstorm.pseudoseed("edibuf" .. ante .. seed_found))
						return edition_poll > 0.997
					end
					return true
				elseif require_negative then
					-- Advance packetper and edibuf chains past this non-target pack card
					pseudorandom(Brainstorm.pseudoseed("packetper" .. ante .. seed_found))
					pseudorandom(Brainstorm.pseudoseed("edibuf" .. ante .. seed_found))
				end
			end
		end
	end
	return false
end
-- key -> rarity (1-3) map, memoized once (pools are static for a search in both
-- the main thread and each worker's fresh state).
function Brainstorm.getJokerRarity(key)
	local m = Brainstorm._jokerRarityByKey
	if not m then
		m = {}
		for r = 1, 3 do
			local pool = G.P_JOKER_RARITY_POOLS[r]
			if pool then
				for _, v in ipairs(pool) do m[v.key] = r end
			end
		end
		Brainstorm._jokerRarityByKey = m
	end
	return m[key]
end

-- ===========================================================================
-- Single-pass ante simulation
-- ---------------------------------------------------------------------------
-- The old matcher re-walked the shop PER TARGET, but Brainstorm.random_state
-- persists across the walk -- so target 2's walk continued the cdt/rarity
-- streams where target 1 left off and effectively checked shop slots 7-12
-- instead of 1-6. These functions roll each ante's sequence ONCE and return it
-- as data; all targets are then string-matched against the same (correct)
-- sequence. Also cheaper: 3 targets cost one walk instead of three.
--
-- wanted = set {rarity=true}: pool picks for rarities outside the target set
-- are skipped -- each rarity has its own pick stream that nothing else reads,
-- so skipping cannot desync anything (~70% of slots roll Common; skipping them
-- removes most of the pick cost when hunting uncommons/rares).
-- needNeg: roll the edisho/edibuf edition stream per joker (its Nth advance is
-- the Nth joker of that ante/source, so it must advance for every joker even
-- when the pick was skipped). The old code also advanced etperpoll; per the
-- real source (common_events.lua:2138) that stream only feeds eternal/perish
-- flags that no filter reads, and streams are independent -- dropped.
-- ===========================================================================
function Brainstorm.simulateShopJokers(seed_found, ante, num_slots, wanted, needNeg)
	local r = SHOP_RATES_ANTE1
	local total = r.joker + r.tarot + r.planet + r.playing_card + r.spectral
	local out = {}
	for slot = 1, num_slots do
		local card_type_roll = pseudorandom(Brainstorm.pseudoseed("cdt" .. ante .. seed_found)) * total
		if card_type_roll < r.joker then
			local rarity_roll = pseudorandom(Brainstorm.pseudoseed("rarity" .. ante .. "sho" .. seed_found))
			local rarity = rarity_roll > 0.95 and 3 or rarity_roll > 0.7 and 2 or 1
			local chosen = nil
			if wanted[rarity] then
				local rkey = "Joker" .. rarity .. "sho" .. ante
				local pool = Brainstorm.getJokerCulledPool(rarity)
				chosen = pseudorandom_element(pool, Brainstorm.pseudoseed(rkey .. seed_found))
				local it = 1
				while chosen == 'UNAVAILABLE' do
					it = it + 1
					chosen = pseudorandom_element(pool, Brainstorm.pseudoseed(rkey .. '_resample' .. it .. seed_found))
				end
			end
			local neg = false
			if needNeg then
				neg = pseudorandom(Brainstorm.pseudoseed("edisho" .. ante .. seed_found)) > 0.997
			end
			out[#out + 1] = { key = chosen, neg = neg, rarity = rarity }
		end
	end
	return out
end

-- The run's forced first pack (source: get_pack's first_shop_buffoon branch).
-- Its variant (normal_1 vs _2) comes from raw math.random, so it's matched by
-- kind, never by key. It consumes NO 'shop_pack' advance and rides with the
-- run's FIRST OPENED shop, whichever ante that lands on.
Brainstorm.FORCED_BUFFOON = { key = "p_buffoon_normal_?", kind = "Buffoon", forced = true, cards = 2 }

-- Soul-capable packs opened immediately by collected tags. The tag code forces
-- these centers directly (Charm's random _1/_2 choice changes only artwork), so
-- a challenge ban that removes one from the random shop pool does not remove the
-- reward. Return the actual snapshotted center so modified card counts remain
-- part of the model instead of being hard-coded here.
function Brainstorm.tagSoulRewardKey(tagKey)
	if tagKey == "tag_charm" then return "p_arcana_mega_1" end
	if tagKey == "tag_ethereal" then return "p_spectral_normal_1" end
	return nil
end

function Brainstorm.tagSoulRewardCenter(tagKey)
	local packKey = Brainstorm.tagSoulRewardKey(tagKey)
	if not packKey then return nil end
	for _, center in ipairs((G.P_CENTER_POOLS and G.P_CENTER_POOLS.Booster) or {}) do
		if center.key == packKey then return center end
	end
	return nil
end

-- Blind-skip assumption implied by the active filters. A skipped blind's shop
-- never opens (source: skip_blind just advances blind_on_deck -- no round, no
-- shop state), so its two get_pack picks are never drawn. Assumed skips:
--   * classic soul/legendary filter: the charm-tag convention skips ante-1
--     Small for the mega Arcana;
--   * the tag filter: you skip the matched blind to take the tag (classic =
--     ante-1 Small; anywhere = this seed's match, from its TagA<n>Sm|Big).
-- Everything else is assumed played -- unfiltered skips are on the player.
-- Pure given settings + tagLoc (no RNG). Returns skipSm, skipBig arrays.
function Brainstorm.mergeSkipRoutes(a, b)
	if not a then return b end
	if not b then return a end
	local out = {}
	for ante = 1, 39 do out[ante] = a[ante] or b[ante] or nil end
	return out
end

function Brainstorm.mergeRewardRoutes(a, b)
	if not a then return b end
	if not b then return a end
	local out = {}
	for ante = 1, 39 do out[ante] = a[ante] or b[ante] or nil end
	return out
end

local POOL_PHASE_ORDER = { small = 0, big = 1, boss = 2 }

function Brainstorm.poolRoutePosition(ante, phase)
	local order = POOL_PHASE_ORDER[phase]
	if not ante or not order then return nil end
	return (ante - 1) * 3 + order
end

function Brainstorm.poolLocationInRange(ante, phase, rule)
	local at = Brainstorm.poolRoutePosition(ante, phase)
	local first = Brainstorm.poolRoutePosition(rule.minAnte, rule.minPhase or "small")
	local last = Brainstorm.poolRoutePosition(rule.maxAnte, rule.maxPhase or "big")
	return at and first and last and at >= first and at <= last
end

-- Omen is active for every Arcana card at or after the shop where it was
-- purchased. A pool voucher route carries the minimum fresh-run purchase
-- schedule; ordinary active filters fall back to the run's starting state.
function Brainstorm.omenOwnedForSoulEvent(route, event)
	if route and route.initialOwned and route.initialOwned.v_omen_globe then return true end
	if not route and G.GAME.used_vouchers and G.GAME.used_vouchers.v_omen_globe then
		return true
	end
	local purchase = route and route.purchase and route.purchase.v_omen_globe
	if not purchase or purchase.visit ~= 1 then return false end
	local ante, phase
	if purchase.ante == 1 then
		ante = 1
		phase = route.skipSm and route.skipSm[1] and "big" or "small"
	elseif purchase.ante and purchase.ante >= 2 then
		ante, phase = purchase.ante - 1, "boss"
	else
		return false
	end
	local boughtAt = Brainstorm.poolRoutePosition(ante, phase)
	local eventAt = Brainstorm.poolRoutePosition(event.humanAnte, event.phase)
	return boughtAt and eventAt and eventAt >= boughtAt
end

-- If the canonical pack route misses an active Legendary-Anywhere target,
-- try each actually rolled Charm tag as one additional skip. Each branch gets
-- a fresh RNG state, so a failed hypothetical never advances the canonical
-- run or the next candidate branch.
function Brainstorm.tryTargetedCharmLegendary(seed_found, target_key,
		require_negative, baseSm, baseBig, baseRewardSm, baseRewardBig, voucherRoute)
	Brainstorm.random_state = { hashed_seed = pseudohash(seed_found) }
	local rolls = {}
	for ante = 1, 8 do
		rolls[ante] = {
			Brainstorm.rollTag(seed_found, ante),
			Brainstorm.rollTag(seed_found, ante),
		}
	end
	for ante = 1, 8 do
		for blind = 1, 2 do
			local alreadySkipped = (blind == 1 and baseSm and baseSm[ante])
				or (blind == 2 and baseBig and baseBig[ante])
			if rolls[ante][blind] == "tag_charm" and not alreadySkipped then
				local branchSm = Brainstorm.mergeSkipRoutes(baseSm,
					blind == 1 and { [ante] = true } or nil)
				local branchBig = Brainstorm.mergeSkipRoutes(baseBig,
					blind == 2 and { [ante] = true } or nil)
				local branchRewardSm = Brainstorm.mergeRewardRoutes(baseRewardSm,
					blind == 1 and { [ante] = "tag_charm" } or nil)
				local branchRewardBig = Brainstorm.mergeRewardRoutes(baseRewardBig,
					blind == 2 and { [ante] = "tag_charm" } or nil)
				Brainstorm.random_state = { hashed_seed = pseudohash(seed_found) }
				Brainstorm.setPackSkipAssumption(seed_found, branchSm, branchBig,
					branchRewardSm, branchRewardBig)
				local oldRouteSm, oldRouteBig
				if voucherRoute then
					oldRouteSm, oldRouteBig = voucherRoute.skipSm, voucherRoute.skipBig
					voucherRoute.skipSm, voucherRoute.skipBig = branchSm, branchBig
				end
				local ok, loc = Brainstorm.checkLegendaryAnywhere(seed_found,
					target_key, require_negative, voucherRoute)
				if ok then
					return true, loc, branchSm, branchBig,
						branchRewardSm, branchRewardBig
				end
				if voucherRoute then
					voucherRoute.skipSm, voucherRoute.skipBig = oldRouteSm, oldRouteBig
				end
			end
		end
	end
	return false
end

function Brainstorm.startingVoucherSet()
	local owned = {}
	local backConfig = G.GAME.selected_back and G.GAME.selected_back.effect
		and G.GAME.selected_back.effect.config
	if backConfig then
		if type(backConfig.voucher) == "string" then owned[backConfig.voucher] = true end
		if type(backConfig.vouchers) == "table" then
			for _, key in pairs(backConfig.vouchers) do
				if type(key) == "string" then owned[key] = true end
			end
		end
	end
	local challenge = G.GAME.challenge_tab
	if challenge and type(challenge.vouchers) == "table" then
		for _, voucher in ipairs(challenge.vouchers) do
			local key = type(voucher) == "table" and (voucher.id or voucher.key) or voucher
			if type(key) == "string" then owned[key] = true end
		end
	end
	return owned
end

local function poolCopyTable(value)
	local out = {}
	for key, item in pairs(value or {}) do out[key] = item end
	return out
end

local function poolCopyPurchase(value)
	local out = {}
	for key, item in pairs(value or {}) do
		out[key] = { ante = item.ante, visit = item.visit }
	end
	return out
end

local function poolVoucherCatalog()
	local centers = (G.P_CENTER_POOLS and G.P_CENTER_POOLS.Voucher) or {}
	local keys = {}
	for _, center in ipairs(centers) do keys[center.key] = true end
	local flags = G.GAME.pool_flags or {}
	local banned = G.GAME.banned_keys or {}
	local catalog, byKey = {}, {}
	for index, center in ipairs(centers) do
		local eligible = center.unlocked ~= false and not banned[center.key]
		if center.no_pool_flag and flags[center.no_pool_flag] then eligible = false end
		if center.yes_pool_flag and not flags[center.yes_pool_flag] then eligible = false end
		local prerequisite
		if center.requires then
			if type(center.requires) == "table" and #center.requires == 1
					and keys[center.requires[1]] then
				prerequisite = center.requires[1]
			else
				eligible = false
			end
		end
		catalog[index] = { key = center.key, eligible = eligible,
			prerequisite = prerequisite }
		byKey[center.key] = catalog[index]
	end
	return catalog, byKey
end

-- Find the deterministic minimum-purchase route that satisfies all voucher
-- occurrence windows embedded in a pool. Skip edges run first, so equal-cost
-- routes resolve exactly like both native engines. Exclusions forbid buying an
-- offer, not seeing it. `legendPass`, when supplied, is evaluated at a leaf and
-- lets Omen timing participate in the same route search.
function Brainstorm.findPoolVoucherRoute(seed_found, header, skipSm, skipBig, options)
	options = options or {}
	local rules = header.pool_vouchers or {}
	local exclusions = {}
	for _, key in ipairs(header.pool_voucher_exclusions or {}) do exclusions[key] = true end
	local catalog, byKey = poolVoucherCatalog()
	local initial = Brainstorm.startingVoucherSet()
	for _, rule in ipairs(rules) do if not byKey[rule.key] then return nil end end
	if options.requireOmen and not byKey.v_omen_globe and not initial.v_omen_globe then
		return nil
	end
	-- No voucher predicate and no requested Omen purchase means the minimum
	-- route buys nothing. Validate a requested Soul branch immediately so a
	-- challenge with every voucher banned/already owned does not reject an
	-- otherwise valid canonical or targeted-Charm route.
	if #rules == 0 and not options.requireOmen then
		local route = { initialOwned = initial, owned = poolCopyTable(initial),
			purchase = {}, purchases = 0, skipSm = skipSm, skipBig = skipBig }
		if options.requireSouls then
			if not options.legendPass then return nil end
			local saved = poolCopyTable(Brainstorm.random_state)
			local passed = options.legendPass(route)
			Brainstorm.random_state = saved
			if not passed then return nil end
		end
		return route
	end

	local maxAnte = options.maxAnte or 1
	for _, rule in ipairs(rules) do if rule.maxAnte > maxAnte then maxAnte = rule.maxAnte end end
	if maxAnte < 1 or maxAnte > 8 then return nil end
	local purchased = poolCopyTable(initial)
	local best
	Brainstorm.random_state = { hashed_seed = pseudohash(seed_found) }

	local function rollVoucher(ante, owned)
		local available, any = {}, false
		for index, item in ipairs(catalog) do
			local ok = item.eligible and not owned[item.key]
				and (not item.prerequisite or owned[item.prerequisite])
			available[index] = ok and item.key or "UNAVAILABLE"
			if ok then any = true end
		end
		if not any then return nil end
		local stream = "Voucher" .. ante
		local chosen = pseudorandom_element(available,
			Brainstorm.pseudoseed(stream .. seed_found))
		local iteration = 1
		while chosen == "UNAVAILABLE" do
			iteration = iteration + 1
			chosen = pseudorandom_element(available,
				Brainstorm.pseudoseed(stream .. "_resample" .. iteration .. seed_found))
		end
		return chosen
	end

	local function search(ante, owned, matched, matchedCount, purchases,
			purchase, visits)
		if best and purchases > best.purchases then return end
		for index, rule in ipairs(rules) do
			if not matched[index] and ante > rule.maxAnte then return end
		end
		if ante > maxAnte then
			if matchedCount ~= #rules then return end
			if options.requireOmen and not owned.v_omen_globe then return end
			local route = { initialOwned = initial, owned = owned, purchase = purchase,
				purchases = purchases, skipSm = skipSm, skipBig = skipBig }
			if options.legendPass then
				local saved = poolCopyTable(Brainstorm.random_state)
				if not options.legendPass(route) then
					Brainstorm.random_state = saved
					return
				end
				Brainstorm.random_state = saved
			end
			if not best or purchases < best.purchases then
				best = { purchases = purchases, route = route }
			end
			return
		end

		local key = rollVoucher(ante, owned)
		if not key then return end
		local visit = (visits[ante] or 0) + 1
		local nextVisits = poolCopyTable(visits); nextVisits[ante] = visit
		local visible = ante ~= 1 or visit ~= 1
			or not (skipSm and skipSm[1]) or not (skipBig and skipBig[1])
		local nextMatched, nextCount = poolCopyTable(matched), matchedCount
		if visible then
			for index, rule in ipairs(rules) do
				if not nextMatched[index] and key == rule.key
						and ante >= rule.minAnte and ante <= rule.maxAnte then
					nextMatched[index], nextCount = true, nextCount + 1
				end
			end
		end
		local afterOffer = poolCopyTable(Brainstorm.random_state)
		search(ante + 1, owned, nextMatched, nextCount, purchases,
			purchase, nextVisits)
		Brainstorm.random_state = poolCopyTable(afterOffer)

		local needMore = nextCount ~= #rules
			or (options.requireOmen and not owned.v_omen_globe)
		local reducer = key == "v_hieroglyph" or key == "v_petroglyph"
		local hasTagRoute = #(header.route_tags or {}) > 0
		if visible and needMore and (not best or purchases + 1 <= best.purchases)
				and not exclusions[key]
				and not (reducer and (options.requireSouls or hasTagRoute)) then
			local bought = poolCopyTable(owned); bought[key] = true
			local boughtAt = poolCopyPurchase(purchase)
			boughtAt[key] = { ante = ante, visit = visit }
			search(reducer and ante or ante + 1, bought, nextMatched, nextCount,
				purchases + 1, boughtAt, nextVisits)
		end
	end

	search(1, purchased, {}, 0, 0, {}, {})
	return best and best.route or nil
end

-- Replay the cumulative route embedded in a .bspool header. Each rule selects
-- its first required matching occurrences, exactly like the external scanner;
-- observe-only stages count toward membership but do not remove shops.
function Brainstorm.poolRouteSkips(seed_found, header)
	local rules = header and header.route_tags or nil
	if not rules or #rules == 0 then return nil, nil, nil, nil end
	local counts, sm, big, rewardSm, rewardBig = {}, nil, nil, nil, nil
	for i = 1, #rules do counts[i] = 0 end
	for ante = 1, 39 do
		local rolled = nil
		for blind = 1, 2 do
			local phase = blind == 1 and "small" or "big"
			local need = false
			for i, rule in ipairs(rules) do
				if rule.collect and counts[i] < rule.count
						and Brainstorm.poolLocationInRange(ante, phase, rule) then
					need = true; break
				end
			end
			if need then
				rolled = Brainstorm.rollTag(seed_found, ante)
				for i, rule in ipairs(rules) do
					if rule.collect and counts[i] < rule.count
							and Brainstorm.poolLocationInRange(ante, phase, rule)
							and rolled == rule.key then
						counts[i] = counts[i] + 1
						local reward = Brainstorm.tagSoulRewardKey(rule.key) and rule.key or nil
						if blind == 1 then
							sm = sm or {}; sm[ante] = true
							if reward then rewardSm = rewardSm or {}; rewardSm[ante] = reward end
						else
							big = big or {}; big[ante] = true
							if reward then rewardBig = rewardBig or {}; rewardBig[ante] = reward end
						end
					end
				end
			end
		end
	end
	return sm, big, rewardSm, rewardBig
end

-- Independent in-game oracle for the criteria embedded in a pool. This is a
-- main-thread safety rail: native membership is fast, while this replay uses
-- Balatro's own Lua RNG/pools to reject any model or transfer error before a
-- seed is applied. It also returns the cumulative skips for overlay filters.
function Brainstorm.evaluatePoolCriteria(seed_found, header, overlaySm, overlayBig,
		overlayRewardSm, overlayRewardBig)
	local routeRules = header.route_tags or {}
	local maxTagAnte = 0
	for _, r in ipairs(routeRules) do maxTagAnte = math.max(maxTagAnte, r.maxAnte) end
	local rolls = {}
	for ante = 1, maxTagAnte do
		rolls[ante] = {
			Brainstorm.rollTag(seed_found, ante),
			Brainstorm.rollTag(seed_found, ante),
		}
	end
	-- route_tags is cumulative: readPoolHeader appends the current stage's
	-- `tag` lines to all inherited `route_tag` lines. Recheck every stage,
	-- including observe-only rules, so this really is an independent safety
	-- oracle rather than trusting source-pool membership for older stages.
	for _, rule in ipairs(routeRules) do
		local count = 0
		for ante = rule.minAnte, rule.maxAnte do
			for blind = 1, 2 do
				local phase = blind == 1 and "small" or "big"
				if Brainstorm.poolLocationInRange(ante, phase, rule)
						and rolls[ante] and rolls[ante][blind] == rule.key then
					count = count + 1
				end
			end
		end
		if count < rule.count then return false end
	end
	local routeCounts, sm, big, rewardSm, rewardBig = {}, nil, nil, nil, nil
	for i = 1, #routeRules do routeCounts[i] = 0 end
	for ante = 1, maxTagAnte do
		for blind = 1, 2 do
			local phase = blind == 1 and "small" or "big"
			for i, rule in ipairs(routeRules) do
				if rule.collect and routeCounts[i] < rule.count
						and Brainstorm.poolLocationInRange(ante, phase, rule)
						and rolls[ante][blind] == rule.key then
					routeCounts[i] = routeCounts[i] + 1
					local reward = Brainstorm.tagSoulRewardKey(rule.key) and rule.key or nil
					if blind == 1 then
						sm = sm or {}; sm[ante] = true
						if reward then rewardSm = rewardSm or {}; rewardSm[ante] = reward end
					else
						big = big or {}; big[ante] = true
						if reward then rewardBig = rewardBig or {}; rewardBig[ante] = reward end
					end
				end
			end
		end
	end
	local finalSm = Brainstorm.mergeSkipRoutes(sm, overlaySm)
	local finalBig = Brainstorm.mergeSkipRoutes(big, overlayBig)
	local finalRewardSm = Brainstorm.mergeRewardRoutes(rewardSm, overlayRewardSm)
	local finalRewardBig = Brainstorm.mergeRewardRoutes(rewardBig, overlayRewardBig)
	local voucherRoute = Brainstorm.findPoolVoucherRoute(seed_found, header,
		finalSm, finalBig, { maxAnte = 1 })
	if not voucherRoute then return false end

	local legendaryRules = header.pool_legendaries or {}
	if #legendaryRules == 0 and header.legendary then legendaryRules = { header.legendary } end
	if #legendaryRules == 0 then
		return true, finalSm, finalBig, finalRewardSm, finalRewardBig, voucherRoute
	end
	if G.GAME.banned_keys and G.GAME.banned_keys.c_soul then return false end

	local function pickLegendary(forbidden)
		local pool = Brainstorm.getJokerCulledPool(4)
		if forbidden then
			local copy = {}
			for i, key in ipairs(pool) do copy[i] = (key == forbidden) and 'UNAVAILABLE' or key end
			pool = copy
		end
		local chosen = pseudorandom_element(pool, Brainstorm.pseudoseed("Joker4" .. seed_found))
		local it = 1
		while chosen == 'UNAVAILABLE' do
			it = it + 1
			chosen = pseudorandom_element(pool,
				Brainstorm.pseudoseed("Joker4_resample" .. it .. seed_found))
		end
		return chosen
	end

	local maxAnte, tagMaxAnte = 0, 0
	for _, rule in ipairs(legendaryRules) do
		tagMaxAnte = math.max(tagMaxAnte, rule.maxAnte)
		maxAnte = math.max(maxAnte,
			rule.maxAnte + (rule.humanLocation
				and (rule.maxPhase or "big") == "boss" and 1 or 0))
	end
	-- Either-depth rules (soulDepth 0) resolve deterministically: the
	-- exclusive Soul #2 pick can never repeat Soul #1's legendary, so the
	-- target is at depth 1 iff it IS the first pick.
	local picked = { pickLegendary() }
	local resolved, needSecond = {}, false
	for i, rule in ipairs(legendaryRules) do
		local depth = rule.soulDepth or 1
		if depth == 0 then depth = (picked[1] == rule.key) and 1 or 2 end
		resolved[i] = depth
		if depth == 2 then needSecond = true end
	end
	if needSecond then picked[2] = pickLegendary(picked[1]) end
	for i, rule in ipairs(legendaryRules) do
		if picked[resolved[i]] ~= rule.key then return false end
	end
	local maxDepth = needSecond and 2 or 1

	local function soulRoutePass(routeSm, routeBig, routeRewardSm, routeRewardBig,
			ownedRoute)
		-- Each targeted branch clones the canonical RNG state. Independent
		-- per-key streams then advance exactly as they would in the game, while
		-- the canonical route remains untouched for the next branch.
		Brainstorm.random_state = { hashed_seed = pseudohash(seed_found) }
		Brainstorm.setPackSkipAssumption(seed_found, routeSm, routeBig,
			routeRewardSm, routeRewardBig)
		if ownedRoute then ownedRoute.skipSm, ownedRoute.skipBig = routeSm, routeBig end
		local soulNumber, events = 0, {}
		for ante = 1, maxAnte do
			local packs = Brainstorm.getSoulPackEvents(seed_found, ante)
			if not packs then return false end
			for _, packEvent in ipairs(packs) do
				local center = packEvent.center
				local kind = center and not center.forced and center.kind or nil
				if kind == 'Arcana' or kind == 'Spectral' then
					local soulInPack, blackHoleInPack = false, false
					for _ = 1, Brainstorm.packCardCount(center) do
						local contentKind = kind
						if kind == 'Arcana'
								and Brainstorm.omenOwnedForSoulEvent(ownedRoute, packEvent)
								and pseudorandom(Brainstorm.pseudoseed(
									'omen_globe' .. seed_found)) > 0.8 then
							contentKind = 'Spectral'
						end
						local stype = contentKind == 'Arcana' and 'Tarot' or 'Spectral'
						local soul = false
						if not soulInPack then
							soul = pseudorandom(Brainstorm.pseudoseed(
								'soul_' .. stype .. ante .. seed_found)) > 0.997
						end
						if contentKind == 'Spectral' and not blackHoleInPack then
							local blackHole = pseudorandom(Brainstorm.pseudoseed(
								'soul_' .. stype .. ante .. seed_found)) > 0.997
							if blackHole then
								if not (G.GAME.banned_keys and G.GAME.banned_keys.c_black_hole) then
									blackHoleInPack = true
								end
								soul = false
							end
						end
						if soul then
							soulInPack = true
							soulNumber = soulNumber + 1
							events[soulNumber] = { ante = ante,
								humanAnte = packEvent.humanAnte, phase = packEvent.phase,
								source = packEvent.source,
								edition = pseudorandom(Brainstorm.pseudoseed(
									"edisou" .. ante .. seed_found)) }
						end
					end
				end
				if soulNumber >= maxDepth then break end
			end
			if soulNumber >= maxDepth then break end
		end
		if soulNumber < maxDepth then return false end
		for i, rule in ipairs(legendaryRules) do
			local event = events[resolved[i]]
			local locationOK = event and (rule.humanLocation
				and Brainstorm.poolLocationInRange(event.humanAnte, event.phase, rule)
				or (not rule.humanLocation and event.ante >= rule.minAnte
					and event.ante <= rule.maxAnte))
			if not locationOK
					or ((rule.source or "any") ~= "any" and event.source ~= rule.source)
					or (rule.negative and event.edition <= 0.997) then
				return false
			end
		end
		return true
	end

	if soulRoutePass(finalSm, finalBig, finalRewardSm, finalRewardBig,
			voucherRoute) then
		return true, finalSm, finalBig, finalRewardSm, finalRewardBig, voucherRoute
	end
	local voucherMaxAnte = math.min(maxAnte, 8)

	-- Targeted alternate routes, cheapest and least-purchased first: an actual
	-- Charm Tag with NO Omen purchase, then an Omen voucher route on the
	-- canonical blinds, then a Charm branch combined with Omen. Each Charm
	-- branch skips only that blind, opens its five-card reward immediately,
	-- and is retained only when it satisfies all embedded Legendary rules.
	Brainstorm.random_state = { hashed_seed = pseudohash(seed_found) }
	local function tryCharmBranches(withOmen)
		for ante = 1, tagMaxAnte do
			if not rolls[ante] then
				rolls[ante] = {
					Brainstorm.rollTag(seed_found, ante),
					Brainstorm.rollTag(seed_found, ante),
				}
			end
			for blind = 1, 2 do
				local alreadySkipped = (blind == 1 and finalSm and finalSm[ante])
					or (blind == 2 and finalBig and finalBig[ante])
				if rolls[ante][blind] == 'tag_charm' and not alreadySkipped then
					local branchSm = Brainstorm.mergeSkipRoutes(finalSm,
						blind == 1 and { [ante] = true } or nil)
					local branchBig = Brainstorm.mergeSkipRoutes(finalBig,
						blind == 2 and { [ante] = true } or nil)
					local branchRewardSm = Brainstorm.mergeRewardRoutes(finalRewardSm,
						blind == 1 and { [ante] = 'tag_charm' } or nil)
					local branchRewardBig = Brainstorm.mergeRewardRoutes(finalRewardBig,
						blind == 2 and { [ante] = 'tag_charm' } or nil)
					local branchRoute = Brainstorm.findPoolVoucherRoute(seed_found, header,
						branchSm, branchBig, {
							requireOmen = withOmen or nil,
							requireSouls = true, maxAnte = voucherMaxAnte,
							legendPass = function(route)
								return soulRoutePass(branchSm, branchBig,
									branchRewardSm, branchRewardBig, route)
							end,
						})
					if branchRoute then
						return true, branchSm, branchBig, branchRewardSm, branchRewardBig,
							branchRoute
					end
				end
			end
		end
		return false
	end
	local okCharm, cSm, cBig, cRewardSm, cRewardBig, cRoute = tryCharmBranches(false)
	if okCharm then
		return true, cSm, cBig, cRewardSm, cRewardBig, cRoute
	end
	-- Fast-exact pools intentionally stop after the canonical and non-Omen
	-- Charm routes. A current-stage directive takes precedence; older pools may
	-- carry only the inherited directive, so use that solely as a fallback.
	local legendaryRoutes = header.legendary_routes
	if legendaryRoutes == "canonical_charm"
			or (not legendaryRoutes
				and header.route_legendary_routes == "canonical_charm") then
		return false
	end
	local omenRoute = Brainstorm.findPoolVoucherRoute(seed_found, header,
		finalSm, finalBig, {
			requireOmen = true, requireSouls = true, maxAnte = voucherMaxAnte,
			legendPass = function(route)
				return soulRoutePass(finalSm, finalBig, finalRewardSm,
					finalRewardBig, route)
			end,
		})
	if omenRoute then
		return true, finalSm, finalBig, finalRewardSm, finalRewardBig, omenRoute
	end
	okCharm, cSm, cBig, cRewardSm, cRewardBig, cRoute = tryCharmBranches(true)
	if okCharm then
		return true, cSm, cBig, cRewardSm, cRewardBig, cRoute
	end
	return false
end

function Brainstorm.skipsFromFilters(tagLoc)
	local ar = Brainstorm.SETTINGS.autoreroll
	local sm, big, rewardSm, rewardBig = nil, nil, nil, nil
	local legAnywhere = ar.searchLegendaryAnywhere and ar.searchLegendary and ar.searchLegendary ~= ""
	if not legAnywhere and ((ar.searchForSoul and ar.searchForSoul > 0) or (ar.searchLegendary and ar.searchLegendary ~= "")) then
		sm = { [1] = true }
		rewardSm = { [1] = "tag_charm" }
	end
	if ar.searchTag and ar.searchTag ~= "" then
		local reward = Brainstorm.tagSoulRewardKey(ar.searchTag) and ar.searchTag or nil
		if not ar.searchTagAnywhere then
			sm = sm or {}
			sm[1] = true
			if reward then rewardSm = rewardSm or {}; rewardSm[1] = reward end
		elseif tagLoc then
			local a = tonumber(tagLoc:match("^TagA(%d+)"))
			if a then
				if tagLoc:sub(-2) == "Sm" then
					sm = sm or {}
					sm[a] = true
					if reward then rewardSm = rewardSm or {}; rewardSm[a] = reward end
				else
					big = big or {}
					big[a] = true
					if reward then rewardBig = rewardBig or {}; rewardBig[a] = reward end
				end
			end
		end
	end
	return sm, big, rewardSm, rewardBig
end

-- How many shops open at this ante: ante 1 has Small + Big; antes 2+ add the
-- post-boss "entry" shop first (SOURCE-VERIFIED: ease_ante fires when the
-- Boss dies, BEFORE its shop, so that shop belongs to the NEXT ante's streams
-- and HUD ante). Minus one per assumed-skipped blind.
local function anteShopCount(ante, skipSm, skipBig)
	return (ante >= 2 and 3 or 2)
		- ((skipSm and skipSm[ante]) and 1 or 0)
		- ((skipBig and skipBig[ante]) and 1 or 0)
end

-- Install the per-seed skip assumption. MUST run before any pack consumer in
-- an evaluation: the pack memo bakes the slot layout in. Idempotent for the
-- same (random_state, seed, assumption) so a repeated call inside one
-- evaluation never re-rolls (and so never double-advances) the pack streams;
-- a fresh Brainstorm.random_state table invalidates the memo, because cached
-- picks may not be extended under a stream state they didn't come from.
function Brainstorm.setPackSkipAssumption(seed_found, skipSm, skipBig, rewardSm, rewardBig)
	local sig = ""
	if skipSm then
		for a = 1, 39 do if skipSm[a] then sig = sig .. "s" .. a end end
	end
	if skipBig then
		for a = 1, 39 do if skipBig[a] then sig = sig .. "b" .. a end end
	end
	if rewardSm then
		for a = 1, 39 do if rewardSm[a] then sig = sig .. "r" .. a .. rewardSm[a] end end
	end
	if rewardBig then
		for a = 1, 39 do if rewardBig[a] then sig = sig .. "R" .. a .. rewardBig[a] end end
	end
	local sim = Brainstorm._packSim
	if sim and sim.seed == seed_found and sim.sig == sig and sim.rs == Brainstorm.random_state then
		return
	end
	local forcedAnte = 1
	for a = 1, 39 do
		if anteShopCount(a, skipSm, skipBig) > 0 then
			forcedAnte = a
			break
		end
	end
	Brainstorm._packSim = {
		seed = seed_found, sig = sig, rs = Brainstorm.random_state,
		skipSm = skipSm or false, skipBig = skipBig or false, forcedAnte = forcedAnte,
		rewardSm = rewardSm or false, rewardBig = rewardBig or false,
		forceBuffoon = not (G.GAME.banned_keys and G.GAME.banned_keys.p_buffoon_normal_1),
	}
end

-- Derive + install the skip assumption for `seed_found` from scratch, rolling
-- the anywhere-tag match if needed (tag streams only -- pack streams are
-- untouched). For prediction/debug paths; passesAllFilters installs from its
-- own in-flight tag result instead.
function Brainstorm.installSkipAssumptionFresh(seed_found)
	local ar = Brainstorm.SETTINGS.autoreroll
	local tagLoc = nil
	if ar.searchTag and ar.searchTag ~= "" and ar.searchTagAnywhere then
		for a = 1, 8 do
			if Brainstorm.rollTag(seed_found, a) == ar.searchTag then
				tagLoc = "TagA" .. a .. "Sm"
				break
			end
			if Brainstorm.rollTag(seed_found, a) == ar.searchTag then
				tagLoc = "TagA" .. a .. "Big"
				break
			end
		end
	end
	local sm, big, rewardSm, rewardBig = Brainstorm.skipsFromFilters(tagLoc)
	Brainstorm.setPackSkipAssumption(seed_found, sm, big, rewardSm, rewardBig)
	return sm, big, rewardSm, rewardBig
end

-- Rolls an ante's PHYSICAL pack slots (2 per opened shop; see anteShopCount:
-- ante 1 up to 4 slots, antes 2+ up to 6, fewer under assumed skips). The
-- run's first opened shop gets the forced normal Buffoon at its slot 1; every
-- other slot consumes one sequential 'shop_pack'..ante advance. Memoized per
-- (random_state, seed, skip signature) and extended on demand -- the stream is
-- sequential, so appending later slots yields exactly what rolling them
-- upfront would have. `count` is a cap: the returned list never exceeds the
-- physical slot count, and callers must iterate #list.
function Brainstorm.getSimulatedPacks(seed_found, ante, count)
	count = count or 2
	local sim = Brainstorm._packSim
	if not sim or sim.seed ~= seed_found or sim.rs ~= Brainstorm.random_state then
		sim = { seed = seed_found, sig = "", rs = Brainstorm.random_state,
			skipSm = false, skipBig = false, forcedAnte = 1,
			rewardSm = false, rewardBig = false,
			forceBuffoon = not (G.GAME.banned_keys and G.GAME.banned_keys.p_buffoon_normal_1) }
		Brainstorm._packSim = sim
	end
	local maxSlots = 2 * anteShopCount(ante, sim.skipSm, sim.skipBig)
	if count > maxSlots then count = maxSlots end
	local out = sim[ante]
	if not out then
		out = {}
		if sim.forceBuffoon and ante == sim.forcedAnte and maxSlots > 0 then
			out[1] = Brainstorm.FORCED_BUFFOON
		end
		sim[ante] = out
	end
	if #out >= count then return out end
	-- Worker path reuses the precomputed sum (buildCulledPools); the inline
	-- fallback keeps the main-thread / cache-off path exactly as before.
	local c = Brainstorm.CULLED
	local cume = c and c.boosterCume
	if not cume then
		cume = 0
		for k, v in ipairs(G.P_CENTER_POOLS['Booster']) do
			if not (G.GAME.banned_keys and G.GAME.banned_keys[v.key]) then
				cume = cume + (v.weight or 1)
			end
		end
	end
	while #out < count do
		local n0 = #out
		local poll = pseudorandom(Brainstorm.pseudoseed("shop_pack" .. ante .. seed_found)) * cume
		local it = 0
		for k, v in ipairs(G.P_CENTER_POOLS['Booster']) do
			if not (G.GAME.banned_keys and G.GAME.banned_keys[v.key]) then
				it = it + (v.weight or 1)
				if it >= poll and it - (v.weight or 1) <= poll then out[#out + 1] = v; break end
			end
		end
		if #out == n0 then out[#out + 1] = false end -- can't happen; guards the loop
	end
	return out
end

-- Chronological Soul-capable pack route for one Ante. Antes 2+ start with the
-- previous Boss's entry shop. A played Small/Big contributes its two flattened
-- shop pack offers; a collected Charm/Ethereal tag contributes its immediate
-- reward instead. getSimulatedPacks already compresses shop_pack advances over
-- skipped shops, so splitting its result into pairs restores the event timing.
function Brainstorm.getSoulPackEvents(seed_found, ante)
	local sim = Brainstorm._packSim
	if not sim or sim.seed ~= seed_found or sim.rs ~= Brainstorm.random_state then
		Brainstorm.setPackSkipAssumption(seed_found, nil, nil, nil, nil)
		sim = Brainstorm._packSim
	end
	local shopPacks = Brainstorm.getSimulatedPacks(seed_found, ante, 6)
	local events, cursor = {}, 1
	local function addShopPair(humanAnte, phase)
		for _ = 1, 2 do
			local center = shopPacks[cursor]
			if center == nil then return end
			events[#events + 1] = { center = center, location = "P" .. cursor,
				humanAnte = humanAnte, phase = phase, source = "shop" }
			cursor = cursor + 1
		end
	end
	local function addReward(route, blind)
		local tagKey = route and route[ante]
		if not tagKey then return true end
		local center = Brainstorm.tagSoulRewardCenter(tagKey)
		if not center then return false end
		events[#events + 1] = { center = center,
			location = (tagKey == "tag_charm" and "Charm" or "Ethereal") .. blind,
			tagReward = true, humanAnte = ante,
			phase = blind == "Sm" and "small" or "big",
			source = tagKey == "tag_charm" and "charm" or "ethereal" }
		return true
	end
	if ante >= 2 then addShopPair(ante - 1, "boss") end
	if sim.skipSm and sim.skipSm[ante] then
		if not addReward(sim.rewardSm, "Sm") then return nil end
	else
		addShopPair(ante, "small")
	end
	if sim.skipBig and sim.skipBig[ante] then
		if not addReward(sim.rewardBig, "Big") then return nil end
	else
		addShopPair(ante, "big")
	end
	return events
end

-- Roll the joker contents of an ante's Buffoon packs (physical slots, in
-- order) into one {key, neg, rarity} sequence. Non-Buffoon packs consume none
-- of the buf-joker streams, same as the game. Includes the run's forced
-- Buffoon (2 cards -- opened first by convention, its jokers consume that
-- ante's rarity/Joker buf streams before any stream-rolled pack's). Card
-- counts come from config.extra via packCardCount (mega Buffoon: 4, not 6).
-- `nslots` is this consumer's window; slots beyond the ante's physical list
-- are nil and skipped.
function Brainstorm.simulatePackJokers(seed_found, ante, wanted, needNeg, nslots)
	local packs = Brainstorm.getSimulatedPacks(seed_found, ante, nslots or 2)
	local out = {}
	for slot = 1, nslots or 2 do
		local center = packs[slot]
		if center and center.kind == 'Buffoon' then
			local num_cards = Brainstorm.packCardCount(center)
			for card = 1, num_cards do
				local rarity_roll = pseudorandom(Brainstorm.pseudoseed("rarity" .. ante .. "buf" .. seed_found))
				local rarity = rarity_roll > 0.95 and 3 or rarity_roll > 0.7 and 2 or 1
				local chosen = nil
				if wanted[rarity] then
					local rkey = "Joker" .. rarity .. "buf" .. ante
					local pool = Brainstorm.getJokerCulledPool(rarity)
					chosen = pseudorandom_element(pool, Brainstorm.pseudoseed(rkey .. seed_found))
					local it = 1
					while chosen == 'UNAVAILABLE' do
						it = it + 1
						chosen = pseudorandom_element(pool, Brainstorm.pseudoseed(rkey .. '_resample' .. it .. seed_found))
					end
				end
				local neg = false
				if needNeg then
					neg = pseudorandom(Brainstorm.pseudoseed("edibuf" .. ante .. seed_found)) > 0.997
				end
				out[#out + 1] = { key = chosen, neg = neg, rarity = rarity }
			end
		end
	end
	return out
end

-- Wildcard joker targets: assign one of these keys to a joker slot to search
-- by RARITY (+ the slot's Negative toggle) instead of a specific joker. No
-- pool pick is needed to decide a wildcard, so those picks are skipped
-- entirely (safe: each pick stream is read by nothing else).
Brainstorm.WILDCARD_RARITY = { ["*any"] = 0, ["*common"] = 1, ["*uncommon"] = 2, ["*rare"] = 3 }

-- Resolve the multi-ante joker window into per-ante {slots, packs} arrays.
-- "Anywhere" mode overrides the per-ante rows with one uniform depth across
-- antes 1..8, packs included -- "find it anywhere reasonable" searches without
-- per-ante fiddling. All models used at antes 5-8 are the same verified
-- per-ante keyed streams the 1-4 search uses (cdt<a>/rarity<a>.../shop_pack<a>).
function Brainstorm.effectiveMultiAnte()
	local ma = Brainstorm.SETTINGS.multiAnteSearch or {}
	local slots, packs = {}, {}
	if ma.anywhereMode then
		local depth = ma.anywhereSlots or 8
		for a = 1, 8 do
			slots[a] = depth
			packs[a] = true
		end
		-- Anywhere mode scans every physical pack slot per ante (the 6 here is
		-- a CAP -- getSimulatedPacks stops at the ante's real slot count: 4 at
		-- ante 1, 6 at antes 2+, fewer under assumed blind skips); per-ante
		-- rows keep the first-shop window (2 slots).
		return slots, packs, 8, 6
	end
	for a = 1, 4 do
		slots[a] = ma["ante" .. a .. "Slots"] or 0
		packs[a] = ma["ante" .. a .. "Packs"] or false
	end
	return slots, packs, 4, 2
end

function Brainstorm.checkMultiAnteJokerSearch(seed_found)
	Brainstorm.AUTOREROLL.jokerFoundAt = nil
	local ar = Brainstorm.SETTINGS.autoreroll
	local slots = ar.jokerSlotData
	if not slots then return true end

	-- All the "is this filter even active?" outs are pure predicates (no RNG
	-- reads), so they can run in any order. Do them table-alloc-free and
	-- BEFORE building the targets list: this function runs for every candidate
	-- seed, and most configs don't use the joker search at all.
	local cfg = Brainstorm.SETTINGS.multiAnteSearch
	if not cfg then return true end

	local anteSlots, antePacks, maxAnte, packSlots = Brainstorm.effectiveMultiAnte()
	local any_ante_active = false
	for ante = 1, maxAnte do
		if anteSlots[ante] > 0 or antePacks[ante] then
			any_ante_active = true; break
		end
	end
	if not any_ante_active then return true end

	local targets = {}
	for i, slot in ipairs(slots) do
		if slot.key and slot.key ~= "" then
			targets[#targets+1] = {
				key = slot.key, slot = i, requireNegative = slot.requireNegative,
				wild = Brainstorm.WILDCARD_RARITY[slot.key],
			}
		end
	end
	if #targets == 0 then return true end

	-- Match ANY (OR): a seed passes if just one of the selected jokers is found
	-- (e.g. to pair a legendary start with any one of the 3). Match ALL (AND,
	-- default): every selected joker must be found.
	local matchAny = ar.jokerSearchMatchAny

	local wanted, needNeg = {}, false
	for _, t in ipairs(targets) do
		if not t.wild then
			local r = Brainstorm.getJokerRarity(t.key)
			if r then
				wanted[r] = true
			else
				wanted[1], wanted[2], wanted[3] = true, true, true
			end
		end
		if t.requireNegative then needNeg = true end
	end

	local foundAt = {}
	local remaining = #targets

	-- Match all still-unfound targets against one simulated {key, neg, rarity}
	-- sequence. Specific keys keep the FIRST-occurrence rule (the joker you'd
	-- actually see/buy is a fixed card): if requireNegative and that occurrence
	-- isn't negative, the target stays unfound for this sequence. Wildcards
	-- instead match if ANY entry of the right rarity satisfies the negative
	-- requirement -- a failed candidate doesn't consume the wildcard.
	local function matchSeq(seq, label)
		for _, t in ipairs(targets) do
			if not foundAt[t.slot] then
				if t.wild then
					for i = 1, #seq do
						local e = seq[i]
						if (t.wild == 0 or e.rarity == t.wild) and ((not t.requireNegative) or e.neg) then
							foundAt[t.slot] = label
							remaining = remaining - 1
							break
						end
					end
				else
					for i = 1, #seq do
						if seq[i].key == t.key then
							if (not t.requireNegative) or seq[i].neg then
								foundAt[t.slot] = label
								remaining = remaining - 1
							end
							break
						end
					end
				end
			end
		end
	end

	for ante = 1, maxAnte do
		local ante_slots = anteSlots[ante]
		local packs = antePacks[ante]
		if ante_slots > 0 then
			matchSeq(Brainstorm.simulateShopJokers(seed_found, ante, ante_slots, wanted, needNeg), "A"..ante.."Shop")
			if remaining == 0 or (matchAny and remaining < #targets) then break end
		end
		if packs then
			matchSeq(Brainstorm.simulatePackJokers(seed_found, ante, wanted, needNeg, packSlots), "A"..ante.."Pack")
			if remaining == 0 or (matchAny and remaining < #targets) then break end
		end
	end

	local ok
	if matchAny then
		ok = remaining < #targets
	else
		ok = remaining == 0
	end
	if not ok then return false end

	local parts = {}
	for i = 1, 3 do if foundAt[i] then parts[#parts+1] = "J"..i..foundAt[i] end end
	Brainstorm.AUTOREROLL.jokerFoundAt = table.concat(parts, " ")
	return true
end

function Brainstorm.debugPredictLegendary(seed_found)
	Brainstorm.random_state = { hashed_seed = pseudohash(seed_found) }
	local soul_found = false
	local charmPack = Brainstorm.tagSoulRewardCenter("tag_charm")
	for i = 1, charmPack and Brainstorm.packCardCount(charmPack) or 0 do
		if pseudorandom(Brainstorm.pseudoseed("soul_Tarot1" .. seed_found)) > 0.997 then
			soul_found = true
			break
		end
	end

	local lines = { "Seed: " .. seed_found, "Soul in Arcana pack: " .. tostring(soul_found) }
	if soul_found then
		local pool = G.P_JOKER_RARITY_POOLS[4]
		local filtered = {}
		for k, v in ipairs(pool) do
			filtered[k] = Brainstorm.joker_is_pool_eligible(v) and v.key or 'UNAVAILABLE'
		end
		local chosen_key = pseudorandom_element(filtered, Brainstorm.pseudoseed("Joker4" .. seed_found))
		local it = 1
		while chosen_key == 'UNAVAILABLE' do
			it = it + 1
			chosen_key = pseudorandom_element(filtered, Brainstorm.pseudoseed("Joker4" .. '_resample' .. it .. seed_found))
		end
		lines[#lines+1] = "Predicted legendary: " .. chosen_key
	end

	local nativefs = require("brainstorm_nativefs")
	nativefs.write(Brainstorm.modPath() .. "/debug_predict.txt", table.concat(lines, "\n"))
end

function Brainstorm.checkLegendarySearch(seed_found, target_key)
	local soul_found = false
	local charmPack = Brainstorm.tagSoulRewardCenter("tag_charm")
	for i = 1, charmPack and Brainstorm.packCardCount(charmPack) or 0 do
		if pseudorandom(Brainstorm.pseudoseed("soul_Tarot1" .. seed_found)) > 0.997 then
			soul_found = true
			break
		end
	end
	if not soul_found then return false end

	local pool = G.P_JOKER_RARITY_POOLS[4]
	local filtered = {}
	for k, v in ipairs(pool) do
		filtered[k] = Brainstorm.joker_is_pool_eligible(v) and v.key or 'UNAVAILABLE'
	end
	local chosen_key = pseudorandom_element(filtered, Brainstorm.pseudoseed("Joker4" .. seed_found))
	local it = 1
	while chosen_key == 'UNAVAILABLE' do
		it = it + 1
		chosen_key = pseudorandom_element(filtered, Brainstorm.pseudoseed("Joker4" .. '_resample' .. it .. seed_found))
	end

	return chosen_key == target_key
end

-- ===========================================================================
-- Background seed search (love.thread)
-- ---------------------------------------------------------------------------
-- The filter suite (Brainstorm.passesAllFilters and its helpers) is pure given
-- (seed, pool snapshot, settings). A love.thread has its own Lua state with no
-- access to G, so we snapshot the pools + game flags on the main thread, hand
-- them to a worker, and let the worker reconstruct a minimal G and load THIS
-- file to get the exact same filter code. Result seeds come back over a channel
-- and are applied via Brainstorm.applyFoundSeed on the main thread. Zero shared
-- mutable state, so no locking is needed.
-- ===========================================================================

Brainstorm.SEARCH_CHANNELS = {
	session = "brainstorm_search_session",
	result = "brainstorm_search_result",
	progress = "brainstorm_search_progress",
}

function Brainstorm.getRerollSource()
	local nativefs = require("brainstorm_nativefs")
	return nativefs.read(Brainstorm.modPath() .. "/Brainstorm_reroll.lua")
end

-- Serialize primitives / nested tables to a Lua literal the worker rebuilds via
-- load("return "..str)(). Avoids depending on love channels deep-copying nested
-- tables. Only handles the value types our snapshot contains.
function Brainstorm.serializeValue(v)
	local t = type(v)
	if t == "string" then
		return string.format("%q", v)
	elseif t == "number" then
		return string.format("%.17g", v)
	elseif t == "boolean" then
		return tostring(v)
	elseif t == "table" then
		local parts = {}
		for k, val in pairs(v) do
			local keyStr
			if type(k) == "number" then
				keyStr = "[" .. string.format("%.17g", k) .. "]"
			else
				keyStr = "[" .. string.format("%q", k) .. "]"
			end
			parts[#parts + 1] = keyStr .. "=" .. Brainstorm.serializeValue(val)
		end
		return "{" .. table.concat(parts, ",") .. "}"
	end
	return "nil"
end

-- Capture everything the filters read off G, resolved for a fresh run. Pool
-- eligibility is static across a search, so the worker rebuilds these arrays
-- and runs joker_is_pool_eligible itself (identical index-preserving culling).
function Brainstorm.buildSearchSnapshot(session)
	local snap = { session = session }

	snap.jokerPools = {}
	for r = 1, 4 do
		local arr = {}
		local src = G.P_JOKER_RARITY_POOLS[r]
		if src then
			for i, v in ipairs(src) do
				arr[i] = {
					key = v.key,
					unlocked = v.unlocked,
					rarity = v.rarity,
					enhancement_gate = v.enhancement_gate,
					no_pool_flag = v.no_pool_flag,
					yes_pool_flag = v.yes_pool_flag,
				}
			end
		end
		snap.jokerPools[r] = arr
	end

	snap.boosterPool = {}
	for i, v in ipairs(G.P_CENTER_POOLS['Booster']) do
		snap.boosterPool[i] = { key = v.key, weight = v.weight, kind = v.kind,
			cards = v.config and v.config.extra }
	end

	snap.tagPool = {}
	for i, v in ipairs(G.P_CENTER_POOLS['Tag']) do
		-- requiresOk resolves the discovery-based culling HERE (profile
		-- collection state); min_ante keeps the ante-dependent culling.
		snap.tagPool[i] = { key = v.key, sort_id = v.sort_id, min_ante = v.min_ante,
			no_pool_flag = v.no_pool_flag, yes_pool_flag = v.yes_pool_flag,
			requiresOk = (not v.requires) and true
				or ((G.P_CENTERS and G.P_CENTERS[v.requires] and G.P_CENTERS[v.requires].discovered) and true or false) }
	end

	snap.voucherPool = {}
	for i, v in ipairs(G.P_CENTER_POOLS['Voucher']) do
		snap.voucherPool[i] = { key = v.key, unlocked = v.unlocked, requires = (v.requires ~= nil) or nil }
	end

	snap.game = { banned_keys = {}, pool_flags = {} }
	if G.GAME and G.GAME.banned_keys then
		for k, val in pairs(G.GAME.banned_keys) do snap.game.banned_keys[k] = val end
	end
	if G.GAME and G.GAME.pool_flags then
		for k, val in pairs(G.GAME.pool_flags) do snap.game.pool_flags[k] = val end
	end

	snap.autoreroll = Brainstorm.SETTINGS.autoreroll
	snap.multiAnteSearch = Brainstorm.SETTINGS.multiAnteSearch
	snap.useCulledCache = Brainstorm.SETTINGS.useCulledCache
	snap.entropy = (love.timer and love.timer.getTime() or os.clock()) * 1000
	return snap
end

-- Resolve the number of parallel worker threads. 0 (the default) means "auto":
-- one per core minus one, so the game + render thread keep a core. A saved
-- positive value overrides. Clamped to >= 1.
function Brainstorm.getSearchThreadCount()
	local n = Brainstorm.SETTINGS.autoreroll.searchThreads or 0
	if n and n > 0 then return n end
	local cores = (love.system and love.system.getProcessorCount and love.system.getProcessorCount()) or 4
	return math.max(1, cores - 1)
end

-- Spawn N worker threads that partition the SAME seed sequence with no overlap:
-- thread `i` (0-based) of `n` tests global indices i, i+n, i+2n, ... (see the
-- worker's `k = (tried-1)*n + i`). They share the result/progress/session
-- channels; the first to find pushes its seed and the others are stopped when
-- the session channel is cleared. Near-linear speedup with core count.
function Brainstorm.startSearchThread()
	local A = Brainstorm.AUTOREROLL
	A.searchSession = (A.searchSession or 0) + 1
	local session = A.searchSession
	local C = Brainstorm.SEARCH_CHANNELS

	local sc = love.thread.getChannel(C.session)
	sc:clear()
	sc:push(session)
	love.thread.getChannel(C.result):clear()
	love.thread.getChannel(C.progress):clear()

	local configStr = Brainstorm.serializeValue(Brainstorm.buildSearchSnapshot(session))
	local rerollSrc = Brainstorm.getRerollSource()

	local n = Brainstorm.getSearchThreadCount()
	if Brainstorm.startSearchBackendCounter then
		local backend = Brainstorm.luaSearchBackendKey
			and Brainstorm.luaSearchBackendKey(n) or ("lua-" .. tostring(n))
		Brainstorm.startSearchBackendCounter(backend)
	end
	A.searchThreads = {}
	A.searchProgress = {}
	A.searchThreadCount = n
	for i = 0, n - 1 do
		local t = love.thread.newThread(Brainstorm.SEARCH_WORKER_SRC)
		t:start(configStr, rerollSrc, i, n)
		A.searchThreads[#A.searchThreads + 1] = t
	end
end

-- Returns the result table {seed, jokerFoundAt, session} once the worker finds a
-- match for the CURRENT session, else nil. Also drains progress + surfaces
-- worker errors (falling back to the synchronous path for the rest of the run).
function Brainstorm.pollSearchThread()
	local A = Brainstorm.AUTOREROLL
	if not A.searchThreads or #A.searchThreads == 0 then return nil end

	for _, t in ipairs(A.searchThreads) do
		local err = t:getError()
		if err then
			print("[Brainstorm] search thread error: " .. tostring(err))
			A.searchThreadFailed = true
			Brainstorm.stopSearchThread()
			return nil
		end
	end

	local C = Brainstorm.SEARCH_CHANNELS
	-- Progress is per-thread ("i:n"); keep the latest count for each and sum
	-- them so A.searchTried reflects total seeds tested across all workers.
	local progressChan = love.thread.getChannel(C.progress)
	A.searchProgress = A.searchProgress or {}
	local praw = progressChan:pop()
	while praw ~= nil do
		-- Workers publish this hot-path counter as "index:count". The previous
		-- serialized Lua table required a fresh parser/compiler invocation for
		-- every message and could flood the main thread on simple searches.
		local worker, count
		if type(praw) == "string" then
			worker, count = praw:match("^(%d+):(%d+)$")
		end
		if worker then
			worker, count = tonumber(worker), tonumber(count)
		end
		if worker and count and worker >= 0
				and (not A.searchThreadCount or worker < A.searchThreadCount) then
			A.searchProgress[worker] = count
		end
		praw = progressChan:pop()
	end
	local total = 0
	for _, v in pairs(A.searchProgress) do total = total + v end
	A.searchTried = total

	-- Any worker's result is acceptable; first one popped wins.
	local raw = love.thread.getChannel(C.result):pop()
	if raw then
		local ok, res = pcall(function() return load("return " .. raw)() end)
		if ok and res and res.session == A.searchSession then
			return res
		end
	end
	return nil
end

function Brainstorm.stopSearchThread()
	local A = Brainstorm.AUTOREROLL
	-- One stop entry point for every search backend: also signals the native
	-- helper (keyhandler toggle-off calls only this function).
	if Brainstorm.stopNativeSearch then Brainstorm.stopNativeSearch() end
	-- Clearing the session channel makes every live worker's peek() ~= its session,
	-- so they all exit their loop (no forced kill; love threads can't be killed).
	love.thread.getChannel(Brainstorm.SEARCH_CHANNELS.session):clear()
	A.searchThreads = nil
	A.searchProgress = nil
end

-- Drives autoreroll each frame. Threaded path polls the worker; fallback path
-- runs the old synchronous auto_reroll. Owns the live search text + the
-- found-joker alert so Brainstorm.update just delegates here.
function Brainstorm.updateAutoReroll(dt)
	local A = Brainstorm.AUTOREROLL
	if not A.autoRerollActive then return end
	A.autoRerollFrames = A.autoRerollFrames or 0
	if not A.searchStatsActive then Brainstorm.beginSearchStats() end

	local useThread = (Brainstorm.SETTINGS.useSearchThread ~= false)
		and love and love.thread and not A.searchThreadFailed
	local useNative = Brainstorm.nativeAvailable() and not A.nativeFailed

	-- Seed-pool searches are native-only and definitive. If the helper can't
	-- run (missing binary/pool, failed session) or reported a pool problem
	-- (bad file, model mismatch, or NO pool seed passing the filters), stop
	-- the search with a message: degrading to the full-space Lua search would
	-- return seeds outside the selected pool.
	local arS = Brainstorm.SETTINGS.autoreroll
	if A.autoPoolAbort then
		local reason = A.autoPoolAbort
		A.autoPoolAbort = nil
		A.autoRerollActive = false
		Brainstorm.stopSearchThread()
		Brainstorm.resetSearchUI()
		Brainstorm.showSeedSlotAlert(reason)
		return
	end
	if arS.seedPoolFile and arS.seedPoolFile ~= "" then
		local reason = nil
		if A.poolAbort then
			reason = A.poolAbort:find("currently recorded seeds")
					and "No match among the seeds recorded so far (source pool is incomplete)"
				or A.poolAbort:find("no seed in the pool")
					and "No seed in the pool matches these filters"
				or "Seed pool error (see console/log)"
		elseif not Brainstorm.seedPoolPath() then
			reason = "Seed pool file is missing"
		elseif not useNative then
			reason = "Seed pools need the native helper (see README)"
		end
		if reason then
			A.poolAbort = nil
			A.autoRerollActive = false
			Brainstorm.stopSearchThread()
			Brainstorm.resetSearchUI()
			Brainstorm.showSeedSlotAlert(reason)
			return
		end
	end

	local seed_found, jokerFoundAt = nil, nil

	if useNative then
		if not A.nativeActive then
			if not Brainstorm.startNativeSearch() then
				if A.autoPoolSelection then
					A.autoPoolSelection = nil
					A.autoPoolDisabled = true
					if Brainstorm.setAttachedPoolEstimateMode then
						Brainstorm.setAttachedPoolEstimateMode(false)
					end
				end
				A.nativeFailed = true -- unserializable config (e.g. modded key with spaces)
			end
		end
		if A.nativeActive then
			local res = Brainstorm.pollNativeSearch()
			if res then
				-- Same SAFETY RAIL as the thread path below: the trusted
				-- main-thread Lua filters must agree before we act on the seed.
				if Brainstorm.passesAllFilters(res.seed) then
					seed_found = res.seed
					jokerFoundAt = res.jokerFoundAt
					Brainstorm.stopNativeSearch()
				else
					Brainstorm.logSeedMismatch(res)
					Brainstorm.stopNativeSearch()
					-- A calibrated helper should never diverge. A pool search is
					-- native-only, so surface the embedded-criteria failure instead
					-- of describing it as a missing helper on the next frame.
					local selected = Brainstorm.effectiveSeedPoolSelection
						and Brainstorm.effectiveSeedPoolSelection()
					if arS.seedPoolFile and arS.seedPoolFile ~= "" then
						A.poolAbort = "pool: in-game verification rejected the native result"
					elseif selected and selected.automatic then
						Brainstorm.retireAutomaticSeedPool(selected)
						A.autoPoolWarned = "automatic pool result failed in-game verification; trying the next safe source"
					else
						A.nativeFailed = true
					end
				end
			end
		end
	elseif useThread then
		if not A.searchThreads or #A.searchThreads == 0 then
			Brainstorm.startSearchThread()
		end
		local res = Brainstorm.pollSearchThread()
		if res then
			-- SAFETY RAIL: re-verify the worker's hit on the MAIN thread, which uses
			-- the trusted inline pools (Brainstorm.CULLED is nil here). If it agrees,
			-- accept and stop; if not, the worker/cache diverged -- log a full
			-- diagnostic and keep the other threads searching instead of starting a
			-- wrong seed. Mismatches should never happen (the paths are RNG-identical),
			-- so this costs one filter pass per find and turns any regression into a
			-- self-correcting, self-reporting event rather than a silent bad seed.
			if Brainstorm.passesAllFilters(res.seed) then
				seed_found = res.seed
				jokerFoundAt = res.jokerFoundAt
				Brainstorm.stopSearchThread()
			else
				Brainstorm.logSeedMismatch(res)
			end
		end
	else
		if A.searchCounterBackend ~= "sync-"
				.. tostring(Brainstorm.SETTINGS.autoreroll.seedsPerFrame or 500) then
			Brainstorm.startSearchBackendCounter("sync-"
				.. tostring(Brainstorm.SETTINGS.autoreroll.seedsPerFrame or 500))
		end
		A.rerollTimer = A.rerollTimer + dt
		if A.rerollTimer >= A.rerollInterval then
			A.rerollTimer = A.rerollTimer - A.rerollInterval
			seed_found = Brainstorm.auto_reroll()
			jokerFoundAt = A.jokerFoundAt
		end
	end
	Brainstorm.updateSearchStats()

	if seed_found then
		-- Search always stops after a find. Destination depends on the setting:
		-- slot 1-5 banks the seed (current run untouched); 0 / "Current run"
		-- overwrites the live run immediately, as before.
		A.autoRerollActive = false
		Brainstorm.resetSearchUI()
		local slot = Brainstorm.SETTINGS.autoreroll.foundSeedSlot or 0
		if slot >= 1 and slot <= 5 then
			Brainstorm.bankFoundSeed(seed_found, slot, jokerFoundAt)
			Brainstorm.showSeedSlotAlert("Seed saved to slot [" .. slot .. "]")
		else
			-- Start at the chosen "Found Stake" (nil-safe: applyFoundSeed falls
			-- back to the current run's stake if the setting is missing).
			Brainstorm.applyFoundSeed(seed_found, Brainstorm.SETTINGS.autoreroll.foundSeedStake)
		end
		if jokerFoundAt then
			-- Stash it for the Ctrl+J hotkey; don't auto-pop the joker text on a
			-- find (it clutters/overlaps the "Seed saved" message).
			A.lastJokerFoundAt = jokerFoundAt
			A.jokerFoundAt = nil
			-- Also persist it keyed by the found seed so Ctrl+J survives a restart
			-- (works whether banked or applied to the current run).
			Brainstorm.recordFoundJoker(seed_found, jokerFoundAt)
		end
		return
	end

	-- Search-in-progress UI phases (time-based so it's framerate-independent):
	--   0 .. BIG_SHOW_AT      : nothing (avoids a flash on instant finds)
	--   BIG_SHOW_AT .. HIDE   : big centered live checked/expected text
	--   after BIG_HIDE_AT     : small spinner in the top-left corner (drawn by
	--                           Brainstorm.draw_search_indicator), no big text
	local BIG_SHOW_AT, BIG_HIDE_AT = 0.25, 2.5
	A.searchElapsed = (A.searchElapsed or 0) + dt
	if not A.bigTextShown and A.searchElapsed >= BIG_SHOW_AT then
		A.bigTextShown = true
		A.rerollText = Brainstorm.attention_text({
			scale = 1.4,
			maxw = 10,
			text = {{ref_table = A.searchHeadline, ref_value = "value"}},
			align = 'cm',
			offset = { x = 0, y = -3.5 },
			major = G.STAGE == G.STAGES.RUN and G.play or G.title_top,
		})
	end
	if A.bigTextShown and not A.bigTextRemoved and A.searchElapsed >= BIG_HIDE_AT then
		A.bigTextRemoved = true
		if A.rerollText then
			Brainstorm.remove_attention_text(A.rerollText)
			A.rerollText = nil
		end
		A.showSearchIndicator = true
	end
end

-- Tears down all search-in-progress UI (big text + corner spinner) and resets
-- the phase flags so the next search starts clean. Safe to call anytime.
function Brainstorm.resetSearchUI()
	local A = Brainstorm.AUTOREROLL
	if A.searchStatsActive then Brainstorm.finishSearchStats() end
	A.searchElapsed = 0
	A.autoRerollFrames = 0
	A.bigTextShown = false
	A.bigTextRemoved = false
	A.showSearchIndicator = false
	if A.rerollText then
		Brainstorm.remove_attention_text(A.rerollText)
		A.rerollText = nil
	end
end

-- Small animated spinner in the top-left corner, shown while a search is running
-- (after the brief "Rerolling..." banner). Drawn straight to the game canvas via
-- a lovely patch in the draw path; origin() puts us in raw canvas pixels so the
-- position is deterministic regardless of the room transform.
function Brainstorm.draw_search_indicator()
	local A = Brainstorm.AUTOREROLL
	if not (A and A.showSearchIndicator) then return end

	love.graphics.push("all")
	love.graphics.origin()
	love.graphics.setShader()
	love.graphics.setBlendMode("alpha")

	local t = (love.timer and love.timer.getTime()) or os.clock()
	local lines = Brainstorm.liveSearchTextLines and Brainstorm.liveSearchTextLines()
		or {"Searching..."}
	local font = love.graphics.getFont()
	local textWidth = 0
	for _, line in ipairs(lines) do
		textWidth = math.max(textWidth, font:getWidth(line))
	end
	local panelX, panelY = 12, 12
	local panelW = 72 + textWidth + 14
	local panelH = math.max(64, 12 + #lines * (font:getHeight() + 2))
	love.graphics.setColor(0.035, 0.025, 0.06, 0.86)
	love.graphics.rectangle("fill", panelX, panelY, panelW, panelH, 9, 9)
	love.graphics.setColor(0.72, 0.58, 1, 0.55)
	love.graphics.setLineWidth(1)
	love.graphics.rectangle("line", panelX + 0.5, panelY + 0.5,
		panelW - 1, panelH - 1, 9, 9)

	local cx, cy, r = 44, panelY + panelH / 2, 16
	local segs = 12
	love.graphics.setLineWidth(3)
	for i = 1, segs do
		local a = (i / segs) * math.pi * 2 - t * 5
		local alpha = 0.12 + 0.88 * (i / segs)
		love.graphics.setColor(1, 1, 1, alpha)
		local inner = r * 0.5
		love.graphics.line(
			cx + math.cos(a) * inner, cy + math.sin(a) * inner,
			cx + math.cos(a) * r, cy + math.sin(a) * r
		)
	end

	local textX = 72
	local textY = panelY + 7
	for i, line in ipairs(lines) do
		if i == 1 then love.graphics.setColor(1, 1, 1, 1)
		elseif i == 2 then love.graphics.setColor(0.83, 0.80, 0.88, 1)
		else love.graphics.setColor(0.74, 0.64, 0.94, 1) end
		love.graphics.print(line, textX, textY + (i - 1) * (font:getHeight() + 2))
	end

	love.graphics.pop()
end

-- ===========================================================================
-- Native helper search (optional, ~10x the Lua workers)
-- ---------------------------------------------------------------------------
-- native/brainstorm_native_search is a C port of passesAllFilters that runs
-- across all cores (build once with native/build.sh). The mod writes a config
-- snapshot (pools with eligibility RESOLVED HERE, settings, and parity checks
-- computed with the game's own RNG functions), spawns the helper detached,
-- and polls a tiny status file at 10 Hz. SAFETY RAILS, in order:
--   1. The helper re-derives nothing: pool eligibility comes from the same
--      joker_is_pool_eligible / voucher rules on this thread.
--   2. Before searching, the helper must reproduce the check_* lines
--      bit-for-bit (this also calibrates how the game's LuaJIT rounded its
--      PRNG seeding); any failure -> it refuses -> we fall back to Lua.
--   3. Every hit is still re-verified by passesAllFilters on the main thread
--      before being applied/banked (same rail as the thread search).
--   4. Any error/timeout/mismatch sets nativeFailed and the existing
--      love.thread search takes over for the rest of the session.
--   5. Liveness: we touch a heartbeat file every ~2s; if the game dies the
--      helper notices the stale heartbeat and exits on its own.
-- Opt out with Brainstorm.SETTINGS.useNativeSearch = false.
-- ===========================================================================

-- love.system.getOS() when available (always, in-game); the package.config
-- fallback (its first byte is the directory separator) keeps this callable
-- from plain-LuaJIT test harnesses.
function Brainstorm.isWindows()
	if love and love.system and love.system.getOS then
		return love.system.getOS() == "Windows"
	end
	return package.config:sub(1, 1) == "\\"
end

function Brainstorm.nativePaths()
	local base = Brainstorm.modPath() .. "/native_search"
	return {
		bin = Brainstorm.modPath() .. "/native/brainstorm_native_search"
			.. (Brainstorm.isWindows() and ".exe" or ""),
		cfg = base .. ".cfg",
		status = base .. ".status",
		stop = base .. ".stop",
		hb = base .. ".hb",
	}
end

function Brainstorm.nativeAvailable()
	if Brainstorm.SETTINGS.useNativeSearch == false then return false end
	local N = Brainstorm.NATIVE_STATE
	if not N then
		local nativefs = require("brainstorm_nativefs")
		N = { binPresent = nativefs.getInfo(Brainstorm.nativePaths().bin) ~= nil }
		Brainstorm.NATIVE_STATE = N
	end
	return N.binPresent
end

-- ---------------------------------------------------------------------------
-- Seed pools (.bspool): exhaustive match sets built by the external
-- native/brainstorm_seed_pool scanner (see README). Selecting one restricts
-- the native search to seeds recorded in the pool; the file lives in
-- <mod>/seed_pools/ so pools can be shared by copying that one file.
-- The Lua fallback search CANNOT honor a pool (it would need the native
-- binary's streaming rank reader), so pool searches never degrade to it --
-- they stop with an alert instead.
-- ---------------------------------------------------------------------------
function Brainstorm.seedPoolDir()
	return Brainstorm.modPath() .. "/seed_pools"
end

-- Identity readout for a .bspool. Schemas 1/2 have a fixed 1 KiB header;
-- event schemas 3/4 advertise a potentially larger header_bytes value in
-- that first KiB. The cap keeps this metadata read bounded even for a
-- malformed pool.
function Brainstorm.readPoolHeader(path)
	local nativefs = require("brainstorm_nativefs")
	local prefixBytes, maxHeaderBytes = 1024, 256 * 1024
	local raw = nativefs.read(path, prefixBytes)
	if not raw or not raw:match("^BRAINSTORM_SEED_POOL ") then return nil end
	local schema = tonumber(raw:match("^BRAINSTORM_SEED_POOL%s+(%d+)[\r\n]"))
	if schema == 3 or schema == 4 then
		local headerBytes
		for line in raw:gmatch("[^\r\n%z]+") do
			headerBytes = tonumber(line:match("^header_bytes%s+(%d+)%s*$")) or headerBytes
		end
		if not headerBytes or headerBytes < prefixBytes or headerBytes > maxHeaderBytes then
			return nil
		end
		if headerBytes > #raw then
			raw = nativefs.read(path, headerBytes)
			if not raw or #raw ~= headerBytes then return nil end
		else
			raw = raw:sub(1, headerBytes)
		end
	end
	local h = {
		tags = {}, route_tags = {}, pool_legendaries = {}, legendaries = {},
		vouchers = {}, route_vouchers = {}, voucher_exclusions = {},
		route_voucher_exclusions = {}, composite_branches = {},
		composite_operands = {},
	}
	local function words(value)
		local out = {}
		for word in value:gmatch("%S+") do out[#out + 1] = word end
		return out
	end
	local function phase(value, allowBoss)
		value = value and value:lower()
		if value == "small" or value == "big" or (allowBoss and value == "boss") then
			return value
		end
	end
	h.schema = schema
	h.metadata_capable = schema == 3 or schema == 4
	h.native_compatible = schema ~= nil and schema >= 1 and schema <= 4
	for line in raw:gmatch("[^\n]+") do
		local k, v = line:match("^(%S+)%s*(.-)%s*$")
		if k == "end" then break end
		if k == "records" or k == "complete" or k == "coverage_complete"
				or k == "modelver" or k == "refilter_depth" or k == "header_bytes"
				or k == "seedspace" or k == "range_start" or k == "range_end"
				or k == "scan_cursor" or k == "input_cursor" or k == "parent_records"
				or k == "parent_data_bytes" or k == "parent_coverage_complete"
				or k == "input_record_start" or k == "input_record_end"
				or k == "shard_index" or k == "shard_total"
				or k == "composite_schema" or k == "composite_inputs"
				or k == "composite_metadata_complete" then h[k] = tonumber(v)
		elseif k == "space" or k == "pool_id" or k == "label"
				or k == "catalog_hash" or k == "criteria_hash" or k == "charset"
				or k == "encoding"
				or k == "tag_route" or k == "family_id"
				or k == "segment_id" or k == "stage_hash" or k == "lineage_id"
				or k == "derivation_id" or k == "snapshot_id"
				or k == "membership_digest" or k == "metadata_digest"
				or k == "parent_snapshot_id" or k == "parent_segment_id"
				or k == "composite_operation" or k == "composite_route_policy"
				or k == "legendary_routes" or k == "route_legendary_routes" then h[k] = v
		elseif k == "composite_branch" then
			h.composite_branches[#h.composite_branches + 1] = v
		elseif k == "composite_operand" then
			h.composite_operands[#h.composite_operands + 1] = v
		elseif k == "tag" then
			local p = words(v)
			local minPhase, maxPhase, count
			if #p == 4 then
				minPhase, maxPhase, count = "small", "big", tonumber(p[4])
			elseif #p == 6 then
				minPhase, maxPhase, count = phase(p[3]), phase(p[5]), tonumber(p[6])
			end
			if p[1] and tonumber(p[2]) and tonumber(p[#p == 4 and 3 or 4])
					and minPhase and maxPhase and count then
				h.tags[#h.tags + 1] = { key = p[1], minAnte = tonumber(p[2]),
					minPhase = minPhase, maxAnte = tonumber(p[#p == 4 and 3 or 4]),
					maxPhase = maxPhase, count = count }
			end
		elseif k == "legendary" then
			local p = words(v)
			local minPhase, maxPhase, neg, source, maxAnte
			if #p == 4 then
				minPhase, maxPhase, maxAnte = "boss", "big", tonumber(p[3])
				neg, source = tonumber(p[4]), "any"
			elseif #p == 7 then
				minPhase, maxPhase, maxAnte = phase(p[3], true), phase(p[5], true), tonumber(p[4])
				neg, source = tonumber(p[6]), p[7]:lower()
			end
			if p[1] and tonumber(p[2]) and maxAnte and minPhase and maxPhase
					and (neg == 0 or neg == 1)
					and (source == "any" or source == "shop" or source == "charm"
						or source == "ethereal") then
				h.legendaries[#h.legendaries + 1] = { key = p[1],
					minAnte = tonumber(p[2]), minPhase = minPhase,
					maxAnte = maxAnte, maxPhase = maxPhase,
					negative = neg == 1, source = source, humanLocation = #p == 7 }
			end
		elseif k == "soul_depth" then
			-- applies to the preceding legendary line; 0 = either Soul
			local rule = h.legendaries[#h.legendaries]
			if rule then rule.soulDepth = (v == "any") and 0 or tonumber(v) end
		elseif k == "route_legendary" then
			local p = words(v)
			local minPhase, maxPhase, neg, source, depth, maxAnte
			if #p == 5 then
				minPhase, maxPhase, maxAnte = "boss", "big", tonumber(p[3])
				neg, source, depth = tonumber(p[4]), "any", tonumber(p[5])
			elseif #p == 8 then
				minPhase, maxPhase, maxAnte = phase(p[3], true), phase(p[5], true), tonumber(p[4])
				neg, source, depth = tonumber(p[6]), p[7]:lower(), tonumber(p[8])
			end
			if p[1] and tonumber(p[2]) and maxAnte and minPhase and maxPhase
					and (neg == 0 or neg == 1) and (depth == 0 or depth == 1 or depth == 2)
					and (source == "any" or source == "shop" or source == "charm"
						or source == "ethereal") then
				h.pool_legendaries[#h.pool_legendaries + 1] = { key = p[1],
					minAnte = tonumber(p[2]), minPhase = minPhase,
					maxAnte = maxAnte, maxPhase = maxPhase,
					negative = neg == 1, source = source, soulDepth = depth,
					humanLocation = #p == 8, inherited = true }
			end
		elseif k == "route_tag" then
			local p = words(v)
			local minPhase, maxPhase, count, maxAnte
			if #p == 5 then
				minPhase, maxPhase, maxAnte, count = "small", "big", tonumber(p[4]), tonumber(p[5])
			elseif #p == 7 then
				minPhase, maxPhase, maxAnte, count = phase(p[4]), phase(p[6]), tonumber(p[5]), tonumber(p[7])
			end
			if (p[1] == "collect" or p[1] == "observe") and p[2]
					and tonumber(p[3]) and maxAnte and minPhase and maxPhase and count then
				h.route_tags[#h.route_tags + 1] = { collect = p[1] == "collect",
					key = p[2], minAnte = tonumber(p[3]), minPhase = minPhase,
					maxAnte = maxAnte, maxPhase = maxPhase, count = count }
			end
		elseif k == "voucher" or k == "route_voucher" then
			local p = words(v)
			if #p == 3 and tonumber(p[2]) and tonumber(p[3]) then
				local list = k == "voucher" and h.vouchers or h.route_vouchers
				list[#list + 1] = { key = p[1], minAnte = tonumber(p[2]),
					maxAnte = tonumber(p[3]) }
			end
		elseif k == "voucher_exclude" or k == "route_voucher_exclude" then
			local p = words(v)
			if #p == 1 then
				local list = k == "voucher_exclude" and h.voucher_exclusions
					or h.route_voucher_exclusions
				list[#list + 1] = p[1]
			end
		end
	end
	local collect = h.tag_route ~= "observe"
	for _, rule in ipairs(h.tags) do
		rule.collect = collect
		h.route_tags[#h.route_tags + 1] = rule
	end
	for _, rule in ipairs(h.legendaries) do
		rule.soulDepth = rule.soulDepth or 1
		rule.legendaryRoutes = h.legendary_routes or "full"
		h.pool_legendaries[#h.pool_legendaries + 1] = rule
	end
	for _, rule in ipairs(h.pool_legendaries) do
		if rule.inherited then
			rule.legendaryRoutes = h.route_legendary_routes or "full"
		else
			rule.legendaryRoutes = rule.legendaryRoutes or h.legendary_routes or "full"
		end
	end
	h.pool_vouchers = {}
	for _, rule in ipairs(h.route_vouchers) do h.pool_vouchers[#h.pool_vouchers + 1] = rule end
	for _, rule in ipairs(h.vouchers) do h.pool_vouchers[#h.pool_vouchers + 1] = rule end
	h.pool_voucher_exclusions = {}
	for _, key in ipairs(h.route_voucher_exclusions) do
		h.pool_voucher_exclusions[#h.pool_voucher_exclusions + 1] = key
	end
	for _, key in ipairs(h.voucher_exclusions) do
		h.pool_voucher_exclusions[#h.pool_voucher_exclusions + 1] = key
	end
	if schema == 4 and h.encoding ~= "adaptive-events-v1" then return nil end
	return h
end

function Brainstorm.poolNativeCompatible(header)
	return header ~= nil and header.schema ~= nil
		and header.schema >= 1 and header.schema <= 4
end

local ATTACHMENT_SEEDSPACE = 1785793904896

local function attachmentWords(value)
	local out = {}
	for word in tostring(value or ""):gmatch("%S+") do out[#out + 1] = word end
	return out
end

-- The Builder binds the canonical predicate list with SHA-256. Verify the
-- same checksum in-game instead of treating signature_hash as decorative
-- metadata. This tiny implementation runs only while discovering attachments;
-- it deliberately has no dependency on LOVE's optional data module so the
-- exact same reader remains testable under bare LuaJIT on Windows and macOS.
local ATTACHMENT_SHA256_CONSTANTS = {
	0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
	0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
	0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
	0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
	0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
	0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
	0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
	0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
	0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
	0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
	0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
	0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
	0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
	0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
	0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
	0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
}
local ATTACHMENT_SHA256_INITIAL = {
	0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
	0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
}

local function attachmentSha256(message)
	local bit = require("bit")
	local band, bnot, bxor = bit.band, bit.bnot, bit.bxor
	local ror, rshift, tobit = bit.ror, bit.rshift, bit.tobit
	local constants, state = ATTACHMENT_SHA256_CONSTANTS, {}
	for i = 1, 8 do state[i] = ATTACHMENT_SHA256_INITIAL[i] end
	local function be32(value)
		value = value % 4294967296
		return string.char(math.floor(value / 16777216) % 256,
			math.floor(value / 65536) % 256, math.floor(value / 256) % 256,
			value % 256)
	end
	local bitLength = #message * 8
	message = message .. "\128"
		.. string.rep("\0", (56 - (#message + 1) % 64) % 64)
		.. be32(math.floor(bitLength / 4294967296)) .. be32(bitLength)
	for offset = 1, #message, 64 do
		local words = {}
		for i = 0, 15 do
			local at = offset + i * 4
			local a, b, c, d = message:byte(at, at + 3)
			words[i] = tobit(a * 16777216 + b * 65536 + c * 256 + d)
		end
		for i = 16, 63 do
			local x, y = words[i - 15], words[i - 2]
			local s0 = bxor(ror(x, 7), ror(x, 18), rshift(x, 3))
			local s1 = bxor(ror(y, 17), ror(y, 19), rshift(y, 10))
			words[i] = tobit(words[i - 16] + s0 + words[i - 7] + s1)
		end
		local a, b, c, d = state[1], state[2], state[3], state[4]
		local e, f, g, h = state[5], state[6], state[7], state[8]
		for i = 0, 63 do
			local sum1 = bxor(ror(e, 6), ror(e, 11), ror(e, 25))
			local choice = bxor(band(e, f), band(bnot(e), g))
			local t1 = tobit(h + sum1 + choice + constants[i + 1] + words[i])
			local sum0 = bxor(ror(a, 2), ror(a, 13), ror(a, 22))
			local majority = bxor(band(a, b), band(a, c), band(b, c))
			local t2 = tobit(sum0 + majority)
			h, g, f, e, d, c, b, a = g, f, e, tobit(d + t1), c, b, a,
				tobit(t1 + t2)
		end
		state[1], state[2], state[3], state[4] = tobit(state[1] + a),
			tobit(state[2] + b), tobit(state[3] + c), tobit(state[4] + d)
		state[5], state[6], state[7], state[8] = tobit(state[5] + e),
			tobit(state[6] + f), tobit(state[7] + g), tobit(state[8] + h)
	end
	local out = {}
	for i = 1, 8 do
		local value = state[i]
		if value < 0 then value = value + 4294967296 end
		out[i] = string.format("%08x", value)
	end
	return table.concat(out)
end

function Brainstorm.poolAttachmentSignatureHash(schema, predicates)
	local body = { "signature_schema " .. tostring(schema) .. "\n" }
	for _, predicate in ipairs(predicates or {}) do
		body[#body + 1] = "predicate " .. tostring(predicate) .. "\n"
	end
	return attachmentSha256(table.concat(body))
end

function Brainstorm.poolAttachmentPredicatesFromHeader(h)
	if not h then return nil end
	local out = {}
	for _, rule in ipairs(h.route_tags or {}) do
		out[#out + 1] = table.concat({ "tag", rule.collect and "collect" or "observe",
			rule.key, rule.minAnte, rule.minPhase, rule.maxAnte, rule.maxPhase,
			rule.count }, " ")
	end
	for _, rule in ipairs(h.pool_legendaries or {}) do
		out[#out + 1] = table.concat({ "legendary", rule.key, rule.minAnte,
			rule.minPhase, rule.maxAnte, rule.maxPhase, rule.negative and 1 or 0,
			rule.source, rule.soulDepth, rule.legendaryRoutes or "full" }, " ")
	end
	for _, rule in ipairs(h.pool_vouchers or {}) do
		out[#out + 1] = table.concat({ "voucher", rule.key, rule.minAnte,
			rule.maxAnte }, " ")
	end
	for _, key in ipairs(h.pool_voucher_exclusions or {}) do
		out[#out + 1] = "voucher_exclude " .. key
	end
	table.sort(out)
	return out
end

local function sameStringList(a, b)
	if not a or not b or #a ~= #b then return false end
	for i = 1, #a do if a[i] ~= b[i] then return false end end
	return true
end

function Brainstorm.readPoolAttachment(markerPath)
	local nativefs = require("brainstorm_nativefs")
	local raw = nativefs.read(markerPath, 256 * 1024)
	if not raw or not raw:match("^BRAINSTORM_POOL_ATTACHMENT 1[\r\n]") then
		return nil, "unsupported or unreadable attachment marker"
	end
	local marker = { predicates = {} }
	local ended = false
	for line in raw:gmatch("[^\r\n]+") do
		local key, value = line:match("^(%S+)%s*(.-)%s*$")
		if key == "end" then ended = true; break end
		if key == "predicate" then
			marker.predicates[#marker.predicates + 1] = value
		elseif key ~= "BRAINSTORM_POOL_ATTACHMENT" then
			if marker[key] ~= nil then return nil, "duplicate attachment field " .. key end
			marker[key] = value
		end
	end
	if not ended or marker.enabled ~= "1" then return nil, "disabled or truncated attachment marker" end
	if marker.role ~= "accelerator" and marker.role ~= "authoritative" then
		return nil, "invalid attachment role"
	end
	if marker.signature_schema ~= "1" then return nil, "unsupported attachment signature" end
	local expectedSignatureHash = Brainstorm.poolAttachmentSignatureHash(
		marker.signature_schema, marker.predicates)
	if not marker.signature_hash
			or marker.signature_hash:lower() ~= expectedSignatureHash then
		return nil, "attachment signature checksum is stale"
	end
	if not marker.pool_file or marker.pool_file == "" or marker.pool_file:find("[\r\n/\\]") then
		return nil, "unsafe attached pool filename"
	end
	local expectedMarker = markerPath:match("([^/\\]+)$")
	if expectedMarker ~= marker.pool_file .. ".attached" then
		return nil, "attachment marker filename does not match its pool"
	end
	local poolPath = Brainstorm.seedPoolDir() .. "/" .. marker.pool_file
	local info = nativefs.getInfo(poolPath)
	local h = info and Brainstorm.readPoolHeader(poolPath)
	if not h then return nil, "attached pool is missing or unreadable" end
	if not Brainstorm.poolNativeCompatible(h) then
		return nil, "attached pool schema is not supported by this Brainstorm build"
	end
	for _, key in ipairs({ "pool_id", "catalog_hash", "criteria_hash", "snapshot_id" }) do
		if not h[key] or h[key] == "" or tostring(h[key]):lower() ~= tostring(marker[key] or ""):lower() then
			return nil, "attachment " .. key .. " no longer matches the pool"
		end
	end
	if not tostring(marker.file_size or ""):match("^%d+$")
			or tonumber(marker.file_size) ~= tonumber(info.size) then
		return nil, "attached pool file size changed"
	end
	if not tostring(marker.file_mtime_ns or ""):match("^%d+$") then
		return nil, "attached pool modification time is invalid"
	end
	if info.modtime and math.floor(tonumber(marker.file_mtime_ns) / 1000000000)
				~= math.floor(tonumber(info.modtime)) then
		return nil, "attached pool modification time changed"
	end
	if h.complete ~= 1 or h.coverage_complete ~= 1 or h.records == nil or h.records <= 0
			or h.modelver ~= 6 or h.space ~= "natural"
			or h.seedspace ~= ATTACHMENT_SEEDSPACE
			or not h.range_start or not h.range_end or h.range_start < 0
			or h.range_start >= h.range_end or h.range_end > ATTACHMENT_SEEDSPACE
			or h.composite_schema then
		return nil, "attached pool is not a complete natural-space conjunction"
	end
	if marker.role == "authoritative"
			and (h.range_start ~= 0 or h.range_end ~= ATTACHMENT_SEEDSPACE) then
		return nil, "authoritative attachment does not cover the full natural seed space"
	end
	local canonical = Brainstorm.poolAttachmentPredicatesFromHeader(h)
	if not sameStringList(marker.predicates, canonical) then
		return nil, "attachment predicates no longer match the pool header"
	end
	marker.path, marker.poolPath, marker.header = markerPath, poolPath, h
	marker.records = h.records
	return marker
end

local ATTACHMENT_MAX_ANTE = 39

local function attachmentPosition(ante, phase)
	local order = ({ small = 0, big = 1, boss = 2 })[phase]
	return ante and order and ((ante - 1) * 3 + order) or nil
end

local function attachmentInteger(value, minimum, maximum)
	local number = tonumber(value)
	if not number or number ~= math.floor(number)
			or number < minimum or number > maximum then return nil end
	return number
end

local function attachmentTagWindowCount(minAnte, minPhase, maxAnte, maxPhase)
	local first, last = attachmentPosition(minAnte, minPhase),
		attachmentPosition(maxAnte, maxPhase)
	if not first or not last or first > last then return 0 end
	local count = 0
	for ante = minAnte, maxAnte do
		for _, phase in ipairs({ "small", "big" }) do
			local position = attachmentPosition(ante, phase)
			if position >= first and position <= last then count = count + 1 end
		end
	end
	return count
end

local function attachmentPredicate(value)
	local p = attachmentWords(value)
	if p[1] == "tag" and #p == 8 then
		local minAnte = attachmentInteger(p[4], 1, ATTACHMENT_MAX_ANTE)
		local maxAnte = attachmentInteger(p[6], 1, ATTACHMENT_MAX_ANTE)
		local count = attachmentInteger(p[8], 1, ATTACHMENT_MAX_ANTE * 2)
		if (p[2] ~= "collect" and p[2] ~= "observe") or not minAnte or not maxAnte
				or (p[5] ~= "small" and p[5] ~= "big")
				or (p[7] ~= "small" and p[7] ~= "big") or not count
				or count > attachmentTagWindowCount(minAnte, p[5], maxAnte, p[7]) then
			return nil
		end
		return { kind = "tag", mode = p[2], key = p[3], minAnte = minAnte,
			minPhase = p[5], maxAnte = maxAnte, maxPhase = p[7], count = count }
	elseif p[1] == "legendary" and #p == 10 then
		local minAnte = attachmentInteger(p[3], 1, ATTACHMENT_MAX_ANTE)
		local maxAnte = attachmentInteger(p[5], 1, ATTACHMENT_MAX_ANTE)
		local depth = attachmentInteger(p[9], 0, 2)
		local validPhase = { small = true, big = true, boss = true }
		local validSource = { any = true, shop = true, charm = true, ethereal = true }
		if not minAnte or not maxAnte or not validPhase[p[4]] or not validPhase[p[6]]
				or not attachmentPosition(minAnte, p[4])
				or attachmentPosition(minAnte, p[4]) > attachmentPosition(maxAnte, p[6])
				or (p[7] ~= "0" and p[7] ~= "1") or not validSource[p[8]] or not depth
				or (p[10] ~= "full" and p[10] ~= "canonical_charm") then return nil end
		return { kind = "legendary", key = p[2], minAnte = minAnte,
			minPhase = p[4], maxAnte = maxAnte, maxPhase = p[6],
			negative = p[7] == "1", source = p[8], depth = depth, routes = p[10] }
	elseif p[1] == "voucher" or p[1] == "voucher_exclude" then
		return { kind = p[1], unsupported = true }
	end
	return nil
end

function Brainstorm.activeAttachmentPredicates()
	local ar = Brainstorm.SETTINGS.autoreroll or {}
	local active = {}
	if ar.searchTag and ar.searchTag ~= "" and not ar.searchTagAnywhere then
		active[#active + 1] = { kind = "tag", mode = "collect", key = ar.searchTag,
			minAnte = 1, minPhase = "small", maxAnte = 1,
			maxPhase = "small", count = 1 }
	end
	if ar.searchLegendary and ar.searchLegendary ~= ""
			and not ar.searchLegendaryAnywhere and (ar.searchForSoul or 0) <= 1 then
		active[#active + 1] = { kind = "legendary", key = ar.searchLegendary,
			minAnte = 1, minPhase = "small", maxAnte = 1, maxPhase = "small",
			negative = ar.searchNegativeLegendary and true or false,
			source = "charm", depth = 1, routes = "canonical_charm" }
	end
	return active
end

local function activeImpliesPoolPredicate(active, pool)
	if not active or not pool or active.kind ~= pool.kind or active.key ~= pool.key then return false end
	local amin, amax = attachmentPosition(active.minAnte, active.minPhase),
		attachmentPosition(active.maxAnte, active.maxPhase)
	local pmin, pmax = attachmentPosition(pool.minAnte, pool.minPhase),
		attachmentPosition(pool.maxAnte, pool.maxPhase)
	if not amin or not amax or not pmin or not pmax or amin ~= pmin or amax > pmax then
		return false
	end
	if pool.kind == "tag" then
		-- Equal route starts preserve which occurrence is collected; extending
		-- only the end cannot change an active request's first matching tag.
		return active.mode == pool.mode and active.count >= (pool.count or 1)
	end
	if pool.kind == "legendary" then
		if active.source ~= pool.source then return false end -- widen only after differential proof
		if pool.negative and not active.negative then return false end
		if pool.depth ~= 0 and pool.depth ~= active.depth then return false end
		if pool.routes == "canonical_charm" and active.routes ~= "canonical_charm" then return false end
		return pool.routes == "full" or pool.routes == "canonical_charm"
	end
	return false
end

function Brainstorm.attachmentMatchesActiveFilters(marker)
	local active = Brainstorm.activeAttachmentPredicates()
	if #active == 0 then return false end
	local decoded, hasTag, hasLegendary = {}, false, false
	for _, encoded in ipairs(marker.predicates or {}) do
		local pool = attachmentPredicate(encoded)
		if not pool or pool.unsupported then return false end
		decoded[#decoded + 1] = pool
		hasTag = hasTag or pool.kind == "tag"
		hasLegendary = hasLegendary or pool.kind == "legendary"
	end
	-- The classic in-game Legendary filter assumes an A1-Small Charm reward.
	-- A combined pool must prove that exact physical tag; pairing the same
	-- Legendary request with Rare/Negative/etc. would replay a different route.
	if hasTag and hasLegendary then
		for _, pool in ipairs(decoded) do
			if pool.kind == "tag" and pool.key ~= "tag_charm" then return false end
			if pool.kind == "legendary" and pool.source ~= "charm" then return false end
		end
	end
	for _, pool in ipairs(decoded) do
		local implied = false
		for _, request in ipairs(active) do
			if activeImpliesPoolPredicate(request, pool) then implied = true; break end
		end
		if not implied then return false end
	end
	return true
end

function Brainstorm.findAutomaticSeedPool()
	local ar = Brainstorm.SETTINGS.autoreroll or {}
	if ar.seedPoolFile and ar.seedPoolFile ~= "" then return nil end
	local nativefs = require("brainstorm_nativefs")
	local dir = Brainstorm.seedPoolDir()
	local choices = {}
	local tried = Brainstorm.AUTOREROLL.autoPoolTried or {}
	local okItems, items = pcall(nativefs.getDirectoryItems, dir)
	for _, name in ipairs(okItems and items or {}) do
		if name:match("%.bspool%.attached$") then
			local okMarker, marker, reason = pcall(
				Brainstorm.readPoolAttachment, dir .. "/" .. name)
			if not okMarker then marker, reason = nil, "attachment validation failed" end
			if marker and not tried[marker.path]
					and Brainstorm.attachmentMatchesActiveFilters(marker) then
				choices[#choices + 1] = marker
			elseif reason then
				-- Invalid automatic markers are non-fatal; native validation remains
				-- authoritative for a marker that passes this bounded Lua preflight.
				Brainstorm.AUTOREROLL.autoPoolInvalid = reason
			end
		end
	end
	table.sort(choices, function(a, b)
		-- ATTACHED_SEED_POOLS.md: a compatible authoritative pool wins. Its
		-- exhaustion is definitive, so a miss ends at the pool instead of
		-- falling back to a full unrestricted scan; an accelerator's smaller
		-- time-to-first-hit can never repay that worst case.
		if a.role ~= b.role then return a.role == "authoritative" end
		if a.records ~= b.records then return a.records < b.records end
		if a.pool_id ~= b.pool_id then return tostring(a.pool_id) < tostring(b.pool_id) end
		return a.pool_file < b.pool_file
	end)
	return choices[1]
end

-- The settings cycler must store the exact filename, but rendering that raw
-- filename lets a long shared-pool name widen Balatro's option-cycle node and
-- push the whole settings tab off-screen.  Keep display labels bounded and
-- return an explicit label -> filename map.  Empty filename is the normal
-- automatic mode: a compatible authoritative attachment wins; otherwise an
-- accelerator can fall back to the next safe source and unrestricted search.
-- create_option_cycle applies w as minw, not a fixed width, so the DynaText
-- inside stretches the bar whenever the option string is longer. Keeping
-- every option at or under this width means the Seed Pool selector renders
-- at exactly w = 4 like its neighbours no matter which pool is chosen.
-- Measured at scale 0.7: bar text room is about 229px at ~12.8px/char.
-- The full name and label are shown on the summary rows beneath it.
local SEED_POOL_OPTION_MAX = 17
-- Measured budget for one row of the pool summary. These rows are single
-- non-wrapping text nodes; a row wider than this drives the settings panel
-- wider than the visible screen and drags the whole layout off centre. The
-- default "Automatic: compatible attached pools first" (42) sits centred, a
-- 66-character row already pulls it about 75px off, and ~140 pulls it ~230px.
-- Keep every row at or under this and the panel geometry stays put.
local SEED_POOL_INFO_ROW_MAX = 48
local SEED_POOL_INFO_LABEL_MAX = SEED_POOL_INFO_ROW_MAX

function Brainstorm.seedPoolOptionLabel(name, header, suffix)
	local value = header and header.label or ""
	if not value or value == "" then
		value = tostring(name or ""):gsub("%.bspool$", "")
	end
	value = value:gsub("[%c]", " "):gsub("^%s+", ""):gsub("%s+$", "")
	if value == "" then value = "Unnamed pool" end
	suffix = suffix and tostring(suffix):sub(1, 8) or ""
	local tail = suffix ~= "" and (" [" .. suffix .. "]") or ""
	local room = math.max(4, SEED_POOL_OPTION_MAX - #tail)
	if #value > room then value = value:sub(1, room - 3) .. "..." end
	return value .. tail
end

function Brainstorm.buildSeedPoolOptions(items, saved, readHeader)
	local filenames = {}
	for _, name in ipairs(items or {}) do
		if type(name) == "string" and name:match("%.bspool$") then
			filenames[#filenames + 1] = name
		end
	end
	table.sort(filenames)
	local options = {"Automatic"}
	local files = {["Automatic"] = ""}
	local current, seenSaved = 1, false
	local function append(name, missing)
		local header
		if readHeader and not missing then
			local ok, value = pcall(readHeader, name)
			if ok then header = value end
		end
		local label = Brainstorm.seedPoolOptionLabel(name, header)
		if files[label] ~= nil then
			local identity = header and header.pool_id or ""
			identity = identity and identity:sub(1, 4) or ""
			if identity == "" then identity = tostring(#options) end
			label = Brainstorm.seedPoolOptionLabel(name, header, identity)
			local collision = 2
			while files[label] ~= nil do
				label = Brainstorm.seedPoolOptionLabel(name, header,
					identity .. "-" .. tostring(collision))
				collision = collision + 1
			end
		end
		options[#options + 1] = label
		files[label] = name
		if name == saved then current, seenSaved = #options, true end
	end
	for _, name in ipairs(filenames) do append(name, false) end
	-- Preserve an explicitly selected file that is temporarily missing.  This
	-- retains the existing safety rule: disappearance must not silently broaden
	-- a manual pool search into an unrestricted one.
	if saved and saved ~= "" and not seenSaved then append(saved, true) end
	return options, files, current
end

-- One-line description shown under the in-game Seed Pool selector; the
-- pool_id prefix is the shareable identity mark (same file => same id).
function Brainstorm.poolInfoString(name)
	if not name or name == "" then return "Automatic: compatible attached pools first" end
	local h = Brainstorm.readPoolHeader(Brainstorm.seedPoolDir() .. "/" .. name)
	if not h then return "(pool file missing or unreadable)" end
	local bits = {}
	if not Brainstorm.poolNativeCompatible(h) then
		bits[#bits + 1] = "UNSUPPORTED POOL SCHEMA"
	end
	if h.pool_id and h.pool_id ~= "" then bits[#bits + 1] = "id " .. h.pool_id:sub(1, 8) end
	if h.space == "settable" then
		bits[#bits + 1] = "all vanilla-settable seeds (no 0)"
	elseif h.space == "total" then
		bits[#bits + 1] = "all possible seeds (includes 0)"
	end
	if h.composite_schema then
		local operation = (h.composite_operation or "composite"):upper()
		local inputCount = #(h.composite_operands or {})
		if inputCount == 0 then inputCount = #(h.composite_branches or {}) end
		bits[#bits + 1] = operation .. " of "
			.. tostring(inputCount) .. " input pools"
		bits[#bits + 1] = tostring(#(h.composite_branches or {})) .. " source filters"
		bits[#bits + 1] = "membership-only route"
		if h.composite_metadata_complete == 0 then
			bits[#bits + 1] = "some exact locations unavailable"
		end
	end
	local hasSoul1, hasSoul2, hasEither = false, false, false
	for _, rule in ipairs(h.pool_legendaries or {}) do
		local depth = rule.soulDepth or 1
		if depth == 2 then hasSoul2 = true
		elseif depth == 0 then hasEither = true
		else hasSoul1 = true end
	end
	if hasEither then bits[#bits + 1] = "Soul #1 or #2"
	elseif hasSoul2 then bits[#bits + 1] = hasSoul1 and "Souls #1 & #2" or "Soul #2 only" end
	if h.legendary_routes == "canonical_charm" then
		bits[#bits + 1] = "fast exact Legendary routes (Shop + Charm)"
	elseif h.route_legendary_routes == "canonical_charm" then
		bits[#bits + 1] = "fast exact Legendary ancestry"
	end
	if h.records == 0 then
		bits[#bits + 1] = "EMPTY RESULT (not searchable)"
	elseif h.records then
		bits[#bits + 1] = tostring(h.records) .. " seeds"
	end
	if h.complete ~= 1 then
		bits[#bits + 1] = "PARTIAL SCAN"
	elseif h.coverage_complete == 0 then
		bits[#bits + 1] = "FILTERED SNAPSHOT (source incomplete)"
	else
		bits[#bits + 1] = "complete"
	end
	bits[#bits + 1] = "+ current filters"
	-- Narrow separators first, then drop trailing detail until the row fits.
	-- Everything ahead of the drop point is ordered most- to least-critical by
	-- the assembly above, so a long pool loses "+ current filters" before it
	-- loses its seed count or completeness.
	local line = table.concat(bits, " | ")
	while #line > SEED_POOL_INFO_ROW_MAX and #bits > 1 do
		table.remove(bits)
		line = table.concat(bits, " | ")
	end
	return line
end

-- The selected pool's label, as its own display row. Empty when the pool has
-- no label, when the label merely repeats the filename, or when nothing is
-- selected -- the caller hides the row in those cases.
function Brainstorm.poolInfoLabel(name)
	if not name or name == "" then return "" end
	local h = Brainstorm.readPoolHeader(Brainstorm.seedPoolDir() .. "/" .. name)
	if not h then return "" end
	local label = h.label
	if not label or label == "" or (label .. ".bspool") == name then return "" end
	if #label > SEED_POOL_INFO_LABEL_MAX then
		label = label:sub(1, SEED_POOL_INFO_LABEL_MAX - 3) .. "..."
	end
	return '"' .. label .. '"'
end

-- Selected pool's full path, or nil when no pool is selected. Existence is
-- intentionally NOT cached: the user may drop a file in while the game runs.
function Brainstorm.seedPoolPath()
	local ar = Brainstorm.SETTINGS.autoreroll
	local name = ar and ar.seedPoolFile
	if not name or name == "" then return nil end
	local nativefs = require("brainstorm_nativefs")
	local p = Brainstorm.seedPoolDir() .. "/" .. name
	if not nativefs.getInfo(p) then return nil end
	return p
end

-- The saved selector remains strictly manual. Automatic attachments live only
-- for the current search session and never rewrite that setting.
function Brainstorm.effectiveSeedPoolSelection()
	local manual = Brainstorm.seedPoolPath()
	if manual then return { path = manual, role = "manual", automatic = false } end
	local selected = Brainstorm.AUTOREROLL and Brainstorm.AUTOREROLL.autoPoolSelection
	if selected then
		return { path = selected.poolPath, role = selected.role, automatic = true,
			name = selected.pool_file, pool_file = selected.pool_file,
			markerPath = selected.path, pool_id = selected.pool_id,
			header = selected.header }
	end
	return nil
end

function Brainstorm.revalidateAutomaticSeedPool(selected)
	if not selected or not selected.automatic or not selected.markerPath then
		return nil, "automatic attachment identity is unavailable"
	end
	local ok, fresh, reason = pcall(Brainstorm.readPoolAttachment, selected.markerPath)
	if not ok or not fresh then
		return nil, reason or "automatic attachment validation failed"
	end
	if fresh.role ~= selected.role or fresh.poolPath ~= selected.path
			or fresh.pool_file ~= selected.pool_file
			or (selected.pool_id and fresh.pool_id ~= selected.pool_id)
			or not Brainstorm.attachmentMatchesActiveFilters(fresh) then
		return nil, "automatic attachment changed after search selection"
	end
	return fresh
end

function Brainstorm.retireAutomaticSeedPool(selected)
	local A = Brainstorm.AUTOREROLL
	local markerPath = selected and (selected.markerPath or selected.path)
	if markerPath then
		A.autoPoolTried = A.autoPoolTried or {}
		A.autoPoolTried[markerPath] = true
	end
	A.autoPoolSelection = nil
end

function Brainstorm.effectiveSeedPoolPath()
	local selected = Brainstorm.effectiveSeedPoolSelection()
	return selected and selected.path or nil
end

-- Parity checks: values computed with the GAME's own pseudohash /
-- math.randomseed / math.random / round13. The helper refuses to search
-- unless it reproduces every one bit-for-bit. Fixed PRNG probes at the end
-- deliberately distinguish fused from separate rounding in LuaJIT's seeding
-- step; ordinary pseudohash-derived inputs can make both modes look correct.
function Brainstorm.buildNativeChecks()
	local L = {}
	local function g17(x) return string.format("%.17g", x) end
	local seeds = {}
	for i = 1, 16 do seeds[i] = "PARITY" .. string.char(64 + i) .. i end
	for _, s in ipairs(seeds) do
		local h = pseudohash(s)
		L[#L + 1] = "check_ph " .. s .. " " .. g17(h)
		local v = (2.134453429141 + h * 1.72431234) % 1
		L[#L + 1] = "check_r13 " .. g17(v) .. " " .. g17(math.abs(tonumber(string.format("%.13f", v))))
		math.randomseed(h)
		L[#L + 1] = "check_pr " .. g17(h) .. " " .. g17(math.random())
		math.randomseed(h)
		L[#L + 1] = "check_prn " .. g17(h) .. " 24 " .. g17(math.random(24))
	end
	-- These values are known to differ between the two supported seeding
	-- modes. Keep them in sync with tests/align_snapshot_prng.lua.
	for _, h in ipairs({ 0.6051828282731726, 0.39349437354872258 }) do
		math.randomseed(h)
		L[#L + 1] = "check_pr " .. g17(h) .. " " .. g17(math.random())
	end
	return L
end

-- Serialize pools + settings to the helper's line format. Returns nil if any
-- key would break the whitespace-delimited format (helper then never runs).
function Brainstorm.buildNativeConfigText(session)
	local ar = Brainstorm.SETTINGS.autoreroll
	local ma = Brainstorm.SETTINGS.multiAnteSearch or {}
	local L = {}
	local bad = false
	local function ck(k)
		if type(k) ~= "string" or k == "" or #k > 63 or k:find("%s") then
			bad = true; return "-"
		end
		return k
	end
	local function add(...) L[#L + 1] = table.concat({ ... }, " ") end
	local function g17(x) return string.format("%.17g", x) end

	add("session", tostring(session))
	add("threads", tostring(Brainstorm.getSearchThreadCount()))
	-- Model 6 composes selected/overlay skips and their immediate Charm/Ethereal
	-- reward packs into one chronological Soul route. Older helpers would omit
	-- those events, so the version handshake must reject them.
	add("modelver", "6")
	add("entropy", g17((love.timer and love.timer.getTime and love.timer.getTime() or os.clock()) * 1000))
	add("soul", tostring(ar.searchForSoul or 0))
	add("legendary", (ar.searchLegendary and ar.searchLegendary ~= "") and ck(ar.searchLegendary) or "-")
	add("neglegendary", ar.searchNegativeLegendary and "1" or "0")
	add("tag", (ar.searchTag and ar.searchTag ~= "") and ck(ar.searchTag) or "-")
	add("voucher", (ar.searchVoucher and ar.searchVoucher ~= "") and ck(ar.searchVoucher) or "-")
	add("voucherante", tostring(ar.searchVoucherAnte or 1))
	add("taganywhere", ar.searchTagAnywhere and "1" or "0")
	add("leganywhere", ar.searchLegendaryAnywhere and "1" or "0")
	add("matchany", ar.jokerSearchMatchAny and "1" or "0")
	for i = 1, 3 do
		local s = ar.jokerSlotData and ar.jokerSlotData[i]
		local k = (s and s.key and s.key ~= "") and ck(s.key) or "-"
		add("jslot", tostring(i), k, (s and s.requireNegative) and "1" or "0")
	end
	-- Per-ante window resolved HERE (anywhere mode -> uniform antes 1-8), so
	-- the helper never needs to know about UI modes.
	local anteSlots, antePacks, _, packSlots = Brainstorm.effectiveMultiAnte()
	local sl, pk = {}, {}
	for a = 1, 8 do
		sl[a] = tostring(anteSlots[a] or 0)
		pk[a] = antePacks[a] and "1" or "0"
	end
	add("maslots", sl[1], sl[2], sl[3], sl[4], sl[5], sl[6], sl[7], sl[8])
	add("mapacks", pk[1], pk[2], pk[3], pk[4], pk[5], pk[6], pk[7], pk[8])
	add("packslots", tostring(packSlots))
	if ar.searchPack then
		for _, k in ipairs(ar.searchPack) do add("pack", ck(k)) end
	end
	-- Seed-pool restriction: the helper only considers seeds recorded in this
	-- .bspool. The directive consumes the rest of the line (mod paths contain
	-- spaces), so it bypasses ck(); only a newline could break the format.
	local poolPath = Brainstorm.effectiveSeedPoolPath and Brainstorm.effectiveSeedPoolPath()
	if poolPath then
		if poolPath:find("\n") then return nil end
		L[#L + 1] = "poolfile " .. poolPath
	end

	-- Pools, eligibility resolved with the exact rules the Lua filters use.
	-- tagdef: key, requires-discovered (profile state resolved here; worker
	-- snapshots carry it pre-resolved as requiresOk), min_ante.
	for _, v in ipairs(G.P_CENTER_POOLS["Tag"]) do
		local reqOk
		if v.requiresOk ~= nil then
			reqOk = v.requiresOk and 1 or 0
		elseif not v.requires then
			reqOk = 1
		else
			reqOk = (G.P_CENTERS and G.P_CENTERS[v.requires] and G.P_CENTERS[v.requires].discovered) and 1 or 0
		end
		if v.no_pool_flag and G.GAME.pool_flags[v.no_pool_flag] then reqOk = 0 end
		if v.yes_pool_flag and not G.GAME.pool_flags[v.yes_pool_flag] then reqOk = 0 end
		if G.GAME.banned_keys and G.GAME.banned_keys[v.key] then reqOk = 0 end
		add("tagdef", ck(v.key), tostring(reqOk), tostring(v.min_ante or 0))
	end
	local startingVouchers = Brainstorm.startingVoucherSet()
	local voucherKeys = {}
	for _, v in ipairs(G.P_CENTER_POOLS["Voucher"]) do voucherKeys[v.key] = true end
	for _, v in ipairs(G.P_CENTER_POOLS["Voucher"]) do
		local prerequisitesMet = not v.requires
		if type(v.requires) == "table" and #v.requires > 0 then
			prerequisitesMet = true
			for _, key in ipairs(v.requires) do
				if not startingVouchers[key] then prerequisitesMet = false; break end
			end
		end
		local eligible = v.unlocked ~= false and prerequisitesMet
			and not startingVouchers[v.key]
		local avail = (eligible and not (G.GAME.banned_keys and G.GAME.banned_keys[v.key])) and "1" or "0"
		add("vouchdef", ck(v.key), avail)
		-- The standalone pool builder can deliberately buy earlier offers, so
		-- it needs dynamic prerequisite data instead of the legacy
		-- "redeem-nothing" availability above. Base-game vouchers have at most
		-- one prerequisite; refuse unusual multi-prerequisite modded entries
		-- until their route state can be represented without ambiguity.
		local prerequisite = "-"
		local routeEligible = v.unlocked ~= false
			and not (G.GAME.banned_keys and G.GAME.banned_keys[v.key])
		if v.no_pool_flag and G.GAME.pool_flags[v.no_pool_flag] then routeEligible = false end
		if v.yes_pool_flag and not G.GAME.pool_flags[v.yes_pool_flag] then routeEligible = false end
		if v.requires then
			if type(v.requires) == "table" and #v.requires == 1
					and voucherKeys[v.requires[1]] then
				prerequisite = v.requires[1]
			else routeEligible = false end
		end
		add("vouchroute", ck(v.key), routeEligible and "1" or "0", ck(prerequisite))
		if startingVouchers[v.key] then add("vouchowned", ck(v.key)) end
	end
	for r = 1, 4 do
		local pool = G.P_JOKER_RARITY_POOLS[r]
		if pool then
			for _, v in ipairs(pool) do
				add("jokerdef", tostring(r), ck(v.key), Brainstorm.joker_is_pool_eligible(v) and "1" or "0")
			end
		end
	end
	for _, v in ipairs(G.P_CENTER_POOLS["Booster"]) do
		local soulkind = (v.kind == "Arcana") and "A" or (v.kind == "Spectral") and "S" or "N"
		add("boostdef", ck(v.key), g17(v.weight or 1), (v.kind == "Buffoon") and "1" or "0",
			tostring(Brainstorm.packCardCount(v)), soulkind,
			(G.GAME.banned_keys and G.GAME.banned_keys[v.key]) and "0" or "1")
	end
	add("specialdef",
		(G.GAME.banned_keys and G.GAME.banned_keys.c_soul) and "0" or "1",
		(G.GAME.banned_keys and G.GAME.banned_keys.c_black_hole) and "0" or "1")

	for _, line in ipairs(Brainstorm.buildNativeChecks()) do L[#L + 1] = line end
	add("end")
	if bad then return nil end
	return table.concat(L, "\n") .. "\n"
end

local NATIVE_STATUS_POLL_INTERVAL = 0.1
local NATIVE_HEARTBEAT_INTERVAL = 2

local function nativeSearchNow()
	if love and love.timer and love.timer.getTime then
		return love.timer.getTime()
	end
	return os.clock()
end

function Brainstorm.startNativeSearch()
	local A = Brainstorm.AUTOREROLL
	local nativefs = require("brainstorm_nativefs")
	local p = Brainstorm.nativePaths()
	local ar = Brainstorm.SETTINGS.autoreroll or {}
	if ar.seedPoolFile and ar.seedPoolFile ~= "" then
		A.autoPoolSelection = nil
	elseif not A.autoPoolDisabled then
		A.autoPoolSelection = Brainstorm.findAutomaticSeedPool
			and Brainstorm.findAutomaticSeedPool() or nil
	else
		A.autoPoolSelection = nil
	end
	if Brainstorm.setAttachedPoolEstimateMode then
		Brainstorm.setAttachedPoolEstimateMode(A.autoPoolSelection ~= nil)
	end
	local selectedPool = Brainstorm.effectiveSeedPoolSelection
		and Brainstorm.effectiveSeedPoolSelection() or nil
	if selectedPool then
		local header = selectedPool.header
			or Brainstorm.readPoolHeader(selectedPool.path)
		if header and not Brainstorm.poolNativeCompatible(header) then
			A.poolAbort = "pool: unsupported pool schema"
			return false
		end
	end
	A.searchSession = (A.searchSession or 0) + 1
	local cfg = Brainstorm.buildNativeConfigText(A.searchSession)
	if not cfg then return false end
	os.remove(p.status)
	os.remove(p.stop)
	nativefs.write(p.hb, tostring(os.time()))
	nativefs.write(p.cfg, cfg)
	if Brainstorm.isWindows() then
		-- cmd.exe (os.execute) would flash a console window and re-parse the
		-- quoting; win_spawn.lua goes straight to CreateProcessA instead.
		local N = Brainstorm.NATIVE_STATE or {}
		if N.winSpawn == nil then
			local okLoad, mod = pcall(function()
				return assert(load(nativefs.read(
					Brainstorm.modPath() .. "/win_spawn.lua"), "win_spawn.lua"))()
			end)
			N.winSpawn = okLoad and mod or false
			Brainstorm.NATIVE_STATE = N
		end
		if not N.winSpawn then
			print("[Brainstorm] win_spawn.lua could not be loaded; native search disabled")
			return false
		end
		local okSpawn, serr = N.winSpawn.spawn({ p.bin, "search", p.cfg, p.status, p.stop, p.hb })
		if not okSpawn then
			print("[Brainstorm] native helper spawn failed: " .. tostring(serr))
			return false
		end
	else
		local function shq(s) return "'" .. s:gsub("'", "'\\''") .. "'" end
		os.execute(shq(p.bin) .. " search " .. shq(p.cfg) .. " " .. shq(p.status)
			.. " " .. shq(p.stop) .. " " .. shq(p.hb) .. " >/dev/null 2>&1 &")
	end
	if Brainstorm.startSearchBackendCounter then
		local threads = Brainstorm.getSearchThreadCount()
		local backend = Brainstorm.nativeSearchBackendKey
			and Brainstorm.nativeSearchBackendKey(threads)
			or ("native-" .. tostring(threads))
		local selected = Brainstorm.effectiveSeedPoolSelection()
		if selected then backend = backend .. "-pool-" .. tostring(selected.role) end
		Brainstorm.startSearchBackendCounter(backend)
	end
	A.nativeActive = true
	local now = nativeSearchNow()
	A.nativeStartedAt = now
	A.nativeLastStatusPollAt = nil -- poll once immediately after launch
	A.nativeLastHeartbeatAt = now
	return true
end

-- Poll the helper's status file. Returns {seed, jokerFoundAt, session} on a
-- hit. Maintains the heartbeat; converts helper errors / silence into a clean
-- fallback (A.nativeFailed) instead of a stuck search.
function Brainstorm.pollNativeSearch()
	local A = Brainstorm.AUTOREROLL
	if not A.nativeActive then return nil end
	local nativefs = require("brainstorm_nativefs")
	local p = Brainstorm.nativePaths()
	local now = nativeSearchNow()

	-- These used to be frame-counted, which made both rates depend on refresh
	-- rate and read the status file every rendered frame. The helper publishes
	-- at a much lower rate, so a 10 Hz read keeps terminal messages responsive
	-- while avoiding redundant filesystem work.
	local lastHeartbeat = A.nativeLastHeartbeatAt
	if not lastHeartbeat or now < lastHeartbeat then
		A.nativeLastHeartbeatAt = now
	elseif now - lastHeartbeat >= NATIVE_HEARTBEAT_INTERVAL then
		nativefs.write(p.hb, tostring(os.time()))
		A.nativeLastHeartbeatAt = now
	end
	local lastPoll = A.nativeLastStatusPollAt
	if lastPoll and now >= lastPoll
			and now - lastPoll < NATIVE_STATUS_POLL_INTERVAL then
		return nil
	end
	A.nativeLastStatusPollAt = now

	local txt = nativefs.read(p.status)
	if not txt then
		if now - (A.nativeStartedAt or now) > 5 then
			print("[Brainstorm] native search wrote no status in 5s; using Lua threads instead")
			Brainstorm.stopNativeSearch()
			A.nativeFailed = true
		end
		return nil
	end
	local tried = txt:match("^P (%d+)") or txt:match("\nP (%d+)")
	if tried then A.searchTried = tonumber(tried) end
	local wmsg = txt:match("^W ([^\n]+)") or txt:match("\nW ([^\n]+)")
	if wmsg and wmsg ~= A.nativeWarned then
		A.nativeWarned = wmsg
		print("[Brainstorm] native search: " .. wmsg)
	end
	local emsg = txt:match("^E ([^\n]+)") or txt:match("\nE ([^\n]+)")
	if emsg then
		local selected = Brainstorm.effectiveSeedPoolSelection
			and Brainstorm.effectiveSeedPoolSelection()
		Brainstorm.stopNativeSearch()
		if emsg:find("^pool") and selected and selected.automatic then
			if selected.role == "authoritative"
					and emsg:find("no seed in the pool", 1, true) then
				local fresh, revalidateError = Brainstorm.revalidateAutomaticSeedPool(selected)
				if fresh then
					A.autoPoolAbort = "No seed in the attached authoritative pool matches these filters"
				else
					Brainstorm.retireAutomaticSeedPool(selected)
					A.autoPoolWarned = "automatic authoritative pool changed during search; trying the next safe source"
					print("[Brainstorm] " .. A.autoPoolWarned .. ": "
						.. tostring(revalidateError))
				end
			else
				Brainstorm.retireAutomaticSeedPool(selected)
				if not A.autoPoolWarned then
					A.autoPoolWarned = "automatic " .. tostring(selected.role)
						.. " pool unavailable or exhausted; trying the next safe source"
					print("[Brainstorm] " .. A.autoPoolWarned .. ": " .. emsg)
				end
			end
			elseif selected and selected.automatic then
				A.autoPoolSelection = nil
				A.autoPoolDisabled = true
				A.autoPoolWarned = "automatic pool search failed; continuing unrestricted"
				if Brainstorm.setAttachedPoolEstimateMode then
					Brainstorm.setAttachedPoolEstimateMode(false)
				end
				print("[Brainstorm] " .. A.autoPoolWarned .. ": " .. emsg)
				A.nativeFailed = true
		elseif emsg:find("^pool") then
			-- Pool problems (bad/missing/exhausted .bspool) must NOT degrade to
			-- the full-space Lua search: that would return seeds outside the
			-- pool. updateAutoReroll stops the search and shows this message.
			print("[Brainstorm] native search: " .. emsg)
			A.poolAbort = emsg
		else
			print("[Brainstorm] native search: " .. emsg .. " -- using Lua threads instead")
			A.nativeFailed = true
		end
		return nil
	end
	local seed, label = txt:match("^R (%S+) ([^\n]+)")
	if not seed then seed, label = txt:match("\nR (%S+) ([^\n]+)") end
	if seed then
		return { seed = seed, jokerFoundAt = (label ~= "-") and label or nil, session = A.searchSession }
	end
	if txt:match("^D\n") or txt:match("\nD\n") then
		A.nativeActive = false -- ran out (hard cap) without a hit: relaunch next frame
	end
	return nil
end

function Brainstorm.stopNativeSearch()
	local A = Brainstorm.AUTOREROLL
	if not A.nativeActive then return end
	local nativefs = require("brainstorm_nativefs")
	nativefs.write(Brainstorm.nativePaths().stop, "1")
	A.nativeActive = false
	A.nativeLastStatusPollAt = nil
	A.nativeLastHeartbeatAt = nil
end

-- Worker thread source. Runs in its own Lua state: no G, no love modules except
-- what it require()s. It rebuilds a minimal G from the serialized snapshot,
-- defines the game's pure RNG globals, then loads Brainstorm_reroll.lua to get
-- the identical filter suite and loops generating + testing seeds off the main
-- thread. See Brainstorm.startSearchThread for the args passed in.
Brainstorm.SEARCH_WORKER_SRC = [==[
require("love.thread")
pcall(require, "love.timer")

local configStr, rerollSrc, threadIndex, numThreads = ...
threadIndex = threadIndex or 0
numThreads = numThreads or 1

package.preload["lovely"] = function() return { mod_dir = "" } end
package.preload["brainstorm_nativefs"] = function()
	return { write = function() end, read = function() return "" end, getInfo = function() return nil end }
end

-- Math verbatim from Balatro functions/misc_functions.lua (library functions
-- hoisted into locals -- pure lookup savings, no numeric change). Global
-- math.random is LuaJIT's and identical across thread states (the game never
-- overrides it), so these reproduce the game's RNG exactly.
local string_byte, string_char, math_pi = string.byte, string.char, math.pi
local math_random, math_randomseed = math.random, math.randomseed

function pseudohash(str)
	local num = 1
	for i = #str, 1, -1 do
		num = ((1.1239285023 / num) * string_byte(str, i) * math_pi + math_pi * i) % 1
	end
	return num
end
-- Hot-path optimized, but RNG-identical to the game's pseudorandom_element.
-- The joker/voucher pools we pass are plain contiguous arrays with STRING values
-- ('key' or 'UNAVAILABLE'). For those the original sorts by integer key (a no-op
-- on a 1..n array) then does ONE math.random(#keys) -- i.e. exactly
-- _t[math.random(#_t)]. So we skip the per-call keys-table allocation + table.sort
-- entirely and index directly. These picks run many times per seed (every ante /
-- slot in the multi-ante joker search), so this removes the search's biggest GC /
-- sort cost. Same single math.random call after the same seed => identical results.
-- Only Tag pools have table values with sort_id and need the real sort -- but
-- when a tag filter is active that sort used to run on EVERY candidate seed
-- (~25 table allocations + a table.sort with a Lua comparator each time). The
-- sorted keys array is a pure function of the pool table, and the only sort_id
-- pool a worker ever passes here is the static tag-pool snapshot (joker/voucher
-- pools are string arrays; rollVoucherSequence's blanked copies are fresh string
-- arrays too), so cache it per pool table and re-roll only the pick. Unique
-- sort_ids make the sorted order deterministic regardless of pairs() order, so
-- the cached array yields the same element for the same math.random roll.
-- Weak keys so a transient pool table can never pin memory.
local sorted_keys_cache = setmetatable({}, { __mode = "k" })
function pseudorandom_element(_t, seed)
	if seed then math_randomseed(seed) end
	local first = _t[1]
	if type(first) ~= 'table' or not first.sort_id then
		local key = math_random(#_t)
		return _t[key], key
	end
	local keys = sorted_keys_cache[_t]
	if not keys then
		keys = {}
		for k, v in pairs(_t) do keys[#keys + 1] = { k = k, v = v } end
		table.sort(keys, function(a, b) return a.v.sort_id < b.v.sort_id end)
		sorted_keys_cache[_t] = keys
	end
	local key = keys[math_random(#keys)].k
	return _t[key], key
end
function pseudorandom(seed, min, max)
	math_randomseed(seed)
	if min and max then return math_random(min, max) else return math_random() end
end
-- Candidate-seed generator: byte-for-byte the same strings and the same
-- math.random consumption as the game's random_string, minus its per-char
-- string concatenations (8 intermediate strings per seed) and the string.upper
-- (every byte produced is already an uppercase letter or digit). Runs once per
-- candidate, so the allocation savings add up over millions of seeds.
local B1, B9 = string_byte('1'), string_byte('9')
local BA, BN = string_byte('A'), string_byte('N')
local BP, BZ = string_byte('P'), string_byte('Z')
local seed_bytes = {}
local function random_string(length, seed)
	if seed then math_randomseed(seed) end
	for i = 1, length do
		seed_bytes[i] = math_random() > 0.7 and math_random(B1, B9)
			or (math_random() > 0.45 and math_random(BA, BN) or math_random(BP, BZ))
	end
	return string_char(unpack(seed_bytes, 1, length))
end

local config = load("return " .. configStr)()

G = {
	FUNCS = {},
	P_JOKER_RARITY_POOLS = config.jokerPools,
	P_CENTER_POOLS = {
		Booster = config.boosterPool,
		Tag = config.tagPool,
		Voucher = config.voucherPool,
	},
	GAME = {
		banned_keys = config.game.banned_keys or {},
		pool_flags = config.game.pool_flags or {},
		used_jokers = {},
		stake = 1,
	},
}

Brainstorm = {
	SETTINGS = { autoreroll = config.autoreroll, multiAnteSearch = config.multiAnteSearch, useCulledCache = config.useCulledCache },
	AUTOREROLL = {},
	random_state = {},
}

-- Defines Brainstorm.passesAllFilters + all helpers. Its love.thread-using
-- functions are defined but never called here; its top-level require()s hit the
-- stubs above; its top-level G.FUNCS assignments hit the mock G.FUNCS table.
assert(load(rerollSrc, "Brainstorm_reroll_worker"))()

-- Precompute the culled joker/voucher pools once for this worker's whole run
-- (fresh Lua state => no staleness). The seed loop's filters then reuse them
-- instead of rebuilding per slot. buildCulledPools honors useCulledCache and is
-- a no-op that leaves Brainstorm.CULLED nil (=> inline fallback) if disabled.
pcall(Brainstorm.buildCulledPools)

local sessionChan = love.thread.getChannel("brainstorm_search_session")
local resultChan = love.thread.getChannel("brainstorm_search_result")
local progressChan = love.thread.getChannel("brainstorm_search_progress")
local mySession = config.session
local entropy = config.entropy or 0
-- Hoisted: both are defined once by the reroll source above and never
-- reassigned; saves two global+field lookups per candidate seed.
local passesAllFilters = Brainstorm.passesAllFilters
local serializeValue = Brainstorm.serializeValue
local monotonicTime = love and love.timer and love.timer.getTime or os.clock
local progressPrefix = tostring(threadIndex) .. ":"
local lastProgressAt = monotonicTime()

-- Searching and cancellation still advance in the existing 250-seed batches.
-- Only the observable counter is rate-limited: the renderer cannot display
-- hundreds of updates per second, and compact strings avoid compiling a Lua
-- table literal for every progress sample on the main thread.
local function publishProgress(tried, force)
	local now = monotonicTime()
	if force or now < lastProgressAt or now - lastProgressAt >= 0.1 then
		progressChan:push(progressPrefix .. tostring(tried))
		lastProgressAt = now
	end
end

-- Partition the global seed sequence across the N workers with no overlap: this
-- thread tests global indices threadIndex, threadIndex+N, threadIndex+2N, ...
local tried = 0
while sessionChan:peek() == mySession do
	for _ = 1, 250 do
		tried = tried + 1
		local k = (tried - 1) * numThreads + threadIndex
		local seed = random_string(8, entropy + k * 0.561892350821)
		if passesAllFilters(seed) then
			-- Serialize with the same helper the config uses (defined in reroll.lua,
			-- loaded above) so we never rely on love channels deep-copying tables.
			resultChan:push(serializeValue({ seed = seed, jokerFoundAt = Brainstorm.AUTOREROLL.jokerFoundAt, session = mySession }))
			publishProgress(tried, true)
			return
		end
	end
	publishProgress(tried, false)
end
publishProgress(tried, true)
]==]
