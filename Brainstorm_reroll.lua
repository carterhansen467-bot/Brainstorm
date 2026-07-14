local nativefs = require("nativefs")

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
		local needed = math.max(ar.searchForSoul or 0, 1)
		local last_soul_found = false
		for i = 1, needed do
			local soul_found = false
			for j = 1, 5 do
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
	local filterSkipSm, filterSkipBig = Brainstorm.skipsFromFilters(tagLoc)
	local finalSkipSm, finalSkipBig = filterSkipSm, filterSkipBig
	if ar.seedPoolFile and ar.seedPoolFile ~= "" and Brainstorm.readPoolHeader then
		local poolPath = Brainstorm.seedPoolPath and Brainstorm.seedPoolPath()
		local poolHeader = poolPath and Brainstorm.readPoolHeader(poolPath)
		if not poolHeader then return false end
		local poolOK
		-- Active tag/classic-Soul predicates were just read on their own pass.
		-- Rewind so the pool's embedded rules replay the same per-blind rolls
		-- from the beginning, then rewind once more for downstream overlays.
		Brainstorm.random_state = { hashed_seed = pseudohash(seed_found) }
		poolOK, finalSkipSm, finalSkipBig = Brainstorm.evaluatePoolCriteria(
			seed_found, poolHeader, filterSkipSm, filterSkipBig)
		if not poolOK then return false end
		-- The pool oracle and active filters share a route but each starts its
		-- independent RNG streams at advance one.
		Brainstorm.random_state = { hashed_seed = pseudohash(seed_found) }
	end
	Brainstorm.setPackSkipAssumption(seed_found, finalSkipSm, finalSkipBig)
	-- 2.5) Legendary ANYWHERE (antes 1-8): scan each ante's spawned Arcana /
	-- Spectral packs for the run's FIRST Soul (see checkLegendaryAnywhere).
	local legLoc = nil
	if legAnywhere then
		local ok, loc = Brainstorm.checkLegendaryAnywhere(seed_found, ar.searchLegendary, ar.searchNegativeLegendary)
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
--   * get_current_pool also excludes any voucher still in the shop
--     (G.shop_vouchers) or already redeemed (used_vouchers). Under "redeem
--     nothing" that means each ante N>=2 excludes ante N-1's voucher (still on
--     offer). We mirror that by blanking prev's slot; ante 1 excludes nothing.
--     Confirmed live at ante 1 and a couple of ante 2+ spot-checks (Ctrl+B).
function Brainstorm.rollVoucherSequence(seed_found, max_ante)
	local base = Brainstorm.getVoucherCulledPool()
	local out = {}
	local prev = nil
	for ante = 1, max_ante do
		local pool = base
		if prev then
			pool = {}
			for i = 1, #base do pool[i] = (base[i] == prev) and 'UNAVAILABLE' or base[i] end
		end
		local key = 'Voucher' .. ante
		local chosen = pseudorandom_element(pool, Brainstorm.pseudoseed(key .. seed_found))
		local it = 1
		while chosen == 'UNAVAILABLE' do
			it = it + 1
			chosen = pseudorandom_element(pool, Brainstorm.pseudoseed(key .. '_resample' .. it .. seed_found))
		end
		out[ante] = chosen
		prev = chosen
	end
	return out
end

