-- Production-Lua side of the automatic-attachment/native-child regression.
-- The Python orchestrator builds real pools and markers. This driver loads the
-- production attachment/start/poll functions, launches the real native helper,
-- and asserts the continuation decision made from its real status file.

local rerollPath, root, poolDir, binary, templatePath, itemsPath,
  smallSeedsPath, scenario = unpack(arg)
assert(scenario, "usage: pool_attachment_native_driver.lua <reroll> <root> "
  .. "<pool-dir> <binary> <template> <items> <small-seeds> <scenario>")

local function readFile(path, bytes)
  local handle = io.open(path, "rb")
  if not handle then return nil end
  local value = handle:read(bytes or "*a")
  handle:close()
  return value
end

local function writeFile(path, value)
  local handle = assert(io.open(path, "wb"))
  handle:write(value)
  handle:close()
end

local directoryItems = {}
for line in assert(readFile(itemsPath)):gmatch("[^\r\n]+") do
  directoryItems[#directoryItems + 1] = line
end

local nativefs = {
  read = readFile,
  write = writeFile,
  getInfo = function(path)
    local handle = io.open(path, "rb")
    if not handle then return nil end
    local size = handle:seek("end")
    handle:close()
    return { type = "file", size = size }
  end,
  getDirectoryItems = function(path)
    assert(path == poolDir)
    return directoryItems
  end,
}

package.loaded.nativefs = nativefs
package.loaded.lovely = { mod_dir = root }
local windows = package.config:sub(1, 1) == "\\"
love = {
  timer = { getTime = os.clock },
  system = { getOS = function() return windows and "Windows" or "OS X" end },
}
G = { FUNCS = {} }
Brainstorm = { SETTINGS = { autoreroll = {}, multiAnteSearch = {} }, AUTOREROLL = {} }
assert(loadfile(rerollPath))()

Brainstorm.modPath = function() return root end
Brainstorm.seedPoolDir = function() return poolDir end
Brainstorm.nativePaths = function()
  local base = poolDir .. "/native-e2e"
  return {
    bin = binary,
    cfg = base .. ".cfg",
    status = base .. ".status",
    stop = base .. ".stop",
    hb = base .. ".hb",
  }
end

Brainstorm.SETTINGS.useNativeSearch = true
Brainstorm.SETTINGS.autoreroll = {
  seedPoolFile = "",
  searchTag = "tag_charm",
  searchTagAnywhere = false,
  searchLegendary = "j_perkeo",
  searchLegendaryAnywhere = false,
  searchNegativeLegendary = false,
  searchForSoul = 0,
  searchVoucher = "v_base_4",
  searchVoucherAnte = 1,
  searchPack = {},
  jokerSearchMatchAny = false,
  jokerSlotData = {
    { key = "j_r2_7", requireNegative = false },
    { key = "", requireNegative = false },
    { key = "", requireNegative = false },
  },
  searchThreads = 1,
}
Brainstorm.SETTINGS.multiAnteSearch = {
  ante1Slots = 0, ante1Packs = false,
  ante2Slots = 8, ante2Packs = true,
  ante3Slots = 8, ante3Packs = true,
  ante4Slots = 12, ante4Packs = true,
}

if scenario == "layered-pack" then
  Brainstorm.SETTINGS.autoreroll.searchLegendary = ""
  Brainstorm.SETTINGS.autoreroll.searchVoucher = ""
  Brainstorm.SETTINGS.autoreroll.searchPack = { "p_spectral_mega_1" }
  Brainstorm.SETTINGS.autoreroll.jokerSlotData = {
    { key = "", requireNegative = false },
    { key = "", requireNegative = false },
    { key = "", requireNegative = false },
  }
  Brainstorm.SETTINGS.multiAnteSearch = {
    ante1Slots = 0, ante1Packs = false,
    ante2Slots = 0, ante2Packs = false,
    ante3Slots = 0, ante3Packs = false,
    ante4Slots = 0, ante4Packs = false,
  }
end

local estimateModes = {}
Brainstorm.setAttachedPoolEstimateMode = function(attached)
  estimateModes[#estimateModes + 1] = attached and true or false
end

local baseConfig = assert(readFile(templatePath))
local mutatedPool = false
Brainstorm.buildNativeConfigText = function(session)
  local config = baseConfig:gsub("^session%s+%d+", "session " .. tostring(session), 1)
  local poolPath = Brainstorm.effectiveSeedPoolPath()
  if poolPath then
    if scenario == "stale-profile" and not mutatedPool then
      config = config:gsub("tagdef tag_1 1 0", "tagdef tag_1 0 0", 1)
      mutatedPool = true
    elseif scenario == "missing-after-selection" and not mutatedPool then
      assert(os.remove(poolPath))
      mutatedPool = true
    elseif scenario == "corrupt-after-selection" and not mutatedPool then
      local handle = assert(io.open(poolPath, "r+b"))
      handle:seek("set", 0)
      handle:write("X")
      handle:close()
      mutatedPool = true
    end
    config = config:gsub("\nend%s*\n?$", "\npoolfile " .. poolPath .. "\nend\n", 1)
  end
  return config
end

local ffi = require("ffi")
local sleepMs
if windows then
  pcall(ffi.cdef, "void Sleep(uint32_t ms);")
  sleepMs = function(ms) ffi.C.Sleep(ms) end
else
  pcall(ffi.cdef, "int usleep(unsigned int usec);")
  sleepMs = function(ms) ffi.C.usleep(ms * 1000) end
end

local function runNative(afterStart, beforeTerminalPoll)
  assert(Brainstorm.startNativeSearch(), "production startNativeSearch failed")
  local selected = Brainstorm.AUTOREROLL.autoPoolSelection
  local selectedName = selected and selected.pool_file or nil
  if afterStart then afterStart(selected) end
  local terminalHookRan = false
  for _ = 1, 1000 do
    if beforeTerminalPoll and not terminalHookRan then
      local status = readFile(Brainstorm.nativePaths().status)
      if status and (status:match("E [^\n]+") or status:match("R %S+ [^\n]+")
          or status:match("^D\n") or status:match("\nD\n")) then
        beforeTerminalPoll(selected)
        terminalHookRan = true
      end
    end
    local result = Brainstorm.pollNativeSearch()
    if result then
      Brainstorm.stopNativeSearch()
      return result, selectedName
    end
    if not Brainstorm.AUTOREROLL.nativeActive then return nil, selectedName end
    sleepMs(10)
  end
  Brainstorm.stopNativeSearch()
  error("native child did not finish within 10 seconds")
end

local smallSeeds = {}
for seed in (readFile(smallSeedsPath) or ""):gmatch("[^\r\n]+") do
  smallSeeds[seed] = true
end
local function assertFallbackHit(result)
  assert(result and result.seed, "unrestricted fallback did not find a seed")
  assert(not smallSeeds[result.seed],
    "fallback returned a seed already present in the exhausted accelerator")
end

if scenario == "hit" then
  local result, selected = runNative()
  assert(result and selected == "01-large.bspool")
  print("PASS compatible accelerator finds a seed through the real native child")

elseif scenario == "chain" then
  local result, selected = runNative()
  assert(not result and selected == "01-small.bspool")
  assert(Brainstorm.AUTOREROLL.autoPoolTried[poolDir .. "/01-small.bspool.attached"])
  result, selected = runNative()
  assert(result and selected == "02-large.bspool")
  print("PASS real native child chains smallest exhausted accelerator to a compatible hit")

elseif scenario == "layered-pack" then
  local result, selected = runNative()
  assert(result and selected == "01-tag.bspool")
  print("PASS active pack predicate is evaluated safely over a broader attached tag pool")

elseif scenario == "incompatible" then
  assert(Brainstorm.findAutomaticSeedPool() == nil)
  local result, selected = runNative()
  assert(result and selected == nil)
  print("PASS unproved attachment relationship is refused before unrestricted fallback")

elseif scenario == "fallback" then
  local result, selected = runNative()
  assert(not result and selected == "01-small.bspool")
  result, selected = runNative()
  assert(selected == nil)
  assertFallbackHit(result)
  assert(estimateModes[1] == true and estimateModes[#estimateModes] == false)
  print("PASS final accelerator exhaustion continues unrestricted and finds an outside seed")

elseif scenario == "authoritative" then
  local result, selected = runNative()
  assert(not result and selected == "01-authoritative.bspool")
  assert(Brainstorm.AUTOREROLL.autoPoolAbort)
  assert(not Brainstorm.AUTOREROLL.autoPoolTried)
  print("PASS unchanged authoritative exhaustion is definitive")

elseif scenario == "authoritative-mutated" then
  local result, selected = runNative(nil, function(chosen)
    assert(chosen and chosen.role == "authoritative")
    assert(os.remove(chosen.path))
  end)
  assert(not result and selected == "01-authoritative.bspool")
  assert(not Brainstorm.AUTOREROLL.autoPoolAbort)
  assert(Brainstorm.AUTOREROLL.autoPoolWarned:find("changed during search", 1, true))
  result, selected = runNative()
  assert(selected == nil)
  assertFallbackHit(result)
  print("PASS mutated authoritative marker is revalidated and falls back safely")

elseif scenario == "authoritative-pool-mutated" then
  local result, selected = runNative(nil, function(chosen)
    assert(chosen and chosen.role == "authoritative")
    local handle = assert(io.open(chosen.poolPath, "ab"))
    handle:write("changed after native exhaustion")
    handle:close()
  end)
  assert(not result and selected == "01-authoritative.bspool")
  assert(not Brainstorm.AUTOREROLL.autoPoolAbort)
  assert(Brainstorm.AUTOREROLL.autoPoolWarned:find("changed during search", 1, true))
  result, selected = runNative()
  assert(selected == nil)
  assertFallbackHit(result)
  print("PASS mutated authoritative pool is revalidated and falls back safely")

elseif scenario == "manual" then
  Brainstorm.SETTINGS.autoreroll.seedPoolFile = "01-small.bspool"
  local result, selected = runNative()
  assert(not result and selected == nil)
  assert(Brainstorm.AUTOREROLL.poolAbort)
  assert(Brainstorm.AUTOREROLL.autoPoolSelection == nil)
  assert(Brainstorm.AUTOREROLL.autoPoolTried == nil)
  print("PASS manual pool overrides attachments and preserves hard exhaustion")

elseif scenario == "invalid-discovery" then
  assert(Brainstorm.findAutomaticSeedPool() == nil)
  local result, selected = runNative()
  assert(selected == nil and Brainstorm.AUTOREROLL.autoPoolInvalid)
  assertFallbackHit(result)
  print("PASS invalid missing/corrupt/changed/stale attachments are non-fatal")

elseif scenario == "stale-profile" or scenario == "missing-after-selection"
    or scenario == "corrupt-after-selection" then
  local result, selected = runNative()
  assert(not result and selected == "01-small.bspool")
  assert(Brainstorm.AUTOREROLL.autoPoolTried[poolDir .. "/01-small.bspool.attached"])
  result, selected = runNative()
  assert(selected == nil)
  assertFallbackHit(result)
  print("PASS " .. scenario .. " native failure retires only that accelerator and falls back")

else
  error("unknown scenario " .. tostring(scenario))
end

print("RESULT " .. scenario .. " ok")
