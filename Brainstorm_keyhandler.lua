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
				-- Restore the found-joker info from the marker so Ctrl+J works after a
				-- reload (lastJokerFoundAt is in-memory only and wiped on quit); also
				-- persist it keyed by seed so it survives a later restart + Continue.
				Brainstorm.AUTOREROLL.lastJokerFoundAt = data.joker
				if Brainstorm.recordFoundJoker then
					Brainstorm.recordFoundJoker(data.brainstorm_found_seed, data.joker)
				end
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
			-- A fresh search gets a fresh verdict: an earlier "nothing in the
			-- seed pool matched" abort must not kill this run before it starts.
			Brainstorm.AUTOREROLL.poolAbort = nil
			Brainstorm.AUTOREROLL.autoPoolSelection = nil
			Brainstorm.AUTOREROLL.autoPoolDisabled = nil
			Brainstorm.AUTOREROLL.autoPoolTried = nil
			Brainstorm.AUTOREROLL.autoPoolWarned = nil
			Brainstorm.AUTOREROLL.autoPoolAbort = nil
			Brainstorm.AUTOREROLL.autoRerollActive = true
		end
	end
	if key == "j" and love.keyboard.isDown("lctrl") then
		local joker = (Brainstorm.currentRunJoker and Brainstorm.currentRunJoker())
			or Brainstorm.AUTOREROLL.lastJokerFoundAt
		if joker then
			Brainstorm.showJokerFoundAlert("Joker: " .. joker)
		end
	end
	-- Voucher prediction self-test: dumps predicted vs live voucher to debug_predict.txt.
	if key == "b" and love.keyboard.isDown("lctrl") then
		Brainstorm.debugPredictVoucher()
		saveManagerAlert("Voucher prediction -> debug_predict.txt")
	end
	-- Pack prediction self-test: dumps predicted vs live shop packs to debug_predict.txt.
	if key == "p" and love.keyboard.isDown("lctrl") then
		Brainstorm.debugPredictPacks()
		saveManagerAlert("Pack prediction -> debug_predict.txt")
	end
	-- Diagnostics dump: current seed + enabled filters + per-filter predictions.
	-- Hit this when a search result looks wrong, then share brainstorm_diagnostics.txt.
	if key == "d" and love.keyboard.isDown("lctrl") then
		Brainstorm.dumpDiagnostics()
		saveManagerAlert("Diagnostics -> brainstorm_diagnostics.txt")
	end
end