-- ante_mode: 1-8 requires the target voucher at exactly that ante; 0 = any of
-- 1-4; -1 = any of 1-8. Deeper antes use the same per-ante 'Voucher'..ante
-- keys + redeem-nothing blanking the 1-4 model was verified with.
function Brainstorm.checkVoucherSearch(seed_found, target_key, ante_mode)
	ante_mode = ante_mode or 1
	local any_to = (ante_mode == 0 and 4) or (ante_mode == -1 and 8) or nil
	local max_ante = any_to or ante_mode
	local seq = Brainstorm.rollVoucherSequence(seed_found, max_ante)
	if any_to then
		for ante = 1, any_to do
			if seq[ante] == target_key then return true end
		end
		return false
	end
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
	local nativefs = require("nativefs")
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
	local nativefs = require("nativefs")
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
		for j = 1, 5 do
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
	local nativefs = require("nativefs")
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
	local nativefs = require("nativefs")
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

	local nativefs = require("nativefs")
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
-- Per ante, scan the PHYSICAL pack slots in order (2 per opened shop: ante 1
-- has Small+Big = up to 4, antes 2+ add the post-boss entry shop = up to 6,
-- fewer under assumed blind skips; the run's first shop leads with the forced
-- Buffoon). Opening an Arcana pack rolls
-- 'soul_Tarot'..ante once per card (config.extra cards); a Spectral pack
-- rolls 'soul_Spectral'..ante TWICE per card -- soul roll then black-hole
-- roll -- and a black-hole hit OVERWRITES a soul hit on the same card
-- (create_card sets forced_key twice). After a black hole exists, later
-- spectral cards skip the second roll (used_jokers gate). The first Soul's
-- legendary is the FIRST 'Joker4' advance (no ante appended -- source:
-- get_current_pool appends the ante to every pool key EXCEPT legendary), and
-- its edition rolls 'edisou'..ante (create_card key_append 'sou').
-- CONVENTION for collecting the find: reach that ante with pools untouched,
-- buy/open that ante's Arcana+Spectral packs in slot order (the labelled one
-- last), then use the Soul in that same ante.
-- ===========================================================================
function Brainstorm.checkLegendaryAnywhere(seed_found, target_key, require_negative)
	if G.GAME.banned_keys and G.GAME.banned_keys.c_soul then return false end
	for ante = 1, 8 do
		local packs = Brainstorm.getSimulatedPacks(seed_found, ante, 6)
		for slot = 1, #packs do
			local center = packs[slot]
			local kind = center and not center.forced and center.kind or nil
			if kind == 'Arcana' or kind == 'Spectral' then
				local stype = (kind == 'Arcana') and 'Tarot' or 'Spectral'
				local ncards = Brainstorm.packCardCount(center)
				local soul_in_pack, bh_in_pack = false, false
				for card = 1, ncards do
					local soul = false
					if not soul_in_pack then
						soul = pseudorandom(Brainstorm.pseudoseed('soul_' .. stype .. ante .. seed_found)) > 0.997
					end
					if kind == 'Spectral' and not bh_in_pack then
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
						return true, "LegA" .. ante .. "P" .. slot
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

-- Replay the cumulative route embedded in a .bspool header. Each rule selects
-- its first required matching occurrences, exactly like the external scanner;
-- observe-only stages count toward membership but do not remove shops.
function Brainstorm.poolRouteSkips(seed_found, header)
	local rules = header and header.route_tags or nil
	if not rules or #rules == 0 then return nil, nil end
	local counts, sm, big = {}, nil, nil
	for i = 1, #rules do counts[i] = 0 end
	for ante = 1, 39 do
		local rolled = nil
		for blind = 1, 2 do
			local need = false
			for i, rule in ipairs(rules) do
				if rule.collect and counts[i] < rule.count
						and ante >= rule.minAnte and ante <= rule.maxAnte then
					need = true; break
				end
			end
			if need then
				rolled = Brainstorm.rollTag(seed_found, ante)
				for i, rule in ipairs(rules) do
					if rule.collect and counts[i] < rule.count
							and ante >= rule.minAnte and ante <= rule.maxAnte
							and rolled == rule.key then
						counts[i] = counts[i] + 1
						if blind == 1 then sm = sm or {}; sm[ante] = true
						else big = big or {}; big[ante] = true end
					end
				end
			end
		end
	end
	return sm, big
end

