local nativefs = require("nativefs")

local searchLegendaryKeys = {"None", "Caino", "Triboulet", "Yorick", "Chicot", "Perkeo"}
local legendaryKeyByName = {["None"]="", ["Caino"]="j_caino", ["Triboulet"]="j_triboulet", ["Yorick"]="j_yorick", ["Chicot"]="j_chicot", ["Perkeo"]="j_perkeo"}

-- Each legendary's card-background colour, sampled from its card face in
-- Jokers.png (the dominant card-face colour, not the character).
Brainstorm.legendaryColours = {
	["j_caino"]     = {0.75, 0.80, 0.85, 1}, -- Caino: pale blue-grey
	["j_triboulet"] = {0.56, 0.75, 0.89, 1}, -- Triboulet: light blue
	["j_yorick"]    = {0.89, 0.71, 0.38, 1}, -- Yorick: gold
	["j_chicot"]    = {0.89, 0.52, 0.52, 1}, -- Chicot: red
	["j_perkeo"]    = {0.52, 0.71, 0.66, 1}, -- Perkeo: teal-green
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

local legendaryNameByKey = {[""]="None", ["j_caino"]="Caino", ["j_triboulet"]="Triboulet", ["j_yorick"]="Yorick", ["j_chicot"]="Chicot", ["j_perkeo"]="Perkeo"}

-- Texture layer that sits on the FACE of the (native, still-3D) legendary bar.
-- Extends Sprite; crops a wide horizontal band of the card's background (matching
-- the bar's aspect so there's no stretch/distortion) and overlays the name. The
-- overlay draw is pcall-guarded so it can never break the settings menu.
local LegendaryTex = Sprite:extend()

function LegendaryTex:init(X, Y, W, H)
	self.bs_ar = (W or 3) / (H or 0.5)
	Sprite.init(self, X, Y, W, H, G.ASSET_ATLAS["Joker"], G.P_CENTERS["j_perkeo"].pos)
	self.bs_label = DynaText({string = {"None"}, colours = {{1, 1, 1, 1}}, shadow = true, scale = 0.4, silent = true})
	-- A bare DynaText registers itself in G.I.MOVEABLE and the engine draws every
	-- parentless MOVEABLE each frame (game.lua:2767) -- at its default position,
	-- i.e. the top-left corner. Parenting it to us makes the engine skip it, so it
	-- only renders via our positioned draw() below.
	self.bs_label.parent = self
	self:bs_apply()
end

-- The tab teardown calls remove() on UIT.O objects (ui.lua:1010), but the label
-- is our own child, not a UI node -- without this override it would stay in
-- G.I.MOVEABLE forever and a new copy would pile up in the corner every time
-- the tab is opened and closed.
function LegendaryTex:remove()
	if self.bs_label then
		self.bs_label:remove()
		self.bs_label = nil
	end
	Sprite.remove(self)
end

function LegendaryTex:set_sprite_pos(pos)
	Sprite.set_sprite_pos(self, pos)
	-- Crop a wide, undistorted band of the card background (upper area, above the
	-- character) with the border trimmed, matching the bar's aspect ratio.
	local px, py = self.atlas.px, self.atlas.py
	local insetX = 9
	local cw = px - 2 * insetX
	local ch = math.max(4, math.floor(cw / (self.bs_ar or 5)))
	local ax = self.sprite_pos.x * px + insetX
	local ay = self.sprite_pos.y * py + 8
	self.sprite = love.graphics.newQuad(ax, ay, cw, ch, self.atlas.image:getDimensions())
	self.scale = {x = cw, y = ch}
end

function LegendaryTex:bs_apply()
	local key = Brainstorm.SETTINGS.autoreroll.searchLegendary or ""
	local center = key ~= "" and G.P_CENTERS[key]
	if center then self:set_sprite_pos(center.pos) end
	self.bs_show = center and true or false
	if self.bs_label then
		self.bs_label.config.string = { legendaryNameByKey[key] or "None" }
		pcall(function() self.bs_label:update_text(true) end)
	end
end

