#!/usr/bin/env python3
"""GDB remote serial protocol (RSP) server on top of the stock MPLAB X MDB.

Usage: mdb_gdbserver.py [--port 3333] [--device PIC32MZ2048ECM100]
                        [--tool TYPE[:SERIAL]] [--mdb MDB_SH] ELF

Starts mdb.sh as a child process, connects the debug tool (by default the
only one connected) in debug mode, programs ELF and then serves GDB
connections on localhost:PORT, one after the other. MDB keeps running between connections, so only the first
connection pays its start (about 30 s).

Supported:
  - stop reason (?), continue (c, vCont;c), single instruction step (s,
    vCont;s), interrupt (Ctrl-C), detach/kill (the server stays up)
  - breakpoints Z0/Z1 (both as hardware breakpoints, MDB "break *addr"),
    watchpoints Z2/Z3/Z4 (MDB "watch"), with the watchpoint that fired
    found from CP0 Debug and the load/store at DEPC (MIPS32, MIPS16e or
    microMIPS)
  - memory read (m) of RAM, program flash, boot flash and SFRs, memory
    write (M, X) of RAM and SFRs; a memory map tells GDB where flash is;
    the memory ranges and the number of hardware breakpoints come from MDB
  - GDB "load": vFlashErase/vFlashWrite are accepted, vFlashDone programs
    ELF (the file given on the command line, read again from disk) with
    "hwtool" + "program"; the data sent by GDB is not used
  - registers: read of r0..r31, sr, lo, hi, bad, cause, pc through the
    Jython helper mdbhelper.py (MDB "LoadScript"/"Execute"); no write,
    except that a write of the PC right after "load" is accepted and ignored
  - monitor commands: "monitor program [ELF]", "monitor reset" (MDB
    "reset", then "maintenance flush register-cache" in GDB), any other text
    goes to MDB as a command

Register layout is the default of GDB for a 32-bit MIPS remote target
without a target description: r0..r31, sr, lo, hi, bad, cause, pc.
"""
import argparse
import os
import re
import select
import signal
import socket
import sys
import threading
import time

from mdbproc import MDB_SH, Mdb, MdbError

NUM_REGS = 38
PC_REGNUM = 37
SIGTRAP = 5
SIGINT = 2

# CP0 Debug bits (MIPS EJTAG)
DEBUG_DSS = 1 << 0   # single step
DEBUG_DBP = 1 << 1   # SDBBP instruction
DEBUG_DDBL = 1 << 2  # data breakpoint, load
DEBUG_DDBS = 1 << 3  # data breakpoint, store
DEBUG_DIB = 1 << 4   # instruction breakpoint
DEBUG_DBD = 1 << 31  # the halt is in a branch delay slot, DEPC is the branch

# MDB memory type letter of each kind of memory range of the device
MEM_LETTERS = {"code": "p", "boot": "p", "ram": "r", "sfr": "m"}
KSEG0 = 0x80000000
KSEG1 = 0xA0000000
# GDB only needs some erase block size for flash; the server ignores the
# data of vFlashErase/vFlashWrite. The boot flash range of the EDC may end
# before the configuration words (PIC32MX: 0x1fc02ff0), the rounding up to
# this size includes them.
FLASH_BLOCK = 0x1000


def le32(value):
    return value.to_bytes(4, "little").hex()


class Device:
    """Memory ranges and hardware breakpoints of the device on this tool.

    MDB knows both: the ranges from the EDC file of the device pack
    (Assembly.GetDevice()), the breakpoints from the .emu file of the tool
    (ControlPointMediator). MDB must never be asked for more breakpoints:
    after one refused "break" ("Breakpoint -1 at ...") it refuses every
    later breakpoint of the session, even with none set.
    """

    def __init__(self, mdb, name):
        # the compressed ISA of the core: the M4K of the PIC32MX has
        # MIPS16e, the newer cores (PIC32MZ, PIC32MK, PIC32MM) microMIPS
        self.compressed_isa = "mips16e" if name.upper().startswith("PIC32MX") else "micromips"
        out = mdb.script("caps()")
        line = next((l for l in out if l.startswith("caps ")), None)
        if line is None:
            raise MdbError(f"caps(): {out}")
        self.max_breakpoints, self.max_watchpoints = (int(v) for v in line.split()[1:])
        self.ranges = []  # (kind, physical begin, physical end)
        for line in mdb.script("memmap()"):
            if line.startswith("memmap "):
                _, kind, begin, end = line.split()
                begin, end = int(begin, 16), int(end, 16)
                if kind in ("code", "boot"):
                    end = (end + FLASH_BLOCK - 1) & ~(FLASH_BLOCK - 1)
                self.ranges.append((kind, begin, end))
        if not self.ranges:
            raise MdbError("memmap(): no memory ranges")

    def memtype(self, addr):
        """MDB memory type letter for a virtual address, None if not handled."""
        phys = addr & 0x1FFFFFFF
        for kind, begin, end in self.ranges:
            if begin <= phys < end:
                return MEM_LETTERS[kind]
        return None

    def memory_map(self):
        """Memory map for GDB: flash makes GDB use hardware breakpoints there
        and "load" use vFlashErase/vFlashWrite/vFlashDone. GDB keeps MIPS
        addresses sign-extended to 64 bits (0x9d000000 is 0xffffffff9d000000),
        the map must use the same form or GDB refuses every access as outside
        the map. SFRs are only reached through KSEG1 (uncached).
        """
        lines = []
        for kind, begin, end in self.ranges:
            for seg in ((KSEG1,) if kind == "sfr" else (KSEG0, KSEG1)):
                start = 0xFFFFFFFF00000000 | seg | begin
                if kind in ("code", "boot"):
                    lines.append(f'  <memory type="flash" start="0x{start:x}" length="0x{end - begin:x}">'
                                 f'<property name="blocksize">0x{FLASH_BLOCK:x}</property></memory>')
                else:
                    lines.append(f'  <memory type="ram" start="0x{start:x}" length="0x{end - begin:x}"/>')
        return ('<?xml version="1.0"?>\n'
                '<!DOCTYPE memory-map PUBLIC "+//IDN gnu.org//DTD GDB Memory Map V1.0//EN" '
                '"http://sourceware.org/gdb/gdb-memory-map.dtd">\n'
                "<memory-map>\n" + "\n".join(lines) + "\n</memory-map>\n")