-- Independent in-game oracle for the criteria embedded in a pool. This is a
-- main-thread safety rail: native membership is fast, while this replay uses
-- Balatro's own Lua RNG/pools to reject any model or transfer error before a
-- seed is applied. It also returns the cumulative skips for overlay filters.
function Brainstorm.evaluatePoolCriteria(seed_found, header, overlaySm, overlayBig)
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
				if rolls[ante] and rolls[ante][blind] == rule.key then count = count + 1 end
			end
		end
		if count < rule.count then return false end
	end
	local routeCounts, sm, big = {}, nil, nil
	for i = 1, #routeRules do routeCounts[i] = 0 end
	for ante = 1, maxTagAnte do
		for blind = 1, 2 do
			for i, rule in ipairs(routeRules) do
				if rule.collect and routeCounts[i] < rule.count
						and ante >= rule.minAnte and ante <= rule.maxAnte
						and rolls[ante][blind] == rule.key then
					routeCounts[i] = routeCounts[i] + 1
					if blind == 1 then sm = sm or {}; sm[ante] = true
					else big = big or {}; big[ante] = true end
				end
			end
		end
	end
	local finalSm = Brainstorm.mergeSkipRoutes(sm, overlaySm)
	local finalBig = Brainstorm.mergeSkipRoutes(big, overlayBig)

	local legendaryRules = header.pool_legendaries or {}
	if #legendaryRules == 0 and header.legendary then legendaryRules = { header.legendary } end
	if #legendaryRules == 0 then return true, finalSm, finalBig end
	if G.GAME.banned_keys and G.GAME.banned_keys.c_soul then return false end
	Brainstorm.setPackSkipAssumption(seed_found, finalSm, finalBig)

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

	local maxDepth, maxAnte = 1, 0
	for _, rule in ipairs(legendaryRules) do
		maxDepth = math.max(maxDepth, rule.soulDepth or 1)
		maxAnte = math.max(maxAnte, rule.maxAnte)
	end
	local picked = { pickLegendary() }
	if maxDepth == 2 then picked[2] = pickLegendary(picked[1]) end
	for _, rule in ipairs(legendaryRules) do
		if picked[rule.soulDepth or 1] ~= rule.key then return false end
	end

	local soulNumber, events = 0, {}
	for ante = 1, maxAnte do
		local packs = Brainstorm.getSimulatedPacks(seed_found, ante, 6)
		for _, center in ipairs(packs) do
			local kind = center and not center.forced and center.kind or nil
			if kind == 'Arcana' or kind == 'Spectral' then
				local stype = kind == 'Arcana' and 'Tarot' or 'Spectral'
				local soulInPack, blackHoleInPack = false, false
				for _ = 1, Brainstorm.packCardCount(center) do
					local soul = false
					if not soulInPack then
						soul = pseudorandom(Brainstorm.pseudoseed(
							'soul_' .. stype .. ante .. seed_found)) > 0.997
					end
					if kind == 'Spectral' and not blackHoleInPack then
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
	for _, rule in ipairs(legendaryRules) do
		local event = events[rule.soulDepth or 1]
		if not event or event.ante < rule.minAnte or event.ante > rule.maxAnte
				or (rule.negative and event.edition <= 0.997) then return false end
	end
	return true, finalSm, finalBig
end

function Brainstorm.skipsFromFilters(tagLoc)
	local ar = Brainstorm.SETTINGS.autoreroll
	local sm, big = nil, nil
	local legAnywhere = ar.searchLegendaryAnywhere and ar.searchLegendary and ar.searchLegendary ~= ""
	if not legAnywhere and ((ar.searchForSoul and ar.searchForSoul > 0) or (ar.searchLegendary and ar.searchLegendary ~= "")) then
		sm = { [1] = true }
	end
	if ar.searchTag and ar.searchTag ~= "" then
		if not ar.searchTagAnywhere then
			sm = sm or {}
			sm[1] = true
		elseif tagLoc then
			local a = tonumber(tagLoc:match("^TagA(%d+)"))
			if a then
				if tagLoc:sub(-2) == "Sm" then
					sm = sm or {}
					sm[a] = true
				else
					big = big or {}
					big[a] = true
				end
			end
		end
	end
	return sm, big
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
function Brainstorm.setPackSkipAssumption(seed_found, skipSm, skipBig)
	local sig = ""
	if skipSm then
		for a = 1, 39 do if skipSm[a] then sig = sig .. "s" .. a end end
	end
	if skipBig then
		for a = 1, 39 do if skipBig[a] then sig = sig .. "b" .. a end end
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
	local sm, big = Brainstorm.skipsFromFilters(tagLoc)
	Brainstorm.setPackSkipAssumption(seed_found, sm, big)
	return sm, big
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
	for i = 1, 5 do
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

	local nativefs = require("nativefs")
	nativefs.write(Brainstorm.modPath() .. "/debug_predict.txt", table.concat(lines, "\n"))
