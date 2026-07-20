-- Exhaustive finite-domain proof of every automatic-attachment relationship
-- currently accepted by the in-game matcher. This is intentionally separate
-- from the native bounded differential: it checks the complete policy domain,
-- including every Ante/phase endpoint and every rejected neighboring value.

local reroll = assert(arg[1], "usage: pool_attachment_matrix.lua <Brainstorm_reroll.lua>")

local directoryItems = {}
package.loaded.nativefs = {
  read = function() return nil end,
  write = function() end,
  getInfo = function() return nil end,
  getDirectoryItems = function() return directoryItems end,
}
package.loaded.lovely = { mod_dir = "" }
G = { FUNCS = {} }
Brainstorm = { SETTINGS = { autoreroll = {}, multiAnteSearch = {} }, AUTOREROLL = {} }
assert(loadfile(reroll))()

-- The in-game selector stores exact filenames but must never render an
-- unbounded filename into Balatro's fixed-width settings layout.  Automatic
-- is the default behavior, duplicate labels remain distinguishable, and a
-- temporarily missing manual selection is preserved safely.
do
  local headers = {
    ["a-very-long-file-name-that-used-to-break-the-settings-layout.bspool"] =
      {label = "A very long shared display label that is also unsafe", pool_id = "aaaabbbb"},
    ["second.bspool"] = {label = "Duplicate", pool_id = "11112222"},
    ["third.bspool"] = {label = "Duplicate", pool_id = "33334444"},
  }
  local options, files, current = Brainstorm.buildSeedPoolOptions({
    "ignored.attached", "third.bspool",
    "a-very-long-file-name-that-used-to-break-the-settings-layout.bspool",
    "second.bspool",
  }, "missing-selected-pool-with-a-long-name.bspool", function(name)
    return headers[name]
  end)
  assert(options[1] == "Automatic" and files.Automatic == "" and current == #options)
  local seen = {}
  for _, label in ipairs(options) do
    assert(#label <= 24, "pool option exceeded its bounded display width")
    assert(not seen[label], "pool option labels collided")
    seen[label] = true
  end
  assert(files[options[current]] == "missing-selected-pool-with-a-long-name.bspool")
  assert(Brainstorm.poolInfoString(""):match("^Automatic:"))
end

local function setActive(tag, legendary, negative, soul, tagAnywhere, legAnywhere)
  Brainstorm.SETTINGS.autoreroll = {
    seedPoolFile = "",
    searchTag = tag or "",
    searchTagAnywhere = tagAnywhere and true or false,
    searchLegendary = legendary or "",
    searchLegendaryAnywhere = legAnywhere and true or false,
    searchNegativeLegendary = negative and true or false,
    searchForSoul = soul or 0,
    searchVoucher = "",
    searchPack = {},
    jokerSlotData = {},
  }
end

local function matches(...)
  return Brainstorm.attachmentMatchesActiveFilters({ predicates = { ... } })
end

local phaseOrder = { small = 0, big = 1, boss = 2 }
local function position(ante, phase)
  return (ante - 1) * 3 + assert(phaseOrder[phase])
end

local function tagPredicate(mode, key, minAnte, minPhase, maxAnte, maxPhase, count)
  return table.concat({ "tag", mode, key, minAnte, minPhase, maxAnte, maxPhase, count }, " ")
end

local function legendaryPredicate(key, minAnte, minPhase, maxAnte, maxPhase,
    negative, source, depth, routes)
  return table.concat({ "legendary", key, minAnte, minPhase, maxAnte, maxPhase,
    negative and 1 or 0, source, depth, routes }, " ")
end

-- Independent containment oracle for the sole exact tag request exposed by
-- the current UI: collect one selected tag at Ante 1 Small. Equal starts are
-- required because moving the start can change which occurrence is collected;
-- extending the end is a pure superset.
setActive("tag_charm")
local acceptedTags = 0
for maxAnte = 1, 39 do
  for _, maxPhase in ipairs({ "small", "big" }) do
    for _, mode in ipairs({ "collect", "observe" }) do
      for _, key in ipairs({ "tag_charm", "tag_rare" }) do
        for count = 1, 2 do
          local expected = mode == "collect" and key == "tag_charm" and count == 1
          local actual = matches(tagPredicate(
            mode, key, 1, "small", maxAnte, maxPhase, count))
          assert(actual == expected, "tag endpoint implication mismatch")
          if actual then
            assert(position(1, "small") <= position(maxAnte, maxPhase))
            acceptedTags = acceptedTags + 1
          end
        end
      end
    end
  end
end
assert(acceptedTags == 78, "unexpected accepted tag relationship count")

-- No later/earlier start is silently accepted, even when the pool endpoint is
-- otherwise the widest possible valid tag window.
for minAnte = 1, 39 do
  for _, minPhase in ipairs({ "small", "big" }) do
    local actual = matches(tagPredicate(
      "collect", "tag_charm", minAnte, minPhase, 39, "big", 1))
    assert(actual == (minAnte == 1 and minPhase == "small"),
      "tag start implication mismatch")
  end
end

-- The Legendary request is A1-Small Charm, first Soul, canonical route. A
-- non-Negative UI request accepts either edition, while the Negative toggle is
-- a subset. Full routes, depth=any, and later endpoints are supersets.
local acceptedLegendary = { [false] = 0, [true] = 0 }
for _, activeNegative in ipairs({ false, true }) do
  setActive("", "j_perkeo", activeNegative, 0)
  for maxAnte = 1, 39 do
    for _, maxPhase in ipairs({ "small", "big", "boss" }) do
      for _, poolNegative in ipairs({ false, true }) do
        for _, source in ipairs({ "any", "shop", "charm", "ethereal" }) do
          for _, depth in ipairs({ 0, 1, 2 }) do
            for _, routes in ipairs({ "full", "canonical_charm" }) do
              for _, key in ipairs({ "j_perkeo", "j_caino" }) do
                local expected = key == "j_perkeo" and source == "charm"
                  and (not poolNegative or activeNegative)
                  and (depth == 0 or depth == 1)
                local actual = matches(legendaryPredicate(key, 1, "small",
                  maxAnte, maxPhase, poolNegative, source, depth, routes))
                assert(actual == expected, "Legendary endpoint implication mismatch")
                if actual then
                  assert(position(1, "small") <= position(maxAnte, maxPhase))
                  acceptedLegendary[activeNegative] = acceptedLegendary[activeNegative] + 1
                end
              end
            end
          end
        end
      end
    end
  end
end
assert(acceptedLegendary[false] == 468)
assert(acceptedLegendary[true] == 936)

for minAnte = 1, 39 do
  for _, minPhase in ipairs({ "small", "big", "boss" }) do
    setActive("", "j_perkeo", false, 0)
    local actual = matches(legendaryPredicate("j_perkeo", minAnte, minPhase,
      39, "boss", false, "charm", 0, "full"))
    assert(actual == (minAnte == 1 and minPhase == "small"),
      "Legendary start implication mismatch")
  end
end

-- searchForSoul 0 and 1 share the first-Soul request. Deeper or Anywhere UI
-- modes deliberately have no accepted automatic attachment relationship yet.
for _, soul in ipairs({ 0, 1 }) do
  setActive("", "j_perkeo", false, soul)
  assert(matches(legendaryPredicate(
    "j_perkeo", 1, "small", 1, "small", false, "charm", 1, "full")))
end
setActive("", "j_perkeo", false, 2)
assert(not matches(legendaryPredicate(
  "j_perkeo", 1, "small", 1, "small", false, "charm", 0, "full")))
setActive("tag_charm", "j_perkeo", false, 2)
assert(matches(tagPredicate("collect", "tag_charm", 1, "small", 1, "small", 1)))
assert(not matches(tagPredicate("collect", "tag_charm", 1, "small", 1, "small", 1),
  legendaryPredicate("j_perkeo", 1, "small", 1, "small",
    false, "charm", 0, "full")))
setActive("", "j_perkeo", false, 0, false, true)
assert(not matches(legendaryPredicate(
  "j_perkeo", 1, "small", 1, "small", false, "charm", 1, "full")))
setActive("tag_charm", "", false, 0, true, false)
assert(not matches(tagPredicate("collect", "tag_charm", 1, "small", 1, "small", 1)))
setActive("", "", false, 1)
assert(not matches(tagPredicate("collect", "tag_charm", 1, "small", 1, "small", 1)))

-- A combined tag/Legendary attachment must describe the physical Charm route.
local charmTag = tagPredicate("collect", "tag_charm", 1, "small", 2, "big", 1)
local rareTag = tagPredicate("collect", "tag_rare", 1, "small", 2, "big", 1)
local perkeo = legendaryPredicate(
  "j_perkeo", 1, "small", 2, "boss", false, "charm", 0, "full")
setActive("tag_charm", "j_perkeo", false, 0)
assert(matches(charmTag, perkeo))
setActive("tag_rare", "j_perkeo", false, 0)
assert(matches(rareTag))
assert(matches(perkeo))
assert(not matches(rareTag, perkeo),
  "unrelated collected tag was treated as the Legendary source route")

-- Joker, pack, and voucher filters are evaluated by the native child over the
-- broader attached membership. They must neither broaden nor narrow the proof.
setActive("tag_charm", "j_perkeo", false, 0)
assert(matches(charmTag, perkeo))
Brainstorm.SETTINGS.autoreroll.searchVoucher = "v_overstock_norm"
Brainstorm.SETTINGS.autoreroll.searchPack = { p_arcana_normal_1 = true }
Brainstorm.SETTINGS.autoreroll.jokerSlotData = {
  { key = "j_blueprint", requireNegative = true },
}
assert(matches(charmTag, perkeo))

-- Canonical-domain neighbors and unsupported predicate families are refused.
setActive("tag_charm", "j_perkeo", true, 0)
for _, invalid in ipairs({
  "tag collect tag_charm 0 small 1 small 1",
  "tag collect tag_charm 1 small 40 big 1",
  "tag collect tag_charm 2 small 1 big 1",
  "tag collect tag_charm 1 boss 2 big 1",
  "tag collect tag_charm 1 small 2 boss 1",
  "tag collect tag_charm 1 small 1 small 2",
  "tag invalid tag_charm 1 small 1 small 1",
  "legendary j_perkeo 0 small 1 small 0 charm 1 full",
  "legendary j_perkeo 1 small 40 boss 0 charm 1 full",
  "legendary j_perkeo 2 small 1 boss 0 charm 1 full",
  "legendary j_perkeo 1 nowhere 2 boss 0 charm 1 full",
  "legendary j_perkeo 1 small 2 boss 2 charm 1 full",
  "legendary j_perkeo 1 small 2 boss 0 omen 1 full",
  "legendary j_perkeo 1 small 2 boss 0 charm 3 full",
  "legendary j_perkeo 1 small 2 boss 0 charm 1 approximate",
  "voucher v_overstock_norm 1 2",
  "voucher_exclude v_clearance_sale",
  "unknown value",
}) do
  assert(not matches(invalid), "accepted malformed/unsupported predicate: " .. invalid)
end

-- Deterministic pool choice is independent of directory enumeration order.
local validPredicate = { charmTag, perkeo }
local markers = {}
local function marker(name, records, role, id)
  local value = { predicates = validPredicate, pool_file = name, records = records,
    role = role, pool_id = id, path = "pool/" .. name .. ".attached" }
  markers[value.path] = value
  return value
end
local large = marker("large.bspool", 20, "accelerator", "20")
local small = marker("small.bspool", 10, "accelerator", "30")
local tiedRole = marker("authoritative.bspool", 10, "authoritative", "40")
local tiedId = marker("id-first.bspool", 10, "accelerator", "10")
local tiedName = marker("a-name.bspool", 10, "accelerator", "10")
Brainstorm.seedPoolDir = function() return "pool" end
Brainstorm.readPoolAttachment = function(path) return markers[path] end
local names = { large.pool_file .. ".attached", small.pool_file .. ".attached",
  tiedRole.pool_file .. ".attached", tiedId.pool_file .. ".attached",
  tiedName.pool_file .. ".attached" }
local function permute(at)
  if at > #names then
    directoryItems = { unpack(names) }
    Brainstorm.AUTOREROLL.autoPoolTried = nil
    assert(Brainstorm.findAutomaticSeedPool().pool_file == "authoritative.bspool")
    Brainstorm.AUTOREROLL.autoPoolTried = { [tiedRole.path] = true }
    assert(Brainstorm.findAutomaticSeedPool().pool_file == "a-name.bspool")
    Brainstorm.AUTOREROLL.autoPoolTried[tiedName.path] = true
    assert(Brainstorm.findAutomaticSeedPool().pool_file == "id-first.bspool")
    return
  end
  for i = at, #names do
    names[at], names[i] = names[i], names[at]
    permute(at + 1)
    names[at], names[i] = names[i], names[at]
  end
end
setActive("tag_charm", "j_perkeo", false, 0)
permute(1)

print("POOL ATTACHMENT IMPLICATION MATRIX: ALL PASS")