def sext(value, bits):
    sign = 1 << (bits - 1)
    return (value & (sign - 1)) - (value & sign)


GP, SP = 28, 29
# 3-bit register field of MIPS16e and of 16-bit microMIPS instructions
REGS3 = (16, 17, 2, 3, 4, 5, 6, 7)
# 3-bit source register field of microMIPS SB16, SH16, SW16
REGS3_STORE = (0, 17, 2, 3, 4, 5, 6, 7)


def access(regs, is_store, base, offset, size, rt=None):
    """(is_store, data address, size) of a load/store, base None for an
    absolute address in offset."""
    if not is_store and rt is not None and rt == base:
        return None  # the load has overwritten its base register
    if base is not None:
        offset += regs[base]
    return is_store, offset & 0xFFFFFFFF, size


def decode_mips32(code, addr, regs):
    insn = int.from_bytes(code[:4], "little")
    op = insn >> 26
    sizes = {0x20: 1, 0x24: 1, 0x21: 2, 0x25: 2, 0x23: 4, 0x22: 4, 0x26: 4, 0x30: 4,
             0x28: 1, 0x29: 2, 0x2B: 4, 0x2A: 4, 0x2E: 4, 0x38: 4,
             0x31: 4, 0x35: 8, 0x39: 4, 0x3D: 8}  # LWC1, LDC1, SWC1, SDC1
    if op == 0x1F and insn & 0x3F == 0x0A:  # DSP LBUX, LHX, LWX rd, index(base)
        size = {6: 1, 4: 2, 0: 4}.get((insn >> 6) & 0x1F)
        if size:
            return access(regs, False, None, regs[(insn >> 21) & 0x1F] + regs[(insn >> 16) & 0x1F], size)
    if op not in sizes:
        return None
    is_store = op in (0x28, 0x29, 0x2B, 0x2A, 0x2E, 0x38, 0x39, 0x3D)
    rt = (insn >> 16) & 0x1F if op < 0x31 else None  # not an FPU register
    return access(regs, is_store, (insn >> 21) & 0x1F, sext(insn, 16), sizes[op], rt)


# major opcode -> (is_store, size)
MIPS16_ACCESS = {0x10: (False, 1), 0x11: (False, 2), 0x13: (False, 4), 0x14: (False, 1),
                 0x15: (False, 2), 0x18: (True, 1), 0x19: (True, 2), 0x1B: (True, 4)}


