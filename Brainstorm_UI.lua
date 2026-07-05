local lovely = require("lovely")
local nativefs = require("nativefs")

local searchLegendaryKeys = {"None", "Caino", "Triboulet", "Yorick", "Chicot", "Perkeo"}
local legendaryKeyByName = {["None"]="", ["Caino"]="j_caino", ["Triboulet"]="j_triboulet", ["Yorick"]="j_yorick", ["Chicot"]="j_chicot", ["Perkeo"]="j_perkeo"}

-- Main shade of each legendary, sampled from its soul sprite in Jokers.png.
Brainstorm.legendaryColours = {
	["j_caino"]     = {0.86, 0.85, 0.86, 1}, -- Caino: white
	["j_triboulet"] = {0.00, 0.55, 0.94, 1}, -- Triboulet: blue
	["j_yorick"]    = {0.94, 0.63, 0.00, 1}, -- Yorick: gold
	["j_chicot"]    = {0.94, 0.31, 0.31, 1}, -- Chicot: red
	["j_perkeo"]    = {0.31, 0.63, 0.47, 1}, -- Perkeo: green
}

-- Live-mutable colour shared by the "Search Legendary" cycle bar and its arrows.
-- Mutating its components in place recolours the bar without a rebuild
-- (create_option_cycle keeps the reference; draw_self reads it every frame).
Brainstorm.legendaryBarColour = {G.C.RED[1], G.C.RED[2], G.C.RED[3], G.C.RED[4]}

function Brainstorm.applyLegendaryBarColour()
	local key = Brainstorm.SETTINGS.autoreroll.searchLegendary or ""
	local target = Brainstorm.legendaryColours[key] or G.C.RED
	local c = Brainstorm.legendaryBarColour
	c[1], c[2], c[3], c[4] = target[1], target[2], target[3], target[4]
end

