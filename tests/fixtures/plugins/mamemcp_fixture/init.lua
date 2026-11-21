-- license:BSD-3-Clause
-- TEST-ONLY fixture plugin for mame-mcp validation. It is NOT part of the server and is only loaded by the
-- validation scripts (-plugin mamemcp,mamemcp_fixture). It does two things the bridge deliberately cannot:
--   1. emulate things a *user* does in MAME (stop in the debugger, pause, add their own breakpoint,
--      touch a debugger temp variable), and
--   2. observe MAME independently of the bridge (MAME's own debugger printf, read-tap side-effect flags,
--      the debugger's visible CPU).
-- Protocol: a file mailbox. The test writes <dir>/cmd.json; this plugin answers in <dir>/reply.json.

local json = require("json")

local exports = {
	name = "mamemcp_fixture", version = "0.1.0", description = "mame-mcp test fixture",
	license = "BSD-3-Clause", author = { name = "mame-mcp POC" },
}

function exports.startplugin()
	local dir = os.getenv("MAMEMCP_FIXTURE_DIR")
	if not dir or dir == "" then
		print("mamemcp_fixture: MAMEMCP_FIXTURE_DIR not set; fixture inactive")
		return
	end
	local taps, tapcalls = {}, { disabled = 0, enabled = 0 }
	local function dev(tag) return assert(manager.machine.devices[tag], "no device " .. tostring(tag)) end
	local function lastline()
		local cl = manager.machine.debugger.consolelog
		return cl[#cl]
	end

	local ops = {}
	function ops.ping() return { pong = true } end

	function ops.exec_state()
		local m = manager.machine
		return { paused = m.paused, debugger_state = m.debugger and m.debugger.execution_state }
	end

	function ops.temps()
		local m = manager.machine
		m.debugger:command('printf "TV|%X|%X|%X|%X|%X|%X|%X|%X|%X|%X",temp0,temp1,temp2,temp3,temp4,temp5,temp6,temp7,temp8,temp9')
		local vals = {}
		for h in lastline():sub(4):gmatch("[^|]+") do vals[#vals + 1] = tonumber(h, 16) end
		return { temps = vals }
	end

	function ops.set_temp(a)
		manager.machine.debugger:command(string.format("temp%d=#%d", a.n, a.value))
		return { set = true }
	end

	function ops.user_stop() manager.machine.debugger.execution_state = "stop"; return { requested = "stop" } end
	function ops.user_pause() emu.pause(); return { requested = "pause" } end
	function ops.user_resume()
		manager.machine.debugger.execution_state = "run"
		emu.unpause()
		return { requested = "resume" }
	end

	function ops.user_bp(a)
		local idx = dev(a.cpu).debug:bpset(a.address, a.cond or "", a.action or "")
		return { index = idx }
	end
	function ops.user_bpclear(a) dev(a.cpu).debug:bpclear(a.index); return { cleared = a.index } end

	function ops.bplist()
		local out = {}
		for tag, d in pairs(manager.machine.devices) do
			local ok, list = pcall(function() return d.debug and d.debug:bplist() end)
			if ok and list then
				for idx, bp in pairs(list) do
					out[#out + 1] = { cpu = tag, index = idx, address = bp.address, condition = bp.condition, action = bp.action, enabled = bp.enabled }
				end
			end
		end
		table.sort(out, function(x, y) return x.cpu .. x.index < y.cpu .. y.index end)
		return { breakpoints = out }
	end

	-- MAME's own debugger as the independent observer: set the visible CPU, run printf, read the console.
	function ops.debugger_printf(a)
		local m = manager.machine
		local before = m.debugger.visible_cpu
		if a.cpu then m.debugger.visible_cpu = dev(a.cpu) end
		m.debugger:command("printf " .. a.text)
		local line = lastline()
		if a.cpu and before then m.debugger.visible_cpu = before end
		return { line = line }
	end

	-- last n debugger console lines (to see what a breakpoint ACTION printed)
	function ops.console_tail(a)
		local cl = manager.machine.debugger.consolelog
		local out, n = {}, tonumber(a.n) or 5
		for i = math.max(1, #cl - n + 1), #cl do out[#out + 1] = cl[i] end
		return { lines = out }
	end

	function ops.visible_cpu()
		local v = manager.machine.debugger.visible_cpu
		return { tag = v and v.tag or nil }
	end

	function ops.set_visible_cpu(a) manager.machine.debugger.visible_cpu = dev(a.cpu); return { set = a.cpu } end

	-- read taps: record machine:side_effects_disabled() for every access that reaches the tapped range
	function ops.tap_install(a)
		local sp = dev(a.cpu).spaces[a.space]
		taps[#taps + 1] = sp:install_read_tap(a.start, a["end"], "fixture_tap", function(off, data, mask)
			if manager.machine:side_effects_disabled() then tapcalls.disabled = tapcalls.disabled + 1
			else tapcalls.enabled = tapcalls.enabled + 1 end
			return nil
		end)
		return { installed = true }
	end
	function ops.tap_take()
		local r = { side_effects_disabled_calls = tapcalls.disabled, side_effects_enabled_calls = tapcalls.enabled }
		tapcalls.disabled, tapcalls.enabled = 0, 0
		return r
	end
	function ops.read_normal(a)
		local sp = dev(a.cpu).spaces[a.space or "program"]
		local path = a.path or "read_u8"
		return { value = sp[path](sp, a.address) }
	end

	-- Bank observation / intervention (TEST-ONLY; the server has no write tool). A "bank switch" intervention is a
	-- normal CPU-space write to the machine's own bank-select register, so the driver's handler runs.
	function ops.bank_list()
		local out = {}
		for tag, b in pairs(manager.machine.memory.banks) do out[#out + 1] = { tag = tag, entry = b.entry } end
		table.sort(out, function(x, y) return x.tag < y.tag end)
		return { banks = out }
	end
	function ops.space_write_u8(a)
		local sp = dev(a.cpu).spaces[a.space or "program"]
		sp:write_u8(a.address, a.value)
		return { wrote = a.value, address = a.address }
	end
	function ops.user_wp(a)
		local idx = dev(a.cpu).debug:wpset(dev(a.cpu).spaces[a.space or "program"], a.kind or "w", a.address, a.length or 1, a.cond or "", a.action or "")
		return { index = idx }
	end
	function ops.user_wpclear(a) dev(a.cpu).debug:wpclear(a.index); return { cleared = a.index } end
	-- Independent watchpoint list of one (cpu, space). ONLY call it for a space that has had a watchpoint set (MAME 0.289
	-- crashes on wplist() for a space that never had one).
	function ops.wplist(a)
		local d = dev(a.cpu)
		local out = {}
		for idx, wp in pairs(d.debug:wplist(d.spaces[a.space or "program"])) do
			out[#out + 1] = { index = idx, address = wp.address, length = wp.length, type = wp.type, condition = wp.condition, action = wp.action, enabled = wp.enabled }
		end
		table.sort(out, function(x, y) return x.index < y.index end)
		return { watchpoints = out }
	end

	-- Rotate the bank-select register on every periodic callback (the same thread that serves the bridge), so a
	-- bridge read can land between two different selections. TEST-ONLY intervention.
	local cycler = nil
	function ops.bank_cycle(a)
		if a.stop then
			local n = cycler and cycler.writes or 0
			cycler = nil
			return { stopped = true, writes = n }
		end
		cycler = { cpu = a.cpu, space = a.space or "program", address = a.address, values = a.values, i = 0, writes = 0 }
		return { started = true }
	end
	function ops.set_pc(a)
		dev(a.cpu).state["PC"].value = a.value
		return { pc = a.value }
	end
	function ops.soft_reset() manager.machine:soft_reset(); return { requested = "soft_reset" } end

	-- Independent input observer (TEST-ONLY): counts, per emulated frame, whether a port's masked bits differ from the idle
	-- value sampled when the watch started. It reads the port the machine itself reads, in its own frame notifier, so it does
	-- not share any code path with the bridge's input counting.
	local watch = nil
	function ops.watch_port(a)
		local port = manager.machine.ioport.ports[a.port]
		if a.stop then
			local w = watch
			watch = nil
			return { pressed_frames = w and w.pressed or 0, frames_seen = w and w.total or 0, pressed_frame_numbers = w and w.numbers or {},
				last_pressed_frame = w and w.last or nil, frames_after_last_press = w and w.after or 0 }
		end
		watch = { port = a.port, mask = a.mask, idle = port:read(), pressed = 0, total = 0, numbers = {}, after = 0 }
		return { watching = true, idle = watch.idle }
	end
	-- Independent MULTI-field input observer (TEST-ONLY): one entry per (name, port, mask); records the screen frame number of every
	-- frame on which that field's masked bits differ from the idle value sampled when the watch started. Used to check a
	-- press_sequence schedule field by field against what the machine itself reads, with no code shared with the bridge.
	local multi = nil
	function ops.watch_multi(a)
		if a.stop then
			local w = multi
			multi = nil
			local out = {}
			for _, e in ipairs(w and w.ws or {}) do out[#out + 1] = { name = e.name, frames = e.frames, truncated = e.truncated } end
			return { watchers = out, frames_seen = w and w.total or 0, first_frame_seen = w and w.first or nil, last_frame_seen = w and w.last or nil }
		end
		local ports = manager.machine.ioport.ports
		multi = { total = 0, ws = {} }
		for i, e in ipairs(a.watchers) do
			multi.ws[i] = { name = e.name, port = e.port, mask = e.mask, idle = ports[e.port]:read(), frames = {} }
		end
		return { watching = #multi.ws }
	end
	local screen
	emu.register_frame_done(function()
		if not screen then
			for _, s in pairs(manager.machine.screens) do screen = s break end
		end
		if multi then
			local f = screen:frame_number()
			-- while paused MAME still repaints and calls this with an unchanged frame number: that is not an emulated frame
			if f ~= multi.last then
				multi.total = multi.total + 1
				multi.first = multi.first or f
				multi.last = f
				for _, e in ipairs(multi.ws) do
					local v = manager.machine.ioport.ports[e.port]:read()
					if ((v ~ e.idle) & e.mask) ~= 0 then
						if #e.frames < 4000 then e.frames[#e.frames + 1] = f else e.truncated = true end
					end
				end
			end
		end
		if not watch then return end
		local v = manager.machine.ioport.ports[watch.port]:read()
		watch.total = watch.total + 1
		if ((v ~ watch.idle) & watch.mask) ~= 0 then
			watch.pressed = watch.pressed + 1
			watch.last = screen:frame_number()
			watch.after = 0
			if #watch.numbers < 200 then watch.numbers[#watch.numbers + 1] = watch.last end
		elseif watch.last then
			watch.after = watch.after + 1
		end
	end)

	-- Independent memory-access observer (TEST-ONLY): a Lua memory tap on the same range, logging the bus-level offset, data,
	-- lane mask and the executing instruction's address (CURPC) of every access, from the moment it is installed. It shares no
	-- code with the debugger watchpoint machinery the bridge uses.
	local taplogs = {}
	function ops.tap_log(a)
		local d = dev(a.cpu)
		local sp = d.spaces[a.space or "program"]
		local pcreg = d.state["CURPC"] or d.state["PC"]
		local log = { events = {}, seen = 0, disabled = 0, cap = a.cap or 5000 }
		local full = (sp.data_width >= 64) and -1 or ((1 << sp.data_width) - 1)
		local maker = (a.kind == "r") and sp.install_read_tap or sp.install_write_tap
		log.handle = maker(sp, a.start, a["end"], "fixture_taplog_" .. a.name, function(off, data, mask)
			-- Reads the DEBUGGER makes with side effects disabled (its disassembly/memory views, one byte lane at a time) reach a
			-- tap but are not CPU accesses and never trigger a watchpoint: they are counted apart so the log is CPU accesses only.
			if manager.machine:side_effects_disabled() then log.disabled = log.disabled + 1; return nil end
			log.seen = log.seen + 1
			-- a.partial_only: record only accesses narrower than the bus (byte lanes of a wider bus), for finding them
			if #log.events < log.cap and not (a.partial_only and mask == full) then log.events[#log.events + 1] = { off = off, data = data, mask = mask, pc = pcreg.value } end
			return nil
		end)
		taplogs[a.name] = log
		return { installed = a.name, data_width = sp.data_width, endianness = sp.endianness }
	end
	function ops.tap_log_take(a)
		local log = taplogs[a.name]
		if not log then error("no tap log " .. tostring(a.name)) end
		local r = { events = log.events, seen = log.seen, cap = log.cap, side_effects_disabled_ignored = log.disabled }
		log.events, log.seen, log.disabled = {}, 0, 0
		if a.stop then taplogs[a.name] = nil end  -- (a tap cannot be uninstalled from Lua; it keeps counting into a dropped log)
		return r
	end

	-- All debugger console lines that start with a prefix (bulk fetch of what a user watchpoint's printf action wrote).
	function ops.console_lines(a)
		local cl = manager.machine.debugger.consolelog
		local out, n = {}, 0
		for i = 1, #cl do
			local line = cl[i]  -- the console log can answer nil for a line it has already discarded
			if line and line:sub(1, #a.prefix) == a.prefix then
				n = n + 1
				if n <= (a.max or 20000) then out[#out + 1] = line end
			end
		end
		return { lines = out, matched = n }
	end

	-- MAME's own `dasm` command run independently of the bridge, optionally with the raw opcode bytes column (opcodes=1).
	local dasm_n = 0
	function ops.dasm_run(a)
		dasm_n = dasm_n + 1
		local path = dir .. "/dasm_" .. dasm_n .. ".txt"
		os.remove(path)
		manager.machine.debugger:command(string.format('dasm "%s",0x%X,0x%X,%d,%s', path, a.address, a.length, a.raw and 1 or 0, a.cpu))
		local f = io.open(path, "r")
		if not f then return { lines = {}, missing = true } end
		local lines = {}
		for l in f:lines() do lines[#lines + 1] = l end
		f:close()
		os.remove(path)
		return { lines = lines }
	end

	local cmdpath, replypath, tmppath = dir .. "/cmd.json", dir .. "/reply.json", dir .. "/reply.tmp"
	emu.register_periodic(function()
		if cycler then
			cycler.i = cycler.i % #cycler.values + 1
			dev(cycler.cpu).spaces[cycler.space]:write_u8(cycler.address, cycler.values[cycler.i])
			cycler.writes = cycler.writes + 1
		end
		local f = io.open(cmdpath, "r")
		if not f then return end
		local text = f:read("*a")
		f:close()
		if #text == 0 or not text:find("}%s*$") then return end  -- partially written
		os.remove(cmdpath)
		local ok, req = pcall(json.decode, text)
		local reply
		if not ok or type(req) ~= "table" then
			reply = { ok = false, error = "bad json" }
		else
			local fn = ops[req.op]
			if not fn then
				reply = { id = req.id, ok = false, error = "unknown fixture op " .. tostring(req.op) }
			else
				local ok2, res = pcall(fn, req.args or {})
				reply = ok2 and { id = req.id, ok = true, result = res } or { id = req.id, ok = false, error = tostring(res) }
			end
		end
		local out = io.open(tmppath, "w")
		out:write(json.encode(reply))
		out:close()
		os.remove(replypath)
		os.rename(tmppath, replypath)
	end)
	print("mamemcp_fixture: active, mailbox " .. dir)
end

return exports