def decode_mips16(code, addr, regs):
    hw = int.from_bytes(code[:2], "little")
    ext = None
    if hw >> 11 == 0x1E:  # EXTEND: the next instruction has a 16-bit immediate
        ext = hw & 0x7FF
        hw = int.from_bytes(code[2:4], "little")
    op = hw >> 11
    rx, ry = REGS3[(hw >> 8) & 7], REGS3[(hw >> 5) & 7]

    def imm(field, scale):
        if ext is None:
            return field * scale
        return sext((ext & 0x1F) << 11 | (ext >> 5) << 5 | hw & 0x1F, 16)

    if op in MIPS16_ACCESS:
        is_store, size = MIPS16_ACCESS[op]
        return access(regs, is_store, rx, imm(hw & 0x1F, size), size, ry)
    if op in (0x12, 0x1A):  # LWSP, SWSP
        return access(regs, op == 0x1A, SP, imm(hw & 0xFF, 4), 4, rx)
    if op == 0x16:  # LWPC, from the instruction (or its EXTEND) address
        return access(regs, False, None, (addr & ~3) + imm(hw & 0xFF, 4), 4)
    if op == 0x0C and (hw >> 8) & 7 == 2:  # SWRASP
        return access(regs, True, SP, imm(hw & 0xFF, 4), 4)
    if op == 0x0C and (hw >> 8) & 7 == 4:  # SAVE, RESTORE
        nregs = bool(hw & 0x40) + bool(hw & 0x20) + bool(hw & 0x10)  # ra, s0, s1
        nargs = 0
        if ext is None:
            frame = (hw & 0xF or 16) * 8
        else:
            frame = (ext & 0xF0 | hw & 0xF) * 8
            aregs = ext & 0xF
            if aregs == 0xF:
                return None
            nargs, nstatics = {0xB: (0, 4), 0xE: (4, 0)}.get(aregs, (aregs >> 2, aregs & 3))
            nregs += (ext >> 8) + nstatics  # s2..s8
        if hw & 0x80:  # SAVE: registers below sp, arguments from sp up
            return access(regs, True, SP, -4 * nregs, 4 * (nregs + nargs))
        if nregs:  # RESTORE: registers below sp + frame
            return access(regs, False, SP, frame - 4 * nregs, 4 * nregs)
    return None


def micromips_size(code):
    """Length of the microMIPS instruction: major opcode bits 2..0 of 1, 2
    or 3 make a 16-bit instruction."""
    return 2 if (code[1] >> 2) & 7 in (1, 2, 3) else 4


# major opcode -> (is_store, size)
MICROMIPS16_ACCESS = {0x02: (False, 1), 0x0A: (False, 2), 0x1A: (False, 4),
                      0x22: (True, 1), 0x2A: (True, 2), 0x3A: (True, 4)}
MICROMIPS32_ACCESS = {0x07: (False, 1), 0x05: (False, 1), 0x0F: (False, 2), 0x0D: (False, 2),
                      0x3F: (False, 4), 0x06: (True, 1), 0x0E: (True, 2), 0x3E: (True, 4)}
MICROMIPS_FPU_ACCESS = {0x27: (False, 4), 0x2F: (False, 8), 0x26: (True, 4), 0x2E: (True, 8)}
# POOL32B and POOL32C function -> (is_store, size); 0 for LWM32/SWM32
MICROMIPS_POOL32B = {0x1: (False, 8), 0x9: (True, 8), 0x5: (False, 0), 0xD: (True, 0)}
MICROMIPS_POOL32C = {0x0: (False, 4), 0x1: (False, 4), 0x8: (True, 4), 0x9: (True, 4),
                     0x3: (False, 4), 0xB: (True, 4)}  # LWL, LWR, SWL, SWR, LL, SC


def decode_micromips(code, addr, regs):
    hw = int.from_bytes(code[:2], "little")
    op = hw >> 10
    if op in MICROMIPS16_ACCESS:
        is_store, size = MICROMIPS16_ACCESS[op]
        field = hw & 0xF
        offset = -1 if op == 0x02 and field == 0xF else field * size
        rt = (REGS3_STORE if is_store else REGS3)[(hw >> 7) & 7]
        return access(regs, is_store, REGS3[(hw >> 4) & 7], offset, size, rt)
    if op in (0x12, 0x32):  # LWSP, SWSP
        return access(regs, op == 0x32, SP, (hw & 0x1F) * 4, 4, (hw >> 5) & 0x1F)
    if op == 0x19:  # LWGP
        return access(regs, False, GP, sext(hw, 7) * 4, 4, REGS3[(hw >> 7) & 7])
    if op == 0x11 and (hw >> 6) & 0xF in (4, 5):  # LWM16, SWM16: s0..sN, ra
        return access(regs, (hw >> 6) & 0xF == 5, SP, (hw & 0xF) * 4, 4 * (((hw >> 4) & 3) + 2))
    if micromips_size(code) == 2:
        return None
    insn = hw << 16 | int.from_bytes(code[2:4], "little")
    rt, base = (insn >> 21) & 0x1F, (insn >> 16) & 0x1F
    if op in MICROMIPS32_ACCESS:
        is_store, size = MICROMIPS32_ACCESS[op]
        return access(regs, is_store, base, sext(insn, 16), size, rt)
    if op in MICROMIPS_FPU_ACCESS:
        is_store, size = MICROMIPS_FPU_ACCESS[op]
        return access(regs, is_store, base, sext(insn, 16), size)
    pool = {0x08: MICROMIPS_POOL32B, 0x18: MICROMIPS_POOL32C}.get(op, {})
    if (insn >> 12) & 0xF in pool:
        is_store, size = pool[(insn >> 12) & 0xF]
        if not size:  # LWM32, SWM32: s0..s7, s8 by count, ra by bit 4
            size = 4 * ((rt & 0xF) + (rt >> 4))
            rt = None
        elif size == 8:  # LWP, SWP: rt and rt + 1
            rt = None
        return access(regs, is_store, base, sext(insn, 12), size, rt)
    # LWXS and DSP LBUX, LHX, LWX rd, index(base): (size, index scale)
    indexed = {0x118: (4, 4), 0x225: (1, 1), 0x165: (2, 1), 0x1A5: (4, 1)}.get(insn & 0x3FF)
    if op == 0x00 and indexed:
        size, scale = indexed
        return access(regs, False, None, regs[base] + scale * regs[rt], size)
    return None


