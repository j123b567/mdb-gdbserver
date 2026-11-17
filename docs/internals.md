# Internals

## Files

| File | Role |
|---|---|
| `mdbproc.py` | starts `mdb.sh`, sends a command followed by `echo <marker>` and reads the output up to `/*<marker>*/`; a reader thread catches the asynchronous `Stop at` / `address:0x...` events and the `@@` lines of the helper |
| `mdbhelper.py` | Jython 2.7 file loaded into MDB with `LoadScript`; functions are called with `Execute regs()`; it reaches MDB's objects through `CommandInterpreter.getCommandInterpreter()` and its `debugger` field, and reads the CPU registers from `RegisterViewProvider` (memory type `eCPURegister`, model `NMMR`) |
| `mdb_gdbserver.py` | RSP packet layer and the mapping to MDB commands |

MDB ships Jython 2.7 (`mplablibs/modules/ext/mchp-jython-standalone.jar`
in MPLAB X) and the scripting library
(`com-microchip-mplab-lib-scripting.jar`, JSR-223 engine `JYTHON2_7`); the
MDB commands `LoadScript file` and `Execute expr` use them. The interpreter
runs inside the MDB JVM and can import every MDB class.

## Mapping of RSP to MDB

| RSP | MDB |
|---|---|
| `g`, `p n` | `Execute regs()` (Jython): r0..r31, `Status`, `lo`, `hi`, `BadVAddr`, `Cause`, `DEPC` as pc, and CP0 `Debug`; read once per stop, 1.5 s the first time, then about 0.04 s |
| `m` | `x /r` (RAM, KSEG0/1 0x8000_0000 / 0xA000_0000), `x /p` (program and boot flash), `x /m` (SFRs), up to 64 words per command |
| `M`, `X` | `write /r`, `write /m` (read-modify-write of whole words); flash is refused |
| `qXfer:memory-map:read` | map from MDB: RAM, program flash, boot flash, SFRs (see below) |
| `vFlashErase`, `vFlashWrite` | accepted, data ignored |
| `vFlashDone` | `hwtool <tool>` + `program <elf>` |
| `Z0`, `Z1` / `z0`, `z1` | `break *addr` / `delete n`, both as hardware breakpoints |
| `Z2`, `Z3`, `Z4` | `watch addr W`, `R`, `RW` |
| `c`, `vCont;c` | `run` the first time, then `continue` |
| `s`, `vCont;s` | `stepi` (one machine instruction, about 0.06 s) |
| Ctrl-C (0x03) | `halt`, stop reply `T02` |
| `qRcmd` (`monitor ...`) | `program [elf]`, `reset`, otherwise the text goes to MDB as a command; empty `O` packets every second keep gdb from timing out |
| `P` of the pc right after `load` | accepted and ignored (the target starts from its reset vector) |
| `D`, `k` | end of this gdb connection; MDB keeps running |

## Selecting the debug tool

MDB selects a tool with `hwtool TYPE [INDEX]`. `ToolFinder` in
`lib/mdb.jar` lists the USB tools of the vendor IDs 04D8 and 03EB; their
IDs look like `:=MPLABCommUSB:=04D8:=9009:=0100:=<maker>:=<product>:=<serial>:=...`.
TYPE is the tool name of `PlatformToolMetaManager` in lower case without
spaces and dashes (`ICD 3` -> `icd3`), which gives a USB product ID
(`0x9009`). INDEX counts only the listed tools whose ID contains that
product ID, so it is not the index printed by `hwtool` without arguments.

MDB has no option for a serial number. The helper function `tools()`
repeats the lookup of `ToolFinder` and prints type, index and serial
number of every tool, and the server picks one. `Execute` works only after
some `hwtool`, so the server sets `hwtool sim` first (about 3 s) and
switches to the real tool after it; that switch prints
`E0108-SIM: Failed simulator operation: java.lang.NullPointerException`,
which does no harm. With `--tool TYPE` alone the simulator step is
skipped. After the switch, `tool()` (`Assembly.GetToolClass()`,
`GetToolID()`) confirms the connected tool.

## Which watchpoint fired

MDB does not say which control point halted the CPU, and the EJTAG `DBS`
register (0xFF302000), which would, reads as 0 through `x`. The server
finds it like this:

