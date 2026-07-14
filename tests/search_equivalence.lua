-- ===========================================================================
-- Brainstorm seed-search equivalence + benchmark harness
-- ---------------------------------------------------------------------------
-- Proves that a modified Brainstorm_reroll.lua accepts/rejects EXACTLY the
-- same seeds as a baseline copy, by running both files' real worker bootstrap
-- (Brainstorm.SEARCH_WORKER_SRC) under a fake love.thread and comparing:
--   1. DIRECT:  passesAllFilters() over a fixed deterministic seed list, per
--               filter configuration (accept/reject + jokerFoundAt must match).
--   2. E2E:     full worker loops (seed generation -> filters -> result/progress
--               channels) run to a hit or a batch cap; (tried, hit seed) must
--               match, including a multi-thread partition case.
--   3. FUZZ:    Brainstorm.round13 vs the original string.format("%.13f")
--               round-trip, including adversarial near-tie values.
--   4. SELF:    new file with useCulledCache on vs off must agree.
--
-- Run with LuaJIT (same interpreter family LOVE embeds -- math.random and
-- string.format semantics match the game):
--
--   luajit tests/search_equivalence.lua --old /path/to/baseline_reroll.lua \
--       --new Brainstorm_reroll.lua [--seeds 20000] [--runs 30] \
--       [--batches 20] [--fuzz 2000000] [--bench 2] [--nojit]
--
-- Get a baseline of the last committed version with:
--   git show HEAD:Brainstorm_reroll.lua > /tmp/baseline_reroll.lua
-- Exit code 0 = all comparisons identical.
-- ===========================================================================

local args = { seeds = 20000, runs = 30, batches = 20, fuzz = 2000000, bench = 0 }
do
	local i = 1
	while arg[i] do
		local a = arg[i]
		if a == "--old" then args.old = arg[i + 1]; i = i + 2
		elseif a == "--new" then args.new = arg[i + 1]; i = i + 2
		elseif a == "--seeds" then args.seeds = tonumber(arg[i + 1]); i = i + 2
		elseif a == "--runs" then args.runs = tonumber(arg[i + 1]); i = i + 2
		elseif a == "--batches" then args.batches = tonumber(arg[i + 1]); i = i + 2
		elseif a == "--fuzz" then args.fuzz = tonumber(arg[i + 1]); i = i + 2
		elseif a == "--bench" then args.bench = tonumber(arg[i + 1]) or 2; i = i + 2
		elseif a == "--nojit" then args.nojit = true; i = i + 1
		else error("unknown arg: " .. a) end
	end
end
if args.nojit and jit then jit.off() end
assert(args.new, "--new <path> is required")

local function readFile(path)
	local f = assert(io.open(path, "rb"), "cannot open " .. path)
	local s = f:read("*a")
	f:close()
	return s
end

local newText = readFile(args.new)
local oldText = args.old and readFile(args.old) or nil

-- Same literal-serializer the mod uses (worker rebuilds via load("return "..s)).
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

