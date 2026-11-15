-- license:BSD-3-Clause
-- mamemcp: minimal local bridge between MAME and an external client (POC).
--
-- Transport : newline-delimited JSON over a loopback TCP socket (emu.file "socket.127.0.0.1:PORT").
-- Servicing : every request is handled from emu.register_periodic(), which was verified on MAME 0.289
--             to run while emulation is running, paused and stopped in the debugger.
-- Safety    : only a fixed set of typed operations exists. No Lua evaluation, no shell, no
--             general debugger command pass-through. Debugger expressions that are built here are
--             assembled only from validated tokens (register names present in cpu.state, integers).

local json = require("json")

local exports = {
	name = "mamemcp",
	version = "0.1.0",
	description = "Local MAME bridge (proof of concept)",
	license = "BSD-3-Clause",
	author = { name = "mame-mcp POC" },
}

local BRIDGE_VERSION = "0.1.0"
local PROTOCOL_VERSION = 1

local LIMITS = {
	max_read_bytes = 4096,
	max_registers = 64,
	max_capture_registers = 8,
	max_memory_fields = 8,
	max_banks = 32,
	max_locations = 8,
	max_events = 10000,
	max_wall_seconds = 600,
	max_line_bytes = 65536,
	max_history = 20,
	max_status_events = 5,
	console_scan_cap = 20000,
}

local function env(name, default)
	local v = os.getenv(name)
	if v == nil or v == "" then return default end
	return v
end

