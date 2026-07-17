-- Source-grounded oracle for Omen Globe and targeted Charm-pack routing.
--
-- This intentionally does not load Brainstorm production code.  It mirrors the
-- relevant Balatro 1.0.1o call order from:
--   card.lua Card:open
--     * every Arcana card advances "omen_globe"
--     * a roll > 0.8 changes that card's create_card type to Spectral
--   functions/common_events.lua create_card
--     * Tarot/Spectral first advance soul_<type><ante>
--     * Spectral then advances that SAME stream for Black Hole
--     * the Black Hole assignment comes second and overwrites Soul
--   tag.lua Tag:apply_to_run
--     * Charm immediately opens p_arcana_mega_1 or _2 (both have 5 cards)
--
-- Usage:
--   luajit tests/omen_charm_oracle.lua trace SEED [ante] [bh_allowed] [omen_owned]
--   luajit tests/omen_charm_oracle.lua fixtures
--   luajit tests/omen_charm_oracle.lua branch SEED [ante]
--   luajit tests/omen_charm_oracle.lua ownership SEED [ante]
--   luajit tests/omen_charm_oracle.lua source-check /path/to/balatro_src

local function pseudohash(str)
	local num = 1
	for i = #str, 1, -1 do
		num = ((1.1239285023 / num) * string.byte(str, i) * math.pi
			+ math.pi * i) % 1
	end
	return num
end

local Rng = {}
Rng.__index = Rng

function Rng.new(seed)
	return setmetatable({
		seed = seed,
		hashed_seed = pseudohash(seed),
		state = {},
		count = {},
	}, Rng)
end

function Rng:clone()
	local copy = Rng.new(self.seed)
	for key, value in pairs(self.state) do copy.state[key] = value end
	for key, value in pairs(self.count) do copy.count[key] = value end
	return copy
end

function Rng:next(key)
	local value = self.state[key]
	if value == nil then value = pseudohash(key .. self.seed) end
	value = math.abs(tonumber(string.format("%.13f",
		(2.134453429141 + value * 1.72431234) % 1)))
	self.state[key] = value
	self.count[key] = (self.count[key] or 0) + 1
	local mixed = (value + self.hashed_seed) / 2
	math.randomseed(mixed)
	return math.random(), self.count[key]
end

local function bool01(value) return value and "1" or "0" end
local function roll_text(value)
	return value == nil and "-" or string.format("%.17g", value)
end

