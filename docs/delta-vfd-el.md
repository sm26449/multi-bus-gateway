# A Delta VFD-EL drive in the gateway

How to read and control a **Delta VFD-EL** micro drive (e.g. the
VFD022EL43A, 3×380 V, 2.2 kW) from Multi-Bus Gateway over its built-in RS-485
port. The bundled map is `delta_vfd_el`.

> **Status: written from the manual, not yet checked on a drive.** Version
> 0.1.0 follows the Delta VFD-EL User Manual, revision 2016/03 (6ELE,
> V1.14). The open points are listed in [§8](#8-still-to-confirm-on-a-drive).
> Until they are checked, run every command as a **dry run** first.

---

## 1. What the drive offers

The VFD-EL speaks **Modbus RTU or ASCII** on the RJ-45 jack on its front.
No option card is needed. The manual names three function codes and no
others:

| Code | What | Limit |
|---|---|---|
| 03H | read registers | up to 20 words in one read; **one parameter per read** |
| 06H | write one register | — |
| 08H | loop detection | — |

There is **no 10H (FC16)**. The map therefore declares
`single_register_writes: true`, and every write goes out as one FC6 per
register. It also declares `max_registers_per_read: 1`, because a parameter
can only be read alone.

**What the map reads:**

| Address | Row | Unit |
|---|---|---|
| 2100H | `error_code`: the present fault (oc, ov, oH1, oL, EF, GFF, PHL…) | — |
| 2101H | `status_word`: run state, direction, sources (decoded below) | — |
| 2102H | `frequency_command`: the frequency the drive is told (F) | 0.01 Hz |
| 2103H | `output_frequency`: the frequency at the motor (H) | 0.01 Hz |
| 2104H | `output_current` | 0.1 A |
| 2106H | `pid_feedback`: the PID feedback input, raw | — |
| 2108H | `dc_bus_voltage` | 0.1 V |
| 2109H | `output_voltage` | 0.1 V |
| 210AH | `igbt_temperature` | °C |
| 0200H | `frequency_source` (Pr 02.00) | — |
| 0201H | `command_source` (Pr 02.01) | — |
| 0902H | `comm_fault_action` (Pr 09.02) | — |
| 0903H | `comm_timeout` (Pr 09.03) | 0.1 s |

**Calculated from the status word:**
- `run_state` (0 stopped, 1 decelerating, 2 standby, 3 running);
- `running`, `reverse`;
- `frequency_from_comm`, `command_from_comm`.

**Calculated from the outputs:** `output_apparent_power` = √3 × U × I. The
real power is lower by the motor's power factor.

**What the map writes,** only through its commands:

| Address | Row | Values |
|---|---|---|
| 2000H | `command_word` | 1 stop · 6 run forward · 10 run reverse |
| 2001H | `frequency_setpoint` | Hz × 100, bounded 0–50 Hz |
| 2002H | `fault_control` | 2 = reset (bit 1); bit 0 (external fault) is never written |

---

## 2. Set the drive (keypad, once)

| Parameter | Set to | Why |
|---|---|---|
| **09.00** | 1 (1–254) | the drive's Modbus address; factory 0 answers nobody |
| **09.01** | 1 = 9600 bps (0 4800, 2 19200, 3 38400) | the bus speed |
| **09.04** | an **RTU** format, e.g. 8,N,2 or 8,E,1 | factory 0 is ASCII 7,N,2 |
| **02.00** | **3** = RS-485 | the frequency comes from the gateway; `set_frequency` refuses otherwise |
| **02.01** | an RS-485 setting **that keeps the keypad STOP key active** | run and stop come from the gateway; the STOP key still works |
| **09.02** | **1** = warn and ramp to stop | what the drive does when the bus goes quiet |
| **09.03** | **2.0** s | how long "quiet" is; factory 0.0 = never |

**09.02 and 09.03 are the most important safety setting.** If the gateway
stops, the network fails or someone pulls the cable, the motor ramps to a
stop by itself within 2 s. The gateway reads the drive every 2 s (the
`fast` group), which keeps that timer fed.

---

## 3. Wire it

- The RS-485 pair goes to the RJ-45 jack. **Check the pin assignment** (SG+,
  SG−, GND) in the manual's RS-485 section before crimping.
- Use a twisted pair, with GND to the adapter's GND. Put 120 Ω at both ends
  of a long bus.
- The drive goes on the gateway like any RTU slave:
  - **directly:** a USB-RS485 adapter mapped into the container;
  - **over the network:** the serial bridge, or an RS485-to-Ethernet
    converter.

  See [rtu-serial.md](rtu-serial.md) and
  [rtu-over-network.md](rtu-over-network.md).
- **Power side:**
  - an emergency stop that cuts the drive's supply;
  - a 3-pole breaker of the right rating;
  - a closed enclosure: the terminals carry dangerous voltage.

---

## 4. Add it in the gateway

1. **Devices → Add → Modbus RTU**, the bus the drive is on.
2. Fill in:
   - template **Delta VFD-EL frequency drive**;
   - **unit id** = Pr 09.00;
   - **speed and framing** = Pr 09.01 / 09.04.
3. **Test** reads the status. Save.
4. **Before any command:** on the device page, the **Drive settings** rows
   must read:
   - `frequency_source` = RS-485;
   - `comm_fault_action` = Warn and ramp to stop;
   - `comm_timeout` = 2.0 s.

---

## 5. Control it

Writes need **Settings → Security → allow writes**. Then each command is
enabled on its own, on the device's **Commands** tab.

| Command | What it does | Checks |
|---|---|---|
| `run` | value 0 stop, 1 forward, 2 reverse | reads 2000H back; with a **lease**, it falls back to **stop** when the lease runs out |
| `stop` | `run` with 0 | — |
| `run_forward`, `run_reverse` | `run` with 1 / 2 | — |
| `run_at` | **the one for rules:** writes the frequency, then runs forward; 0 Hz = stop | **guard:** Pr 02.00 = 3; reads 2102H and 2000H back; with a lease, it falls back to **stop** |
| `set_frequency` | 0–50 Hz | **guard:** Pr 02.00 must be 3; reads 2102H back (± 0.05 Hz) |
| `reset_fault` | writes 2 to 2002H | reads 2100H back: `success` only if the fault cleared |

**Always start with a dry run.** It shows the exact frame that would go
out, for example `2001H ← 3750` for 37.5 Hz, and writes nothing.

The bound of 50 Hz sits in two places:
- `frequency_setpoint.write_max`;
- the `set_frequency` parameter's `max`.

Raise both only up to the drive's Pr 01.00 and the motor's rating.

**Do not write the drive's parameters (GGnnH) from rules or loops.** They
are stored in the drive's memory and wear it out. Runtime control uses
2000H–2002H only.

---

## 6. Examples: rules that drive the motor

### 6.1 One condition: over X, run at 25 %

`run_at` starts the motor and sets its speed in one command, so one rule is
enough. 25 % of 50 Hz is 12.5 Hz. Here the trigger is the water temperature
read by another device (`boiler.water_temperature`), with a hysteresis:

```yaml
- id: vfd-pump-on-temperature
  label: Pump at 25 % over 55 °C, off under 50 °C
  target: { device: vfd1, command: run_at }
  kind: condition
  when: "boiler.water_temperature > 55"
  release: "boiler.water_temperature < 50"
  then: { params: { value: 12.5 } }
  else: { params: { value: 0 } }                     # 0 Hz = stop
  timing: { every_s: 5, debounce: 3, min_interval_s: 60 }
  stale_after_s: 60
  on_stale: safe                                     # the sensor is gone → stop
  on_disable: safe
```

Any value works as the trigger: PV power, export, a battery's SOC, a
temperature, a pressure.

### 6.2 Solar surplus into a pump or a fan

Two rules, both starting in **shadow**: they decide and log, and write
nothing until they are armed. `site-meter.power_active_total` stands for
your grid meter, and the example assumes **export reads negative**; check
the sign on its device page first.

```yaml
- id: vfd-run-on-surplus
  label: Drive · run while exporting
  target: { device: vfd1, command: run }
  kind: condition
  when: "site-meter.power_active_total < -800"      # exporting more than 800 W
  release: "site-meter.power_active_total > -100"   # stop only near zero export
  then: { params: { value: 1 } }
  else: { params: { value: 0 } }
  timing: { every_s: 5, debounce: 6, min_interval_s: 120 }
  stale_after_s: 60
  on_stale: hold
  on_disable: safe                                   # disabled → the run command's safe value: stop

- id: vfd-speed-from-surplus
  label: Drive · speed follows the surplus
  target: { device: vfd1, command: set_frequency }
  kind: steps
  signal: "-site-meter.power_active_total"           # export as a positive number
  steps:
    - { at: 800,  value: 25, label: Low }
    - { at: 1200, value: 35, label: Medium }
    - { at: 1600, value: 45, label: High }
    - { at: 2000, value: 50, label: Full }
  release_below: 600
  normal: { value: 25 }
  timing: { every_s: 5, debounce: 3, min_interval_s: 30 }
  stale_after_s: 60
  on_stale: hold
```

**Better: one `trim` rule.** It replaces both rules above: it raises
the speed while there is export and lowers it while there is not, down to a
stop. See [rules.md §3.6](rules.md#36-a-drive-that-soaks-up-the-solar-surplus-trim).

The thresholds are only a starting point: a 2.2 kW motor at 50 Hz draws
far more than 2 kW of surplus only under a full load. Watch the shadow log
for a sunny day, then adjust and arm. See [rules.md](rules.md).

---

## 7. Troubleshooting

| What you see | Likely cause |
|---|---|
| No answer | Pr 09.00 still 0; speed or format differ from Pr 09.01 / 09.04; A/B swapped |
| *Illegal function* on a write | something sent FC16: the template must keep `single_register_writes` |
| *Illegal data address* on a read | a read of more than one parameter; keep `max_registers_per_read` at 1 |
| `set_frequency` rejected by its guard | Pr 02.00 is not 3 |
| `run` succeeds but the motor does not turn | Pr 02.01 is not an RS-485 setting; or a fault (see `error_code`) |
| cE10 on the drive's display | the bus went quiet longer than Pr 09.03: the gateway stopped, or the cable |
| `reset_fault` answers *mismatch* | the fault is still there; fix its cause first |

---

## 8. Still to confirm on a drive

- **2001H scaling:**
  - the map assumes 0.01 Hz (5000 = 50.00 Hz);
  - the dry run shows the raw word; the read-back from 2102H confirms it.
- **2101H bit 10** (run command from the communication port). It is not
  on the manual pages read for this map.
- **The Pr 02.01 values** for "RS-485, keypad STOP active".
- **Whether 2000H reads back the last command.** If it does not, `run`
  answers *mismatch*. The verify can then move to `run_state`.
- **Block reads of 2100H–210AH:**
  - the manual allows up to 20 words for status registers;
  - if the drive answers them, `max_registers_per_read` can go up for fewer
    requests;
  - only with the parameter rows unselected.
- **The RJ-45 pin assignment.**

Each confirmed point goes into the map's `source_document`, and its version
moves to 1.0.0.