end

function Brainstorm.checkLegendarySearch(seed_found, target_key)
	local soul_found = false
	for i = 1, 5 do
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
	local nativefs = require("nativefs")
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
	A.searchThreads = {}
	A.searchProgress = {}
	A.searchTried = 0
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
	-- Progress is per-thread ({i, n}); keep the latest count for each and sum them
	-- so A.searchTried reflects total seeds tested across all workers.
	local progressChan = love.thread.getChannel(C.progress)
	A.searchProgress = A.searchProgress or {}
	local praw = progressChan:pop()
	while praw ~= nil do
		local ok, pv = pcall(function() return load("return " .. praw)() end)
		if ok and type(pv) == "table" and pv.i ~= nil then
			A.searchProgress[pv.i] = pv.n
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
-- runs the old synchronous auto_reroll. Owns the "Rerolling..." text + the
-- found-joker alert so Brainstorm.update just delegates here.
function Brainstorm.updateAutoReroll(dt)
	local A = Brainstorm.AUTOREROLL
	if not A.autoRerollActive then return end
	A.autoRerollFrames = A.autoRerollFrames or 0

	local useThread = (Brainstorm.SETTINGS.useSearchThread ~= false)
		and love and love.thread and not A.searchThreadFailed
	local useNative = Brainstorm.nativeAvailable() and not A.nativeFailed

	-- Seed-pool searches are native-only and definitive. If the helper can't
	-- run (missing binary/pool, failed session) or reported a pool problem
	-- (bad file, model mismatch, or NO pool seed passing the filters), stop
	-- the search with a message: degrading to the full-space Lua search would
	-- return seeds outside the selected pool.
	local arS = Brainstorm.SETTINGS.autoreroll
	if arS.seedPoolFile and arS.seedPoolFile ~= "" then
		local reason = nil
		if A.poolAbort then
			reason = A.poolAbort:find("no seed in the pool")
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
					if arS.seedPoolFile and arS.seedPoolFile ~= "" then
						A.poolAbort = "pool: in-game verification rejected the native result"
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
		A.rerollTimer = A.rerollTimer + dt
		if A.rerollTimer >= A.rerollInterval then
			A.rerollTimer = A.rerollTimer - A.rerollInterval
			seed_found = Brainstorm.auto_reroll()
			jokerFoundAt = A.jokerFoundAt
		end
	end

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
	--   BIG_SHOW_AT .. HIDE   : big centered "Rerolling..." text
	--   after BIG_HIDE_AT     : small spinner in the top-left corner (drawn by
	--                           Brainstorm.draw_search_indicator), no big text
	local BIG_SHOW_AT, BIG_HIDE_AT = 0.25, 2.5
	A.searchElapsed = (A.searchElapsed or 0) + dt
	if not A.bigTextShown and A.searchElapsed >= BIG_SHOW_AT then
		A.bigTextShown = true
		A.rerollText = Brainstorm.attention_text({
			scale = 1.4,
			text = "Rerolling...",
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
	local cx, cy, r = 44, 44, 16
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

	love.graphics.pop()
end

-- ===========================================================================
-- Native helper search (optional, ~10x the Lua workers)
-- ---------------------------------------------------------------------------
-- native/brainstorm_native_search is a C port of passesAllFilters that runs
-- across all cores (build once with native/build.sh). The mod writes a config
-- snapshot (pools with eligibility RESOLVED HERE, settings, and parity checks
-- computed with the game's own RNG functions), spawns the helper detached,
-- and polls a tiny status file each frame. SAFETY RAILS, in order:
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
		local nativefs = require("nativefs")
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

-- Identity readout for a .bspool: the header is a fixed 1 KiB text block, so
-- one bounded read is enough even for multi-GB pools. Returns nil when the
-- file is missing or not a pool.
function Brainstorm.readPoolHeader(path)
	local nativefs = require("nativefs")
	local raw = nativefs.read(path, 1024)
	if not raw or not raw:match("^BRAINSTORM_SEED_POOL ") then return nil end
	local h = { tags = {}, route_tags = {}, pool_legendaries = {} }
	for line in raw:gmatch("[^\n]+") do
		local k, v = line:match("^(%S+)%s*(.-)%s*$")
		if k == "end" then break end
		if k == "records" or k == "complete" or k == "soul_depth"
				or k == "modelver" or k == "refilter_depth" then h[k] = tonumber(v)
		elseif k == "space" or k == "pool_id" or k == "label"
				or k == "catalog_hash" or k == "tag_route" then h[k] = v
		elseif k == "tag" then
			local key, lo, hi, count = v:match("^(%S+)%s+(%d+)%s+(%d+)%s+(%d+)$")
			if key then h.tags[#h.tags + 1] = {
				key = key, minAnte = tonumber(lo), maxAnte = tonumber(hi), count = tonumber(count),
			} end
		elseif k == "legendary" then
			local key, lo, hi, neg = v:match("^(%S+)%s+(%d+)%s+(%d+)%s*(%d*)$")
			if key then h.legendary = {
				key = key, minAnte = tonumber(lo), maxAnte = tonumber(hi),
				negative = tonumber(neg) == 1,
			} end
		elseif k == "route_legendary" then
			local key, lo, hi, neg, depth = v:match(
				"^(%S+)%s+(%d+)%s+(%d+)%s+(%d+)%s+(%d+)$")
			if key then h.pool_legendaries[#h.pool_legendaries + 1] = {
				key = key, minAnte = tonumber(lo), maxAnte = tonumber(hi),
				negative = tonumber(neg) == 1, soulDepth = tonumber(depth),
			} end
		elseif k == "route_tag" then
			local mode, key, lo, hi, count = v:match("^(%S+)%s+(%S+)%s+(%d+)%s+(%d+)%s+(%d+)$")
			if key then h.route_tags[#h.route_tags + 1] = {
				collect = mode == "collect", key = key, minAnte = tonumber(lo),
				maxAnte = tonumber(hi), count = tonumber(count),
			} end
		end
	end
	local collect = h.tag_route ~= "observe"
	for _, rule in ipairs(h.tags) do
		rule.collect = collect
		h.route_tags[#h.route_tags + 1] = rule
	end
	if h.legendary then
		h.legendary.soulDepth = h.soul_depth or 1
		h.pool_legendaries[#h.pool_legendaries + 1] = h.legendary
	end
	return h
end

-- One-line description shown under the in-game Seed Pool selector; the
-- pool_id prefix is the shareable identity mark (same file => same id).
function Brainstorm.poolInfoString(name)
	if not name or name == "" then return "" end
	local h = Brainstorm.readPoolHeader(Brainstorm.seedPoolDir() .. "/" .. name)
	if not h then return "(pool file missing or unreadable)" end
	local bits = {}
	if h.label and h.label ~= "" and (h.label .. ".bspool") ~= name then
		bits[#bits + 1] = '"' .. h.label .. '"'
	end
	if h.pool_id and h.pool_id ~= "" then bits[#bits + 1] = "id " .. h.pool_id:sub(1, 8) end
	if h.space == "total" then bits[#bits + 1] = "all typeable seeds" end
	local hasSoul2 = false
	for _, rule in ipairs(h.pool_legendaries or {}) do
		if rule.soulDepth == 2 then hasSoul2 = true; break end
	end
	if hasSoul2 then bits[#bits + 1] = "Soul #2 only" end
	if h.records then bits[#bits + 1] = tostring(h.records) .. " seeds" end
	bits[#bits + 1] = (h.complete == 1) and "complete" or "PARTIAL SCAN"
	bits[#bits + 1] = "+ current filters"
	return table.concat(bits, "  |  ")
end

-- Selected pool's full path, or nil when no pool is selected. Existence is
-- intentionally NOT cached: the user may drop a file in while the game runs.
function Brainstorm.seedPoolPath()
	local ar = Brainstorm.SETTINGS.autoreroll
	local name = ar and ar.seedPoolFile
	if not name or name == "" then return nil end
	local nativefs = require("nativefs")
	local p = Brainstorm.seedPoolDir() .. "/" .. name
	if not nativefs.getInfo(p) then return nil end
	return p
end

-- Parity checks: values computed with the GAME's own pseudohash /
-- math.randomseed / math.random / round13. The helper refuses to search
-- unless it reproduces every one bit-for-bit. The 16 pr/prn pairs double as
-- calibration for the one FP detail that can differ per LuaJIT binary (fused
-- vs separate rounding in its PRNG seeding step) -- 64 seeding steps make an
-- undetected mismatch astronomically unlikely.
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
	-- Model 5 composes a selected pool's collected-tag route with overlay
	-- filters and snapshots booster/Soul bans. Older helpers would silently
	-- evaluate a different route, so the version handshake must reject them.
	add("modelver", "5")
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
	local poolPath = Brainstorm.seedPoolPath and Brainstorm.seedPoolPath()
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
	for _, v in ipairs(G.P_CENTER_POOLS["Voucher"]) do
		local eligible = v.unlocked ~= false and not v.requires
		local avail = (eligible and not (G.GAME.banned_keys and G.GAME.banned_keys[v.key])) and "1" or "0"
		add("vouchdef", ck(v.key), avail)
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

function Brainstorm.startNativeSearch()
	local A = Brainstorm.AUTOREROLL
	local nativefs = require("nativefs")
	local p = Brainstorm.nativePaths()
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
	A.nativeActive = true
	A.nativeStartedAt = love.timer and love.timer.getTime and love.timer.getTime() or os.clock()
	A.nativeHbFrame = 0
	A.searchTried = 0
	return true
end

-- Poll the helper's status file. Returns {seed, jokerFoundAt, session} on a
-- hit. Maintains the heartbeat; converts helper errors / silence into a clean
-- fallback (A.nativeFailed) instead of a stuck search.
function Brainstorm.pollNativeSearch()
	local A = Brainstorm.AUTOREROLL
	if not A.nativeActive then return nil end
	local nativefs = require("nativefs")
	local p = Brainstorm.nativePaths()
	A.nativeHbFrame = (A.nativeHbFrame or 0) + 1
	if A.nativeHbFrame % 120 == 0 then
		nativefs.write(p.hb, tostring(os.time()))
	end
	local txt = nativefs.read(p.status)
	if not txt then
		local now = love.timer and love.timer.getTime and love.timer.getTime() or os.clock()
		if now - (A.nativeStartedAt or now) > 5 then
			print("[Brainstorm] native search wrote no status in 5s; using Lua threads instead")
			Brainstorm.stopNativeSearch()
			A.nativeFailed = true
		end
		return nil
	end
	local tried = txt:match("P (%d+)")
	if tried then A.searchTried = tonumber(tried) end
	local wmsg = txt:match("W ([^\n]+)")
	if wmsg and wmsg ~= A.nativeWarned then
		A.nativeWarned = wmsg
		print("[Brainstorm] native search: " .. wmsg)
	end
	local emsg = txt:match("E ([^\n]+)")
	if emsg then
		Brainstorm.stopNativeSearch()
		if emsg:find("^pool") then
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
	local seed, label = txt:match("R (%S+) ([^\n]+)")
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
	local nativefs = require("nativefs")
	nativefs.write(Brainstorm.nativePaths().stop, "1")
	A.nativeActive = false
end

-- Worker thread source. Runs in its own Lua state: no G, no love modules except
-- what it require()s. It rebuilds a minimal G from the serialized snapshot,
-- defines the game's pure RNG globals, then loads Brainstorm_reroll.lua to get
-- the identical filter suite and loops generating + testing seeds off the main
-- thread. See Brainstorm.startSearchThread for the args passed in.
Brainstorm.SEARCH_WORKER_SRC = [==[
require("love.thread")

local configStr, rerollSrc, threadIndex, numThreads = ...
threadIndex = threadIndex or 0
numThreads = numThreads or 1

package.preload["lovely"] = function() return { mod_dir = "" } end
package.preload["nativefs"] = function()
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
			progressChan:push(serializeValue({ i = threadIndex, n = tried }))
			return
		end
	end
	progressChan:push(serializeValue({ i = threadIndex, n = tried }))
end
]==]