function exports.startplugin()
	local cfg = {
		port = tonumber(env("MAMEMCP_PORT", "9411")),
		evidence_dir = env("MAMEMCP_EVIDENCE_DIR", "mamemcp-evidence"),
		-- TEST-ONLY fault injection: make the Nth breakpoint installation of a capture fail (validates partial-install cleanup).
		-- Unset in normal use.
		test_fail_install_at = tonumber(os.getenv("MAMEMCP_TEST_FAIL_INSTALL_AT") or ""),
		-- TEST-ONLY fault injection: the bridge's own bpclear of the Nth location (1-based) of any experiment is suppressed
		-- and reported as failed, so the breakpoint really stays installed in MAME. Unset in normal use.
		test_fail_clear = tonumber(os.getenv("MAMEMCP_TEST_FAIL_CLEAR") or ""),
	}

	local ticks_per_s = emu.osd_ticks_per_second()
	local function host_ms() return math.floor(emu.osd_ticks() * 1000 / ticks_per_s) end
	local function iso_utc() return os.date("!%Y-%m-%dT%H:%M:%SZ") end

	local session_id = string.format("s%d-%06x", os.time(), (emu.osd_ticks() ~ (os.time() * 7919)) & 0xffffff)
	local started_iso = iso_utc()
	local counters = { periodic = 0, requests = 0, errors = 0, bad_lines = 0 }

	local sock = emu.file("", 7)
	local function open_socket()
		local err = sock:open("socket.127.0.0.1:" .. tostring(cfg.port))
		if err then
			print("mamemcp: cannot open socket on port " .. tostring(cfg.port) .. ": " .. tostring(err))
		end
		return err
	end
	local open_err = open_socket()
	if not open_err then
		print(string.format("mamemcp: listening on 127.0.0.1:%d session=%s evidence=%s",
			cfg.port, session_id, cfg.evidence_dir))
	end

	local inbuf, outbuf = "", ""
	-- MAME's socket file accepts exactly one client and then never listens again, so after a client
	-- goes away the socket has to be closed and opened again to accept the next one.
	local client_seen = false      -- bytes have been received from the current client
	local write_failures = 0
	local last_heartbeat_ms = 0
	local reopen_after_flush = false
	local reconnects = 0
	local function reopen_socket(reason)
		pcall(function() sock:close() end)
		inbuf, outbuf = "", ""
		client_seen, write_failures, reopen_after_flush = false, 0, false
		reconnects = reconnects + 1
		local err = open_socket()
		print("mamemcp: socket reopened (" .. reason .. ")" .. (err and (" ERROR " .. tostring(err)) or ""))
	end
	local experiments = {}  -- id -> experiment
	local order = {}        -- ids oldest -> newest
	local active = nil
	local exp_counter = 0
	local stopping = false

	---------------------------------------------------------------------------
	-- helpers
	---------------------------------------------------------------------------
	local function fail(code, message, extra)
		return nil, { code = code, message = message, detail = extra }
	end

	local function hex(v) return string.format("0x%X", v) end

	local function try(fn, ...)
		local ok, v = pcall(fn, ...)
		if ok and v ~= nil then return v end
		return { unavailable = true, reason = ok and "nil" or tostring(v) }
	end

	local function machine() return manager.machine end

	local function exec_state()
		local m = machine()
		local dbg = m.debugger
		local dstate = dbg and dbg.execution_state or nil
		local state
		-- precedence: a debugger stop also sets machine.paused on this build, so check it first
		if dstate == "stop" then state = "debugger_stopped"
		elseif m.paused then state = "paused"
		else state = "running" end
		return { state = state, paused = m.paused, debugger_state = dstate }
	end

	local function emu_clock()
		local m = machine()
		local frame = nil
		local ok, scr = pcall(function()
			for _, s in pairs(m.screens) do return s end
		end)
		if ok and scr then
			local ok2, f = pcall(function() return scr:frame_number() end)
			if ok2 then frame = f end
		end
		return { emu_time_s = m.time:as_double(), frame = frame }
	end

	local function find_cpu(tag)
		if type(tag) ~= "string" or tag == "" then
			return fail("bad_argument", "cpu must be a non-empty device tag such as ':maincpu'")
		end
		local dev = machine().devices[tag]
		if not dev then return fail("unknown_cpu", "no device with tag " .. tag) end
		local has_spaces = false
		for _ in pairs(dev.spaces) do has_spaces = true break end
		if not has_spaces then return fail("not_a_cpu", tag .. " has no address spaces") end
		return dev
	end

	local function list_spaces(dev)
		local t = {}
		for name, sp in pairs(dev.spaces) do
			t[#t + 1] = {
				name = name,
				address_mask = hex(sp.address_mask),
				shift = sp.shift,
				data_width_bits = sp.data_width,
				endianness = sp.endianness,
			}
		end
		table.sort(t, function(a, b) return a.name < b.name end)
		return t
	end

	local function parse_uint(v, what)
		if math.type(v) == "integer" then
			if v < 0 then return fail("bad_argument", what .. " must be non-negative") end
			return v
		end
		if type(v) == "number" and v >= 0 and v == math.floor(v) and v < 2 ^ 53 then
			return math.tointeger(v)
		end
		if type(v) == "string" and v:match("^0[xX]%x+$") then return tonumber(v) end
		return fail("bad_argument", what .. " must be a non-negative integer or a 0x-prefixed hex string")
	end

	local function valid_ident(s)
		return type(s) == "string" and s:match("^[A-Za-z_][A-Za-z0-9_]*$") ~= nil and #s <= 24
	end

	---------------------------------------------------------------------------
	-- evidence files
	---------------------------------------------------------------------------
	local lfs = require("lfs")

	local function mkdir_p(path)
		local acc = ""
		for part in path:gmatch("[^/\\]+") do
			if acc == "" then
				acc = part
				if part:match("^%a:$") then acc = part .. "/" ; goto continue end
			else
				acc = (acc:sub(-1) == "/") and (acc .. part) or (acc .. "/" .. part)
			end
			lfs.mkdir(acc)
			::continue::
		end
		return lfs.attributes(path, "mode") == "directory"
	end

	local function write_manifest(exp)
		local f, err = io.open(exp.manifest_path, "w")
		if not f then
			exp.evidence_errors[#exp.evidence_errors + 1] = "manifest write failed: " .. tostring(err)
			return false
		end
		f:write(json.encode(exp.manifest, { indent = true }))
		f:close()
		return true
	end

	---------------------------------------------------------------------------
	-- operations
	---------------------------------------------------------------------------
	local ops = {}

	-- breakpoints currently installed on a cpu, flagged when owned by an experiment of this bridge
	local function list_breakpoints(dev, tag)
		local out = {}
		local ok, list = pcall(function() return dev.debug:bplist() end)
		if not ok then return { unavailable = true, reason = tostring(list) } end
		local owned = {}
		for _, id in ipairs(order) do
			local e = experiments[id]
			if e and e.state == "running" and e.cpu == tag then
				for _, loc in ipairs(e.locs or {}) do
					if loc.bp_index then owned[loc.bp_index] = id end
				end
			end
		end
		for idx, bp in pairs(list) do
			out[#out + 1] = {
				index = idx, address = hex(bp.address), condition = bp.condition, action = bp.action,
				enabled = bp.enabled, owned_by_experiment = owned[idx],
			}
		end
		table.sort(out, function(a, b) return a.index < b.index end)
		return out
	end

	local orphan_rows  -- defined with the unresolved-instrumentation registry below

	function ops.session_info(args)
		local m = machine()
		local sys = m.system
		local devices = {}
		for tag, dev in pairs(m.devices) do
			local has_spaces = false
			for _ in pairs(dev.spaces) do has_spaces = true break end
			if has_spaces then
				devices[#devices + 1] = {
					tag = tag,
					type = try(function() return dev.shortname end),
					name = try(function() return dev.name end),
					address_spaces = list_spaces(dev),
					register_count = try(function() local n = 0 for _ in pairs(dev.state) do n = n + 1 end return n end),
					breakpoints = machine().debugger and list_breakpoints(dev, tag) or { unavailable = true, reason = "no debugger (start MAME with -debug)" },
				}
			end
		end
		table.sort(devices, function(a, b) return a.tag < b.tag end)
		local regions = {}
		pcall(function()
			for tag, r in pairs(m.memory.regions) do
				regions[#regions + 1] = { tag = tag, size_bytes = r.size, endianness = r.endianness, bitwidth = r.bitwidth }
			end
		end)
		table.sort(regions, function(a, b) return a.tag < b.tag end)
		return {
			mame_version = emu.app_version(),
			machine = {
				name = sys.name,
				description = sys.description,
				year = sys.year,
				manufacturer = sys.manufacturer,
				parent = try(function() return sys.parent end),
				source_file = try(function() return sys.source_file end),
			},
			cpus = devices,
			memory_regions = regions,
			rom_identity = {
				hashes = { unavailable = true, reason = "MAME's Lua API exposes no ROM hash; use the launcher's launch.json (listxml hashes, -verifyroms)" },
				region_sizes = regions,
			},
			execution = exec_state(),
			clock = emu_clock(),
			bridge = {
				version = BRIDGE_VERSION,
				protocol = PROTOCOL_VERSION,
				session_id = session_id,
				started_utc = started_iso,
				transport = "ndjson over loopback tcp",
				port = cfg.port,
				evidence_dir = cfg.evidence_dir,
				debugger_available = machine().debugger ~= nil,
				limits = LIMITS,
				operations = { "session_info", "read_memory", "read_registers", "capture_breakpoint",
					"experiment_status", "cancel_experiment", "execution_control", "goodbye" },
				capture_scope = "breakpoints on the program space of the named cpu; registers are read in the breakpoint condition (that cpu's own symbol table); memory fields use explicitly device-qualified, side-effect-suppressed debugger operators (tag.pb@ etc.). Verified on :maincpu (68000) and :soundcpu (Z80) of shinobi only.",
			},
			active_experiment = active and active.id or nil,
			unresolved_instrumentation = orphan_rows(),
			test_hooks = (function()
				local t = {}
				if cfg.test_fail_install_at then t[#t + 1] = "MAMEMCP_TEST_FAIL_INSTALL_AT=" .. cfg.test_fail_install_at end
				if cfg.test_fail_clear then t[#t + 1] = "MAMEMCP_TEST_FAIL_CLEAR=" .. cfg.test_fail_clear end
				return t
			end)(),
			counters = { periodic_calls = counters.periodic, requests = counters.requests, errors = counters.errors, socket_reopens = reconnects },
		}
	end

	-- Space letters understood by symbol_table:memory_value / debugger expressions.
	local SPACE_LETTER = { program = "p", data = "d", io = "i", opcodes = "3" }

	function ops.read_memory(args)
		local dev, err = find_cpu(args.cpu)
		if not dev then return nil, err end
		local space_name = args.space or "program"
		local sp = dev.spaces[space_name]
		if not sp then return fail("unknown_space", "cpu " .. args.cpu .. " has no address space '" .. tostring(space_name) .. "'") end
		if sp.shift ~= 0 then
			return fail("unsupported_space", "address shift " .. sp.shift .. " not supported by this POC")
		end
		local letter = SPACE_LETTER[space_name]
		if not letter then
			return fail("unsupported_space", "space '" .. space_name .. "' has no debugger-expression equivalent; supported: program, data, io, opcodes")
		end
		local mode = args.mode or "inspect"
		if mode ~= "inspect" and mode ~= "direct" then
			return fail("bad_argument", "mode must be 'inspect' or 'direct'")
		end
		if mode == "direct" then
			if space_name ~= "program" then return fail("unsupported_space", "mode 'direct' is only available for the program space") end
			letter = "r"
		end
		local addr, e1 = parse_uint(args.address, "address")
		if not addr then return nil, e1 end
		local len, e2 = parse_uint(args.length, "length")
		if not len then return nil, e2 end
		if len < 1 then return fail("bad_argument", "length must be >= 1") end
		if len > LIMITS.max_read_bytes then
			return fail("too_large", "length " .. len .. " exceeds limit " .. LIMITS.max_read_bytes)
		end
		local last = addr + len - 1
		if last > sp.address_mask then
			return fail("out_of_range", string.format("range %s-%s exceeds address mask %s",
				hex(addr), hex(last), hex(sp.address_mask)))
		end
		-- emu.symbol_table(dev):memory_value(tag, space, addr, size, disable_se) is the same primitive the
		-- debugger's '@' memory operator uses (symbol_table::memory_value, mame0289 express.cpp), with side
		-- effects disabled. Address-space:read_u8/readv_u8 are NOT used: they are plain reads (luaengine_mem.cpp).
		local st = emu.symbol_table(dev)
		local t = {}
		for i = 0, len - 1 do
			t[#t + 1] = string.format("%02x", st:memory_value(args.cpu, letter, addr + i, 1, true))
		end
		local info = {
			cpu = args.cpu, space = space_name,
			address = hex(addr), length = len,
			data_hex = table.concat(t),
			representation = "bytes in ascending address order, lowercase hex, 2 chars per byte (not endian-swapped)",
			address_units = "address-space native units; shift=0 here so one unit == one byte",
			read_mode = mode,
		}
		if mode == "inspect" then
			info.read_semantics = "symbol_table:memory_value(disable_se=true), logical program/data/io address, one byte per call"
			info.side_effects = "MAME's side-effects-disabled flag is set during each read. Handlers that honour machine.side_effects_disabled() skip their effects; a handler that ignores it may still have effects. Not proven absent for any specific device."
		else
			info.read_semantics = "direct program-space pointer read (debugger 'r' space); no device handler is ever invoked"
			info.side_effects = "none: handlers are bypassed"
			info.limitations = "bytes without a direct read pointer (I/O, handler-backed, unmapped) come back as 0x00 and cannot be told apart from real zeros"
		end
		if args.bank_context then
			-- Opt-in. manager.machine.memory.banks[tag].entry is the currently selected entry of each memory bank.
			-- It is sampled in this same plugin call as the bytes (emulation does not advance in between), so for
			-- THIS read it is atomic with the data. MAME's Lua API does not say which addresses a bank backs, so no
			-- mapping from the requested range to a bank is claimed; the caller (or a machine profile) supplies it.
			local banks, n, truncated = {}, 0, false
			local tags = {}
			for tag in pairs(manager.machine.memory.banks) do tags[#tags + 1] = tag end
			table.sort(tags)
			for _, tag in ipairs(tags) do
				if n >= LIMITS.max_banks then truncated = true break end
				local ok, entry = pcall(function() return manager.machine.memory.banks[tag].entry end)
				n = n + 1
				banks[n] = ok and { tag = tag, entry = entry } or { tag = tag, entry = nil, error = "entry not readable" }
			end
			info.bank_context = {
				banks = banks, truncated = truncated,
				sampled = "same plugin call as the bytes; emulation does not advance between them (atomic for this read only)",
				covers_requested_range = "not determined: the MAME Lua API does not report which address range a bank backs",
				backing_region_offset = "not available from the API; a machine profile must state it",
			}
		end
		info.execution = exec_state()
		info.clock = emu_clock()
		return info
	end

	local function register_entry(dev, name)
		local st = dev.state[name]
		if not st then return nil end
		local value = st.value
		return {
			name = name,
			hex = string.format("0x%X", value),
			dec = string.format("%d", value),
		}
	end

	function ops.read_registers(args)
		local dev, err = find_cpu(args.cpu)
		if not dev then return nil, err end
		local names = {}
		if args.registers ~= nil then
			if type(args.registers) ~= "table" then return fail("bad_argument", "registers must be a list of names") end
			for _, n in ipairs(args.registers) do
				if not valid_ident(n) then return fail("bad_argument", "invalid register name") end
				if not dev.state[n] then return fail("unknown_register", "cpu " .. args.cpu .. " has no register " .. n) end
				names[#names + 1] = n
			end
			if #names > LIMITS.max_registers then return fail("too_large", "too many registers requested") end
		else
			for n in pairs(dev.state) do names[#names + 1] = n end
			table.sort(names)
			if #names > LIMITS.max_registers then
				return fail("too_large", "cpu exposes " .. #names .. " registers; name the ones you want")
			end
		end
		local out = {}
		for _, n in ipairs(names) do out[#out + 1] = register_entry(dev, n) end
		return {
			cpu = args.cpu, registers = out,
			numeric_format = "hex is unsigned, '0x'-prefixed; dec is an unsigned decimal string (no float rounding)",
			note = "PC is the CPU's prefetch-adjusted program counter on some cores; CURPC is the address of the executing instruction (m68000 observed).",
			execution = exec_state(), clock = emu_clock(),
		}
	end

	function ops.goodbye(args)
		reopen_after_flush = true
		return { closing = true, note = "bridge will accept a new client after this reply is flushed" }
	end

	function ops.execution_control(args)
		local action = args.action
		if action ~= "pause" and action ~= "resume" then
			return fail("bad_argument", "action must be 'pause' or 'resume'")
		end
		local before = exec_state()
		local m = machine()
		if action == "pause" then
			emu.pause()
		else
			if m.debugger then m.debugger.execution_state = "run" end
			emu.unpause()
		end
		-- Requested only: the new state takes effect asynchronously w.r.t. this callback.
		return { action = action, requested = true, before = before, after_request = exec_state(),
			note = "state is re-read at request time; confirm with session_info" }
	end

	-- debugger temporaries -------------------------------------------------------
	-- MAME 0.289 offers exactly ten global writable debugger variables, temp0..temp9
	-- (debugcpu.cpp NUM_TEMP_VARIABLES). There is no way to create experiment-private state, so an
	-- experiment has to borrow some of them. Policy:
	--   * a temp is borrowed only if its value is 0 AND no installed breakpoint (any CPU) mentions it;
	--   * borrowed temps are recorded and released (reset to the 0 we found) only if the counter shows
	--     nobody else touched them; otherwise they are left alone and the disturbance is reported;
	--   * this is NOT concurrency-safe against a user who starts using the same temp mid-experiment: that is
	--     detected at release time via the hit counter, not prevented.
	local TEMP_COUNT = 10
	local claimed_temps = {}  -- index -> experiment id

	local function read_temps()
		local dbg = machine().debugger
		local names = {}
		for i = 0, TEMP_COUNT - 1 do names[#names + 1] = "temp" .. i end
		dbg:command('printf "TV|%X|%X|%X|%X|%X|%X|%X|%X|%X|%X",' .. table.concat(names, ","))
		local cl = dbg.consolelog
		local line = cl[#cl] or ""
		if line:sub(1, 3) ~= "TV|" then return nil, "unexpected console line: " .. line end
		local vals = {}
		for h in line:sub(4):gmatch("[^|]+") do vals[#vals + 1] = tonumber(h, 16) end
		if #vals ~= TEMP_COUNT then return nil, "expected 10 values, got " .. #vals end
		return vals
	end

	local function temps_referenced_by_breakpoints()
		local used = {}
		for _, dev in pairs(machine().devices) do
			local ok, list = pcall(function() return dev.debug and dev.debug:bplist() end)
			if ok and list then
				for _, bp in pairs(list) do
					for _, text in ipairs({ bp.condition or "", bp.action or "" }) do
						for n in text:gmatch("temp(%d)") do used[tonumber(n)] = true end
					end
				end
			end
		end
		return used
	end

	-- returns an array of `count` temp indices (highest free first) or nil, error
	local function allocate_temps(count)
		local vals, verr = read_temps()
		if not vals then return nil, { code = "debug_state_unavailable", message = "cannot read debugger temporaries: " .. tostring(verr) } end
		local used = temps_referenced_by_breakpoints()
		local picked, report = {}, {}
		for i = TEMP_COUNT - 1, 0, -1 do
			local why
			if claimed_temps[i] then why = "claimed by " .. claimed_temps[i]
			elseif vals[i + 1] ~= 0 then why = string.format("non-zero (0x%X): may be in use", vals[i + 1])
			elseif used[i] then why = "referenced by an installed breakpoint"
			end
			report[#report + 1] = { temp = i, available = why == nil, reason = why }
			if not why and #picked < count then picked[#picked + 1] = i end
		end
		if #picked < count then
			return nil, { code = "debug_state_unavailable",
				message = "need " .. count .. " free debugger temporaries (temp0..temp9 are shared, global debugger state); only " .. #picked .. " are free",
				detail = report }
		end
		return picked, nil, report
	end

	-- capture_breakpoint ------------------------------------------------------
	local MEMOP = { [1] = "b", [2] = "w", [4] = "d" }

	local function build_capture(dev, args)
		local dbg = machine().debugger
		if not dbg then
			return fail("debugger_unavailable", "MAME was started without -debug; breakpoints are unavailable")
		end
		if active then return fail("experiment_active", "experiment " .. active.id .. " is still running") end
		local space = dev.spaces["program"]
		if not space then return fail("unknown_space", "cpu has no program space") end
		-- Locations: either one legacy `address`, or 1..max_locations {address, id} on this one CPU.
		local locs, multi = {}, false
		if args.locations ~= nil then
			if args.address ~= nil then return fail("bad_argument", "give either address or locations, not both") end
			local raw = args.locations
			if type(raw) ~= "table" or #raw < 1 or #raw > LIMITS.max_locations then
				return fail("bad_argument", "locations must be a list of 1.." .. LIMITS.max_locations .. " entries")
			end
			local ids, addrs = {}, {}
			for i, l in ipairs(raw) do
				if type(l) ~= "table" then return fail("bad_argument", "each location must be an object") end
				local a, ea = parse_uint(l.address, "locations[" .. (i - 1) .. "].address")
				if not a then return nil, ea end
				local lid = l.id
				if lid == nil then lid = "L" .. i end
				if type(lid) ~= "string" or #lid > 24 or not lid:match("^%a[%w_]*$") then
					return fail("bad_argument", "location id must be 1-24 characters: a letter, then letters, digits or underscore")
				end
				if ids[lid] then return fail("bad_argument", "duplicate location id " .. lid) end
				if addrs[a] then return fail("bad_argument", "duplicate location address " .. hex(a)) end
				if a > space.address_mask then return fail("out_of_range", "location " .. lid .. " address exceeds program space mask") end
				ids[lid], addrs[a] = true, true
				locs[#locs + 1] = { id = lid, address = a }
			end
			multi = true
		else
			local addr, e = parse_uint(args.address, "address")
			if not addr then return nil, e end
			if addr > space.address_mask then return fail("out_of_range", "address exceeds program space mask") end
			locs[1] = { id = "L1", address = addr }
		end
		local max_events, e2 = parse_uint(args.max_events, "max_events")
		if not max_events then return nil, e2 end
		if max_events < 1 or max_events > LIMITS.max_events then
			return fail("bad_argument", "max_events must be 1.." .. LIMITS.max_events)
		end
		local max_wall = args.max_wall_seconds
		if type(max_wall) ~= "number" or max_wall <= 0 or max_wall > LIMITS.max_wall_seconds then
			return fail("bad_argument", "max_wall_seconds must be a number in (0, " .. LIMITS.max_wall_seconds .. "]")
		end
		local regs = {}
		for _, n in ipairs(args.registers or {}) do
			if not valid_ident(n) or not dev.state[n] then
				return fail("unknown_register", "register '" .. tostring(n) .. "' is not a state entry of this cpu")
			end
			local ok, sym = pcall(function() return dev.state[n].symbol end)
			if not ok or type(sym) ~= "string" or sym == "" or not valid_ident(sym) then
				return fail("unknown_register", "register '" .. n .. "' has no debugger-expression symbol")
			end
			regs[#regs + 1] = { name = n, symbol = sym:lower() }
		end
		if #regs > LIMITS.max_capture_registers then
			return fail("too_large", "at most " .. LIMITS.max_capture_registers .. " registers per capture (each needs a borrowed debugger temporary)")
		end
		local mems = {}
		for _, mf in ipairs(args.memory or {}) do
			if type(mf) ~= "table" then return fail("bad_argument", "memory entries must be objects") end
			local ma, e3 = parse_uint(mf.address, "memory.address")
			if not ma then return nil, e3 end
			local size = mf.size
			if size ~= 1 and size ~= 2 and size ~= 4 then return fail("bad_argument", "memory.size must be 1, 2 or 4") end
			local sname = mf.space or "program"
			local sp = dev.spaces[sname]
			if not sp then return fail("unknown_space", "cpu " .. args.cpu .. " has no address space '" .. tostring(sname) .. "'") end
			if sp.shift ~= 0 then return fail("unsupported_space", "address shift not supported") end
			local letter = SPACE_LETTER[sname]
			if not letter or letter == "3" then return fail("unsupported_space", "capture memory space must be program, data or io") end
			if ma + size - 1 > sp.address_mask then return fail("out_of_range", "memory field outside address space") end
			mems[#mems + 1] = { address = ma, size = size, space = sname, letter = letter }
		end
		if #mems > LIMITS.max_memory_fields then return fail("too_large", "too many memory fields") end
		-- MAME evaluates only the first matching breakpoint at an address (debugcpu.cpp breakpoint_check
		-- breaks after the first hit), so a second breakpoint here would shadow, or be shadowed by, ours.
		local okbp, bps = pcall(function() return dev.debug:bplist() end)
		if okbp and bps then
			for _, loc in ipairs(locs) do
				for idx, bp in pairs(bps) do
					if bp.address == loc.address and bp.enabled then
						return fail("address_in_use", string.format("breakpoint %d already exists at %s on %s (location %s); MAME runs only one breakpoint per address, so a capture there could hide it or be hidden; nothing was installed",
							idx, hex(loc.address), args.cpu, loc.id))
					end
				end
			end
		end
		return {
			locs = locs, multi = multi, max_events = max_events, max_wall = max_wall, regs = regs, mems = mems,
			label = type(args.label) == "string" and args.label:sub(1, 80) or nil,
		}
	end

	local function release_temps(exp, cleanup)
		local info = { counter = exp.temps.counter and ("temp" .. exp.temps.counter) or nil, registers = {}, errors = {} }
		cleanup.debug_state = info
		local ok, vals, verr = pcall(read_temps)
		if not ok or not vals then
			info.errors[#info.errors + 1] = "cannot read temporaries: " .. tostring(not ok and vals or verr)
			info.verified = false
			return
		end
		local cval = vals[exp.temps.counter + 1]
		info.counter_value_at_release = cval
		info.hits_seen_by_bridge = exp.last_seq
		-- trailing hits that were never recoverable from the console log
		if cval > exp.last_seq and cval <= LIMITS.max_events + 1 then
			exp.dropped = exp.dropped + (cval - exp.last_seq)
			exp.errors[#exp.errors + 1] = string.format("events %d..%d not recoverable from console log", exp.last_seq + 1, cval)
			exp.last_seq = cval
		end
		info.undisturbed = (cval == exp.last_seq)
		if not info.undisturbed then
			info.errors[#info.errors + 1] = string.format("hit counter %s is 0x%X but the bridge saw %d hits: another user of the debugger changed it; borrowed temporaries left untouched", info.counter, cval, exp.last_seq)
			exp.errors[#exp.errors + 1] = "shared debugger state disturbed (see cleanup.debug_state)"
			info.reset_to_zero = false
			info.verified = false
		else
			local m = machine().debugger
			local toreset = { exp.temps.counter }
			for _, t in pairs(exp.temps.regs) do toreset[#toreset + 1] = t end
			for _, t in ipairs(toreset) do pcall(function() m:command(string.format("temp%d=0", t)) end) end
			local vals2 = read_temps()
			local allzero = vals2 ~= nil
			for _, t in ipairs(toreset) do if not vals2 or vals2[t + 1] ~= 0 then allzero = false end end
			info.reset_to_zero = true
			info.verified = allzero
			if not allzero then info.errors[#info.errors + 1] = "temporaries not confirmed zero after reset" end
		end
		for name, t in pairs(exp.temps.regs) do info.registers[name] = "temp" .. t end
		for _, t in ipairs({ exp.temps.counter }) do claimed_temps[t] = nil end
		for _, t in pairs(exp.temps.regs) do claimed_temps[t] = nil end
	end

	-- Unresolved instrumentation: breakpoints this bridge installed and could not remove (or verify removed). While any
	-- exist, new captures are refused; resolve_orphans() re-checks (and retries the clear) on every capture request.
	local orphans = {}  -- list of { exp_id, cpu, source, locs = {{id,address,bp_index,ord}}, temps = {n,...} }

	local function safe_bpclear(dev, idx, ord)
		if cfg.test_fail_clear and ord == cfg.test_fail_clear then error("injected test failure: bpclear suppressed for location " .. ord) end
		dev.debug:bpclear(idx)
	end

	orphan_rows = function()
		local out = {}
		for _, g in ipairs(orphans) do
			if #g.locs == 0 then
				out[#out + 1] = { experiment_id = g.exp_id, cpu = g.cpu, source = g.source, debugger_temporaries = g.temps,
					note = "breakpoints are gone but the retained temporaries could not yet be confirmed zero" }
			end
			for _, l in ipairs(g.locs) do
				out[#out + 1] = { experiment_id = g.exp_id, cpu = g.cpu, location = l.id, address = hex(l.address),
					breakpoint_index = l.bp_index, source = g.source, debugger_temporaries = g.temps }
			end
		end
		return out
	end

	local RECOVERY = "Remove the listed breakpoints (MAME debugger: 'bpclear <index>' on that CPU) or restart MAME, then retry; "
		.. "the next capture request re-checks the breakpoint list, retries the clear itself and zeroes the retained temporaries once nothing is left."

	local function resolve_orphans()
		local recovered = {}
		for gi = #orphans, 1, -1 do
			local g = orphans[gi]
			local dev = machine().devices[g.cpu]
			for li = #g.locs, 1, -1 do
				local l = g.locs[li]
				local function present()
					local ok, v = pcall(function() local b = dev.debug:bplist()[l.bp_index]; return b ~= nil and b.address == l.address end)
					return (not ok) or v  -- cannot verify = treat as present
				end
				if dev and present() then
					pcall(safe_bpclear, dev, l.bp_index, l.ord)
				end
				if not dev or not present() then
					recovered[#recovered + 1] = { experiment_id = g.exp_id, location = l.id, address = hex(l.address), breakpoint_index = l.bp_index }
					table.remove(g.locs, li)
				end
			end
			if #g.locs == 0 then
				local ok = pcall(function()
					for _, t in ipairs(g.temps) do machine().debugger:command(string.format("temp%d=0", t)) end
				end)
				local vals = ok and read_temps() or nil
				local zero = vals ~= nil
				for _, t in ipairs(g.temps) do if not vals or vals[t + 1] ~= 0 then zero = false end end
				if zero then
					for _, t in ipairs(g.temps) do claimed_temps[t] = nil end
					table.remove(orphans, gi)
				end
			end
		end
		return orphan_rows(), recovered
	end

	local function finalize(exp, reason, err_text)
		if exp.state ~= "running" then return end
		exp.state = "finalizing"
		local cleanup = { breakpoint_cleared = false, verified = false, errors = {} }
		local m = machine()
		local dev = m.devices[exp.cpu]
		if err_text then exp.errors[#exp.errors + 1] = err_text end
		-- Remove OUR breakpoints (by index, nothing else) FIRST so no further hits happen, then take the last harvest.
		cleanup.locations = {}
		local all_cleared, verified_all = true, true
		for ord, loc in ipairs(exp.locs) do
			loc.ord = ord
			local row = { id = loc.id, address = hex(loc.address), breakpoint_index = loc.bp_index }
			cleanup.locations[#cleanup.locations + 1] = row
			if dev and loc.bp_index then
				local ok, e = pcall(function() safe_bpclear(dev, loc.bp_index, ord) end)
				if not ok then cleanup.errors[#cleanup.errors + 1] = loc.id .. ": bpclear failed: " .. tostring(e) end
				local ok2, still = pcall(function() return dev.debug:bplist()[loc.bp_index] ~= nil end)
				if ok2 then
					row.cleared = not still
					if still then cleanup.errors[#cleanup.errors + 1] = loc.id .. ": breakpoint still present after bpclear" end
				else
					row.cleared = false
					verified_all = false
					cleanup.errors[#cleanup.errors + 1] = loc.id .. ": bplist verification failed: " .. tostring(still)
				end
				if not row.cleared then all_cleared = false end
			else
				row.cleared = (loc.bp_index == nil)  -- never installed: nothing to clear
				if loc.bp_index ~= nil then all_cleared = false end
			end
		end
		cleanup.verified = verified_all
		cleanup.breakpoint_cleared = all_cleared
		pcall(exp.harvest, exp)
		local okr, rerr
		if all_cleared then
			okr, rerr = pcall(release_temps, exp, cleanup)
		else
			-- Instrumentation is still installed: do NOT reset or release its temporaries (its condition/action still use them),
			-- do NOT report a clean state, and register it so no further capture is accepted over it.
			local left, tl = {}, { exp.temps.counter }
			for _, t in pairs(exp.temps.regs) do tl[#tl + 1] = t end
			for _, loc in ipairs(exp.locs) do
				if loc.bp_index and not cleanup.locations[loc.ord].cleared then left[#left + 1] = { id = loc.id, address = loc.address, bp_index = loc.bp_index, ord = loc.ord } end
			end
			orphans[#orphans + 1] = { exp_id = exp.id, cpu = exp.cpu, source = "finalize", locs = left, temps = tl }
			cleanup.remaining_instrumentation = {}
			for _, l in ipairs(left) do cleanup.remaining_instrumentation[#cleanup.remaining_instrumentation + 1] = { location = l.id, address = hex(l.address), breakpoint_index = l.bp_index } end
			cleanup.recovery = RECOVERY
			cleanup.debug_state = { retained = true, verified = false, registers = {}, errors = { "temporaries retained and NOT reset: instrumentation using them is still installed (see cleanup.remaining_instrumentation)" } }
			okr = true
		end
		if not okr then cleanup.errors[#cleanup.errors + 1] = "temporary release failed: " .. tostring(rerr) end
		if cleanup.debug_state and #cleanup.debug_state.errors > 0 then
			for _, e in ipairs(cleanup.debug_state.errors) do cleanup.errors[#cleanup.errors + 1] = e end
		end
		if exp.events_file then
			exp.events_file:flush()
			exp.events_file:close()
			exp.events_file = nil
		end
		exp.completion_reason = reason
		exp.cleanup = cleanup
		exp.ended_host_ms = host_ms()
		-- Execution state is only OBSERVED here. Finalising never resumes, pauses or stops emulation.
		local es = exec_state()
		exp.final_execution = es
		exp.state = (#cleanup.errors > 0 or err_text) and "failed" or "completed"
		if reason == "cancelled" then exp.state = (#cleanup.errors > 0) and "failed" or "cancelled" end
		local mf = exp.manifest
		mf.status = exp.state
		mf.ended_utc = iso_utc()
		mf.end_clock = emu_clock()
		mf.end_execution = es
		mf.execution_transitions = exp.transitions
		mf.completion_reason = reason
		mf.final_state = exp.state
		mf.cleanup = cleanup
		mf.counts = { events_captured = exp.captured, hits_observed = exp.last_seq, events_dropped = exp.dropped }
		mf.per_location = {}
		for _, loc in ipairs(exp.locs) do
			mf.per_location[#mf.per_location + 1] = { id = loc.id, address = hex(loc.address), armed = loc.bp_index ~= nil,
				events_captured = loc.captured, no_hits_observed = (loc.captured == 0) }
		end
		mf.errors = exp.errors
		mf.evidence_errors = exp.evidence_errors
		mf.truncated = exp.dropped > 0
		mf.host_wall_ms = exp.ended_host_ms - exp.started_host_ms
		write_manifest(exp)
		if active == exp then active = nil end
	end

	local function make_harvest()
		return function(exp)
			local dbg = machine().debugger
			local cl = dbg and dbg.consolelog
			if not cl then return end
			local n = #cl
			local last_line = n > 0 and cl[n] or nil
			if n == exp.cl_n and last_line == exp.cl_last then return end
			exp.cl_n, exp.cl_last = n, last_line
			-- scan backwards until we meet an already seen sequence number
			local found = {}
			local scanned = 0
			local i = n
			while i >= 1 and scanned < LIMITS.console_scan_cap do
				local line = cl[i]
				local tag, seq = line:match("^MCP|(%w+)|(%d+)|")
				if tag == exp.tag then
					seq = tonumber(seq)
					if seq <= exp.last_seq then break end
					found[#found + 1] = { seq = seq, line = line }
				end
				i = i - 1
				scanned = scanned + 1
			end
			if #found == 0 then return end
			table.sort(found, function(a, b) return a.seq < b.seq end)
			local clock = emu_clock()
			local expected = exp.last_seq + 1
			for _, ev in ipairs(found) do
				if ev.seq > expected then
					exp.dropped = exp.dropped + (ev.seq - expected)
					exp.errors[#exp.errors + 1] = string.format("events %d..%d not recoverable from console log", expected, ev.seq - 1)
				end
				expected = ev.seq + 1
				exp.last_seq = ev.seq
				local fields = {}
				for f in (ev.line:match("^MCP|%w+|%d+|(.*)$") or ""):gmatch("[^|]+") do fields[#fields + 1] = f end
				-- pc: the breakpoint address by construction (a hit means the CPU is about to execute it)
					local lid = fields[1]
				local loc = exp.loc_by_id[lid]
				if not loc then
					exp.errors[#exp.errors + 1] = "event " .. ev.seq .. " names unknown location '" .. tostring(lid) .. "'"
				end
				-- pc: for multi-location experiments the value CAPTURED at the hit (curpc); for a single legacy
				-- address it is the breakpoint address by construction (a hit means the CPU is about to execute it).
				local rec = { seq = ev.seq, location = lid, address = loc and hex(loc.address) or nil, pc = loc and hex(loc.address) or nil, regs = {}, mem = {} }
				local k = 2
				for _, r in ipairs(exp.regs) do rec.regs[r.name] = "0x" .. (fields[k] or "?"); k = k + 1 end
				for _, mf in ipairs(exp.mems) do
					rec.mem[#rec.mem + 1] = { space = mf.space, address = hex(mf.address), size = mf.size, value = "0x" .. (fields[k] or "?") }
					k = k + 1
				end
				-- HARVEST-time observations, not hit-time (see manifest.time_semantics)
				rec.harvest_emu_time_s = clock.emu_time_s
				rec.harvest_frame = clock.frame
				rec.harvest_host_ms = host_ms() - exp.started_host_ms
					if exp.temps.pc then
					rec.pc = "0x" .. (fields[k] or "?")
					rec.pc_source = "captured at the hit (debugger symbol curpc)"
				end
				exp.captured = exp.captured + 1
				if loc then loc.captured = loc.captured + 1 end
				if exp.events_file then
					exp.events_file:write(json.encode(rec), "\n")
				end
				if #exp.recent >= LIMITS.max_status_events then table.remove(exp.recent, 1) end
				exp.recent[#exp.recent + 1] = rec
			end
			if exp.events_file then exp.events_file:flush() end
		end
	end
	local harvest = make_harvest()

	-- partial-installation cleanup: remove every breakpoint THIS attempt installed (by index) and verify
	local function remove_installed(dev, installed)
		local rows = {}
		for _, loc in ipairs(installed) do
			local row = { id = loc.id, address = hex(loc.address), breakpoint_index = loc.bp_index }
			local ok, e = pcall(function() safe_bpclear(dev, loc.bp_index, loc.ord) end)
			if not ok then row.error = "bpclear failed: " .. tostring(e) end
			local ok2, still = pcall(function() return dev.debug:bplist()[loc.bp_index] ~= nil end)
			row.cleared = ok2 and not still
			if not ok2 then row.error = (row.error and (row.error .. "; ") or "") .. "bplist verification failed: " .. tostring(still) end
			if ok2 and still then row.error = (row.error and (row.error .. "; ") or "") .. "breakpoint still present after bpclear" end
			rows[#rows + 1] = row
		end
		return rows
	end

	function ops.capture_breakpoint(args)
		local dev, err = find_cpu(args.cpu)
		if not dev then return nil, err end
		local left, recovered = resolve_orphans()
		if #left > 0 then
			return fail("unresolved_instrumentation", string.format("%d breakpoint(s)/temporary group(s) from an earlier experiment are unresolved; no new capture is accepted until they are gone. %s", #left, RECOVERY),
				{ remaining_instrumentation = left, recovery = RECOVERY })
		end
		local p, perr = build_capture(dev, args)
		if not p then return nil, perr end
		for i, l in ipairs(p.locs) do l.ord = i end

		-- borrow debugger temporaries: 1 hit counter + 1 per captured register
		-- (+1 for the hit-time PC in multi-location experiments; the single-address form is unchanged)
		local picked, aerr, areport = allocate_temps(1 + #p.regs + (p.multi and 1 or 0))
		if not picked then return nil, aerr end
		local temps = { counter = picked[1], regs = {} }
		for i, r in ipairs(p.regs) do temps.regs[r.name] = picked[i + 1] end
		if p.multi then temps.pc = picked[#p.regs + 2]; temps.regs["(hit-time pc)"] = temps.pc end  -- tracked with the register temps so it is claimed, verified and released with them

		exp_counter = exp_counter + 1
		local id = string.format("exp-%s-%03d", session_id, exp_counter)
		local tag = string.format("e%d", exp_counter)
		local dir = cfg.evidence_dir .. "/" .. id
		if not mkdir_p(dir) then
			return fail("evidence_unavailable", "cannot create evidence directory " .. dir)
		end
		local events_path = dir .. "/events.jsonl"
		local ef, ferr = io.open(events_path, "w")
		if not ef then return fail("evidence_unavailable", "cannot open " .. events_path .. ": " .. tostring(ferr)) end

		-- CONDITION (compiled by MAME in this CPU's own symbol table, so register names always mean THIS
		-- cpu): store the selected registers into the borrowed temps, then test the hit limit.
		-- '&&' evaluates both sides, '|1' keeps each assignment truthy. MAME expressions default to HEX:
		-- '#' forces decimal (a bare 10000 would mean 0x10000).
		local conds = {}
		for _, r in ipairs(p.regs) do
			conds[#conds + 1] = string.format("((temp%d=%s)|1)", temps.regs[r.name], r.symbol)
		end
		if temps.pc then conds[#conds + 1] = string.format("((temp%d=curpc)|1)", temps.pc) end
		-- ONE counter temp is shared by every location: that is the global event limit and the global sequence.
		conds[#conds + 1] = string.format("(temp%d<#%d)", temps.counter, p.max_events)
		local cond = table.concat(conds, "&&")
		-- ACTION (runs in the console against the *visible* CPU, so it may only use global temps and
		-- explicitly device-qualified memory operators): 'g' first so a failing printf cannot leave MAME stopped.
		-- The action differs per location only by its literal id in the format string.
		local function action_for(loc)
			local fmt, argv = { "MCP|" .. tag .. "|%d|" .. loc.id }, { string.format("(temp%d=temp%d+1)", temps.counter, temps.counter) }
			for _, r in ipairs(p.regs) do fmt[#fmt + 1] = "%X"; argv[#argv + 1] = "temp" .. temps.regs[r.name] end
			for _, mf in ipairs(p.mems) do
				fmt[#fmt + 1] = "%X"
				-- '@' = side effects disabled, 'tag.' = explicit device, 'p/d/i' = space, b/w/d = size
				argv[#argv + 1] = string.format("%s.%s%s@0x%X", args.cpu, mf.letter, MEMOP[mf.size], mf.address)
			end
			if temps.pc then fmt[#fmt + 1] = "%X"; argv[#argv + 1] = "temp" .. temps.pc end
			return string.format('g;printf "%s",%s', table.concat(fmt, "|"), table.concat(argv, ","))
		end
		local loc_by_id = {}
		for _, loc in ipairs(p.locs) do
			loc.captured, loc.action = 0, action_for(loc)
			loc_by_id[loc.id] = loc
		end

		local exp = {
			id = id, tag = tag, cpu = args.cpu, locs = p.locs, loc_by_id = loc_by_id, multi = p.multi, max_events = p.max_events, max_wall = p.max_wall,
			regs = p.regs, mems = p.mems, label = p.label, temps = temps,
			state = "running", started_host_ms = host_ms(), last_seq = 0, captured = 0, dropped = 0,
			cl_n = -1, cl_last = nil, errors = {}, evidence_errors = {}, recent = {}, transitions = {},
			events_file = ef, harvest = harvest, dir = dir, manifest_path = dir .. "/manifest.json",
			events_path = events_path, cond = cond,
		}
		local treg = {}
		for name, t in pairs(temps.regs) do treg[name] = "temp" .. t end
		local locparams = {}
		for _, loc in ipairs(p.locs) do locparams[#locparams + 1] = { id = loc.id, address = hex(loc.address) } end
		local mparams = {}
		for _, mf in ipairs(p.mems) do mparams[#mparams + 1] = { space = mf.space, address = hex(mf.address), size = mf.size } end
		local rnames = {}
		for _, r in ipairs(p.regs) do rnames[#rnames + 1] = r.name end
		exp.manifest = {
			manifest_version = 2, status = "running",
			experiment_id = id, label = p.label, session_id = session_id,
			parameters = { cpu = args.cpu, locations = locparams, location_form = p.multi and "locations" or "address (single, legacy)",
				address = (not p.multi) and hex(p.locs[1].address) or nil, registers = rnames, memory = mparams,
				max_events = p.max_events, max_wall_seconds = p.max_wall },
			mame_version = emu.app_version(), bridge_version = BRIDGE_VERSION, protocol_version = PROTOCOL_VERSION,
			machine = { name = machine().system.name, description = machine().system.description },
			cpu_context = { tag = args.cpu, name = try(function() return dev.name end), address_spaces = list_spaces(dev) },
			bank_context = { per_event = "unavailable",
				reason = "memory fields are read at the hit by the breakpoint action (hit-time, in the selected bank), but the debugger expression language has no bank-entry operator and Lua only runs at harvest time; a bank id sampled then would not describe the hit",
				memory_fields_semantics = "logical CPU-visible address; value is whatever the currently selected bank mapped there at the hit" },
			starting_state = { execution = exec_state(), clock = emu_clock(),
				description = "state at the moment the experiment was installed; no savestate was loaded" },
			interventions = { inputs = {}, patches = {}, savestate_loads = {},
				notes = "none by this bridge. The breakpoint action issues 'g' after each recorded hit; installing, cancelling or finishing a capture never starts, pauses or stops emulation by itself." },
			instrumentation = { { kind = "breakpoints", cpu = args.cpu, condition = cond, locations = {},
				debugger_temporaries = { counter = "temp" .. temps.counter, registers = treg,
					ownership = "borrowed from the shared global temp0..temp9; chosen because value was 0 and unreferenced by any breakpoint; ONE counter and one register set are shared by all locations (no per-location temps); reset to 0 at release if the counter shows no interference",
					selection_report = areport },
				global_limit = "one hit counter shared by every location's condition enforces max_events across the experiment; its value is the global event sequence" } },
			event_semantics = { location = "id of the breakpoint that fired", address = "logical breakpoint address of that location (not a ROM offset)",
				pc = p.multi and "hit-time PC captured at the hit (curpc)" or "breakpoint address by construction (single legacy form)" },
			resumes_automatically = "after each recorded hit only (action 'g'); not at install, cancel or completion",
			limits = { max_events = p.max_events, max_wall_seconds = p.max_wall },
			started_utc = iso_utc(),
			time_semantics = "emu_time_s/frame/host_ms in events are HARVEST-time observations (taken when the bridge read the event from the debugger console), not hit-time; hits are ordered by exact sequence numbers produced inside MAME. Not instruction-accurate timing. Emulated time and host wall-clock are separate fields.",
			events_file = events_path,
		}
		write_manifest(exp)

		-- Install ALL locations in this one plugin callback: emulation cannot run between the bpset calls, so no hit can
		-- be seen at location 1 before location 2 exists, and no temporary stop is needed even when MAME is running.
		-- Optional bpset args MUST be passed: bpset(addr) alone crashes MAME 0.289.
		local installed, outcomes, failure = {}, {}, nil
		for i, loc in ipairs(p.locs) do
			local ok, res = pcall(function()
				if cfg.test_fail_install_at == i then error("injected test failure at location " .. i) end
				return dev.debug:bpset(loc.address, cond, loc.action)
			end)
			local row = { id = loc.id, address = hex(loc.address), armed = false }
			outcomes[#outcomes + 1] = row
			if ok then
				-- verify it really is in the CPU's breakpoint list at that address
				local okv, present = pcall(function() local b = dev.debug:bplist()[res]; return b ~= nil and b.address == loc.address end)
				if okv and present then
					loc.bp_index = res
					row.armed, row.breakpoint_index = true, res
					installed[#installed + 1] = loc
				else
					loc.bp_index = res
					installed[#installed + 1] = loc  -- exists somewhere: remove it with the rest
					row.error = "breakpoint was created but is not in the breakpoint list at the requested address"
					failure = row.error
					break
				end
			else
				row.error = tostring(res)
				failure = "bpset failed for location " .. loc.id .. ": " .. tostring(res)
				break
			end
		end
		for i = #outcomes + 1, #p.locs do
			outcomes[i] = { id = p.locs[i].id, address = hex(p.locs[i].address), armed = false, not_attempted = true }
		end
		if failure then
			local cleanup_rows = remove_installed(dev, installed)
			local cleanup_ok = true
			for _, r in ipairs(cleanup_rows) do if not r.cleared then cleanup_ok = false end end
			ef:close()
			if not cleanup_ok then
				local left, tl = {}, { temps.counter }
				for _, t in pairs(temps.regs) do tl[#tl + 1] = t end
				for i, r in ipairs(cleanup_rows) do
					if not r.cleared then left[#left + 1] = { id = r.id, address = installed[i].address, bp_index = installed[i].bp_index, ord = installed[i].ord } end
				end
				orphans[#orphans + 1] = { exp_id = id, cpu = args.cpu, source = "install_rollback", locs = left, temps = tl }
				for _, t in ipairs(tl) do claimed_temps[t] = id end
			end
			exp.manifest.status = "failed"
			exp.manifest.final_state = "failed"
			exp.manifest.errors = { failure }
			exp.manifest.installation = { all_armed = false, locations = outcomes, rollback = { all_removed = cleanup_ok, removed = cleanup_rows } }
			exp.manifest.counts = { events_captured = 0, hits_observed = 0, events_dropped = 0 }
			write_manifest(exp)
			return fail("install_failed", failure .. (cleanup_ok and "; every breakpoint installed by this attempt was removed and verified"
				or "; CLEANUP FAILED: some breakpoints installed by this attempt could not be removed (see detail.rollback)"),
				{ evidence = dir, installation = outcomes, rollback = { all_removed = cleanup_ok, removed = cleanup_rows, recovery = (not cleanup_ok) and RECOVERY or nil } })
		end
		for _, loc in ipairs(p.locs) do
			exp.manifest.instrumentation[1].locations[#exp.manifest.instrumentation[1].locations + 1] = {
				id = loc.id, address = hex(loc.address), breakpoint_index = loc.bp_index, action = loc.action }
		end
		exp.manifest.installation = { all_armed = true, locations = outcomes,
			note = "all locations installed in one plugin callback before emulation could run again" }
		write_manifest(exp)

		claimed_temps[temps.counter] = id
		for _, t in pairs(temps.regs) do claimed_temps[t] = id end
		experiments[id] = exp
		order[#order + 1] = id
		while #order > LIMITS.max_history do experiments[table.remove(order, 1)] = nil end
		active = exp
		exp.last_exec_state = exec_state().state
		return {
			experiment_id = id, state = "running", asynchronous = true,
			breakpoint_index = (not p.multi) and p.locs[1].bp_index or nil, evidence_dir = dir,
			locations = outcomes, all_armed = true, location_limit = LIMITS.max_locations,
			recovered_instrumentation = (#recovered > 0) and recovered or nil,
			debugger_temporaries = { counter = "temp" .. temps.counter, registers = treg },
			execution_policy = "capture_breakpoint does not change execution state. If MAME is paused or debugger-stopped now, the capture is armed but records nothing until emulation is resumed (execution_control resume).",
			note = "poll experiment_status; the breakpoint action resumes execution after each recorded hit",
			execution = exec_state(),
		}
	end

	-- Per-location view. "armed" = the breakpoint was installed and verified; hits = events captured at it. armed with
	-- hits = 0 means "no hits observed"; armed = false would mean it was never successfully armed. Events dropped from the
	-- console log lose their location, so per-location counts can sum to less than counts.events_captured + dropped.
	local function locs_status(exp)
		local out = {}
		for _, loc in ipairs(exp.locs) do
			out[#out + 1] = { id = loc.id, address = hex(loc.address), armed = loc.bp_index ~= nil, breakpoint_index = loc.bp_index,
				events_captured = loc.captured, no_hits_observed = (loc.captured == 0) }
		end
		return out
	end

	local function exp_status(exp)
		local es = exec_state()
		local now = host_ms()
		local s = {
			experiment_id = exp.id, state = exp.state, label = exp.label,
			completion_reason = exp.completion_reason,
			counts = { events_captured = exp.captured, hits_observed = exp.last_seq,
				events_dropped = exp.dropped, max_events = exp.max_events },
			locations = locs_status(exp),
			elapsed_wall_ms = (exp.ended_host_ms or now) - exp.started_host_ms,
			max_wall_ms = math.floor(exp.max_wall * 1000),
			errors = exp.errors, evidence_errors = exp.evidence_errors,
			cleanup = exp.cleanup,
			execution = es, final_execution = exp.final_execution,
			execution_transitions = exp.transitions,
			recent_events = exp.recent,
			summary = string.format("%s: %d/%d events captured, %d dropped%s",
				exp.state, exp.captured, exp.max_events, exp.dropped,
				exp.completion_reason and (", reason=" .. exp.completion_reason) or ""),
			evidence = { events_jsonl = exp.events_path, manifest_json = exp.manifest_path },
			clock = emu_clock(),
		}
		return s
	end

	-- IDs embed the session id, so an id from a previous MAME run is recognised and refused as such
	local function fail_unknown_experiment(id)
		if type(id) == "string" and id:match("^exp%-s[%w%-]+%-%d+$") and id:sub(1, #session_id + 5) ~= "exp-" .. session_id .. "-" then
			return fail("experiment_from_other_session", "experiment " .. id .. " belongs to a different MAME session than the current " .. session_id .. "; its state and evidence are not available here")
		end
		return fail("unknown_experiment", "no experiment " .. tostring(id))
	end

	function ops.experiment_status(args)
		local id = args.experiment_id
		if id == nil and active then id = active.id end
		if id == nil and #order > 0 then id = order[#order] end
		if id == nil then return fail("unknown_experiment", "no experiments in this session") end
		local exp = experiments[id]
		if not exp then return fail_unknown_experiment(id) end
		if exp.state == "running" then pcall(exp.harvest, exp) end
		return exp_status(exp)
	end

	function ops.cancel_experiment(args)
		local exp = experiments[args.experiment_id]
		if not exp then return fail_unknown_experiment(args.experiment_id) end
		if exp.state ~= "running" then
			local s = exp_status(exp)
			s.already_finished = true
			return s
		end
		finalize(exp, "cancelled")
		return exp_status(exp)
	end

	---------------------------------------------------------------------------
	-- request plumbing
	---------------------------------------------------------------------------
	local function respond(req_id, ok, payload)
		local env_ = { v = PROTOCOL_VERSION, id = req_id, session_id = session_id, ok = ok }
		if ok then env_.result = payload else env_.error = payload; counters.errors = counters.errors + 1 end
		local s = json.encode(env_)
		outbuf = outbuf .. s .. "\n"
	end

	local function handle_line(line)
		local ok, req = pcall(json.decode, line)
		if not ok or type(req) ~= "table" then
			counters.bad_lines = counters.bad_lines + 1
			respond(nil, false, { code = "bad_request", message = "line is not a JSON object" })
			return
		end
		local id = req.id
		if type(id) ~= "string" then
			respond(nil, false, { code = "bad_request", message = "request needs a string id" })
			return
		end
		if req.session_id ~= nil and req.session_id ~= session_id then
			respond(id, false, { code = "session_mismatch",
				message = "request is for session " .. tostring(req.session_id) .. " but this MAME is " .. session_id })
			return
		end
		local fn = ops[req.op]
		if type(req.op) ~= "string" or not fn then
			respond(id, false, { code = "unknown_op", message = "unknown operation " .. tostring(req.op) })
			return
		end
		counters.requests = counters.requests + 1
		local args = type(req.args) == "table" and req.args or {}
		local ok2, result, err = pcall(fn, args)
		if not ok2 then
			respond(id, false, { code = "internal_error", message = tostring(result) })
		elseif result == nil then
			respond(id, false, err or { code = "internal_error", message = "no result" })
		else
			respond(id, true, result)
		end
	end

	local function service_socket()
		-- read what is available (bounded per tick)
		for _ = 1, 16 do
			local data = sock:read(8192)
			if not data or #data == 0 then break end
			client_seen = true
			inbuf = inbuf .. data
		end
		while true do
			local nl = inbuf:find("\n", 1, true)
			if not nl then
				if #inbuf > LIMITS.max_line_bytes then
					inbuf = ""
					respond(nil, false, { code = "bad_request", message = "request line too long" })
				end
				break
			end
			local line = inbuf:sub(1, nl - 1)
			inbuf = inbuf:sub(nl + 1)
			if #line > 0 then handle_line(line) end
		end
		if #outbuf > 0 then
			local n = sock:write(outbuf)
			if type(n) ~= "number" then n = #outbuf end
			if n == 0 and client_seen and not reopen_after_flush then
				write_failures = write_failures + 1
				if write_failures >= 3 then reopen_socket("client gone: writes not accepted") return end
			else
				write_failures = 0
			end
			outbuf = outbuf:sub(n + 1)
		end
		if reopen_after_flush and #outbuf == 0 then
			reopen_socket("client said goodbye")
			return
		end
		-- heartbeat: an empty line (ignored by the client) reveals a vanished client, because
		-- writes to a closed connection return 0 bytes.
		if client_seen and #outbuf == 0 then
			local now = host_ms()
			if now - last_heartbeat_ms >= 1000 then
				last_heartbeat_ms = now
				local n = sock:write("\n")
				if n == 0 then
					write_failures = write_failures + 1
					if write_failures >= 2 then reopen_socket("client gone: heartbeat not accepted") end
				else
					write_failures = 0
				end
			end
		end
	end

	local function housekeeping()
		local exp = active
		if not exp or exp.state ~= "running" then return end
		harvest(exp)
		local cur = exec_state().state
		if cur ~= exp.last_exec_state then
			if #exp.transitions < 40 then
				local c = emu_clock()
				exp.transitions[#exp.transitions + 1] = { from = exp.last_exec_state, to = cur,
					host_ms = host_ms() - exp.started_host_ms, emu_time_s = c.emu_time_s, frame = c.frame }
			end
			exp.last_exec_state = cur
		end
		if exp.last_seq >= exp.max_events then
			finalize(exp, "event_limit")
		elseif host_ms() - exp.started_host_ms >= exp.max_wall * 1000 then
			finalize(exp, "wall_clock_limit")
		end
	end

	emu.register_periodic(function()
		counters.periodic = counters.periodic + 1
		local ok, e = pcall(service_socket)
		if not ok then print("mamemcp: socket service error: " .. tostring(e)) end
		local ok2, e2 = pcall(housekeeping)
		if not ok2 then
			print("mamemcp: housekeeping error: " .. tostring(e2))
			if active then pcall(finalize, active, "error", tostring(e2)) end
		end
	end)

	stop_sub = emu.add_machine_stop_notifier(function()
		stopping = true
		if active then
			-- MAME is exiting; breakpoints die with it. Record what we know.
			pcall(finalize, active, "mame_exit")
		end
	end)
end

return exports