-- ---------------------------------------------------------------------------
-- Synthetic pool snapshot: same SHAPE as Brainstorm.buildSearchSnapshot, with
-- deliberately awkward data -- shuffled tag sort_ids (so sorting matters),
-- locked/gated/flagged jokers and upgrade vouchers (so resample loops run).
-- Equivalence is data-agnostic (both files see identical pools); this data
-- just has to exercise every branch.
-- ---------------------------------------------------------------------------
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
		{ key = "p_arcana_normal_1", weight = 1, kind = "Arcana" },
		{ key = "p_arcana_normal_2", weight = 1, kind = "Arcana" },
		{ key = "p_arcana_jumbo_1", weight = 0.5, kind = "Arcana" },
		{ key = "p_arcana_mega_1", weight = 0.25, kind = "Arcana" },
		{ key = "p_celestial_normal_1", weight = 1, kind = "Celestial" },
		{ key = "p_celestial_jumbo_1", weight = 0.5, kind = "Celestial" },
		{ key = "p_standard_normal_1", weight = 2, kind = "Standard" },
		{ key = "p_standard_jumbo_1", weight = 1, kind = "Standard" },
		{ key = "p_buffoon_normal_1", weight = 1.2, kind = "Buffoon" },
		{ key = "p_buffoon_jumbo_1", weight = 0.6, kind = "Buffoon" },
		{ key = "p_buffoon_mega_1", weight = 0.15, kind = "Buffoon" },
		{ key = "p_spectral_normal_1", weight = 0.3, kind = "Spectral" },
	}

	-- 24 tags with sort_ids that are a shuffled permutation of 1..24 so the
	-- sorted order differs from array order (exercises the sort/cache).
	snap.tagPool = {}
	local ids, x = {}, 99991
	for i = 1, 24 do ids[i] = i end
	for i = 24, 2, -1 do
		x = lcg(x)
		local j = (x % i) + 1
		ids[i], ids[j] = ids[j], ids[i]
	end
	for i = 1, 24 do
		snap.tagPool[i] = { key = "tag_" .. i, sort_id = ids[i] }
	end
	snap.tagPool[11].key = "tag_charm" -- named target used by the cases

	snap.voucherPool = {}
	for i = 1, 16 do
		local base = { key = "v_base_" .. i }
		if i == 9 then base.unlocked = false end
		snap.voucherPool[#snap.voucherPool + 1] = base
		snap.voucherPool[#snap.voucherPool + 1] = { key = "v_upg_" .. i, requires = true }
	end

	snap.game = { banned_keys = {
		j_r1_5 = true, p_arcana_normal_1 = true, c_black_hole = true,
	}, pool_flags = {} }

	local ar = defaultAutoreroll()
	for k, v in pairs(case.ar or {}) do ar[k] = v end
	local ma = defaultMultiAnte()
	for k, v in pairs(case.ma or {}) do ma[k] = v end
	snap.autoreroll = ar
	snap.multiAnteSearch = ma
	snap.useCulledCache = case.useCulledCache -- nil = default on
	return snap
end

-- ---------------------------------------------------------------------------
-- Filter configurations under test
-- ---------------------------------------------------------------------------
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
}

-- ---------------------------------------------------------------------------
-- Worker execution under a fake love.thread
-- ---------------------------------------------------------------------------

-- Load the reroll file once, standalone, purely to capture its embedded
-- SEARCH_WORKER_SRC (each file version carries its own copy).
local function captureWorkerSrc(fileText)
	package.loaded["lovely"] = { mod_dir = "" }
	package.loaded["nativefs"] = {
		write = function() end, read = function() return "" end, getInfo = function() return nil end,
	}
	G = { FUNCS = {} }
	Brainstorm = { SETTINGS = { autoreroll = {}, multiAnteSearch = {} }, AUTOREROLL = {}, random_state = {} }
	assert(load(fileText, "bootstrap"))()
	return assert(Brainstorm.SEARCH_WORKER_SRC, "file has no SEARCH_WORKER_SRC")
end

-- Run a file's worker chunk exactly as love would, with channels we control.
-- maxBatches limits how many 250-seed batches run (0 = bootstrap only, the
-- while loop never starts). Returns {tried, hit, jokerFoundAt}; afterwards the
-- process globals (G, Brainstorm, pseudohash, ...) are that worker's state,
-- which is what the DIRECT tests then drive.
local function runWorker(fileText, workerSrc, snap, maxBatches, threadIndex, numThreads)
	local channels = {}
	local batches = 0
	local function getChannel(name)
		local c = channels[name]
		if not c then
			c = { items = {} }
			c.push = function(self, v)
				self.items[#self.items + 1] = v
				if name == "brainstorm_search_progress" then
					batches = batches + 1
					if batches >= maxBatches then channels["brainstorm_search_session"].items = {} end
				end
			end
			c.pop = function(self) return table.remove(self.items, 1) end
			c.peek = function(self) return self.items[1] end
			c.clear = function(self) self.items = {} end
			channels[name] = c
		end
		return c
	end
	if maxBatches > 0 then getChannel("brainstorm_search_session"):push(snap.session) end
	love = { thread = { getChannel = getChannel } }
	package.loaded["love.thread"] = true
	assert(load(workerSrc, "worker"))(serialize(snap), fileText, threadIndex or 0, numThreads or 1)

	local out = { tried = 0 }
	local prog = channels["brainstorm_search_progress"]
	if prog then
		for _, raw in ipairs(prog.items) do
			local t = load("return " .. raw)()
			if t and t.n and t.n > out.tried then out.tried = t.n end
		end
	end
	local resChan = channels["brainstorm_search_result"]
	local raw = resChan and resChan.items[1]
	if raw then
		local r = load("return " .. raw)()
		out.hit, out.jokerFoundAt = r.seed, r.jokerFoundAt
	end
	return out
end

-- Deterministic seed list from the same charset random_string emits
-- (digits 1-9, letters A-N and P-Z; no 0/O).
local CHARSET = "123456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
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
	return list
end

-- Bootstrap a file for a case, then evaluate every seed in the list.
-- Result per seed: "0" reject, or "1|<jokerFoundAt>" accept.
local function directResults(fileText, workerSrc, case, seedList)
	local snap = buildSnapshot(case, 1, 0)
	runWorker(fileText, workerSrc, snap, 0)
	if snap.useCulledCache ~= false then
		assert(Brainstorm.CULLED ~= nil, "buildCulledPools did not run (pcall swallowed an error?)")
	end
	local passes = Brainstorm.passesAllFilters
	local out, accepted = {}, 0
	for i = 1, #seedList do
		if passes(seedList[i]) then
			accepted = accepted + 1
			out[i] = "1|" .. tostring(Brainstorm.AUTOREROLL.jokerFoundAt or "")
		else
			out[i] = "0"
		end
	end
	return out, accepted
end

local failures = 0
local function report(ok, label, detail)
	if ok then
		print(string.format("  PASS  %s%s", label, detail and ("  (" .. detail .. ")") or ""))
	else
		failures = failures + 1
		print(string.format("  FAIL  %s%s", label, detail and ("  (" .. detail .. ")") or ""))
	end
end

local newWorkerSrc = captureWorkerSrc(newText)
local oldWorkerSrc = oldText and captureWorkerSrc(oldText) or nil

-- ---------------------------------------------------------------------------
-- 1) DIRECT equivalence: old vs new on identical seed lists
-- ---------------------------------------------------------------------------
if oldText then
	print(("=" ):rep(70))
	print(string.format("DIRECT equivalence: %d seeds per case", args.seeds))
	local seedList = makeSeedList(args.seeds, 1)
	for _, case in ipairs(CASES) do
		local oldRes, oldAcc = directResults(oldText, oldWorkerSrc, case, seedList)
		local newRes, newAcc = directResults(newText, newWorkerSrc, case, seedList)
		local same, firstBad = true, nil
		for i = 1, #seedList do
			if oldRes[i] ~= newRes[i] then
				same = false; firstBad = i; break
			end
		end
		report(same, case.name,
			same and string.format("accepts: %d/%d", newAcc, #seedList)
			or string.format("first divergence at seed %s: old=%s new=%s",
				seedList[firstBad], oldRes[firstBad], newRes[firstBad]))
	end
end

-- ---------------------------------------------------------------------------
-- 2) E2E worker-loop equivalence: seed generation + loop + channels
-- ---------------------------------------------------------------------------
if oldText then
	print(("=" ):rep(70))
	print(string.format("E2E worker equivalence: %d runs per case, cap %d batches (%d seeds)",
		args.runs, args.batches, args.batches * 250))
	for ci, case in ipairs(CASES) do
		local same, detail, hits = true, nil, 0
		for run = 1, args.runs do
			-- Single-thread partition plus one multi-thread slice (index 2 of 5)
			-- to cover the k = (tried-1)*numThreads + threadIndex arithmetic.
			local ti, nt = 0, 1
			if run % 5 == 0 then ti, nt = 2, 5 end
			local snap = function(text) return buildSnapshot(case, 100 + run, 1000.5 + run * 137.7331 + ci * 17.1) end
			local o = runWorker(oldText, oldWorkerSrc, snap(oldText), args.batches, ti, nt)
			local n = runWorker(newText, newWorkerSrc, snap(newText), args.batches, ti, nt)
			if o.hit then hits = hits + 1 end
			if o.tried ~= n.tried or o.hit ~= n.hit or o.jokerFoundAt ~= n.jokerFoundAt then
				same = false
				detail = string.format("run %d: old={tried=%d,hit=%s,at=%s} new={tried=%d,hit=%s,at=%s}",
					run, o.tried, tostring(o.hit), tostring(o.jokerFoundAt),
					n.tried, tostring(n.hit), tostring(n.jokerFoundAt))
				break
			end
		end
		report(same, case.name, same and string.format("hits: %d/%d runs", hits, args.runs) or detail)
	end
end

-- ---------------------------------------------------------------------------
-- 3) round13 fuzz (new file): must equal the string round-trip everywhere
-- ---------------------------------------------------------------------------
do
	print(("=" ):rep(70))
	runWorker(newText, newWorkerSrc, buildSnapshot(CASES[1], 1, 0), 0)
	local round13 = Brainstorm.round13
	if not round13 then
		print("  SKIP  round13 fuzz (Brainstorm.round13 not exported by --new file)")
	else
		local sf, ab, tn, fl = string.format, math.abs, tonumber, math.floor
		math.randomseed(42)
		local bad, worst = 0, nil
		for i = 1, args.fuzz do
			local x
			local mode = i % 4
			if mode == 0 then x = math.random()
			elseif mode == 1 then x = (2.134453429141 + math.random() * 1.72431234) % 1
			elseif mode == 2 then x = (fl(math.random() * 1e13) + 0.5) / 1e13 -- exact near-ties
			else
				x = (fl(math.random() * 1e13) + 0.5) / 1e13
				x = x + (math.random() - 0.5) * x * 4e-16 -- +/- ~2 ulp around the tie
				if x >= 1 then x = 1 - 2 ^ -53 elseif x < 0 then x = 0 end
			end
			local want = ab(tn(sf("%.13f", x)))
			if round13(x) ~= want then
				bad = bad + 1
				worst = worst or x
			end
		end
		-- chained, like real pseudoseed streams
		math.randomseed(7)
		for s = 1, 2000 do
			local a = math.random()
			local b = a
			for t = 1, 100 do
				a = round13((2.134453429141 + a * 1.72431234) % 1)
				b = ab(tn(sf("%.13f", (2.134453429141 + b * 1.72431234) % 1)))
				if a ~= b then bad = bad + 1; worst = worst or b; break end
			end
		end
		report(bad == 0, string.format("round13 fuzz (%d values + 200k chained)", args.fuzz),
			bad == 0 and "bit-identical" or string.format("%d mismatches, e.g. x=%.17g", bad, worst))
	end