DECODERS = {"mips32": decode_mips32, "mips16e": decode_mips16, "micromips": decode_micromips}


def decode_access(isa, code, addr, regs):
    """Decode the load/store at addr: (is_store, data address, size).

    code holds the 4 bytes at addr; regs are the GDB registers. Returns
    None for other instructions.
    """
    return DECODERS[isa](code, addr, regs)


def branch_size(isa, code):
    """Length of the branch or jump in code, whose delay slot follows it."""
    if isa == "mips16e":
        return 4 if int.from_bytes(code[:2], "little") >> 11 == 0x03 else 2  # JAL, JALX
    if isa == "micromips":
        return micromips_size(code)
    return 4


class Target:
    """Debug operations of the PIC32 through MDB."""

    WATCH_TYPES = {2: "W", 3: "R", 4: "RW"}  # GDB Z2/Z3/Z4 -> MDB "watch"
    WATCH_NAMES = {2: "watch", 3: "rwatch", 4: "awatch"}

    def __init__(self, mdb, log, elf, hwtool, device):
        self.mdb = mdb
        self.device = device
        self.log = log
        self.elf = elf
        self.hwtool = hwtool  # argument of MDB "hwtool", e.g. "icd3 0"
        self.reset_state()

    def reset_state(self):
        """State of a freshly programmed target, before the first run."""
        self.started = False
        self.running = False
        self.pc = 0xBFC00000
        self.breakpoints = {}  # address -> MDB breakpoint number
        self.watchpoints = {}  # (kind, address) -> (MDB number, length)
        self.stepping = False
        self.regs = None  # GDB registers + CP0 Debug of the current stop
        self.just_loaded = False

    def read_registers(self):
        """The 38 GDB registers followed by CP0 Debug."""
        if self.regs is None:
            t = time.monotonic()
            out = self.mdb.script("regs()")
            line = next((l for l in out if l.startswith("regs ")), None)
            if line is None:
                raise MdbError(f"regs(): {out}")
            self.regs = [int(v, 16) for v in line.split()[1:]]
            self.log.write(f"## regs() {time.monotonic() - t:.2f} s\n")
        return self.regs

    def read_words(self, addr, count):
        t = self.device.memtype(addr)
        if t is None:
            raise MdbError(f"no memory at 0x{addr:08x}")
        words = []
        while count:
            n = min(count, 64)
            out = self.mdb.command(f"x /{t}{n}xw 0x{addr:08x}")
            vals = [int(v, 16) for line in out for v in re.findall(r"\b[0-9a-fA-F]{8}\b", line)]
            if len(vals) != n:
                raise MdbError(f"x at 0x{addr:08x}: {out}")
            words += vals
            addr += 4 * n
            count -= n
        return words

    def read_memory(self, addr, length):
        start = addr & ~3
        end = (addr + length + 3) & ~3
        words = self.read_words(start, (end - start) // 4)
        data = b"".join(w.to_bytes(4, "little") for w in words)
        return data[addr - start:addr - start + length]

    def write_memory(self, addr, data):
        t = self.device.memtype(addr)
        if t not in ("r", "m"):
            raise MdbError(f"write to 0x{addr:08x} not allowed")
        start = addr & ~3
        end = (addr + len(data) + 3) & ~3
        old = bytearray(self.read_memory(start, end - start))
        old[addr - start:addr - start + len(data)] = data
        words = [int.from_bytes(old[i:i + 4], "little") for i in range(0, len(old), 4)]
        out = self.mdb.command(f"write /{t} 0x{start:08x} " + " ".join(f"0x{w:x}" for w in words))
        if any(line.strip() for line in out):
            raise MdbError(f"write: {out}")

    def set_breakpoint(self, addr):
        # GDB sets bit 0 (ISA mode) for MIPS16e and microMIPS code, but not
        # always when it removes the breakpoint; MDB refuses such an address
        addr &= ~1
        if addr in self.breakpoints:
            return
        if len(self.breakpoints) >= self.device.max_breakpoints:
            raise MdbError(f"all {self.device.max_breakpoints} hardware breakpoints in use")
        out = self.mdb.command(f"break *0x{addr:08x}")
        m = re.search(r"Breakpoint (\d+) at", "\n".join(out))  # "-1" does not match
        if not m:
            raise MdbError(f"break: {out}")
        self.breakpoints[addr] = int(m.group(1))

    def remove_breakpoint(self, addr):
        num = self.breakpoints.pop(addr & ~1, None)
        if num is not None:
            self.mdb.command(f"delete {num}")

    def set_watchpoint(self, kind, addr, length):
        if (kind, addr) in self.watchpoints:
            return
        if len(self.watchpoints) >= self.device.max_watchpoints:
            raise MdbError(f"all {self.device.max_watchpoints} data breakpoints in use")
        out = self.mdb.command(f"watch 0x{addr:08x} {self.WATCH_TYPES[kind]}")
        m = re.search(r"Watchpoint (\d+)", "\n".join(out))
        if not m:
            raise MdbError(f"watch: {out}")
        self.watchpoints[(kind, addr)] = (int(m.group(1)), length)

    def remove_watchpoint(self, kind, addr):
        num, _ = self.watchpoints.pop((kind, addr), (None, None))
        if num is not None:
            self.mdb.command(f"delete {num}")

    def remove_all(self):
        """Delete every breakpoint and watchpoint, e.g. when GDB disconnects."""
        if self.breakpoints or self.watchpoints:
            self.mdb.command("delete")
        self.breakpoints.clear()
        self.watchpoints.clear()

    def stop_reason(self):
        """GDB stop reason of the last stop: "", or "watch:addr;" and the like.

        MDB does not say which control point halted the CPU. CP0 Debug says
        whether it was a data breakpoint, on a load or a store. The data
        breakpoint is precise: DEPC is the load/store itself, not executed
        yet (GDB steps over it), or the branch before it when Debug.DBD is
        set. Its data address is base register + offset.
        """
        if self.stepping or not self.watchpoints:
            return ""
        try:
            regs = self.read_registers()
        except MdbError as e:
            self.log.write(f"!! {e}\n")
            return ""
        debug = regs[NUM_REGS]
        if not debug & (DEBUG_DDBL | DEBUG_DDBS):
            return ""
        is_store = bool(debug & DEBUG_DDBS)
        kinds = (2, 4) if is_store else (3, 4)
        candidates = [(k, a, n) for (k, a), (_, n) in self.watchpoints.items() if k in kinds]
        if not candidates:
            candidates = [(k, a, n) for (k, a), (_, n) in self.watchpoints.items()]
        chosen = candidates[0]
        depc = regs[PC_REGNUM]
        # bit 0 of DEPC is the ISA mode: MIPS16e or microMIPS code
        isa = self.device.compressed_isa if depc & 1 else "mips32"
        insn_addr = depc & ~1
        how = "first"
        try:
            if debug & DEBUG_DBD:
                insn_addr += branch_size(isa, self.read_memory(insn_addr, 4))
            access = decode_access(isa, self.read_memory(insn_addr, 4), insn_addr, regs)
            if access and access[0] == is_store:
                _, daddr, size = access
                for k, a, n in candidates:
                    if daddr < a + max(n, 1) and a < daddr + size:
                        chosen = (k, a, n)
                        how = f"decoded 0x{daddr:08x}"
                        break
        except MdbError as e:
            self.log.write(f"!! {e}\n")
        kind, addr, _ = chosen
        self.log.write(f"## watchpoint {self.WATCH_NAMES[kind]} 0x{addr:08x} ({how}), "
                       f"Debug=0x{debug:08x}, DEPC=0x{depc:08x}\n")
        return f"{self.WATCH_NAMES[kind]}:{addr:x};"

    def program(self, elf=None):
        """Program ELF (default: the current one) into the target.

        A second "program" in the same MDB debug session fails ("Failed to
        program device"), a new "hwtool" before it makes it work.
        """
        if elf:
            self.elf = os.path.abspath(elf)
        self.remove_all()
        out = self.mdb.command(f"hwtool {self.hwtool}", timeout=120)
        out += self.mdb.command(f"program {self.elf}", timeout=600)
        self.reset_state()
        if not any("Program succeeded" in line for line in out):
            raise MdbError("programming failed: " + " | ".join(l for l in out if l.strip()))
        return out

    def monitor(self, text):
        words = text.split()
        if words and words[0] == "program":
            t = time.monotonic()
            self.program(words[1] if len(words) > 1 else None)
            return [f"programmed {self.elf} in {time.monotonic() - t:.1f} s"]
        if words == ["reset"]:
            # the target is halted while GDB waits for a monitor command;
            # MDB keeps the breakpoints, "continue" starts from the reset vector
            out = self.mdb.command("reset")
            self.pc = 0xBFC00000
            self.regs = None
            return out + ["GDB still has the old registers: maintenance flush register-cache"]
        return self.mdb.command(text, timeout=600)

    def stop_pc(self):
        """PC of the stop as DEPC has it, with bit 0 set in MIPS16e and
        microMIPS code; MDB "Stop at" gives it without, and GDB then does
        not recognize its breakpoint there."""
        if not self.started:
            return self.pc
        try:
            return self.read_registers()[PC_REGNUM]
        except MdbError as e:
            self.log.write(f"!! {e}\n")
            return self.pc

    def resume(self, step):
        self.regs = None
        self.just_loaded = False
        self.stepping = step
        self.mdb.stopped.clear()
        if step:
            cmd = "stepi"
        elif not self.started:
            cmd = "run"
        else:
            cmd = "continue"
        # stepi before the first run starts the program as well
        self.started = True
        self.running = True
        self.mdb.command(cmd)

    def wait_stop(self, timeout):
        if self.mdb.stopped.wait(timeout):
            self.pc = self.mdb.stop_addr
            self.running = False
            return True
        return False

    def halt(self):
        self.mdb.command("halt")


class GdbConnection:
    def __init__(self, sock, target, log):
        self.sock = sock
        self.target = target
        self.log = log
        self.ack = True
        self.buf = b""

    # --- packet layer ---------------------------------------------------

    def _recv(self, timeout=None):
        if timeout is not None:
            r, _, _ = select.select([self.sock], [], [], timeout)
            if not r:
                return None
        data = self.sock.recv(4096)
        if not data:
            raise ConnectionError("gdb closed the connection")
        return data

    def read_packet(self):
        """Return the next packet payload, or b"\\x03" for an interrupt."""
        while True:
            while self.buf and self.buf[:1] in (b"+", b"-"):
                self.buf = self.buf[1:]
            if self.buf[:1] == b"\x03":
                self.buf = self.buf[1:]
                return b"\x03"
            start = self.buf.find(b"$")
            if start >= 0:
                end = self.buf.find(b"#", start)
                if end >= 0 and len(self.buf) >= end + 3:
                    payload = self.buf[start + 1:end]
                    self.buf = self.buf[end + 3:]
                    if self.ack:
                        self.sock.sendall(b"+")
                    self.log.write(f"-> {payload[:200]!r}\n")
                    return payload
            self.buf += self._recv()

    def send_packet(self, payload):
        if isinstance(payload, str):
            payload = payload.encode()
        escaped = bytearray()
        for b in payload:
            if b in b"#$}*":
                escaped += bytes([0x7D, b ^ 0x20])
            else:
                escaped.append(b)
        csum = sum(escaped) & 0xFF
        self.log.write(f"<- {payload[:200]!r}\n")
        self.log.flush()
        self.sock.sendall(b"$" + bytes(escaped) + b"#" + f"{csum:02x}".encode())

    # --- commands -------------------------------------------------------

    def stop_reply(self, signal=SIGTRAP):
        reason = self.target.stop_reason() if signal == SIGTRAP else ""
        return f"T{signal:02x}{PC_REGNUM:02x}:{le32(self.target.stop_pc())};{reason}"

    def registers(self):
        if not self.target.started:
            # before the first run MDB has no CPU state, only the reset PC
            regs = ["xxxxxxxx"] * NUM_REGS
            regs[PC_REGNUM] = le32(self.target.pc)
            return "".join(regs)
        return "".join(le32(v) for v in self.target.read_registers()[:NUM_REGS])

    def run_target(self, step):
        self.target.resume(step)
        while True:
            if self.target.wait_stop(0.05):
                return self.stop_reply(SIGTRAP)
            data = self._recv(timeout=0.05)
            if data:
                if b"\x03" in data:
                    self.target.halt()
                    if self.target.wait_stop(10):
                        return self.stop_reply(SIGINT)
                    return "E01"
                self.buf += data

    def keepalive(self, func, *args):
        """Run func in a thread and send empty console output ("O") every
        second, so that GDB does not time out (default 2 s) while MDB
        programs. Only valid while GDB waits for a qRcmd reply."""
        result = {}

        def run():
            try:
                result["value"] = func(*args)
            except Exception as e:
                result["error"] = e
        th = threading.Thread(target=run)
        th.start()
        while th.is_alive():
            th.join(1.0)
            if th.is_alive():
                self.send_packet("O")
        if "error" in result:
            raise result["error"]
        return result["value"]

    def qxfer(self, annex_data, offset, length):
        part = annex_data[offset:offset + length]
        return ("l" if offset + length >= len(annex_data) else "m") + part

    def handle(self, pkt):
        cmd = pkt.decode("latin-1")
        try:
            if cmd == "\x03":
                return None
            if cmd.startswith("qSupported"):
                return "PacketSize=4000;QStartNoAckMode+;vContSupported+;qXfer:memory-map:read+"
            if cmd == "QStartNoAckMode":
                self.send_packet("OK")
                self.ack = False
                return None
            if cmd.startswith("qXfer:memory-map:read::"):
                offset, length = (int(v, 16) for v in cmd.split("::")[1].split(","))
                return self.qxfer(self.target.device.memory_map(), offset, length)
            if cmd == "?":
                return self.stop_reply()
            if cmd in ("qAttached",):
                return "1"
            if cmd == "qC":
                return "QC1"
            if cmd == "qfThreadInfo":
                return "m1"
            if cmd == "qsThreadInfo":
                return "l"
            if cmd.startswith("H"):
                return "OK"
            if cmd == "g":
                return self.registers()
            if cmd.startswith("p"):
                n = int(cmd[1:], 16)
                if n >= NUM_REGS:
                    # FPU and other registers GDB knows for MIPS; not read
                    return "xxxxxxxx"
                if not self.target.started:
                    return le32(self.target.pc) if n == PC_REGNUM else "xxxxxxxx"
                return le32(self.target.read_registers()[n])
            if cmd.startswith("P"):
                n, _ = cmd[1:].split("=")
                if int(n, 16) == PC_REGNUM and self.target.just_loaded:
                    # "load" sets the PC to the ELF entry; the target starts
                    # from its reset vector anyway after programming
                    return "OK"
                return "E01"
            if cmd.startswith("G"):
                return "E01"
            if cmd.startswith("m"):
                addr, length = (int(v, 16) for v in cmd[1:].split(","))
                return self.target.read_memory(addr, length).hex()
            if cmd.startswith("M"):
                head, data = cmd[1:].split(":", 1)
                addr, _ = (int(v, 16) for v in head.split(","))
                self.target.write_memory(addr, bytes.fromhex(data))
                return "OK"
            if cmd.startswith("X"):
                head, data = pkt[1:].split(b":", 1)
                addr, length = (int(v, 16) for v in head.decode().split(","))
                data = unescape(data)
                if length:
                    self.target.write_memory(addr, data[:length])
                return "OK"
            if cmd.startswith(("vFlashErase:", "vFlashWrite:")):
                return "OK"
            if cmd == "vFlashDone":
                t = time.monotonic()
                self.target.program()
                self.target.just_loaded = True
                self.log.write(f"## load: programmed {self.target.elf} in {time.monotonic() - t:.1f} s\n")
                return "OK"
            if cmd.startswith(("Z0,", "Z1,")):
                addr = int(cmd.split(",")[1], 16)
                self.target.set_breakpoint(addr)
                return "OK"
            if cmd.startswith(("z0,", "z1,")):
                addr = int(cmd.split(",")[1], 16)
                self.target.remove_breakpoint(addr)
                return "OK"
            if cmd[:3] in ("Z2,", "Z3,", "Z4,"):
                _, addr, length = cmd.split(",")
                self.target.set_watchpoint(int(cmd[1]), int(addr, 16), int(length, 16))
                return "OK"
            if cmd[:3] in ("z2,", "z3,", "z4,"):
                addr = int(cmd.split(",")[1], 16)
                self.target.remove_watchpoint(int(cmd[1]), addr)
                return "OK"
            if cmd.startswith("qRcmd,"):
                text = bytes.fromhex(cmd[6:]).decode()
                out = "\n".join(self.keepalive(self.target.monitor, text)) + "\n"
                self.send_packet("O" + out.encode().hex())
                return "OK"
            if cmd == "vCont?":
                return "vCont;c;C;s;S"
            if cmd.startswith("vCont;"):
                action = cmd[6:7].lower()
                return self.run_target(step=(action == "s"))
            if cmd.startswith(("c", "C")):
                return self.run_target(step=False)
            if cmd.startswith(("s", "S")):
                return self.run_target(step=True)
            if cmd.startswith("D"):
                self.send_packet("OK")
                raise ConnectionError("detach")
            if cmd == "k":
                raise ConnectionError("kill")
        except MdbError as e:
            self.log.write(f"!! {e}\n")
            return "E01"
        return ""

    def serve(self):
        while True:
            pkt = self.read_packet()
            reply = self.handle(pkt)
            if reply is not None:
                self.send_packet(reply)


def unescape(data):
    out = bytearray()
    it = iter(data)
    for b in it:
        if b == 0x7D:
            out.append(next(it) ^ 0x20)
        else:
            out.append(b)
    return bytes(out)


def watch_elf(target, busy, log):
    """Program the ELF again after it changed, when no GDB is connected.

    The file must be unchanged for 2 s, so a linker that is still writing it
    is not caught in the middle.
    """
    def stamp():
        try:
            st = os.stat(target.elf)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None
    programmed = stamp()
    while True:
        time.sleep(1)
        now = stamp()
        if now is None or now == programmed:
            continue
        time.sleep(2)
        if stamp() != now or not busy.acquire(blocking=False):
            continue
        try:
            t = time.monotonic()
            target.program()
            programmed = now
            print(f"ELF changed, programmed {target.elf} in {time.monotonic() - t:.1f} s", flush=True)
        except MdbError as e:
            print(f"ELF changed, programming failed: {e}", flush=True)
            log.write(f"!! {e}\n")
        finally:
            busy.release()


def connect_tool(mdb, spec, helper):
    """Connect the debug tool given by spec ("", "TYPE", "TYPE:SERIAL" or
    ":SERIAL") and load the helper. Returns the argument for "hwtool".

    Without a serial number, "hwtool TYPE" takes the first tool of that
    type. Otherwise the helper lists the connected tools; it can run only
    after some "hwtool", so the simulator is set first (about 3 s). The
    tool must then be unique.
    """
    def hwtool_command(arg):
        out = mdb.command(f"hwtool {arg}", timeout=120)
        if any("did not find" in line for line in out):
            raise MdbError(f"hwtool {arg}: " + " ".join(l.strip() for l in out if l.strip()))

    kind, _, serial = spec.partition(":")
    kind = kind.lower()
    if kind and not serial:
        hwtool = kind
        hwtool_command(hwtool)
        mdb.command(f"LoadScript {helper}", timeout=120)
    else:
        hwtool_command("sim")
        mdb.command(f"LoadScript {helper}", timeout=120)
        found = [line.split(None, 4)[1:] for line in mdb.script("tools()")
                 if line.startswith("tool ")]
        listing = "\n".join(f"  {k}:{s}  {d}" for k, _, s, d in found) or "  none"
        matches = [(k, i, s) for k, i, s, _ in found
                   if k != "?" and (not kind or k == kind) and (not serial or s == serial)]
        if len(matches) != 1:
            what = "no" if not matches else "more than one"
            raise MdbError(f"{what} debug tool matches --tool {spec!r}; connected:\n{listing}")
        kind, index, serial = matches[0]
        hwtool = f"{kind} {index}"
        hwtool_command(hwtool)
    line = next((l for l in mdb.script("tool()") if l.startswith("tool ")), "")
    if "Simulator" in line and kind != "sim" or serial and serial not in line:
        raise MdbError(f"hwtool {hwtool} did not connect the tool ({line or 'no tool'})")
    return hwtool


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3333)
    ap.add_argument("--device", default="PIC32MZ2048ECM100")
    ap.add_argument("--tool", default="",
                    help="debug tool as TYPE[:SERIAL], e.g. icd3 or icd3:JIT123456789; "
                         "default: the only one connected")
    ap.add_argument("--mdb", default=MDB_SH, help="path of mdb.sh of MPLAB X")
    ap.add_argument("--log", default="mdb_gdbserver.log")
    ap.add_argument("--mdb-log", default="mdb_gdbserver.mdb.log")
    ap.add_argument("--once", action="store_true", help="quit after the first GDB connection")
    ap.add_argument("--watch-elf", action="store_true",
                    help="program the ELF again when it changes and no GDB is connected")
    ap.add_argument("elf")
    args = ap.parse_args()

    # a server started in the background by a shell ignores SIGINT; make
    # SIGTERM (and SIGINT) end it cleanly, with MDB quit
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log = open(args.log, "w", buffering=1)
    mdb = Mdb(mdb_sh=args.mdb, log=open(args.mdb_log, "w"))
    t0 = time.monotonic()
    mdb.command("echo start", timeout=300)
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mdbhelper.py")
    for cmd in (f"device {args.device}", "set AutoSelectMemRanges auto"):
        mdb.command(cmd, timeout=120)
    try:
        hwtool = connect_tool(mdb, args.tool, helper)
    except MdbError as e:
        mdb.quit()
        sys.exit(str(e))
    try:
        device = Device(mdb, args.device)
    except MdbError as e:
        mdb.quit()
        sys.exit(str(e))
    print(f"debug tool: hwtool {hwtool}, {device.max_breakpoints} breakpoints, "
          f"{device.max_watchpoints} watchpoints", flush=True)
    elf = os.path.abspath(args.elf)
    out = mdb.command(f"program {elf}", timeout=600)
    if not any("Program succeeded" in line for line in out):
        mdb.quit()
        sys.exit("programming failed:\n" + "\n".join(out))
    print(f"ready after {time.monotonic() - t0:.1f} s, listening on localhost:{args.port}", flush=True)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.port))
    srv.listen(1)
    target = Target(mdb, log, elf, hwtool, device)
    # held while a GDB is connected, so the ELF watcher never programs then
    busy = threading.Lock()
    if args.watch_elf:
        threading.Thread(target=watch_elf, args=(target, busy, log), daemon=True).start()
    try:
        while True:
            sock, peer = srv.accept()
            busy.acquire()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            print(f"gdb connected from {peer}", flush=True)
            log.write(f"## connection from {peer}\n")
            try:
                GdbConnection(sock, target, log).serve()
            except (ConnectionError, OSError) as e:
                print(f"connection closed: {e}", flush=True)
            finally:
                sock.close()
                # the next GDB starts without control points; the target
                # stays halted where it is
                try:
                    if target.running:
                        target.halt()
                        target.wait_stop(10)
                    target.remove_all()
                except MdbError as e:
                    log.write(f"!! {e}\n")
                busy.release()
            if args.once:
                break
    except KeyboardInterrupt:
        pass
    finally:
        mdb.quit()


if __name__ == "__main__":
    main()
