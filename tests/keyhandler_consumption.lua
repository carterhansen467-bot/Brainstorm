-- Regression coverage for Brainstorm hotkey dispatch. The injected handler must
-- consume recognized chords; otherwise Balatro's debug-mode J/B/P handlers run
-- afterward (J deletes the current run and returns to the splash screen).

package.loaded.lovely = {}
package.loaded.brainstorm_nativefs = {}

local down = {}
love = {
	keyboard = {
		isDown = function(key) return down[key] == true end,
	},
}

local counters = {
	alert = 0,
	apply = 0,
	debug_d = 0,
	debug_p = 0,
	debug_voucher = 0,
	delete = 0,
	fast = 0,
	load = 0,
	record = 0,
	reset = 0,
	save = 0,
	slot_alert = 0,
	start = 0,
	stop = 0,
}
local currentJoker
local lastAlert
local loadData = { ordinary_save = true }

G = {
	STAGE = "run",
	STAGES = { RUN = "run" },
	SETTINGS = { profile = "1" },
	ARGS = { save_run = { marker = "save" } },
	delete_run = function() counters.delete = counters.delete + 1 end,
	start_run = function(_, args)
		counters.start = counters.start + 1
		assert(type(args) == "table")
	end,
}

Brainstorm = {
	SETTINGS = {
		keybinds = {
			saveState = "z",
			loadState = "x",
			rerollSeed = "t",
			autoReroll = "a",
		},
	},
	currentRunJoker = function() return currentJoker end,
	showJokerFoundAlert = function(text)
		counters.alert = counters.alert + 1
		lastAlert = text
	end,
	debugPredictVoucher = function() counters.debug_voucher = counters.debug_voucher + 1 end,
	debugPredictPacks = function() counters.debug_p = counters.debug_p + 1 end,
	dumpDiagnostics = function() counters.debug_d = counters.debug_d + 1 end,
	resetSearchUI = function() counters.reset = counters.reset + 1 end,
	stopSearchThread = function() counters.stop = counters.stop + 1 end,
	applyFoundSeed = function() counters.apply = counters.apply + 1 end,
	recordFoundJoker = function() counters.record = counters.record + 1 end,
	showSeedSlotAlert = function() counters.slot_alert = counters.slot_alert + 1 end,
}

function FastReroll() counters.fast = counters.fast + 1 end
function compress_and_save() counters.save = counters.save + 1 end
function get_compressed()
	counters.load = counters.load + 1
	return loadData
end
function STR_UNPACK(value) return value end
function saveManagerAlert() end

assert(loadfile("Brainstorm_keyhandler.lua"))()

local function press(key, held)
	down = {}
	for _, heldKey in ipairs(held or {}) do down[heldKey] = true end
	return Brainstorm.key_press_update(key)
end

-- Both Ctrl keys invoke the location alert and consume J.
currentJoker = "J1A2Shop"
assert(press("j", {"lctrl"}) == true)
assert(lastAlert == "Joker: J1A2Shop")
currentJoker = "J2A3Pack"
assert(press("j", {"rctrl"}) == true)
assert(lastAlert == "Joker: J2A3Pack")
assert(counters.alert == 2)

-- Ctrl+J remains consumed even when there is no usable persisted location.
-- This is what prevents the vanilla debug J action from deleting the run.
currentJoker = nil
Brainstorm.AUTOREROLL.lastJokerFoundAt = nil
lastAlert = nil
assert(press("j", {"lctrl"}) == true)
assert(lastAlert == "No searched Joker location is saved for this seed")
currentJoker = {}
assert(press("j", {"rctrl"}) == true)
assert(lastAlert == "No searched Joker location is saved for this seed")

-- Plain J is not Brainstorm's chord and must continue through the controller.
currentJoker = "J1A1Shop"
assert(press("j", {}) == false)

-- Every other recognized Brainstorm chord is consumed as well. In debug mode,
-- Balatro's plain B and P handlers are destructive or state-changing too.
assert(press("t", {"lctrl"}) == true)
assert(counters.fast == 1)
assert(press("a", {"rctrl"}) == true)
assert(Brainstorm.AUTOREROLL.autoRerollActive == true)
assert(press("a", {"lctrl"}) == true)
assert(Brainstorm.AUTOREROLL.autoRerollActive == false)
assert(counters.stop == 1)
assert(press("b", {"rctrl"}) == true)
assert(press("p", {"lctrl"}) == true)
assert(press("d", {"rctrl"}) == true)
assert(counters.debug_voucher == 1 and counters.debug_p == 1 and counters.debug_d == 1)

assert(press("1", {"z"}) == true)
assert(counters.save == 1)
assert(press("2", {"x"}) == true)
assert(counters.load == 1 and counters.delete == 1 and counters.start == 1)
assert(press("q", {}) == false)

-- Execute the exact one-line Lovely payload from the manifest. A consumed
-- Ctrl+J must return before the modeled downstream vanilla debug branch.
local manifestFile = assert(io.open("lovely.toml", "rb"))
local manifest = assert(manifestFile:read("*a"))
manifestFile:close()
local keyPayload
for payload in manifest:gmatch('payload%s*=%s*"([^"\r\n]+)"') do
	if payload:find("key_press_update", 1, true) then
		keyPayload = payload
		break
	end
end
assert(keyPayload == "if Brainstorm.key_press_update(key) then return end",
	"Lovely key patch must return when Brainstorm consumes a chord")

vanilla_debug_runs = 0
local controller = assert(load(
	"return function(key)\n"
		.. keyPayload
		.. "\nvanilla_debug_runs = vanilla_debug_runs + 1\nend",
	"modeled_controller"
))()

currentJoker = "J1A1Shop"
down = { lctrl = true }
controller("j")
assert(vanilla_debug_runs == 0, "consumed Ctrl+J reached vanilla debug handling")
down = { rctrl = true }
controller("j")
assert(vanilla_debug_runs == 0, "right Ctrl+J reached vanilla debug handling")
down = {}
controller("j")
assert(vanilla_debug_runs == 1, "plain J should remain available to the controller")

print("keyhandler consumption regression: OK")
