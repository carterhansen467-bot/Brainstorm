-- win_spawn.lua -- detached-process launcher for Windows.
--
-- The native helper must start from inside the game with (a) no console
-- window flash and (b) no cmd.exe quoting pitfalls. os.execute goes through
-- cmd.exe and fails both, so this goes straight to kernel32 CreateProcessA
-- via LuaJIT's FFI with CREATE_NO_WINDOW. Standalone on purpose: it needs
-- only the ffi module (Balatro ships LuaJIT), no love/G, so the CI Windows
-- job can exercise the exact same file with a bare luajit.exe.
--
-- Returns a module table: spawn(argv) -> true | false, errorMessage.

local ffi = require("ffi")

-- Other mods may cdef the same Win32 functions; a redefinition makes cdef
-- throw but leaves the earlier (compatible) declaration usable, so failures
-- here are ignored and spawn() probes callability itself.
pcall(ffi.cdef, [[
typedef struct {
	uint32_t cb;
	char *lpReserved, *lpDesktop, *lpTitle;
	uint32_t dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars,
		dwFillAttribute, dwFlags;
	uint16_t wShowWindow, cbReserved2;
	uint8_t *lpReserved2;
	void *hStdInput, *hStdOutput, *hStdError;
} BS_STARTUPINFOA;
typedef struct {
	void *hProcess, *hThread;
	uint32_t dwProcessId, dwThreadId;
} BS_PROCESS_INFORMATION;
]])
pcall(ffi.cdef, [[
int CreateProcessA(const char *lpApplicationName, char *lpCommandLine,
	void *lpProcessAttributes, void *lpThreadAttributes, int bInheritHandles,
	uint32_t dwCreationFlags, void *lpEnvironment, const char *lpCurrentDirectory,
	BS_STARTUPINFOA *lpStartupInfo, BS_PROCESS_INFORMATION *lpProcessInformation);
int CloseHandle(void *hObject);
uint32_t GetLastError(void);
]])

local CREATE_NO_WINDOW = 0x08000000

-- Canonical Windows argv quoting (what CommandLineToArgvW / the CRT parse):
-- quote the arg, double runs of backslashes before an embedded or closing
-- quote, backslash-escape embedded quotes.
local function quote_arg(s)
	s = tostring(s)
	if s ~= "" and not s:find('[%s"]') then return s end
	local out = { '"' }
	local backslashes = 0
	for i = 1, #s do
		local c = s:sub(i, i)
		if c == "\\" then
			backslashes = backslashes + 1
		elseif c == '"' then
			out[#out + 1] = string.rep("\\", backslashes * 2 + 1) .. '"'
			backslashes = 0
		else
			out[#out + 1] = string.rep("\\", backslashes) .. c
			backslashes = 0
		end
	end
	out[#out + 1] = string.rep("\\", backslashes * 2) .. '"'
	return table.concat(out)
end

local M = {}
M.quote_arg = quote_arg -- exposed for the test harness

function M.spawn(argv)
	local ok, err = pcall(function()
		local parts = {}
		for i = 1, #argv do parts[i] = quote_arg(argv[i]) end
		local cmd = table.concat(parts, " ")
		local si = ffi.new("BS_STARTUPINFOA")
		si.cb = ffi.sizeof("BS_STARTUPINFOA")
		local pi = ffi.new("BS_PROCESS_INFORMATION")
		-- CreateProcess may scribble on the command line: hand it a copy.
		local buf = ffi.new("char[?]", #cmd + 1, cmd)
		if ffi.C.CreateProcessA(nil, buf, nil, nil, 0, CREATE_NO_WINDOW,
				nil, nil, si, pi) == 0 then
			error("CreateProcessA failed, code " .. tostring(ffi.C.GetLastError()))
		end
		ffi.C.CloseHandle(pi.hProcess)
		ffi.C.CloseHandle(pi.hThread)
	end)
	if not ok then return false, tostring(err) end
	return true
end

return M