function LegendaryTex:draw()
	local w, h = self.VT.w, self.VT.h
	-- prep_draw puts us in the sprite's LOCAL space in game units: (0,0) is the
	-- face's top-left, (w,h) its bottom-right (same transform DynaText uses).
	prep_draw(self, 1)
	love.graphics.push("all")
	if self.bs_show and self.atlas and self.sprite then
		-- Card-background band, scaled to exactly fill the face. NOTE: no stencil
		-- clipping here -- the game's canvases are created without stencil buffers
		-- (main.lua:379/386), so love.graphics.stencil would error. Instead the
		-- corners are visually rounded below with rim-coloured patches.
		local qx, qy, qw, qh = self.sprite:getViewport()
		love.graphics.setColor(1, 1, 1, 1)
		love.graphics.draw(self.atlas.image, self.sprite, 0, 0, 0, w / qw, h / qh)
		-- Stepped vertical gradient over the texture = the 3D curvature: lit along
		-- the top edge, falling into shadow at the bottom lip (reads like the
		-- vanilla embossed buttons, but in the card's own colours).
		love.graphics.setColor(1, 1, 1, 0.16)
		love.graphics.rectangle("fill", 0, 0, w, h * 0.16)
		love.graphics.setColor(1, 1, 1, 0.07)
		love.graphics.rectangle("fill", 0, h * 0.16, w, h * 0.18)
		love.graphics.setColor(0, 0, 0, 0.09)
		love.graphics.rectangle("fill", 0, h * 0.60, w, h * 0.18)
		love.graphics.setColor(0, 0, 0, 0.22)
		love.graphics.rectangle("fill", 0, h * 0.78, w, h * 0.22)
		-- Round the inlay's corners by painting the corner notches in the bar's
		-- rim colour (sampled quarter-arc polygons; the bar behind is the same
		-- colour so the patches blend into the frame seamlessly).
		local cr = 0.07
		local bc = Brainstorm.legendaryBarColour
		love.graphics.setColor(bc[1], bc[2], bc[3], bc[4] or 1)
		local corners = {
			{cr, cr, math.pi, 1.5 * math.pi, 0, 0},
			{w - cr, cr, 1.5 * math.pi, 2 * math.pi, w, 0},
			{w - cr, h - cr, 0, 0.5 * math.pi, w, h},
			{cr, h - cr, 0.5 * math.pi, math.pi, 0, h},
		}
		for _, c in ipairs(corners) do
			-- The notch (square corner minus quarter disc) is concave, so fill it
			-- as a triangle fan from the corner point (the region is star-shaped
			-- from there), since love's polygon fill assumes convex input.
			local prevx, prevy
			for s = 0, 8 do
				local a = c[3] + (c[4] - c[3]) * s / 8
				local ax = c[1] + cr * math.cos(a)
				local ay = c[2] + cr * math.sin(a)
				if s > 0 then
					love.graphics.polygon("fill", c[5], c[6], prevx, prevy, ax, ay)
				end
				prevx, prevy = ax, ay
			end
		end
		-- Thin dark inner outline for definition against the coloured rim.
		love.graphics.setColor(0, 0, 0, 0.28)
		love.graphics.setLineWidth(0.02)
		love.graphics.rectangle("line", 0.01, 0.01, w - 0.02, h - 0.02, cr, cr)
	end
	-- Option pips, drawn by us so they sit ON the texture (bottom-centred).
	local n = #searchLegendaryKeys
	local pip, gap = 0.07, 0.06
	local total = n * pip + (n - 1) * gap
	local px = (w - total) / 2
	local py = h - 0.13
	local cur = Brainstorm.SETTINGS.autoreroll.searchLegendaryID or 1
	for i = 1, n do
		-- soft shadow under each pip so they read on any texture
		love.graphics.setColor(0, 0, 0, 0.35)
		love.graphics.rectangle("fill", px + (i - 1) * (pip + gap) + 0.012, py + 0.012, pip, pip, 0.035, 0.035)
		if i == cur then
			love.graphics.setColor(1, 1, 1, 1)
		else
			love.graphics.setColor(0, 0, 0, 0.5)
		end
		love.graphics.rectangle("fill", px + (i - 1) * (pip + gap), py, pip, pip, 0.035, 0.035)
	end
	love.graphics.setColor(1, 1, 1, 1)
	love.graphics.pop()
	love.graphics.pop() -- prep_draw's push
	-- Name label, truly centred: DynaText measures its text into T.w/T.h, so
	-- centre that box on the region above the pips row.
	local lbl = self.bs_label
	if lbl and self.VT then
		pcall(function()
			lbl.VT.x = self.VT.x + (self.VT.w - lbl.T.w) / 2
			lbl.VT.y = self.VT.y + (self.VT.h - 0.14 - lbl.T.h) / 2
			lbl.VT.w, lbl.VT.h = lbl.T.w, lbl.T.h
			lbl.VT.r, lbl.VT.scale = 0, 1
			lbl:draw()
		end)
	end
end

-- Sized to the vanilla bar's inner content area: the other cycles at scale=0.8,
-- w=4 build a face of minw 3.2 x minh 0.64 with padding 0.05, so the content
-- region is 3.1 x 0.54 -- filling exactly that keeps the outer box identical to
-- every other bar while the coloured rim frames the texture.
function Brainstorm.makeLegendaryTex()
	Brainstorm.legendaryTex = LegendaryTex(0, 0, 3.1, 0.54)
	return Brainstorm.legendaryTex
end

function Brainstorm.applyLegendaryTex()
	if Brainstorm.legendaryTex then pcall(function() Brainstorm.legendaryTex:bs_apply() end) end
end

G.FUNCS.change_search_legendary = function(x)
	Brainstorm.SETTINGS.autoreroll.searchLegendaryID = x.to_key
	Brainstorm.SETTINGS.autoreroll.searchLegendary = legendaryKeyByName[x.to_val]
	Brainstorm.applyLegendaryBarColour()
	Brainstorm.applyLegendaryTex()
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
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
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

-- Per-frame visual on the negative-search buttons, stealing the collection's
-- negative-edition badge look: OFF keeps the normal grey button; ON paints the
-- box dark like a DARK_EDITION badge while its text shimmers bright, shifting
-- blue/white/lavender (the negative palette). Colour tables are mutated in place,
-- exactly like the vanilla 'flash' func. Both the box colour and the child text
-- colour must be dedicated tables (never a global G.C ref).
G.FUNCS.brainstorm_negative_shimmer = function(e)
	local rt = e.config.ref_table
	local on
	if rt and rt.slotIdx then
		local slot = Brainstorm.SETTINGS.autoreroll.jokerSlotData[rt.slotIdx]
		on = slot and slot.requireNegative
	else
		on = Brainstorm.SETTINGS.autoreroll.searchNegativeLegendary
	end
	local c = e.config.colour
	local txt = e.children and e.children[1]
	local tc = txt and txt.config and txt.config.colour
	if on then
		-- Shifting blue/white/lavender box = the visible effect.
		local t = (G.TIMERS and G.TIMERS.REAL) or 0
		local s = 0.5 + 0.5 * math.sin(t * 3.0)
		c[1] = 0.65 + 0.35 * s
		c[2] = 0.70 + 0.30 * (1 - s)
		c[3] = 1.0
		c[4] = 1
		-- White text on top.
		if tc then tc[1], tc[2], tc[3], tc[4] = 1, 1, 1, 1 end
	else
		local src = darken(G.C.GREY, 0.2)
		c[1], c[2], c[3], c[4] = src[1], src[2], src[3], src[4]
		if tc then
			local L = G.C.UI.TEXT_LIGHT
			tc[1], tc[2], tc[3], tc[4] = L[1], L[2], L[3], L[4]
		end
	end
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
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

G.FUNCS.change_search_voucher = function(x)
	Brainstorm.SETTINGS.autoreroll.searchVoucherID = x.to_key
	Brainstorm.SETTINGS.autoreroll.searchVoucher = Brainstorm.voucherKeyByName[x.to_val]
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

-- Which ante the searched voucher must be offered at. Vouchers roll from one
-- shared RNG stream advanced once per ante, so "Ante N" means the Nth voucher.
-- New options are appended so saved searchVoucherAnteID indices stay valid.
-- -1 = any of antes 1-8 (deep-search companion to the Anywhere joker mode).
Brainstorm.voucherAnteOptions = {"Ante 1", "Ante 2", "Ante 3", "Ante 4", "Any (1-4)",
	"Ante 5", "Ante 6", "Ante 7", "Ante 8", "Any (1-8)"}
Brainstorm.voucherAnteValues  = {["Ante 1"]=1, ["Ante 2"]=2, ["Ante 3"]=3, ["Ante 4"]=4, ["Any (1-4)"]=0,
	["Ante 5"]=5, ["Ante 6"]=6, ["Ante 7"]=7, ["Ante 8"]=8, ["Any (1-8)"]=-1}

G.FUNCS.change_search_voucher_ante = function(x)
	Brainstorm.SETTINGS.autoreroll.searchVoucherAnteID = x.to_key
	Brainstorm.SETTINGS.autoreroll.searchVoucherAnte = Brainstorm.voucherAnteValues[x.to_val] or 1
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

-- Where a found seed is delivered. "Current run" overwrites the live run
-- immediately (old behavior); "Slot N" banks the seed to that save slot and
-- leaves the current run untouched (load it later with the load keybind).
Brainstorm.foundSeedSlotOptions = {"Current run", "Slot 1", "Slot 2", "Slot 3", "Slot 4", "Slot 5"}
Brainstorm.foundSeedSlotValues  = {["Current run"]=0, ["Slot 1"]=1, ["Slot 2"]=2, ["Slot 3"]=3, ["Slot 4"]=4, ["Slot 5"]=5}

G.FUNCS.change_found_seed_slot = function(x)
	Brainstorm.SETTINGS.autoreroll.foundSeedSlotID = x.to_key
	Brainstorm.SETTINGS.autoreroll.foundSeedSlot = Brainstorm.foundSeedSlotValues[x.to_val] or 0
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

-- Seed pool (.bspool) restricting the native search to a prebuilt match set.
-- Options are rebuilt from <mod>/seed_pools/ every time the tab opens, so a
-- freshly copied-in pool shows up without a restart. The setting stores the
-- FILENAME (not the index): the list can change between sessions.
G.FUNCS.change_seed_pool = function(x)
	Brainstorm.SETTINGS.autoreroll.seedPoolFile = (x.to_val ~= "None") and x.to_val or ""
	Brainstorm.UI_POOL_INFO.text = Brainstorm.poolInfoString(Brainstorm.SETTINGS.autoreroll.seedPoolFile)
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

-- Identity line under the Seed Pool cycler (label / pool_id / space /
-- records). A ref'd table so the text swaps live as the cycler moves.
Brainstorm.UI_POOL_INFO = { text = "" }

-- Stake the found run starts at (applies to both "Current run" overwrite and
-- banked slots). Only the gameplay-relevant tiers: White (base), Black
-- (eternals in shop), Gold (all modifiers). Stake never changes the RNG streams
-- the filters read (stake modifiers only gate what the rolled values DO), so
-- the same found seed is valid at any of these.
Brainstorm.foundStakeOptions = {"White Stake", "Black Stake", "Gold Stake"}
Brainstorm.foundStakeValues  = {["White Stake"]=1, ["Black Stake"]=4, ["Gold Stake"]=8}

G.FUNCS.change_found_seed_stake = function(x)
	Brainstorm.SETTINGS.autoreroll.foundSeedStakeID = x.to_key
	Brainstorm.SETTINGS.autoreroll.foundSeedStake = Brainstorm.foundStakeValues[x.to_val] or 1
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
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
-- placeholder box when key == ""). Wildcard keys ("*rare" etc., no center to
-- draw) render as a rarity-coloured chip instead.
function Brainstorm.jokerSprite(key, w, h)
	local wild = key ~= "" and Brainstorm.WILDCARD_RARITY and Brainstorm.WILDCARD_RARITY[key]
	if wild then
		local label = ({[0] = "ANY", [1] = "C", [2] = "U", [3] = "R"})[wild] or "?"
		local col = (wild >= 1 and G.C.RARITY and G.C.RARITY[wild]) or G.C.BLUE
		return {n = G.UIT.C, config = {align = "cm", minw = w, minh = h, r = 0.1, colour = darken(G.C.BLACK, 0.05)}, nodes = {
			{n = G.UIT.T, config = {text = label, scale = 0.34, colour = col}},
		}}
	end
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
					-- Fresh per-button colour table (never a global G.C ref) so the
					-- shimmer func can mutate it in place safely.
					align = "cm", colour = {0, 0, 0, 1}, r = 0.08,
					hover = true, shadow = true,
					button = "brainstorm_toggle_neg_slot", ref_table = {slotIdx = i},
					func = "brainstorm_negative_shimmer",
					minw = 1.4, min_h = 0.34, padding = 0.05,
				}, nodes = {
					{n = G.UIT.T, config = {text = negOn and "Negative: ON" or "Negative: OFF", scale = 0.26, colour = {1, 1, 1, 1}}},
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
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
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
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
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
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
	Brainstorm.refreshJokerSection("bs_joker_slots", Brainstorm.buildJokerSlots)
end

local multiAnteSlotOptions = {"Off", "2", "4", "6", "8", "12", "16", "24", "32", "48", "64"}
local multiAnteSlotValues  = {["Off"]=0, ["2"]=2, ["4"]=4, ["6"]=6, ["8"]=8, ["12"]=12, ["16"]=16, ["24"]=24, ["32"]=32, ["48"]=48, ["64"]=64}

for ante = 1, 4 do
	local _ante = ante
	G.FUNCS["change_multi_ante_slots_" .. ante] = function(x)
		Brainstorm.SETTINGS.multiAnteSearch["ante" .. _ante .. "SlotsID"] = x.to_key
		Brainstorm.SETTINGS.multiAnteSearch["ante" .. _ante .. "Slots"] = multiAnteSlotValues[x.to_val] or 0
		nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
	end
end

-- "Anywhere" mode: search every ante 1-8 at one uniform shop depth with packs
-- on, ignoring the per-ante rows below. Depth stays modest by design so found
-- seeds are affordable to actually reach (rerolls cost money).
local anywhereSlotOptions = {"4", "6", "8", "12", "16"}
local anywhereSlotValues  = {["4"]=4, ["6"]=6, ["8"]=8, ["12"]=12, ["16"]=16}

G.FUNCS.change_anywhere_slots = function(x)
	Brainstorm.SETTINGS.multiAnteSearch.anywhereSlotsID = x.to_key
	Brainstorm.SETTINGS.multiAnteSearch.anywhereSlots = anywhereSlotValues[x.to_val] or 8
	nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
end

local searchTagKeys = {"None", "Charm Tag", "Double Tag", "Uncommon Tag", "Rare Tag", "Holographic Tag", "Foil Tag", "Polychrome Tag", "Investment Tag", "Voucher Tag", "Boss Tag", "Juggle Tag", "Coupon Tag", "Economy Tag", "Skip Tag", "D6 Tag"}
local searchPackKeys = {"None", "Arcana", "Celestial", "Standard", "Buffoon", "Spectral", "Normal Arcana", "Jumbo Arcana", "Mega Arcana", "Normal Celestial", "Jumbo Celestial", "Mega Celestial", "Normal Standard", "Jumbo Standard", "Mega Standard", "Normal Buffoon", "Jumbo Buffoon", "Mega Buffoon", "Normal Spectral", "Jumbo Spectral", "Mega Spectral"}
local seedsPerFrame = {"500", "750", "1000", "2500", "5000", "10000"}

-- Parallel search worker count. "Auto" (=0) resolves to cores-1 at search start.
Brainstorm.searchThreadsOptions = {"Auto", "1", "2", "3", "4", "6", "8", "12", "16"}
Brainstorm.searchThreadsValues  = {["Auto"]=0, ["1"]=1, ["2"]=2, ["3"]=3, ["4"]=4, ["6"]=6, ["8"]=8, ["12"]=12, ["16"]=16}

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
				-- Seed pools: whatever .bspool files are in <mod>/seed_pools/
				-- right now (created here so drag-and-drop sharing "just works").
				local poolDir = Brainstorm.seedPoolDir and Brainstorm.seedPoolDir()
					or (Brainstorm.modPath() .. "/seed_pools")
				nativefs.createDirectory(poolDir)
				local poolNames = {"None"}
				local poolItems = nativefs.getDirectoryItems(poolDir) or {}
				table.sort(poolItems)
				for _, fn in ipairs(poolItems) do
					if fn:match("%.bspool$") then poolNames[#poolNames + 1] = fn end
				end
				local savedPool = Brainstorm.SETTINGS.autoreroll.seedPoolFile
				local poolCurrent = 1
				if savedPool and savedPool ~= "" then
					local seen = false
					for i, n in ipairs(poolNames) do
						if i > 1 and n == savedPool then poolCurrent = i; seen = true; break end
					end
					-- Selected pool no longer on disk: still show (and keep) the
					-- selection so a temporarily missing drive doesn't silently
					-- turn a pool search into a full-space search.
					if not seen then
						poolNames[#poolNames + 1] = savedPool
						poolCurrent = #poolNames
					end
				end
				Brainstorm.UI_POOL_INFO.text = Brainstorm.poolInfoString(savedPool or "")
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
					{n = G.UIT.R, config = {align = "cm", padding = 0.02}, nodes = {
						create_toggle({
							label = "Tag: any blind, antes 1-8",
							label_scale = 0.32,
							w = 1.2,
							ref_table = Brainstorm.SETTINGS.autoreroll,
							ref_value = "searchTagAnywhere",
							callback = function() nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS)) end,
						}),
					}},
					create_option_cycle({
						label = "Seed Pool",
						scale = 0.8,
						w = 4,
						options = poolNames,
						opt_callback = "change_seed_pool",
						current_option = poolCurrent,
					}),
					{n = G.UIT.R, config = {align = "cm", padding = 0.02}, nodes = {
						{n = G.UIT.T, config = {
							ref_table = Brainstorm.UI_POOL_INFO,
							ref_value = "text",
							scale = 0.26,
							colour = G.C.UI.TEXT_INACTIVE,
						}},
					}},
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
						no_pips = true,
						-- Exact clone of the vanilla cycle bar box (same minw/minh/r/padding/
						-- emboss as the other bars at scale=0.8, w=4) so size, shape and the 3D
						-- emboss match; the card-background texture + name + pips render on its
						-- face via LegendaryTex:draw.
						mid = {n = G.UIT.C, config = {id = "cycle_main", align = "cm", minw = 3.2, minh = 0.64, r = 0.1, padding = 0.05, colour = Brainstorm.legendaryBarColour, emboss = 0.1, hover = true, can_collide = true}, nodes = {
							{n = G.UIT.O, config = {object = Brainstorm.makeLegendaryTex(), w = 3.1, h = 0.54}},
						}},
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
							func = "brainstorm_negative_shimmer",
							minw = 2.2, min_h = 0.5, padding = 0.08,
						}, nodes = {
							{n = G.UIT.T, config = {ref_table = Brainstorm.negLegendaryDisplay, ref_value = "text", scale = 0.35, colour = {1, 1, 1, 1}}},
						}},
					}},
					{n = G.UIT.R, config = {align = "cm", padding = 0.02}, nodes = {
						create_toggle({
							label = "Legendary: any pack, antes 1-8",
							label_scale = 0.32,
							w = 1.2,
							ref_table = Brainstorm.SETTINGS.autoreroll,
							ref_value = "searchLegendaryAnywhere",
							callback = function() nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS)) end,
						}),
					}},
					create_option_cycle({
						label = "Search Threads",
						scale = 0.8,
						w = 4,
						options = Brainstorm.searchThreadsOptions,
						opt_callback = "change_search_threads",
						current_option = Brainstorm.SETTINGS.autoreroll.searchThreadsID or 1,
					}),
					create_option_cycle({
						label = "Found Seed",
						scale = 0.8,
						w = 4,
						options = Brainstorm.foundSeedSlotOptions,
						opt_callback = "change_found_seed_slot",
						current_option = Brainstorm.SETTINGS.autoreroll.foundSeedSlotID or 1,
					}),
					create_option_cycle({
						label = "Found Stake",
						scale = 0.8,
						w = 4,
						options = Brainstorm.foundStakeOptions,
						opt_callback = "change_found_seed_stake",
						current_option = Brainstorm.SETTINGS.autoreroll.foundSeedStakeID or 1,
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
						-- Debug Mode + Illegal Seed Input centered beneath both columns.
						{n = G.UIT.R, config = {align = "cm", padding = 0.05}, nodes = {
							{n = G.UIT.C, config = {align = "cm"}, nodes = {
								create_toggle({
									label = "Debug Mode",
									ref_table = Brainstorm.SETTINGS,
									ref_value = "debug_mode",
									callback = function(_set_toggle)
										_RELEASE_MODE = not Brainstorm.SETTINGS.debug_mode
										G.F_NO_ACHIEVEMENTS = Brainstorm.SETTINGS.debug_mode
									end,
								}),
							}},
							{n = G.UIT.C, config = {align = "cm", minw = 0.6}, nodes = {}},
							{n = G.UIT.C, config = {align = "cm"}, nodes = {
								-- Lets the vanilla seed box accept '0' (see the wrappers at
								-- the end of this file); off = stock input behavior.
								create_toggle({
									label = "Illegal Seed Input",
									ref_table = Brainstorm.SETTINGS,
									ref_value = "illegalSeedInput",
									callback = function()
										nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS))
									end,
								}),
							}},
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
				-- Wildcard entries first (type "any" to see them): match by rarity
				-- instead of a specific joker. Combined with a slot's Negative
				-- toggle they search e.g. "any natural negative Rare".
				local wildcards = {
					{name = "Any Joker (wildcard)",    key = "*any"},
					{name = "Any Common (wildcard)",   key = "*common"},
					{name = "Any Uncommon (wildcard)", key = "*uncommon"},
					{name = "Any Rare (wildcard)",     key = "*rare"},
				}
				for _, w in ipairs(wildcards) do
					Brainstorm.allJokerNames[#Brainstorm.allJokerNames+1] = w.name
					Brainstorm.allJokerKeys[#Brainstorm.allJokerKeys+1]  = w.key
				end
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
				-- Header: the Anywhere toggle + its depth cycle, above the per-ante rows.
				local anywhereRow = {n = G.UIT.R, config = {align = "cm", padding = 0.03}, nodes = {
					{n = G.UIT.C, config = {align = "cm", padding = 0.08}, nodes = {
						create_toggle({
							label = "Anywhere (Antes 1-8)",
							ref_table = Brainstorm.SETTINGS.multiAnteSearch,
							ref_value = "anywhereMode",
							callback = function() nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS)) end,
						}),
					}},
					{n = G.UIT.C, config = {align = "cm", padding = 0.08}, nodes = {
						{n = G.UIT.R, config = {align = "cm"}, nodes = {
							{n = G.UIT.T, config = {text = "Depth (slots/ante)", scale = 0.36, colour = G.C.UI.TEXT_LIGHT}},
						}},
						{n = G.UIT.R, config = {align = "cm"}, nodes = {
							create_option_cycle({
								scale = 0.7,
								w = 2.6,
								options = anywhereSlotOptions,
								opt_callback = "change_anywhere_slots",
								current_option = cfg.anywhereSlotsID or 3,
							}),
						}},
					}},
				}}
				local anywhereNote = {n = G.UIT.R, config = {align = "cm", padding = 0.01}, nodes = {
					{n = G.UIT.T, config = {text = "ON: searches shops + packs in every ante 1-8; the per-ante rows below are ignored.", scale = 0.26, colour = G.C.UI.TEXT_INACTIVE}},
				}}
				-- One row per ante: shop-slot cycle on the left, its pack toggle on the right.
				local anteRows = {}
				anteRows[#anteRows + 1] = anywhereRow
				anteRows[#anteRows + 1] = anywhereNote
				for ante = 1, 4 do
					-- Label sits directly above the bar (shared column) so it stays centred
					-- on the bar. The toggle column uses an invisible label of the same size
					-- so the Packs toggle drops to the bar's vertical middle.
					local shopLabel = "Ante " .. ante .. " Shop Slots"
					anteRows[#anteRows + 1] = {n = G.UIT.R, config = {align = "cm", padding = 0.02}, nodes = {
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
									callback = function() nativefs.write(Brainstorm.modPath() .. "/settings.lua", STR_PACK(Brainstorm.SETTINGS)) end,
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
				-- E_MANAGER delays count in game-time (dt * SPEEDFACTOR), and
				-- SPEEDFACTOR == GAMESPEED during a run. Scale the hold by it so the
				-- popup lasts ~3 REAL seconds regardless of game speed (at GAMESPEED
				-- 32 a fixed hold=3 vanished in ~0.1s).
				hold = 3 * (G.SPEEDFACTOR or 1),
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

-- Brief notification for a seed slot (banking a found seed, or loading one back).
-- The purple backdrop flashes once (REAL timer -> ~0.35s at any game speed) and
-- the text holds ~1.2 real seconds, then fades. Delays are scaled by SPEEDFACTOR
-- because E_MANAGER counts in game-time (dt * SPEEDFACTOR) during a run.
function Brainstorm.showSeedSlotAlert(text)
	G.E_MANAGER:add_event(Event({
		trigger = "after",
		delay = 0.4,
		blockable = false,
		blocking = false,
		func = function()
			local at = Brainstorm.attention_text({
				scale = 0.7,
				text = text,
				align = "cm",
				offset = {x = 0, y = -3.5},
				major = G.STAGE == G.STAGES.RUN and G.play or G.title_top,
				backdrop_colour = G.C.SECONDARY_SET.Tarot,
				backdrop_timer_type = "REAL",
				backdrop_timer = 0.35,
				backdrop_lifespan = 0.35,
			})
			play_sound("other1", 0.76, 0.4)
			G.E_MANAGER:add_event(Event({
				trigger = "after",
				delay = 1.2 * (G.SPEEDFACTOR or 1),
				blockable = false,
				blocking = false,
				func = function()
					Brainstorm.remove_attention_text(at)
					return true
				end,
			}))
			return true
		end,
	}))
end
-- ===========================================================================
-- Illegal seed input (gated by the "Illegal Seed Input" settings toggle)
-- ---------------------------------------------------------------------------
-- The game's seed box can't reproduce every seed a run can actually have:
-- text_input_key's FIRST line remaps the '0' key to 'o' (and its corpus has
-- no zero either), and the Paste Seed button replays the clipboard through
-- that same handler one key at a time -- so a seed containing '0' (possible
-- via Brainstorm total-space pools, which start runs through start_run
-- directly) silently turns into a different seed when typed or pasted back
-- in. The vanilla PLAY flow itself is already exact: start_setup_run hands
-- G.setup_seed straight to start_run, which marks the run seeded. The
-- keystroke filter is the ONLY gate, so nothing is added to the setup screen
-- -- with the toggle on, the stock seed box simply accepts '0' (typed and
-- via Paste Seed); with it off, input behavior is byte-for-byte vanilla.
-- ===========================================================================

-- Tag the vanilla seed box as it is built. create_text_input's args table IS
-- the input's hook config (config.ref_table), so a marker added here is
-- visible to the key handler below. "toggle" = honor the settings switch at
-- keystroke time, so flipping it needs no menu rebuild.
local orig_create_text_input = create_text_input
function create_text_input(args)
	if args and args.ref_table == G and args.ref_value == "setup_seed" then
		args.bs_accept_zero = "toggle"
	end
	return orig_create_text_input(args)
end

-- Let '0' into tagged inputs. We must intercept BEFORE the original runs --
-- its 0->'o' remap happens before any corpus/config check -- and insert the
-- character ourselves, mirroring the original's corpus-accept branch (cursor
-- transpose, insert, advance).
local orig_text_input_key = G.FUNCS.text_input_key
G.FUNCS.text_input_key = function(args)
	local hook = G.CONTROLLER and G.CONTROLLER.text_input_hook
	local hcfg = hook and hook.config and hook.config.ref_table
	local zero_ok = hcfg and hcfg.bs_accept_zero
	if zero_ok == "toggle" then zero_ok = Brainstorm.SETTINGS.illegalSeedInput end
	if args and args.key == "0" and zero_ok then
		-- Same lazy stash the original does on every key; its RETURN branch
		-- reads orig_colour, which would be nil if '0' were the only key typed.
		hcfg.orig_colour = hcfg.orig_colour or copy_table(hcfg.colour)
		local text = hcfg.text
		if hcfg.max_length > string.len(text.ref_table[text.ref_value]) then
			TRANSPOSE_TEXT_INPUT(0)
			MODIFY_TEXT_INPUT({ letter = "0", text_table = text, pos = text.current_position + 1 })
			TRANSPOSE_TEXT_INPUT(1)
		end
		return
	end
	return orig_text_input_key(args)
end