end

-- ---------------------------------------------------------------------------
-- 4) SELF check (new file): culled cache on vs off must agree
-- ---------------------------------------------------------------------------
do
	print(("=" ):rep(70))
	print("SELF check: useCulledCache on vs off (new file)")
	local seedList = makeSeedList(math.min(args.seeds, 10000), 2)
	for _, name in ipairs({ "tag only", "pack", "legendary", "jokers ALL shop+packs", "voucher any ante" }) do
		local case
		for _, c in ipairs(CASES) do if c.name == name then case = c end end
		local onRes, onAcc = directResults(newText, newWorkerSrc, case, seedList)
		local offCase = {}
		for k, v in pairs(case) do offCase[k] = v end
		offCase.useCulledCache = false
		local offRes = directResults(newText, newWorkerSrc, offCase, seedList)
		local same = true
		for i = 1, #seedList do
			if onRes[i] ~= offRes[i] then same = false break end
		end
		report(same, name, string.format("accepts: %d/%d", onAcc, #seedList))
	end
end

-- ---------------------------------------------------------------------------
-- 5) Benchmark (optional): direct filter throughput + full worker loop
-- ---------------------------------------------------------------------------
if args.bench > 0 then
	print(("=" ):rep(70))
	print(string.format("BENCH: %gs per measurement, jit=%s", args.bench,
		args.nojit and "off" or "on"))
	local benchSeeds = makeSeedList(50000, 3)

	local function benchDirect(fileText, workerSrc, case)
		runWorker(fileText, workerSrc, buildSnapshot(case, 1, 0), 0)
		local passes = Brainstorm.passesAllFilters
		for i = 1, 20000 do passes(benchSeeds[(i % #benchSeeds) + 1]) end -- warmup
		collectgarbage("collect")
		local t0, n, i = os.clock(), 0, 0
		repeat
			for _ = 1, 4096 do
				i = (i % #benchSeeds) + 1
				passes(benchSeeds[i])
			end
			n = n + 4096
		until os.clock() - t0 >= args.bench
		return n / (os.clock() - t0)
	end

	local function benchWorker(fileText, workerSrc, case)
		-- Filter that can never accept => measures the full generate+test loop.
		local t0 = os.clock()
		local r = runWorker(fileText, workerSrc, buildSnapshot(case, 1, 424242.5), 200)
		return r.tried / (os.clock() - t0)
	end

	local benchCases = { "tag only", "soul x1", "legendary negative", "jokers ALL shop+packs", "stacked (rare hit)" }
	for _, name in ipairs(benchCases) do
		local case
		for _, c in ipairs(CASES) do if c.name == name then case = c end end
		local newRate = benchDirect(newText, newWorkerSrc, case)
		if oldText then
			local oldRate = benchDirect(oldText, oldWorkerSrc, case)
			print(string.format("  direct %-28s old %9.0f/s   new %9.0f/s   x%.2f",
				name, oldRate, newRate, newRate / oldRate))
		else
			print(string.format("  direct %-28s new %9.0f/s", name, newRate))
		end
	end
	local loopCase = { name = "loop reject-all", ar = { searchTag = "tag_no_such" } }
	local newRate = benchWorker(newText, newWorkerSrc, loopCase)
	if oldText then
		local oldRate = benchWorker(oldText, oldWorkerSrc, loopCase)
		print(string.format("  worker %-28s old %9.0f/s   new %9.0f/s   x%.2f",
			"loop (gen+tag reject)", oldRate, newRate, newRate / oldRate))
	else
		print(string.format("  worker %-28s new %9.0f/s", "loop (gen+tag reject)", newRate))
	end
end

print(("=" ):rep(70))
if failures == 0 then
	print("ALL CHECKS PASSED")
	os.exit(0)
else
	print(failures .. " CHECK(S) FAILED")
	os.exit(1)
end
