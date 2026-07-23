-- Validate one real attachment marker through the production in-game reader.
-- The Python mutation orchestrator supplies real stat values because the bare
-- LuaJIT test environment does not expose LOVE/nativefs directory metadata.

local reroll, poolDir, markerPath, poolPath, size, modtime, expected = unpack(arg)
assert(expected, "usage: pool_attachment_marker_check.lua <reroll> <pool-dir> "
  .. "<marker> <pool> <size> <modtime> <valid|invalid>")

local function read(path, bytes)
  local handle = io.open(path, "rb")
  if not handle then return nil end
  local value = handle:read(bytes or "*a")
  handle:close()
  return value
end

local function normalized(path)
  path = tostring(path):gsub("\\", "/")
  return package.config:sub(1, 1) == "\\" and path:lower() or path
end

package.loaded.brainstorm_nativefs = {
  read = read,
  write = function() end,
  getInfo = function(path)
    if normalized(path) ~= normalized(poolPath) then return nil end
    return { type = "file", size = assert(tonumber(size)),
      modtime = assert(tonumber(modtime)) }
  end,
  getDirectoryItems = function() return {} end,
}
package.loaded.lovely = { mod_dir = "" }
G = { FUNCS = {} }
Brainstorm = { SETTINGS = { autoreroll = {}, multiAnteSearch = {} }, AUTOREROLL = {} }
assert(loadfile(reroll))()
Brainstorm.seedPoolDir = function() return poolDir end

local marker, reason = Brainstorm.readPoolAttachment(markerPath)
if expected == "valid" then
  assert(marker, reason)
else
  assert(not marker, "mutated marker was accepted")
end
