# mdb_gdbserver

A GDB remote serial protocol (RSP) server for PIC32 targets on the MPLAB
debug tools. It runs the unchanged MDB (the command-line debugger of MPLAB
X) as a child process and translates gdb requests into MDB commands, so gdb
or any IDE that talks to a gdb remote target can debug the chip. Nothing in MPLAB X
is patched.

## Requirements

- MPLAB X v5.45 on Linux. Other versions are not tested. The server runs
  `/opt/microchip/mplabx/v5.45/mplab_platform/bin/mdb.sh`; set another path
  with `--mdb` or the environment variable `MDB_SH`.
- An MPLAB debug tool. Tested with the ICD 3; the others MDB knows
  (`icd4`, `pickit3`, `pickit4`, `snap`, `realice`, ...) are selected the
  same way, but are not tested.
- A PIC32 with a MIPS32 core (PIC32MX, PIC32MZ, PIC32MK), set with
  `--device`. The memory map and the number of hardware breakpoints come
  from MDB. Tested on the PIC32MZ2048ECM100; the map of other devices was
  checked in the MDB simulator only.
- Python 3.
- gdb with MIPS support (for example a multiarch or multitarget build).

## Quick start

```sh
# terminal 1: starts MDB, programs the ELF, then serves gdb connections
# until SIGTERM or Ctrl-C
python3 mdb_gdbserver.py --watch-elf app.elf

# terminal 2, as often as needed
gdb app.elf
(gdb) target remote localhost:3333
(gdb) break main
(gdb) continue
```

The start of MDB takes about 30 s, programming takes from a few seconds to
about a minute depending on the image size. MDB keeps running between gdb
connections, so later connections are immediate. When gdb detaches or
disconnects, the server removes all breakpoints and watchpoints, halts the
target and waits for the next connection.

Options:

| Option | Meaning |
|---|---|
| `--port` | TCP port, default 3333 (only on 127.0.0.1) |
| `--device` | device name as in MPLAB X, default `PIC32MZ2048ECM100` |
| `--tool` | debug tool as `TYPE[:SERIAL]`, e.g. `icd3` or `icd3:JIT123456789`, or `:SERIAL` alone; default: the only tool connected |
| `--mdb` | path of `mdb.sh`, default `$MDB_SH` or the MPLAB X v5.45 location |
| `--watch-elf` | program the ELF again when the file changes and no gdb is connected |
| `--once` | quit after the first gdb connection |
| `--log`, `--mdb-log` | logs of the RSP packets and of the MDB dialogue, default `mdb_gdbserver.log`, `mdb_gdbserver.mdb.log` |

## Selecting the debug tool

Without `--tool` the server uses the only debug tool connected; with
none or several it stops and lists them as `TYPE:SERIAL`. `--tool TYPE`
takes the first tool of that type (the type names are those of MDB's
`hwtool supported`), `--tool TYPE:SERIAL` or `--tool :SERIAL` one tool by
its serial number.

## Building a debuggable ELF

The ELF must be built with the flags MPLAB X uses for a "Debug" build
(XC32). For the ICD 3:

```
compile: -g -D__DEBUG -D__MPLAB_DEBUGGER_ICD3=1
link:    -g -mdebugger -D__MPLAB_DEBUGGER_ICD3=1 -mreserve=data@0x0:0x27F
         -Wl,--defsym=__MPLAB_BUILD=1,--defsym=__MPLAB_DEBUG=1,--defsym=__DEBUG=1,--defsym=__MPLAB_DEBUGGER_ICD3=1
```

`-mreserve=data@0x0:0x27F` keeps the first 640 bytes of RAM free for the
debug executive. MDB programs only what is in the ELF, so an application
that is started by a bootloader must have the bootloader linked into the
same ELF.

## Loading a new build

Three ways, none of them restarts MDB:

- `--watch-elf`: the server programs the file as soon as it has changed
  and no gdb is connected;
- `load` in gdb: the server programs the ELF file given on its command
  line, read again from disk. The data gdb sends is not used, so gdb must
  load the same file;
- `monitor program [ELF]`: programs the given or the current ELF.

After `monitor program` and `monitor reset`, run
`maintenance flush register-cache`, otherwise gdb shows the registers of
the last stop. `load` does not need it.

## Use from an IDE

Configure the IDE to connect to an already running remote target:

- debugger: the gdb with MIPS support;
- remote target: `localhost:3333`;
- symbol file: the ELF the server was started with;
- no download and no server start by the IDE.

Start the server once with `--watch-elf`. After each build it programs the
new image before the next debug session. A connection that arrives during
programming waits until programming is done, so set
`set remotetimeout 120` in the gdb init file.

## Supported

- `break` on functions, lines and addresses, `continue`, `step`, `next`,
  `stepi`, `finish`
- `backtrace`, `info frame`, `info locals`, `info registers`, `x/i $pc`
- `print` and `set var` of variables in RAM
- `watch`, `rwatch`, `awatch`, several at once, with the right watchpoint
  reported
- Ctrl-C while running
- `load`, `monitor program`, `monitor reset`, `monitor <mdb command>`
- several gdb sessions one after another on one MDB

## Limitations

- Only hardware breakpoints and watchpoints, as many as the tool has for
  the device (the server prints them at the start; ICD 3 with PIC32MZ EC: 8
  and 4). gdb's `next` and `finish` need one breakpoint each, so keep one
  free.
- MIPS32 and microMIPS code are tested on a PIC32MZ, MIPS16e code on a
  PIC32MX (ICD 3: 6 breakpoints, 2 watchpoints). The PIC32MM (microMIPS
  only) is not tested at all.
- A load or store in the delay slot of a jump is missed by its watchpoint
  when the target resumes from that jump (e.g. after a breakpoint on it).
- External memory (EBI, SQI, DDR) is not in the memory map.
- Registers cannot be written: `set $reg`, `call`, `return` and `jump` do
  not work.
- The server cannot attach to a running target. The first `continue`
  starts the program from the reset vector; before it gdb sees only the
  reset PC 0xBFC00000.
- A halt (breakpoint, Ctrl-C) takes about 0.8 s.

## License

MIT, see [LICENSE](LICENSE).