-- Simulate one opened booster.  soul_in_pack and black_hole_in_pack model
-- G.GAME.used_jokers while all generated cards remain in G.pack_cards.
local function open_pack(rng, options)
	local ante = assert(options.ante)
	local original_kind = assert(options.kind)
	local cards = assert(options.cards)
	local omen_owned = options.omen_owned == true
	local soul_allowed = options.soul_allowed ~= false
	local black_hole_allowed = options.black_hole_allowed ~= false
	local soul_in_pack = options.soul_in_pack == true
	local black_hole_in_pack = options.black_hole_in_pack == true
	local rows = {}

	for card = 1, cards do
		local row = { card = card, original_kind = original_kind }
		-- Card:open passes "Tarot" to create_card for an ordinary Arcana
		-- card; "Arcana" is the booster kind, not a create_card pool type.
		local kind = original_kind == "Arcana" and "Tarot" or original_kind
		if original_kind == "Arcana" and omen_owned then
			row.omen, row.omen_index = rng:next("omen_globe")
			row.converted = row.omen > 0.8
			if row.converted then kind = "Spectral" end
		else
			row.converted = false
		end
		row.kind = kind

		local soul = false
		if soul_allowed and not soul_in_pack
				and (kind == "Tarot" or kind == "Spectral" or kind == "Tarot_Planet") then
			row.soul, row.soul_index = rng:next("soul_" .. kind .. ante)
			row.soul_hit = row.soul > 0.997
			soul = row.soul_hit
		else
			row.soul_hit = false
		end

		if kind == "Spectral" and not black_hole_in_pack then
			-- This is deliberately after the Soul roll and uses the same key.
			row.black_hole, row.black_hole_index = rng:next("soul_Spectral" .. ante)
			row.black_hole_hit = row.black_hole > 0.997
			if row.black_hole_hit then
				row.overwrote_soul = soul
				soul = false
				if black_hole_allowed then black_hole_in_pack = true end
			end
		else
			row.black_hole_hit = false
		end

		if soul then
			row.outcome = "Soul"
			soul_in_pack = true
		elseif row.black_hole_hit and black_hole_allowed then
			row.outcome = "BlackHole"
		else
			-- If Black Hole is banned its roll still overwrites forced Soul, but
			-- create_card rejects the banned forced key and picks a normal card.
			row.outcome = kind
		end
		rows[#rows + 1] = row
	end

	return rows, {
		soul_in_pack = soul_in_pack,
		black_hole_in_pack = black_hole_in_pack,
	}
end

local function row_text(prefix, row)
	return table.concat({
		prefix,
		"card=" .. row.card,
		"omen=" .. roll_text(row.omen),
		"oi=" .. (row.omen_index or 0),
		"converted=" .. bool01(row.converted),
		"type=" .. row.kind,
		"soul=" .. roll_text(row.soul),
		"si=" .. (row.soul_index or 0),
		"soul_hit=" .. bool01(row.soul_hit),
		"black_hole=" .. roll_text(row.black_hole),
		"bi=" .. (row.black_hole_index or 0),
		"black_hole_hit=" .. bool01(row.black_hole_hit),
		"overwrite=" .. bool01(row.overwrote_soul),
		"outcome=" .. row.outcome,
	}, "\t")
end

local function print_rows(prefix, rows)
	for _, row in ipairs(rows) do print(row_text(prefix, row)) end
end

local function count_outcome(rows, outcome)
	local count = 0
	for _, row in ipairs(rows) do
		if row.outcome == outcome then count = count + 1 end
	end
	return count
end

local function first_outcome(rows, outcome)
	for _, row in ipairs(rows) do
		if row.outcome == outcome then return row.card end
	end
	return 0
end

local function fixture_summary(seed, ante, black_hole_allowed, omen_owned)
	local rng = Rng.new(seed)
	local rows = open_pack(rng, {
		ante = ante, kind = "Arcana", cards = 5, omen_owned = omen_owned ~= false,
		black_hole_allowed = black_hole_allowed,
	})
	local converted = 0
	for _, row in ipairs(rows) do if row.converted then converted = converted + 1 end end
	return table.concat({
		seed,
		"converted=" .. converted,
		"soul=" .. first_outcome(rows, "Soul"),
		"black_hole=" .. first_outcome(rows, "BlackHole"),
		"normal_spectral=" .. count_outcome(rows, "Spectral"),
		"omen_advances=" .. (rng.count.omen_globe or 0),
		"tarot_advances=" .. (rng.count["soul_Tarot" .. ante] or 0),
		"spectral_advances=" .. (rng.count["soul_Spectral" .. ante] or 0),
	}, "\t"), rows
end

-- The targeted branch must receive a complete clone of canonical RNG state.
-- It opens a five-card Charm reward only in the clone.  The canonical state's
-- next pack must therefore be byte-for-byte identical to a no-branch control.
local function branch_trace(seed, ante, omen_owned)
	local canonical = Rng.new(seed)
	local control = canonical:clone()
	local branch = canonical:clone()

	local charm = open_pack(branch, {
		ante = ante, kind = "Arcana", cards = 5, omen_owned = omen_owned,
	})
	local canonical_next = open_pack(canonical, {
		ante = ante, kind = "Arcana", cards = 3, omen_owned = omen_owned,
	})
	local control_next = open_pack(control, {
		ante = ante, kind = "Arcana", cards = 3, omen_owned = omen_owned,
	})

	local same = #canonical_next == #control_next
	for i = 1, #canonical_next do
		same = same and row_text("", canonical_next[i]) == row_text("", control_next[i])
	end
	return charm, canonical_next, same
end

-- Opening Arcana cards before buying Omen must not consume its stream.  Thus a
-- pack opened immediately after purchase sees the same first Omen roll as a
-- fresh state.  This models buying Omen before a same-shop pack.
local function ownership_trace(seed, ante)
	local routed = Rng.new(seed)
	open_pack(routed, {
		ante = ante, kind = "Arcana", cards = 3, omen_owned = false,
	})
	local after_purchase = open_pack(routed, {
		ante = ante, kind = "Arcana", cards = 5, omen_owned = true,
	})
	local fresh = open_pack(Rng.new(seed), {
		ante = ante, kind = "Arcana", cards = 5, omen_owned = true,
	})
	return after_purchase, fresh,
		after_purchase[1].omen == fresh[1].omen
		and after_purchase[1].omen_index == 1
end

local function read(path)
	local file = assert(io.open(path, "rb"))
	local text = file:read("*a")
	file:close()
	return text
end

local function source_check(root)
	local card = read(root .. "/card.lua")
	local common = read(root .. "/functions/common_events.lua")
	local tag = read(root .. "/tag.lua")
	assert(card:find("pseudorandom%('omen_globe'%) > 0%.8"),
		"Card:open Omen threshold/call is missing")
	assert(card:find('create_card%("Spectral".-\'ar2\'%)'),
		"Card:open does not convert an Omen hit to Spectral")
	assert(common:find("pseudorandom%('soul_'%.%._type%.%.G%.GAME%.round_resets%.ante%) > 0%.997"),
		"create_card Soul stream/threshold is missing")
	local first = assert(common:find("forced_key = 'c_soul'", 1, true))
	local second = assert(common:find("forced_key = 'c_black_hole'", 1, true))
	assert(first < second, "Black Hole no longer overwrites Soul")
	assert(tag:find("self%.name == 'Charm Tag'"), "Charm Tag handler is missing")
	assert(tag:find("p_arcana_mega_"), "Charm no longer opens a Mega Arcana pack")
	local game = read(root .. "/game.lua")
	assert(game:find("p_arcana_mega_1.-config = {extra = 5", 1),
		"Mega Arcana reward is no longer five cards")
	print("PASS source call order and Charm pack shape")
end

local function usage()
	io.stderr:write("usage: omen_charm_oracle.lua trace SEED [ante] [bh_allowed] [omen_owned] | fixtures | branch SEED [ante] | ownership SEED [ante] | source-check PATH\n")
	os.exit(2)
end

local command = arg[1]
if command == "trace" then
	local seed, ante = arg[2], tonumber(arg[3]) or 1
	if not seed then usage() end
	local black_hole_allowed = arg[4] ~= "0"
	local omen_owned = arg[5] ~= "0"
	local summary, rows = fixture_summary(seed, ante, black_hole_allowed, omen_owned)
	print("SUMMARY\t" .. summary)
	print_rows("CHARM", rows)
elseif command == "fixtures" then
	for _, seed in ipairs({ "11111111", "M8111111", "ER111111", "MV111111" }) do
		local summary = fixture_summary(seed, 1, true)
		print("SUMMARY\t" .. summary)
	end
elseif command == "branch" then
	local seed, ante = arg[2], tonumber(arg[3]) or 1
	if not seed then usage() end
	local charm, canonical, unchanged = branch_trace(seed, ante, true)
	print("BRANCH\tseed=" .. seed .. "\tcanonical_unchanged=" .. bool01(unchanged)
		.. "\tcharm_cards=" .. #charm .. "\tcanonical_cards=" .. #canonical
		.. "\tcharm_soul=" .. first_outcome(charm, "Soul")
		.. "\tcanonical_soul=" .. first_outcome(canonical, "Soul")
		.. "\trequired_charm=" .. bool01(first_outcome(charm, "Soul") > 0
			and first_outcome(canonical, "Soul") == 0))
	print_rows("CHARM", charm)
	print_rows("CANONICAL", canonical)
elseif command == "ownership" then
	local seed, ante = arg[2], tonumber(arg[3]) or 1
	if not seed then usage() end
	local after_purchase, fresh, starts_at_first = ownership_trace(seed, ante)
	print("OWNERSHIP\tseed=" .. seed
		.. "\tprepurchase_omen_advances=0"
		.. "\tpostpurchase_first_index=" .. (after_purchase[1].omen_index or 0)
		.. "\tmatches_fresh=" .. bool01(starts_at_first)
		.. "\tpostpurchase_first_roll=" .. roll_text(after_purchase[1].omen)
		.. "\tfresh_first_roll=" .. roll_text(fresh[1].omen))
elseif command == "source-check" then
	if not arg[2] then usage() end
	source_check(arg[2])
else
	usage()
end