G.FUNCS.change_search_legendary = function(x)
	Brainstorm.SETTINGS.autoreroll.searchLegendaryID = x.to_key
	Brainstorm.SETTINGS.autoreroll.searchLegendary = legendaryKeyByName[x.to_val]
	Brainstorm.applyLegendaryBarColour()
	nativefs.write(lovely.mod_dir .. "/Brainstorm/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

-- "Negative" toggle for the legendary search, styled like the joker-slot
-- negative button. Text + colour are mutated in place so it updates live.
Brainstorm.negLegendaryDisplay = {text = ""}
Brainstorm.negLegendaryColour  = {G.C.GREY[1], G.C.GREY[2], G.C.GREY[3], G.C.GREY[4]}

function Brainstorm.applyNegLegendaryDisplay()
	local on = Brainstorm.SETTINGS.autoreroll.searchNegativeLegendary
	Brainstorm.negLegendaryDisplay.text = on and "Negative: ON" or "Negative: OFF"
	local src = on and G.C.SECONDARY_SET.Planet or darken(G.C.GREY, 0.2)
	local c = Brainstorm.negLegendaryColour
	c[1], c[2], c[3], c[4] = src[1], src[2], src[3], src[4]
end

G.FUNCS.brainstorm_toggle_neg_legendary = function(e)
	local s = Brainstorm.SETTINGS.autoreroll
	s.searchNegativeLegendary = not s.searchNegativeLegendary
	Brainstorm.applyNegLegendaryDisplay()
	nativefs.write(lovely.mod_dir .. "/Brainstorm/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

-- Match mode for the 3 joker slots: ALL (AND, default) requires every selected
-- joker in the seed; ANY (OR) passes if just one is found (e.g. to pair a
-- legendary start with any one of the 3). Live text + colour like the buttons
-- above so it updates without rebuilding the tab.
Brainstorm.matchAnyDisplay = {text = ""}
Brainstorm.matchAnyColour  = {G.C.BLUE[1], G.C.BLUE[2], G.C.BLUE[3], G.C.BLUE[4]}

function Brainstorm.applyMatchAnyDisplay()
	local anyMode = Brainstorm.SETTINGS.autoreroll.jokerSearchMatchAny
	Brainstorm.matchAnyDisplay.text = anyMode and "Match: ANY of the 3" or "Match: ALL of the 3"
	local src = anyMode and G.C.GREEN or G.C.BLUE
	local c = Brainstorm.matchAnyColour
	c[1], c[2], c[3], c[4] = src[1], src[2], src[3], src[4]
end

G.FUNCS.brainstorm_toggle_match_any = function(e)
	local s = Brainstorm.SETTINGS.autoreroll
	s.jokerSearchMatchAny = not s.jokerSearchMatchAny
	Brainstorm.applyMatchAnyDisplay()
	nativefs.write(lovely.mod_dir .. "/Brainstorm/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

G.FUNCS.change_search_voucher = function(x)
	Brainstorm.SETTINGS.autoreroll.searchVoucherID = x.to_key
	Brainstorm.SETTINGS.autoreroll.searchVoucher = Brainstorm.voucherKeyByName[x.to_val]
	nativefs.write(lovely.mod_dir .. "/Brainstorm/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

-- Which ante the searched voucher must be offered at. Vouchers roll from one
-- shared RNG stream advanced once per ante, so "Ante N" means the Nth voucher.
Brainstorm.voucherAnteOptions = {"Ante 1", "Ante 2", "Ante 3", "Ante 4", "Any (1-4)"}
Brainstorm.voucherAnteValues  = {["Ante 1"]=1, ["Ante 2"]=2, ["Ante 3"]=3, ["Ante 4"]=4, ["Any (1-4)"]=0}

G.FUNCS.change_search_voucher_ante = function(x)
	Brainstorm.SETTINGS.autoreroll.searchVoucherAnteID = x.to_key
	Brainstorm.SETTINGS.autoreroll.searchVoucherAnte = Brainstorm.voucherAnteValues[x.to_val] or 1
	nativefs.write(lovely.mod_dir .. "/Brainstorm/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

Brainstorm.SearchTagList = {
	["None"]="",
	["Uncommon Tag"]="tag_uncommon",
	["Rare Tag"]="tag_rare",
	["Holographic Tag"]="tag_holo",
  ["Foil Tag"]="tag_foil",
	["Polychrome Tag"]="tag_polychrome",
	["Investment Tag"]="tag_investment",
	["Voucher Tag"]="tag_voucher",
	["Boss Tag"]="tag_boss",
	["Charm Tag"]="tag_charm",
	["Juggle Tag"]="tag_juggle",
	["Double Tag"]="tag_double",
	["Coupon Tag"]="tag_coupon",
	["Economy Tag"]="tag_economy",
	["Skip Tag"]="tag_skip",
	["D6 Tag"]="tag_d_six",
}

Brainstorm.SearchPackList = {
	["None"] = {},
	["Arcana"] = {"p_arcana_normal_1","p_arcana_normal_2","p_arcana_normal_3","p_arcana_normal_4","p_arcana_jumbo_1","p_arcana_jumbo_2","p_arcana_mega_1", "p_arcana_mega_2"},
	["Celestial"] = {"p_celestial_normal_1","p_celestial_normal_2","p_celestial_normal_3","p_celestial_normal_4","p_celestial_jumbo_1","p_celestial_jumbo_2","p_celestial_mega_1", "p_celestial_mega_2"},
	["Standard"] = {"p_standard_normal_1","p_standard_normal_2","p_standard_normal_3","p_standard_normal_4","p_standard_jumbo_1","p_standard_jumbo_2","p_standard_mega_1", "p_standard_mega_2"},
	["Buffoon"] = {"p_buffoon_normal_1","p_buffoon_normal_2","p_buffoon_jumbo_1","p_buffoon_mega_1"},
	["Spectral"] = {"p_spectral_normal_1","p_spectral_normal_2","p_spectral_jumbo_1","p_spectral_mega_1"},
	["Normal Arcana"] = {"p_arcana_normal_1","p_arcana_normal_2","p_arcana_normal_3","p_arcana_normal_4"},
	["Jumbo Arcana"] = {"p_arcana_jumbo_1","p_arcana_jumbo_2"},
	["Mega Arcana"] = {"p_arcana_mega_1", "p_arcana_mega_2"},
	["Normal Celestial"] = {"p_celestial_normal_1","p_celestial_normal_2","p_celestial_normal_3","p_celestial_normal_4"},
	["Jumbo Celestial"] = {"p_celestial_jumbo_1","p_celestial_jumbo_2"},
	["Mega Celestial"] = {"p_celestial_mega_1", "p_celestial_mega_2"},
	["Normal Standard"] = {"p_standard_normal_1","p_standard_normal_2","p_standard_normal_3","p_standard_normal_4"},
	["Jumbo Standard"] = {"p_standard_jumbo_1","p_standard_jumbo_2"},
	["Mega Standard"] = {"p_standard_mega_1", "p_standard_mega_2"},
	["Normal Buffoon"] = {"p_buffoon_normal_1","p_buffoon_normal_2"},
	["Jumbo Buffoon"] = {"p_buffoon_jumbo_1"},
	["Mega Buffoon"] = {"p_buffoon_mega_1"},
	["Normal Spectral"] = {"p_spectral_normal_1","p_spectral_normal_2"},
	["Jumbo Spectral"] = {"p_spectral_jumbo_1"},
	["Mega Spectral"] = {"p_spectral_mega_1"},
}
Brainstorm.seedsPerFrame = {
    ["500"] = 500,
    ["750"] = 750,
    ["1000"] = 1000,
    ["2500"] = 2500,
    ["5000"] = 5000,
    ["10000"] = 10000,
}

-- ============================================================================
-- Brainstorm: Jokers tab — sprite-based, self-refreshing UI
-- ============================================================================

-- Build a node that renders an actual joker card sprite (or an empty
-- placeholder box when key == "").
function Brainstorm.jokerSprite(key, w, h)
	local center = key ~= "" and G.P_CENTERS[key]
	if not center then
		return {n = G.UIT.B, config = {w = w, h = h, r = 0.1, colour = darken(G.C.BLACK, 0.05)}}
	end
	local spr = Sprite(0, 0, w, h, G.ASSET_ATLAS[center.atlas or "Joker"], center.pos)
	return {n = G.UIT.O, config = {object = spr, w = w, h = h}}
end

-- Look up a joker's display name from its key.
function Brainstorm.jokerNameForKey(key)
	if not key or key == "" then return "None" end
	for j, k in ipairs(Brainstorm.allJokerKeys or {}) do
		if k == key then return Brainstorm.allJokerNames[j] end
	end
	return key
end

-- Swap the contents of one of the tab's dynamic sections in place (same
-- mechanism the base game uses in G.FUNCS.change_tab).
function Brainstorm.refreshJokerSection(id, builder)
	if not G.OVERLAY_MENU then return end
	local node = G.OVERLAY_MENU:get_UIE_by_ID(id)
	if not node then return end
	if node.config.object then node.config.object:remove() end
	node.config.object = UIBox{
		definition = builder(),
		config = {offset = {x = 0, y = 0}, parent = node, type = "cm"},
	}
	node.UIBox:recalculate()
end

-- Filter the joker list into the shared results buffer. Only populated when
-- the user has actually typed something, so an empty search hides the list.
function Brainstorm.doJokerFilter()
	local ss = Brainstorm.jokerSearchState
	if not ss then return end
	local lower = (ss.search or ""):lower()
	local count = 0
	if lower ~= "" then
		for j, name in ipairs(Brainstorm.allJokerNames) do
			if name ~= "None" and name:lower():find(lower, 1, true) then
				count = count + 1
				ss.results[count].name = name
				ss.results[count].key  = Brainstorm.allJokerKeys[j] or ""
				if count >= 6 then break end
			end
		end
	end
	for j = count + 1, 6 do ss.results[j].name = ""; ss.results[j].key = "" end
	ss.listVisible = (lower ~= "" and count > 0)
end

-- Section: the pop-up list of matching jokers (visible only while searching).
function Brainstorm.buildJokerResults()
	local ss = Brainstorm.jokerSearchState
	local nodes = {}
	if ss and ss.listVisible then
		local row = {}
		for j = 1, 6 do
			local rs = ss.results[j]
			if rs.key ~= "" then
				row[#row + 1] = {n = G.UIT.C, config = {align = "cm", colour = G.C.CLEAR, padding = 0.05}, nodes = {
					{n = G.UIT.C, config = {
						align = "cm", colour = G.C.BLACK, r = 0.1, padding = 0.08,
						hover = true, shadow = true,
						button = "brainstorm_joker_pick_result", ref_table = rs,
						focus_args = {type = "none"},
					}, nodes = {
						Brainstorm.jokerSprite(rs.key, 0.7, 0.95),
					}},
				}}
			end
		end
		nodes[1] = {n = G.UIT.R, config = {align = "cm", padding = 0.04}, nodes = row}
	else
		nodes[1] = {n = G.UIT.R, config = {align = "cm", minh = 0.1}, nodes = {}}
	end
	return {n = G.UIT.ROOT, config = {align = "cm", colour = G.C.CLEAR}, nodes = nodes}
end

-- Section: the currently selected joker + "add to slot" 1/2/3 buttons.
function Brainstorm.buildJokerSelected()
	local ss = Brainstorm.jokerSearchState
	local sel = ss and ss.selected
	if not sel or sel.key == "" then
		return {n = G.UIT.ROOT, config = {align = "cm", colour = G.C.CLEAR}, nodes = {
			{n = G.UIT.R, config = {align = "cm", minh = 0.1}, nodes = {}},
		}}
	end
	-- Compact single row: sprite, "Add to slot:" and the 1/2/3 buttons side by
	-- side, so the selection preview stays short. The buttons sit in their own
	-- inner row so the tall sprite beside them can't stretch their height.
	local buttons = {}
	for i = 1, 3 do
		buttons[#buttons + 1] = {n = G.UIT.C, config = {
			align = "cm", colour = G.C.BLUE, r = 0.08,
			hover = true, shadow = true,
			button = "brainstorm_assign_to_slot", ref_table = {slotIdx = i},
			minw = 0.38, min_h = 0.38, padding = 0.04,
		}, nodes = {
			{n = G.UIT.T, config = {text = tostring(i), scale = 0.32, colour = G.C.UI.TEXT_LIGHT}},
		}}
	end
	local row = {
		Brainstorm.jokerSprite(sel.key, 0.9, 1.2),
		{n = G.UIT.C, config = {align = "cm", padding = 0.06}, nodes = {
			{n = G.UIT.T, config = {text = "Add to slot:", scale = 0.44, colour = G.C.UI.TEXT_LIGHT}},
		}},
		{n = G.UIT.C, config = {align = "cm", padding = 0.04}, nodes = {
			{n = G.UIT.R, config = {align = "cm"}, nodes = buttons},
		}},
	}
	return {n = G.UIT.ROOT, config = {align = "cm", colour = G.C.CLEAR}, nodes = {
		{n = G.UIT.R, config = {align = "cm", padding = 0.04}, nodes = row},
	}}
end

-- Section: the 3 configured joker slots, centered.
function Brainstorm.buildJokerSlots()
	local cards = {}
	for i = 1, 3 do
		local slot = Brainstorm.SETTINGS.autoreroll.jokerSlotData[i]
		local key = (slot and slot.key) or ""
		local filled = key ~= ""
		local negOn = slot and slot.requireNegative
		cards[#cards + 1] = {n = G.UIT.C, config = {align = "cm", colour = G.C.CLEAR, padding = 0.1, minw = 2.1}, nodes = {
			{n = G.UIT.R, config = {align = "cm", padding = 0.03}, nodes = {
				{n = G.UIT.T, config = {text = "Slot " .. i, scale = 0.42, colour = G.C.UI.TEXT_LIGHT}},
			}},
			{n = G.UIT.R, config = {align = "cm", padding = 0.04}, nodes = {
				Brainstorm.jokerSprite(key, 0.9, 1.2),
			}},
			{n = G.UIT.R, config = {align = "cm", padding = 0.02}, nodes = {
				{n = G.UIT.T, config = {text = filled and Brainstorm.jokerNameForKey(key) or "Empty", scale = 0.28, colour = filled and G.C.WHITE or G.C.UI.TEXT_INACTIVE}},
			}},
			{n = G.UIT.R, config = {align = "cm", padding = 0.05}, nodes = {
				{n = G.UIT.C, config = {
					align = "cm", colour = filled and G.C.RED or darken(G.C.BLACK, 0.05), r = 0.08,
					hover = filled, shadow = filled,
					button = filled and "brainstorm_joker_clear" or nil, ref_table = {slotIdx = i},
					minw = 1.1, min_h = 0.34, padding = 0.05,
				}, nodes = {
					{n = G.UIT.T, config = {text = "Remove", scale = 0.28, colour = filled and G.C.UI.TEXT_LIGHT or G.C.UI.TEXT_INACTIVE}},
				}},
			}},
			{n = G.UIT.R, config = {align = "cm", padding = 0.03}, nodes = {
				{n = G.UIT.C, config = {
					align = "cm", colour = negOn and G.C.SECONDARY_SET.Planet or darken(G.C.GREY, 0.2), r = 0.08,
					hover = true, shadow = true,
					button = "brainstorm_toggle_neg_slot", ref_table = {slotIdx = i},
					minw = 1.4, min_h = 0.34, padding = 0.05,
				}, nodes = {
					{n = G.UIT.T, config = {text = negOn and "Negative: ON" or "Negative: OFF", scale = 0.26, colour = G.C.UI.TEXT_LIGHT}},
				}},
			}},
		}}
	end
	return {n = G.UIT.ROOT, config = {align = "cm", colour = G.C.CLEAR}, nodes = {
		{n = G.UIT.R, config = {align = "cm", padding = 0.1}, nodes = cards},
	}}
end

G.FUNCS.brainstorm_toggle_neg_slot = function(e)
	local si = e.config.ref_table.slotIdx
	local slot = Brainstorm.SETTINGS.autoreroll.jokerSlotData[si]
	slot.requireNegative = not slot.requireNegative
	nativefs.write(lovely.mod_dir .. "/Brainstorm/settings.lua", STR_PACK(Brainstorm.SETTINGS))
	Brainstorm.refreshJokerSection("bs_joker_slots", Brainstorm.buildJokerSlots)
end

G.FUNCS.brainstorm_joker_pick_result = function(e)
	local rs = e.config.ref_table
	if not rs or rs.key == "" then return end
	if Brainstorm.jokerSearchState then
		Brainstorm.jokerSearchState.selected.name = rs.name
		Brainstorm.jokerSearchState.selected.key  = rs.key
		Brainstorm.jokerSearchState.listVisible   = false
	end
	Brainstorm.refreshJokerSection("bs_joker_results", Brainstorm.buildJokerResults)
	Brainstorm.refreshJokerSection("bs_joker_selected", Brainstorm.buildJokerSelected)
end

G.FUNCS.brainstorm_assign_to_slot = function(e)
	local si = e.config.ref_table.slotIdx
	if not Brainstorm.jokerSearchState then return end
	local ss = Brainstorm.jokerSearchState.selected
	if not ss or ss.key == "" then return end
	local slot = Brainstorm.SETTINGS.autoreroll.jokerSlotData[si]
	slot.key = ss.key
	slot.index = 1
	for j, k in ipairs(Brainstorm.allJokerKeys) do
		if k == slot.key then slot.index = j; break end
	end
	nativefs.write(lovely.mod_dir .. "/Brainstorm/settings.lua", STR_PACK(Brainstorm.SETTINGS))
	Brainstorm.refreshJokerSection("bs_joker_slots", Brainstorm.buildJokerSlots)
	-- Clear the selection so the "selected" area resets to default immediately.
	ss.name = "None"
	ss.key  = ""
	Brainstorm.refreshJokerSection("bs_joker_selected", Brainstorm.buildJokerSelected)
end

G.FUNCS.brainstorm_joker_clear = function(e)
	local si = e.config.ref_table.slotIdx
	local slot = Brainstorm.SETTINGS.autoreroll.jokerSlotData[si]
	slot.key = ""
	slot.index = 1
	nativefs.write(lovely.mod_dir .. "/Brainstorm/settings.lua", STR_PACK(Brainstorm.SETTINGS))
	Brainstorm.refreshJokerSection("bs_joker_slots", Brainstorm.buildJokerSlots)
end

local multiAnteSlotOptions = {"Off", "2", "4", "6", "8", "12", "16", "24", "32", "48", "64"}
local multiAnteSlotValues  = {["Off"]=0, ["2"]=2, ["4"]=4, ["6"]=6, ["8"]=8, ["12"]=12, ["16"]=16, ["24"]=24, ["32"]=32, ["48"]=48, ["64"]=64}

for ante = 1, 4 do
	local _ante = ante
	G.FUNCS["change_multi_ante_slots_" .. ante] = function(x)
		Brainstorm.SETTINGS.multiAnteSearch["ante" .. _ante .. "SlotsID"] = x.to_key
		Brainstorm.SETTINGS.multiAnteSearch["ante" .. _ante .. "Slots"] = multiAnteSlotValues[x.to_val] or 0
		nativefs.write(lovely.mod_dir .. "/Brainstorm/settings.lua", STR_PACK(Brainstorm.SETTINGS))
	end
end

local searchTagKeys = {"None", "Charm Tag", "Double Tag", "Uncommon Tag", "Rare Tag", "Holographic Tag", "Foil Tag", "Polychrome Tag", "Investment Tag", "Voucher Tag", "Boss Tag", "Juggle Tag", "Coupon Tag", "Economy Tag", "Skip Tag", "D6 Tag"}
local searchPackKeys = {"None", "Arcana", "Celestial", "Standard", "Buffoon", "Spectral", "Normal Arcana", "Jumbo Arcana", "Mega Arcana", "Normal Celestial", "Jumbo Celestial", "Mega Celestial", "Normal Standard", "Jumbo Standard", "Mega Standard", "Normal Buffoon", "Jumbo Buffoon", "Mega Buffoon", "Normal Spectral", "Jumbo Spectral", "Mega Spectral"}
local seedsPerFrame = {"500", "750", "1000", "2500", "5000", "10000"}

Brainstorm.G_FUNCS_options_ref = G.FUNCS.options
G.FUNCS.options = function(e)
	Brainstorm.G_FUNCS_options_ref(e)
end

local ct = create_tabs
function create_tabs(args)
	if args and args.tab_h == 7.05 then
		args.tabs[#args.tabs + 1] = {
			label = "Brainstorm",
			tab_definition_function = function()
				local voucherNames = {"None"}
				local voucherKeyByName = {["None"] = ""}
				local voucherList = {}
				for k, v in ipairs(G.P_CENTER_POOLS['Voucher']) do
					if v.unlocked ~= false and not v.requires then
						voucherList[#voucherList+1] = {name = v.name or v.key, key = v.key}
					end
				end
				table.sort(voucherList, function(a, b) return a.name < b.name end)
				for _, item in ipairs(voucherList) do
					voucherNames[#voucherNames+1] = item.name
					voucherKeyByName[item.name] = item.key
				end
				Brainstorm.voucherKeyByName = voucherKeyByName
				local savedVoucher = Brainstorm.SETTINGS.autoreroll.searchVoucher
				local voucherCurrent = 1
				if savedVoucher and savedVoucher ~= "" then
					for i, n in ipairs(voucherNames) do
						if voucherKeyByName[n] == savedVoucher then voucherCurrent = i; break end
					end
				end
				-- Left column: the core search settings
				local leftColumn = {n = G.UIT.C, config = {align = "cm", padding = 0.08}, nodes = {
					create_option_cycle({
						label = "Search Tag",
						scale = 0.8,
						w = 4,
						options = searchTagKeys,
						opt_callback = "change_search_tag",
						current_option = Brainstorm.SETTINGS.autoreroll.searchTagID or 1,
					}),
					create_option_cycle({
						label = "Search Pack",
						scale = 0.8,
						w = 4,
						options = searchPackKeys,
						opt_callback = "change_search_pack",
						current_option = Brainstorm.SETTINGS.autoreroll.searchPackID or 1,
					}),
					create_option_cycle({
						label = "Search Voucher",
						scale = 0.8,
						w = 4,
						options = voucherNames,
						opt_callback = "change_search_voucher",
						current_option = voucherCurrent,
					}),
					create_option_cycle({
						label = "Voucher Ante",
						scale = 0.8,
						w = 4,
						options = Brainstorm.voucherAnteOptions,
						opt_callback = "change_search_voucher_ante",
						current_option = Brainstorm.SETTINGS.autoreroll.searchVoucherAnteID or 1,
					}),
				}}

				-- Right column: legendary + misc settings. The legendary cycle bar
				-- is tinted to the selected legendary's sprite colour.
				Brainstorm.applyLegendaryBarColour()
				Brainstorm.applyNegLegendaryDisplay()
				local rightColumn = {n = G.UIT.C, config = {align = "cm", padding = 0.08}, nodes = {
					create_option_cycle({
						label = "Search Legendary",
						scale = 0.8,
						w = 4,
						colour = Brainstorm.legendaryBarColour,
						options = searchLegendaryKeys,
						opt_callback = "change_search_legendary",
						current_option = Brainstorm.SETTINGS.autoreroll.searchLegendaryID or 1,
					}),
					-- Negative toggle, styled like the joker-slot negative button.
					{n = G.UIT.R, config = {align = "cm", padding = 0.06}, nodes = {
						{n = G.UIT.C, config = {
							align = "cm", colour = Brainstorm.negLegendaryColour, r = 0.08,
							hover = true, shadow = true,
							button = "brainstorm_toggle_neg_legendary", ref_table = {},
							minw = 2.2, min_h = 0.5, padding = 0.08,
						}, nodes = {
							{n = G.UIT.T, config = {ref_table = Brainstorm.negLegendaryDisplay, ref_value = "text", scale = 0.35, colour = G.C.UI.TEXT_LIGHT}},
						}},
					}},
					create_option_cycle({
						label = "Rerolls per Frame",
						scale = 0.8,
						w = 4,
						options = seedsPerFrame,
						opt_callback = "change_seeds_per_frame",
						current_option = Brainstorm.SETTINGS.autoreroll.seedsPerFrameID or 1,
					}),
					create_toggle({
						label = "Debug Mode",
						ref_table = Brainstorm.SETTINGS,
						ref_value = "debug_mode",
						callback = function(_set_toggle)
							_RELEASE_MODE = not Brainstorm.SETTINGS.debug_mode
							G.F_NO_ACHIEVEMENTS = Brainstorm.SETTINGS.debug_mode
						end,
					}),
				}}

				return {
					n = G.UIT.ROOT,
					config = {
						align = "cm",
						padding = 0.05,
						colour = G.C.CLEAR,
						minh = 6.5,
					},
					nodes = {
						{n = G.UIT.R, config = {align = "cm", padding = 0.1}, nodes = {
							leftColumn,
							rightColumn,
						}},
					},
				}
			end,
			tab_definition_function_args = "Brainstorm",
		}
		args.tabs[#args.tabs + 1] = {
			label = "Brainstorm: Jokers",
			tab_definition_function = function()
				Brainstorm.allJokerNames = {"None"}
				Brainstorm.allJokerKeys  = {""}
				local allJokers = {}
				for _, v in pairs(G.P_CENTER_POOLS["Joker"]) do
					-- Legendaries have their own search on the Brainstorm tab.
					if v.rarity ~= 4 then
						allJokers[#allJokers+1] = {name = v.name or v.key, key = v.key}
					end
				end
				table.sort(allJokers, function(a, b) return a.name < b.name end)
				for _, j in ipairs(allJokers) do
					Brainstorm.allJokerNames[#Brainstorm.allJokerNames+1] = j.name
					Brainstorm.allJokerKeys[#Brainstorm.allJokerKeys+1]  = j.key
				end

				if not Brainstorm.SETTINGS.autoreroll.jokerSlotData then
					Brainstorm.SETTINGS.autoreroll.jokerSlotData = {}
				end
				for i = 1, 3 do
					if not Brainstorm.SETTINGS.autoreroll.jokerSlotData[i] then
						Brainstorm.SETTINGS.autoreroll.jokerSlotData[i] = {index = 1, key = ""}
					end
					local slot = Brainstorm.SETTINGS.autoreroll.jokerSlotData[i]
					slot.index = 1
					if slot.key and slot.key ~= "" then
						for j, k in ipairs(Brainstorm.allJokerKeys) do
							if k == slot.key then slot.index = j; break end
						end
					end
				end

				-- Single shared search state (fresh each time the tab opens)
				local results = {}
				for j = 1, 6 do results[j] = {name = "", key = ""} end
				Brainstorm.jokerSearchState = {
					search      = "",
					selected    = {name = "None", key = ""},
					results     = results,
					listVisible = false,
				}
				Brainstorm.doJokerFilter()
				Brainstorm.applyMatchAnyDisplay()

				-- Centered, enlarged search bar
				local inputArgs = {
					ref_table = Brainstorm.jokerSearchState, ref_value = "search",
					w = 5, h = 0.7, text_scale = 0.5, max_length = 30,
					prompt_text = "Search jokers...",
					extended_corpus = true,
					callback = function()
						Brainstorm.doJokerFilter()
						Brainstorm.refreshJokerSection("bs_joker_results", Brainstorm.buildJokerResults)
					end,
				}

				-- Each dynamic section is a UIT.O holding its own UIBox, swapped
				-- in place on state changes (same pattern as G.FUNCS.change_tab).
				local resultsSection = {n = G.UIT.R, config = {align = "cm", colour = G.C.CLEAR, padding = 0.02}, nodes = {
					{n = G.UIT.O, config = {id = "bs_joker_results", object = UIBox{
						definition = Brainstorm.buildJokerResults(),
						config = {offset = {x = 0, y = 0}},
					}}},
				}}
				local selectedSection = {n = G.UIT.R, config = {align = "cm", colour = G.C.CLEAR, padding = 0.02}, nodes = {
					{n = G.UIT.O, config = {id = "bs_joker_selected", object = UIBox{
						definition = Brainstorm.buildJokerSelected(),
						config = {offset = {x = 0, y = 0}},
					}}},
				}}
				local slotsSection = {n = G.UIT.R, config = {align = "cm", colour = G.C.CLEAR, padding = 0.02}, nodes = {
					{n = G.UIT.O, config = {id = "bs_joker_slots", object = UIBox{
						definition = Brainstorm.buildJokerSlots(),
						config = {offset = {x = 0, y = 0}},
					}}},
				}}

				-- Match-mode toggle: whether a seed needs ALL 3 jokers or ANY 1.
				local matchModeRow = {n = G.UIT.R, config = {align = "cm", padding = 0.04}, nodes = {
					{n = G.UIT.C, config = {
						align = "cm", colour = Brainstorm.matchAnyColour, r = 0.08,
						hover = true, shadow = true,
						button = "brainstorm_toggle_match_any", ref_table = {},
						minw = 2.8, min_h = 0.45, padding = 0.08,
					}, nodes = {
						{n = G.UIT.T, config = {ref_table = Brainstorm.matchAnyDisplay, ref_value = "text", scale = 0.34, colour = G.C.UI.TEXT_LIGHT}},
					}},
				}}

				return {
					n = G.UIT.ROOT,
					config = {align = "cm", padding = 0.08, colour = G.C.CLEAR, minh = 6.5},
					nodes = {
						{n = G.UIT.R, config = {align = "cm", padding = 0.1}, nodes = {create_text_input(inputArgs)}},
						resultsSection,
						selectedSection,
						{n = G.UIT.R, config = {align = "cm", padding = 0.08}, nodes = {
							{n = G.UIT.B, config = {w = 5, h = 0.02, colour = G.C.WHITE}},
						}},
						matchModeRow,
						slotsSection,
					},
				}
			end,
			tab_definition_function_args = "BrainstormJokers",
		}
		args.tabs[#args.tabs + 1] = {
			label = "Brainstorm: Multi-Ante",
			tab_definition_function = function()
				local cfg = Brainstorm.SETTINGS.multiAnteSearch or {}
				-- One row per ante: shop-slot cycle on the left, its pack toggle on the right.
				local anteRows = {}
				for ante = 1, 4 do
					-- Label sits directly above the bar (shared column) so it stays centred
					-- on the bar. The toggle column uses an invisible label of the same size
					-- so the Packs toggle drops to the bar's vertical middle.
					local shopLabel = "Ante " .. ante .. " Shop Slots"
					anteRows[ante] = {n = G.UIT.R, config = {align = "cm", padding = 0.02}, nodes = {
						{n = G.UIT.C, config = {align = "cm"}, nodes = {
							{n = G.UIT.R, config = {align = "cm"}, nodes = {
								{n = G.UIT.T, config = {text = shopLabel, scale = 0.4, colour = G.C.UI.TEXT_LIGHT}},
							}},
							{n = G.UIT.R, config = {align = "cm"}, nodes = {
								create_option_cycle({
									scale = 0.8,
									w = 4,
									options = multiAnteSlotOptions,
									opt_callback = "change_multi_ante_slots_" .. ante,
									current_option = cfg["ante" .. ante .. "SlotsID"] or 1,
								}),
							}},
						}},
						{n = G.UIT.C, config = {align = "cm", padding = 0}, nodes = {
							{n = G.UIT.R, config = {align = "cm"}, nodes = {
								{n = G.UIT.T, config = {text = shopLabel, scale = 0.4, colour = G.C.CLEAR}},
							}},
							{n = G.UIT.R, config = {align = "cm"}, nodes = {
								create_toggle({
									label = "Packs",
									w = 1.2,
									ref_table = Brainstorm.SETTINGS.multiAnteSearch,
									ref_value = "ante" .. ante .. "Packs",
									callback = function() nativefs.write(lovely.mod_dir .. "/Brainstorm/settings.lua", STR_PACK(Brainstorm.SETTINGS)) end,
								}),
							}},
						}},
					}}
				end
				return {
					n = G.UIT.ROOT,
					config = { align = "cm", padding = 0.05, colour = G.C.CLEAR, minh = 6.5 },
					nodes = anteRows,
				}
			end,
			tab_definition_function_args = "BrainstormMultiAnte",
		}
	end
	return ct(args)
end

function saveManagerAlert(text)
	G.E_MANAGER:add_event(Event({
		trigger = "after",
		delay = 0.4,
		func = function()
			attention_text({
				text = text,
				scale = 0.7,
				hold = 3,
				major = G.STAGE == G.STAGES.RUN and G.play or G.title_top,
				backdrop_colour = G.C.SECONDARY_SET.Tarot,
				align = "cm",
				offset = {
					x = 0,
					y = -3.5,
				},
				silent = true,
			})
			G.E_MANAGER:add_event(Event({
				trigger = "after",
				delay = 0.06 * G.SETTINGS.GAMESPEED,
				blockable = false,
				blocking = false,
				func = function()
					play_sound("other1", 0.76, 0.4)
					return true
				end,
			}))
			return true
		end,
	}))
end

function Brainstorm.showJokerFoundAlert(text)
	G.E_MANAGER:add_event(Event({
		trigger = "after",
		delay = 0.4,
		blockable = false,
		blocking = false,
		func = function()
			local jokerText = Brainstorm.attention_text({
				scale = 0.7,
				text = text,
				align = 'cm',
				offset = {x = 0, y = -3.5},
				major = G.STAGE == G.STAGES.RUN and G.play or G.title_top,
			})
			G.E_MANAGER:add_event(Event({
				trigger = "after",
				delay = 3 * (G.SPEEDFACTOR or 1),
				blockable = false,
				blocking = false,
				func = function()
					Brainstorm.remove_attention_text(jokerText)
					return true
				end,
			}))
			return true
		end,
	}))
end