-- EXTERNAL ordinary-input script for the 1942 bank research (loaded with MAME's own -autoboot_script).
-- Not part of the MCP server and not a new input subsystem: it only presses the machine's own coin/start/control
-- fields, like a person would, at fixed emulated times, and logs every press (emulated time, frame, action).
-- It touches no memory, writes no registers and selects no bank.
local log_path = os.getenv("MAMEMCP_INPUT_LOG")
local log = log_path and io.open(log_path, "w")
local ports = manager.machine.ioport.ports
local screen = nil
for _, s in pairs(manager.machine.screens) do screen = s break end

local function field(port, name) return ports[port].fields[name] end
local function now() return manager.machine.time:as_double() end
local function frame() return screen and screen:frame_number() or -1 end
local function note(msg)
	if log then log:write(string.format("t=%.3f frame=%d %s\n", now(), frame(), msg)); log:flush() end
end

-- schedule: { emulated time (s), port, field, hold seconds }
local sched = {
	{ 3.0, ":SYSTEM", "Coin 1", 0.15 },
	{ 4.5, ":SYSTEM", "1 Player Start", 0.15 },
}
-- after the start press: ordinary play, fire pulses every 0.5 s and left/right sweeps every 2 s
for i = 0, 400 do
	local t = 9.0 + i * 0.5
	sched[#sched + 1] = { t, ":P1", "P1 Button 1", 0.15 }
	if i % 4 == 0 then sched[#sched + 1] = { t, ":P1", (i // 4) % 2 == 0 and "P1 Left" or "P1 Right", 1.0 } end
end
table.sort(sched, function(a, b) return a[1] < b[1] end)

local active, nextidx = {}, 1
note("input script started")
emu.register_frame_done(function()
	local t = now()
	while nextidx <= #sched and sched[nextidx][1] <= t do
		local e = sched[nextidx]
		field(e[2], e[3]):set_value(1)
		active[#active + 1] = { f = field(e[2], e[3]), off = t + e[4], label = e[2] .. "/" .. e[3] }
		if nextidx <= 2 or nextidx % 20 == 0 then note("press " .. e[2] .. " " .. e[3] .. " (scheduled " .. e[1] .. ")") end
		nextidx = nextidx + 1
	end
	for i = #active, 1, -1 do
		if t >= active[i].off then active[i].f:set_value(0); table.remove(active, i) end
	end
end)