1. CP0 `Debug` (read with the registers) gives the kind of halt:
   `0x40128010` breakpoint (bit 4 DIB), `0x40128008` data breakpoint on a
   store (bit 3 DDBS), `0x40128004` on a load (bit 2 DDBL), `0x40128001`
   after `stepi` (bit 0 DSS).
2. The data breakpoint is precise: `DEPC` is the load/store itself, not
   executed yet (or the branch before it when bit 31 DBD is set, then the
   access is at `DEPC + 4`). gdb steps over it itself, MIPS has
   non-steppable watchpoints.
3. The server reads that instruction, decodes the MIPS32 load/store and
   computes base register + offset; the watchpoint whose range contains
   this address is reported (`watch:`, `rwatch:` or `awatch:` in the stop
   reply). The RSP log has a `## watchpoint` line for each such stop.

When decoding is not possible the first watchpoint of the right kind is
reported. That happens for code in MIPS16e (PIC32MX) or microMIPS
(PIC32MZ, PIC32MK, PIC32MM), where bit 0 of `DEPC` is set and the server
does not decode the instruction, and for a load that overwrites its own
base register.
Reporting a wrong one is not harmless: gdb then sees no change, continues
silently, and the session looks hung.

## MDB behavior the server depends on

- **Breakpoint limit.** The tool has a fixed number of instruction and
  data breakpoints per device, e.g. 8 and 4 for the ICD 3 on the PIC32MZ
  EC (see below). The server refuses one more itself (`E01`). It must not
  ask MDB: after one refused `break` (`Breakpoint -1 at ...`) MDB refuses
  every later breakpoint of the session, even when none is set.
- **No register write.** `P`/`G` return `E01` (except the pc after `load`).
  Writes through `RegisterViewProvider.writeSFRValue`,
  `SFRegister.WriteValue` and `NMMR.GetPhysicalMemory().WriteWord` all
  report success, but the next read (also after a `stepi`) shows the old
  value. `RIDbgToolBase.SetTargetPC(long)` is a candidate for the PC; not
  tried.
- **A second `program` in one MDB debug session fails** (`Failed to
  program device`, then `The target device is not ready for debugging`).
  `hwtool <tool>` (7 s with the ICD 3) before it fixes it; the server
  always does that.
  MDB `reset` itself is fine.
- **MIPS addresses in gdb are sign-extended** (`0x9d000000` is
  `0xffffffff9d000000`); the memory map must use that form, otherwise gdb
  refuses every access as outside the map.
- `halt` takes about 0.8 s.
- Before the first `continue` MDB has no CPU state; gdb sees only the
  reset PC 0xBFC00000.
- `Execute` prints nothing itself and MDB removes double quotes from its
  argument, so the helper prints `@@` lines through `System.out` and is
  called with numbers only.
- A server started in the background by a shell ignores SIGINT; it ends on
  SIGTERM (and on Ctrl-C in a terminal) and quits MDB.
- Batch-mode gdb does not process the stop after `interrupt` while it runs
  `shell sleep`; interactive gdb and Ctrl-C (SIGINT) work.

## Device-specific parts

Read from MDB after `hwtool`, through the helper:

- `caps()`: the number of instruction and data breakpoints,
  `ControlPointMediator.getEmulationCapabilities()`
  (`getMaxInstructionBPs()`, `getMaxDataBPs()`). MDB takes them from the
  `.emu` file of the tool in `emu.jar` (`ICD3DbgToolManager.emu` and
  others): per device an emulation engine, e.g. `ejtagonlyiflowtrace2`
  with 8 and 4 for the PIC32MZ EC, `ejtagbvci` with 6 and 2 for the
  PIC32MX795. The simulator reports 1000 of each.
- `memmap()`: physical ranges of program flash, boot flash, RAM and SFRs
  from the EDC file of the device pack, `Assembly.GetDevice()
  .getMainPartition()` (`getCodeRange()`, `getBootConfigRange()`,
  `getGPRRange()`, `getSFRRange()`). The server maps flash and RAM into
  KSEG0 and KSEG1 and SFRs into KSEG1, and uses the ranges both for the
  gdb memory map and for the MDB memory type of `x` and `write` (`/p`,
  `/r`, `/m`). Flash ranges are rounded up to 4 KB: the boot flash range
  of the PIC32MX795 ends at 0x1fc02ff0, before its configuration words.
  External memory (EBI, SQI, DDR) is not in the map.

MIPS registers, CP0 `Debug` and the KSEG layout are the same on PIC32MX,
PIC32MZ and PIC32MK.
