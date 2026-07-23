-- Regression coverage for Brainstorm's private nativefs module name. Some
-- other mods register an incompatible module as "nativefs"; Brainstorm must
-- never depend on whichever generic module Lovely happened to load last.

local root = arg[1] or "."

local function path(name)
	if root == "." then return name end
	local last = root:sub(-1)
	if last == "/" or last == "\\" then return root .. name end
	return root .. "/" .. name
end

local function read_file(name)
	local filename = path(name)
	local file, err = io.open(filename, "rb")
	assert(file, string.format("could not open %s: %s", filename, tostring(err)))
	local contents = assert(file:read("*a"))
	file:close()
	return contents:gsub("\r\n", "\n")
end

local function line_number(contents, offset)
	local _, newlines = contents:sub(1, offset):gsub("\n", "\n")
	return newlines + 1
end

-- Lovely must publish Brainstorm's bundled nativefs under a private name.
local manifest = read_file("lovely.toml")
local module_block = (manifest .. "\n[[patches]]"):match(
	"%[patches%.module%]([%s%S]-)\n%s*%[%[patches%]%]"
)
assert(module_block, "lovely.toml has no [patches.module] block")
assert(module_block:match("source%s*=%s*['\"]nativefs%.lua['\"]"),
	"lovely.toml must register the bundled nativefs.lua module")
assert(module_block:match("name%s*=%s*['\"]brainstorm_nativefs['\"]"),
	"lovely.toml must register nativefs.lua as brainstorm_nativefs")
assert(not module_block:match("name%s*=%s*['\"]nativefs['\"]"),
	"lovely.toml must not register Brainstorm's module under the shared nativefs name")

-- Keep every production consumer off the shared module name. This is static
-- by design: it catches lazy/local requires that a smoke test may not execute.
local consumers = {
	"Brainstorm.lua",
	"Brainstorm_main.lua",
	"Brainstorm_UI.lua",
	"Brainstorm_keyhandler.lua",
	"Brainstorm_reroll.lua",
	"Brainstorm_estimate.lua",
}
local generic_require = "require%s*%(%s*['\"]nativefs['\"]%s*%)"
for _, name in ipairs(consumers) do
	local contents = read_file(name)
	local offset = contents:find(generic_require)
	assert(not offset, string.format(
		"%s:%d requires the shared nativefs module; use brainstorm_nativefs",
		name, offset and line_number(contents, offset) or 1
	))
end

-- Search workers have a separate Lua state, so the private module must also be
-- preloaded inside the embedded worker source before Brainstorm_reroll is read.
local reroll = read_file("Brainstorm_reroll.lua")
local worker = reroll:match(
	"Brainstorm%.SEARCH_WORKER_SRC%s*=%s*%[==%[([%s%S]-)%]==%]"
)
assert(worker, "could not locate Brainstorm.SEARCH_WORKER_SRC")
assert(worker:match(
	"package%.preload%s*%[%s*['\"]brainstorm_nativefs['\"]%s*%]"
), "search worker must preload brainstorm_nativefs")
assert(not worker:match(
	"package%.preload%s*%[%s*['\"]nativefs['\"]%s*%]"
), "search worker must not preload the shared nativefs name")

-- Model the real collision: another mod owns a broken generic nativefs while
-- Brainstorm's private module remains usable and distinct.
package.loaded["nativefs"] = nil
package.loaded["brainstorm_nativefs"] = nil

local generic_accesses = 0
package.preload["nativefs"] = function()
	return setmetatable({ owner = "other-mod" }, {
		__index = function(_, key)
			generic_accesses = generic_accesses + 1
			error("incompatible generic nativefs member: " .. tostring(key), 2)
		end,
	})
end

local private_module = { owner = "brainstorm" }
package.preload["brainstorm_nativefs"] = function()
	return private_module
end

local brainstorm_nativefs = require("brainstorm_nativefs")
local other_nativefs = require("nativefs")
assert(brainstorm_nativefs == private_module,
	"private require did not resolve Brainstorm's module")
assert(other_nativefs ~= brainstorm_nativefs,
	"generic and private nativefs modules unexpectedly alias each other")
assert(generic_accesses == 0,
	"loading Brainstorm's private module touched the incompatible generic module")

local generic_ok = pcall(function()
	return other_nativefs.read("pool.bspool", 1024)
end)
assert(not generic_ok, "collision model's generic nativefs should be incompatible")

-- Exercise the production header reader, not only the two require calls. The
-- private stub models an 8 KiB schema-3 header and rejects any unbounded or
-- unexpectedly large request before it can allocate a pool-sized payload.
local header_prefix = table.concat({
	"BRAINSTORM_SEED_POOL 3",
	"header_bytes 8192",
	"records 7",
	"complete 1",
	"end",
	"",
}, "\n")
local header = header_prefix .. string.rep("\0", 8192 - #header_prefix)
local read_sizes = {}
private_module.read = function(filename, bytes)
	assert(filename == "collision-test.bspool")
	assert(type(bytes) == "number" and bytes <= 256 * 1024,
		"production header read was unbounded")
	read_sizes[#read_sizes + 1] = bytes
	return header:sub(1, bytes), bytes
end
private_module.write = function() end
private_module.getInfo = function() return nil end
private_module.getDirectoryItems = function() return {} end

package.loaded.lovely = { mod_dir = "" }
G = { FUNCS = {} }
Brainstorm = {
	SETTINGS = { autoreroll = {}, multiAnteSearch = {} },
	AUTOREROLL = {},
}
assert(loadstring(reroll, "@Brainstorm_reroll.lua"))()
local parsed = assert(Brainstorm.readPoolHeader("collision-test.bspool"))
assert(parsed.schema == 3 and parsed.records == 7 and parsed.complete == 1,
	"production header reader did not use the private module")
assert(#read_sizes == 2 and read_sizes[1] == 1024 and read_sizes[2] == 8192,
	"production header reader did not preserve bounded schema-3 reads")
assert(generic_accesses == 1,
	"production header read was routed through the generic module")

print("nativefs module-isolation regression: ok")
