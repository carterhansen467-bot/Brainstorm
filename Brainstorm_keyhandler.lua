local lovely = require("lovely")
local nativefs = require("nativefs")

Brainstorm.AUTOREROLL = {}

local saveKeys = { "1", "2", "3", "4", "5" }

function Brainstorm.key_press_update(key)
	-- Brainstorm Key Handler
	for i, k in ipairs(saveKeys) do
		--  SaveState
		if key == k and love.keyboard.isDown(Brainstorm.SETTINGS.keybinds.saveState) then
			if G.STAGE == G.STAGES.RUN then
				compress_and_save(G.SETTINGS.profile .. "/" .. "saveState" .. k .. ".jkr", G.ARGS.save_run)
				saveManagerAlert("Saved state to slot [" .. k .. "]")
			end
		end
		--  LoadState
		if key == k and love.keyboard.isDown(Brainstorm.SETTINGS.keybinds.loadState) then
			local data = get_compressed(G.SETTINGS.profile .. "/" .. "saveState" .. k .. ".jkr")
			if data ~= nil then
				data = STR_UNPACK(data)
			end
			if type(data) == "table" and data.brainstorm_found_seed then
				-- Banked found-seed slot: there's no real save blob, so start a
				-- fresh run on the stored seed (applyFoundSeed does its own delete_run).
				Brainstorm.applyFoundSeed(data.brainstorm_found_seed, data.stake)
				Brainstorm.showSeedSlotAlert("Seed loaded from slot [" .. k .. "]")
			else
				G:delete_run()
				G.SAVED_GAME = data
				G:start_run({
					savetext = G.SAVED_GAME,
				})
				saveManagerAlert("Loaded save from slot [" .. k .. "]")
			end
		end
  end
	--  FastReroll
	if key == Brainstorm.SETTINGS.keybinds.rerollSeed and love.keyboard.isDown("lctrl") then
		FastReroll()
	end
	if key == Brainstorm.SETTINGS.keybinds.autoReroll and love.keyboard.isDown("lctrl") then
		if Brainstorm.AUTOREROLL.autoRerollActive then
			Brainstorm.AUTOREROLL.autoRerollActive = false
			if Brainstorm.stopSearchThread then
				Brainstorm.stopSearchThread()
			end
			if Brainstorm.resetSearchUI then
				Brainstorm.resetSearchUI()
			end
		else
			if Brainstorm.resetSearchUI then
				Brainstorm.resetSearchUI()
			end
			Brainstorm.AUTOREROLL.autoRerollActive = true
		end
	end
	if key == "j" and love.keyboard.isDown("lctrl") then
		if Brainstorm.AUTOREROLL.lastJokerFoundAt then
			Brainstorm.showJokerFoundAlert("Joker: " .. Brainstorm.AUTOREROLL.lastJokerFoundAt)
		end
	end
	-- Voucher prediction self-test: dumps predicted vs live voucher to debug_predict.txt.
	if key == "b" and love.keyboard.isDown("lctrl") then
		Brainstorm.debugPredictVoucher()
		saveManagerAlert("Voucher prediction -> debug_predict.txt")
	end
end
