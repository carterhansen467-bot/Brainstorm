-- Regression coverage for nativefs's overloaded read signatures. A bounded
-- nativefs.read(path, bytes) call reaches File:read("string", bytes); that
-- explicit-container form must preserve the requested byte count.

local source = arg[1] or "nativefs.lua"
local ffi = require("ffi")

-- Bare Windows LuaJIT has the CRT functions used by nativefs, but not LOVE's
-- DLL. Directory operations need LOVE/PhysFS; this file-read test does not.
local ffiLoad = ffi.load
ffi.load = function(name, ...)
	if name == "love" then return ffi.C end
	return ffiLoad(name, ...)
end

love = {
	data = {},
	filesystem = {},
}

local allocationLimit = math.huge
function love.data.newByteData(size)
	assert(size <= allocationLimit,
		"bounded read attempted a payload-sized allocation: " .. tostring(size))
	local buffer = ffi.new("uint8_t[?]", size)
	return {
		getFFIPointer = function() return buffer end,
		getString = function() return ffi.string(buffer, size) end,
		release = function() end,
	}
end

function love.filesystem.newFileData(contents, name)
	return {
		contents = contents,
		name = name,
		getString = function(self) return self.contents end,
	}
end

local nativefs = assert(loadfile(source))()
ffi.load = ffiLoad
local path = os.tmpname()
local payload = ("0123456789abcdef"):rep(4096)
local output = assert(io.open(path, "wb"))
assert(output:write(payload))
assert(output:close())

local function expect_string(actual, count, expected, label)
	assert(count == expected, label .. " returned the wrong byte count")
	assert(#actual == expected, label .. " returned the wrong string length")
	assert(actual == payload:sub(1, expected), label .. " returned the wrong bytes")
end

local bounded, count = nativefs.read(path, 17)
expect_string(bounded, count, 17, "nativefs.read(path, bytes)")

local explicit
explicit, count = nativefs.read("string", path, 23)
expect_string(explicit, count, 23, "nativefs.read(string, path, bytes)")

local data
data, count = nativefs.read("data", path, 29)
assert(count == 29, "nativefs.read(data, path, bytes) returned the wrong byte count")
assert(data:getString() == payload:sub(1, 29),
	"nativefs.read(data, path, bytes) returned the wrong bytes")

local file = nativefs.newFile(path)
assert(file:open("r"))
local direct
direct, count = file:read(31)
expect_string(direct, count, 31, "File:read(bytes)")
assert(file:seek(0))
direct, count = file:read("string", 37)
expect_string(direct, count, 37, "File:read(string, bytes)")
assert(file:close())

local all
all, count = nativefs.read(path)
expect_string(all, count, #payload, "nativefs.read(path)")

local empty
empty, count = nativefs.read(path, 0)
assert(empty == "" and count == 0, "zero-byte read was not empty")

assert(os.remove(path))

-- Keep the original failure mode safe to test: the old overload attempted to
-- allocate this sparse file's full logical size. Reject anything above 1 MiB
-- before allocation, while a correct bounded read needs only 1 KiB.
local sparsePath = os.tmpname()
local sparse = assert(io.open(sparsePath, "wb"))
assert(sparse:seek("set", 64 * 1024 * 1024 - 1))
assert(sparse:write("x"))
assert(sparse:close())
allocationLimit = 1024 * 1024
local prefix
prefix, count = nativefs.read(sparsePath, 1024)
assert(count == 1024 and #prefix == 1024,
	"bounded sparse-file read returned more than its prefix")
assert(os.remove(sparsePath))

print("nativefs bounded-read regression: ok")
