local lovely = require("lovely")
local nativefs = require("nativefs")

Brainstorm.AUTOREROLL = {}

G.FUNCS.change_search_tag = function(x)
	Brainstorm.SETTINGS.autoreroll.searchTagID = x.to_key
	Brainstorm.SETTINGS.autoreroll.searchTag = Brainstorm.SearchTagList[x.to_val]
	nativefs.write(lovely.mod_dir .. "/Brainstorm/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

G.FUNCS.change_search_pack = function(x)
	Brainstorm.SETTINGS.autoreroll.searchPackID = x.to_key
	Brainstorm.SETTINGS.autoreroll.searchPack = Brainstorm.SearchPackList[x.to_val]
	nativefs.write(lovely.mod_dir .. "/Brainstorm/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

G.FUNCS.change_search_soul_count = function(x)
	Brainstorm.SETTINGS.autoreroll.searchForSoul = x.to_val
	nativefs.write(lovely.mod_dir .. "/Brainstorm/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

G.FUNCS.change_seeds_per_frame = function(x)
	Brainstorm.SETTINGS.autoreroll.seedsPerFrameID = x.to_key
	Brainstorm.SETTINGS.autoreroll.seedsPerFrame = Brainstorm.seedsPerFrame[x.to_val]
	nativefs.write(lovely.mod_dir .. "/Brainstorm/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

G.FUNCS.change_search_threads = function(x)
	Brainstorm.SETTINGS.autoreroll.searchThreadsID = x.to_key
	Brainstorm.SETTINGS.autoreroll.searchThreads = Brainstorm.searchThreadsValues[x.to_val] or 0
	nativefs.write(lovely.mod_dir .. "/Brainstorm/settings.lua", STR_PACK(Brainstorm.SETTINGS))
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
function Brainstorm.passesAllFilters(seed_found)
	Brainstorm.random_state = {
		hashed_seed = pseudohash(seed_found),
	}
	if Brainstorm.SETTINGS.autoreroll.searchTag ~= "" then
		local _tag = pseudorandom_element(G.P_CENTER_POOLS["Tag"], Brainstorm.pseudoseed("Tag1" .. seed_found)).key
		if _tag ~= Brainstorm.SETTINGS.autoreroll.searchTag then
			return false
		end
	end
	if Brainstorm.SETTINGS.autoreroll.searchPack and #Brainstorm.SETTINGS.autoreroll.searchPack > 0 then
		local cume, it, center = 0, 0, nil
		for k, v in ipairs(G.P_CENTER_POOLS['Booster']) do
			if (not _type or _type == v.kind) then cume = cume + (v.weight or 1) end
		end
		local poll = pseudorandom(Brainstorm.pseudoseed("shop_pack1" .. seed_found)) * cume
		for k, v in ipairs(G.P_CENTER_POOLS['Booster']) do
			if not _type or _type == v.kind then it = it + (v.weight or 1) end
			if it >= poll and it - (v.weight or 1) <= poll then center = v; break end
		end
		local pack_found = false
		for i = 1, #Brainstorm.SETTINGS.autoreroll.searchPack do
			if Brainstorm.SETTINGS.autoreroll.searchPack[i] == center.key then
				pack_found = true
				break
			end
		end
		if not pack_found then
			return false
		end
	end
	if Brainstorm.SETTINGS.autoreroll.searchVoucher and Brainstorm.SETTINGS.autoreroll.searchVoucher ~= "" then
		local ante_mode = Brainstorm.SETTINGS.autoreroll.searchVoucherAnte or 1
		if not Brainstorm.checkVoucherSearch(seed_found, Brainstorm.SETTINGS.autoreroll.searchVoucher, ante_mode) then
			return false
		end
	end
	if not Brainstorm.checkMultiAnteJokerSearch(seed_found) then
		return false
	end
	if (Brainstorm.SETTINGS.autoreroll.searchForSoul and Brainstorm.SETTINGS.autoreroll.searchForSoul > 0) or (Brainstorm.SETTINGS.autoreroll.searchLegendary and Brainstorm.SETTINGS.autoreroll.searchLegendary ~= "") then
		local needed = math.max(Brainstorm.SETTINGS.autoreroll.searchForSoul or 0, 1)
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

		if Brainstorm.SETTINGS.autoreroll.searchLegendary and Brainstorm.SETTINGS.autoreroll.searchLegendary ~= "" then
			if not last_soul_found then
				return false
			else
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
				if chosen_key ~= Brainstorm.SETTINGS.autoreroll.searchLegendary then
					return false
				elseif Brainstorm.SETTINGS.autoreroll.searchNegativeLegendary then
					local edition_poll = pseudorandom(Brainstorm.pseudoseed("edisou1" .. seed_found))
					if edition_poll <= 0.997 then return false end
				end
			end
		end
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
		stake = (G.GAME and G.GAME.stake) or nil,
		joker = jokerFoundAt,
		ts = os.time(),
	}
	compress_and_save(G.SETTINGS.profile .. "/" .. "saveState" .. slot .. ".jkr", marker)
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
	local base = {}
	for k, v in ipairs(G.P_CENTER_POOLS['Voucher']) do
		local eligible = v.unlocked ~= false and not v.requires
		base[k] = (eligible and not (G.GAME.banned_keys and G.GAME.banned_keys[v.key])) and v.key or 'UNAVAILABLE'
	end
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

-- ante_mode: 1-4 requires the target voucher at exactly that ante; 0 = any of 1-4.
function Brainstorm.checkVoucherSearch(seed_found, target_key, ante_mode)
	ante_mode = ante_mode or 1
	local max_ante = (ante_mode == 0) and 4 or ante_mode
	local seq = Brainstorm.rollVoucherSequence(seed_found, max_ante)
	if ante_mode == 0 then
		for ante = 1, 4 do
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
	local lovely = require("lovely")
	local nativefs = require("nativefs")
	nativefs.write(lovely.mod_dir .. "/Brainstorm/debug_predict.txt", table.concat(lines, "\n"))
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

function Brainstorm.pseudoseed(key, predict_seed)
	if key == "seed" then
		return math.random()
	end

	if predict_seed then
		local _pseed = pseudohash(key .. (predict_seed or ""))
		_pseed = math.abs(tonumber(string.format("%.13f", (2.134453429141 + _pseed * 1.72431234) % 1)))
		return (_pseed + (pseudohash(predict_seed) or 0)) / 2
	end

	if not Brainstorm.random_state[key] then
		Brainstorm.random_state[key] = pseudohash(key .. (Brainstorm.random_state.seed or ""))
	end

	Brainstorm.random_state[key] =
		math.abs(tonumber(string.format("%.13f", (2.134453429141 + Brainstorm.random_state[key] * 1.72431234) % 1)))
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

	local lovely = require("lovely")
	local nativefs = require("nativefs")
	nativefs.write(lovely.mod_dir .. "/Brainstorm/debug_predict.txt", table.concat(lines, "\n"))
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
			local source_pool = G.P_JOKER_RARITY_POOLS[rarity]
			local pool = {}
			for k, v in ipairs(source_pool) do
				pool[k] = Brainstorm.joker_is_pool_eligible(v) and v.key or 'UNAVAILABLE'
			end

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
				local pool = {}
				for k, v in ipairs(G.P_JOKER_RARITY_POOLS[rarity]) do
					pool[k] = Brainstorm.joker_is_pool_eligible(v) and v.key or 'UNAVAILABLE'
				end
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
function Brainstorm.checkMultiAnteJokerSearch(seed_found)
	Brainstorm.AUTOREROLL.jokerFoundAt = nil
	local slots = Brainstorm.SETTINGS.autoreroll.jokerSlotData
	if not slots then return true end

	local targets = {}
	for i, slot in ipairs(slots) do
		if slot.key and slot.key ~= "" then
			targets[#targets+1] = {key = slot.key, slot = i, requireNegative = slot.requireNegative}
		end
	end
	if #targets == 0 then return true end

	local cfg = Brainstorm.SETTINGS.multiAnteSearch
	if not cfg then return true end

	local any_ante_active = false
	for ante = 1, 4 do
		if (cfg["ante"..ante.."Slots"] or 0) > 0 or cfg["ante"..ante.."Packs"] then
			any_ante_active = true; break
		end
	end
	if not any_ante_active then return true end

	-- Match ANY (OR): a seed passes if just one of the selected jokers is found
	-- (e.g. to pair a legendary start with any one of the 3). Match ALL (AND,
	-- default): every selected joker must be found.
	local matchAny = Brainstorm.SETTINGS.autoreroll.jokerSearchMatchAny

	local foundAt = {}
	local anyFound = false
	for _, target in ipairs(targets) do
		local found = false
		for ante = 1, 4 do
			local ante_slots = cfg["ante"..ante.."Slots"] or 0
			local packs = cfg["ante"..ante.."Packs"] or false
			if ante_slots > 0 and Brainstorm.checkShopJokerSearch(seed_found, ante, ante_slots, target.key, target.requireNegative) then
				foundAt[target.slot] = "A"..ante.."Shop"; found = true; break
			end
			if packs and Brainstorm.checkAntePacksForJoker(seed_found, ante, target.key, target.requireNegative) then
				foundAt[target.slot] = "A"..ante.."Pack"; found = true; break
			end
		end
		if found then
			anyFound = true
			if matchAny then break end
		elseif not matchAny then
			return false
		end
	end
	if matchAny and not anyFound then return false end

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

	local lovely = require("lovely")
	local nativefs = require("nativefs")
	nativefs.write(lovely.mod_dir .. "/Brainstorm/debug_predict.txt", table.concat(lines, "\n"))
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
	local lovely = require("lovely")
	local nativefs = require("nativefs")
	return nativefs.read(lovely.mod_dir .. "/Brainstorm/Brainstorm_reroll.lua")
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
		snap.boosterPool[i] = { key = v.key, weight = v.weight, kind = v.kind }
	end

	snap.tagPool = {}
	for i, v in ipairs(G.P_CENTER_POOLS['Tag']) do
		snap.tagPool[i] = { key = v.key, sort_id = v.sort_id }
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

	local seed_found, jokerFoundAt = nil, nil

	if useThread then
		if not A.searchThreads or #A.searchThreads == 0 then
			Brainstorm.startSearchThread()
		end
		local res = Brainstorm.pollSearchThread()
		if res then
			seed_found = res.seed
			jokerFoundAt = res.jokerFoundAt
			Brainstorm.stopSearchThread()
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
			Brainstorm.applyFoundSeed(seed_found)
		end
		if jokerFoundAt then
			-- Stash it for the Ctrl+J hotkey; don't auto-pop the joker text on a
			-- find (it clutters/overlaps the "Seed saved" message).
			A.lastJokerFoundAt = jokerFoundAt
			A.jokerFoundAt = nil
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

-- Verbatim from Balatro functions/misc_functions.lua. Global math.random is
-- LuaJIT's and identical across thread states (the game never overrides it), so
-- these reproduce the game's RNG exactly.
function pseudohash(str)
	local num = 1
	for i = #str, 1, -1 do
		num = ((1.1239285023 / num) * string.byte(str, i) * math.pi + math.pi * i) % 1
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
-- Only Tag pools have table values with sort_id; those need the real sort, but are
-- picked at most once per seed, so we keep the original path for them.
function pseudorandom_element(_t, seed)
	if seed then math.randomseed(seed) end
	local first = _t[1]
	if type(first) ~= 'table' or not first.sort_id then
		local key = math.random(#_t)
		return _t[key], key
	end
	local keys = {}
	for k, v in pairs(_t) do keys[#keys + 1] = { k = k, v = v } end
	table.sort(keys, function(a, b) return a.v.sort_id < b.v.sort_id end)
	local key = keys[math.random(#keys)].k
	return _t[key], key
end
function pseudorandom(seed, min, max)
	math.randomseed(seed)
	if min and max then return math.random(min, max) else return math.random() end
end
local function random_string(length, seed)
	if seed then math.randomseed(seed) end
	local ret = ''
	for i = 1, length do
		ret = ret .. string.char(math.random() > 0.7 and math.random(string.byte('1'), string.byte('9')) or (math.random() > 0.45 and math.random(string.byte('A'), string.byte('N')) or math.random(string.byte('P'), string.byte('Z'))))
	end
	return string.upper(ret)
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
	SETTINGS = { autoreroll = config.autoreroll, multiAnteSearch = config.multiAnteSearch },
	AUTOREROLL = {},
	random_state = {},
}

-- Defines Brainstorm.passesAllFilters + all helpers. Its love.thread-using
-- functions are defined but never called here; its top-level require()s hit the
-- stubs above; its top-level G.FUNCS assignments hit the mock G.FUNCS table.
assert(load(rerollSrc, "Brainstorm_reroll_worker"))()

local sessionChan = love.thread.getChannel("brainstorm_search_session")
local resultChan = love.thread.getChannel("brainstorm_search_result")
local progressChan = love.thread.getChannel("brainstorm_search_progress")
local mySession = config.session
local entropy = config.entropy or 0

-- Partition the global seed sequence across the N workers with no overlap: this
-- thread tests global indices threadIndex, threadIndex+N, threadIndex+2N, ...
local tried = 0
while sessionChan:peek() == mySession do
	for _ = 1, 250 do
		tried = tried + 1
		local k = (tried - 1) * numThreads + threadIndex
		local seed = random_string(8, entropy + k * 0.561892350821)
		if Brainstorm.passesAllFilters(seed) then
			-- Serialize with the same helper the config uses (defined in reroll.lua,
			-- loaded above) so we never rely on love channels deep-copying tables.
			resultChan:push(Brainstorm.serializeValue({ seed = seed, jokerFoundAt = Brainstorm.AUTOREROLL.jokerFoundAt, session = mySession }))
			progressChan:push(Brainstorm.serializeValue({ i = threadIndex, n = tried }))
			return
		end
	end
	progressChan:push(Brainstorm.serializeValue({ i = threadIndex, n = tried }))
end
]==]
