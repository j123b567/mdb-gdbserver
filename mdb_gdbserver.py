#!/usr/bin/env python3
"""GDB remote serial protocol (RSP) server on top of the stock MPLAB X MDB.

Usage: mdb_gdbserver.py [--port 3333] [--device PIC32MZ2048ECM100] [--mdb MDB_SH] ELF

Starts mdb.sh as a child process, connects the ICD 3 in debug mode,
programs ELF and then serves GDB connections on localhost:PORT, one after
the other. MDB keeps running between connections, so only the first
connection pays its start (about 30 s).

Supported:
  - stop reason (?), continue (c, vCont;c), single instruction step (s,
    vCont;s), interrupt (Ctrl-C), detach/kill (the server stays up)
  - breakpoints Z0/Z1 (both as hardware breakpoints, MDB "break *addr"),
    watchpoints Z2/Z3/Z4 (MDB "watch"), with the watchpoint that fired
    found from CP0 Debug and the load/store before DEPC
  - memory read (m) of RAM, program flash, boot flash and SFRs, memory
    write (M, X) of RAM and SFRs; a memory map tells GDB where flash is
  - GDB "load": vFlashErase/vFlashWrite are accepted, vFlashDone programs
    ELF (the file given on the command line, read again from disk) with
    "hwtool ICD3" + "program"; the data sent by GDB is not used
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

# hardware resources of the PIC32MZ EC with the ICD 3 (ICD3DbgToolManager.emu
# in emu.jar of MPLAB X, engine ejtagonlyiflowtrace2: maxprogbps="8"
# maxdatabps="4"). MDB must never be asked for more: after one refused
# "break" ("Breakpoint -1 at ...") it refuses every later breakpoint of the
# session, even with none set.
MAX_BREAKPOINTS = 8
MAX_WATCHPOINTS = 4

# CP0 Debug bits (MIPS EJTAG)
DEBUG_DSS = 1 << 0   # single step
DEBUG_DBP = 1 << 1   # SDBBP instruction
DEBUG_DDBL = 1 << 2  # data breakpoint, load
DEBUG_DDBS = 1 << 3  # data breakpoint, store
DEBUG_DIB = 1 << 4   # instruction breakpoint
DEBUG_DBD = 1 << 31  # the halt is in a branch delay slot, DEPC is the branch

# memory map of the PIC32MZ2048EC for GDB: flash makes GDB use hardware
# breakpoints there and "load" use vFlashErase/vFlashWrite/vFlashDone.
# GDB keeps MIPS addresses sign-extended to 64 bits (0x9d000000 is
# 0xffffffff9d000000), the map must use the same form or GDB refuses every
# access as outside the map.
MEMORY_MAP = """<?xml version="1.0"?>
<!DOCTYPE memory-map PUBLIC "+//IDN gnu.org//DTD GDB Memory Map V1.0//EN" "http://sourceware.org/gdb/gdb-memory-map.dtd">
<memory-map>
  <memory type="ram" start="0xffffffff80000000" length="0x80000"/>
  <memory type="ram" start="0xffffffffa0000000" length="0x80000"/>
  <memory type="flash" start="0xffffffff9d000000" length="0x200000"><property name="blocksize">0x4000</property></memory>
  <memory type="flash" start="0xffffffffbd000000" length="0x200000"><property name="blocksize">0x4000</property></memory>
  <memory type="flash" start="0xffffffff9fc00000" length="0x74000"><property name="blocksize">0x4000</property></memory>
  <memory type="flash" start="0xffffffffbfc00000" length="0x74000"><property name="blocksize">0x4000</property></memory>
  <memory type="ram" start="0xffffffffbf800000" length="0x100000"/>
</memory-map>
"""


def le32(value):
    return value.to_bytes(4, "little").hex()


def memtype(addr):
    """MDB memory type letter for a virtual address, None if not handled."""
    phys = addr & 0x1FFFFFFF
    if phys < 0x00080000:
        return "r"
    if 0x1D000000 <= phys < 0x1D200000 or 0x1FC00000 <= phys < 0x1FC80000:
        return "p"
    if 0x1F800000 <= phys < 0x1F900000:
        return "m"
    return None


def decode_access(insn):
    """Decode a MIPS32 load/store: (is_store, base register, offset, size).

    Returns None for other instructions.
    """
    op = insn >> 26
    sizes = {0x20: 1, 0x24: 1, 0x21: 2, 0x25: 2, 0x23: 4, 0x22: 4, 0x26: 4, 0x30: 4,
             0x28: 1, 0x29: 2, 0x2B: 4, 0x2A: 4, 0x2E: 4, 0x38: 4}
    if op not in sizes:
        return None
    base = (insn >> 21) & 0x1F
    offset = insn & 0xFFFF
    if offset & 0x8000:
        offset -= 0x10000
    is_store = op in (0x28, 0x29, 0x2B, 0x2A, 0x2E, 0x38)
    rt = (insn >> 16) & 0x1F
    if not is_store and rt == base:
        return None  # the load has overwritten its base register
    return is_store, base, offset, sizes[op]


class Target:
    """Debug operations of the PIC32 through MDB."""

    WATCH_TYPES = {2: "W", 3: "R", 4: "RW"}  # GDB Z2/Z3/Z4 -> MDB "watch"
    WATCH_NAMES = {2: "watch", 3: "rwatch", 4: "awatch"}

    def __init__(self, mdb, log, elf):
        self.mdb = mdb
        self.log = log
        self.elf = elf
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
        t = memtype(addr)
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
        t = memtype(addr)
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
        if addr in self.breakpoints:
            return
        if len(self.breakpoints) >= MAX_BREAKPOINTS:
            raise MdbError(f"all {MAX_BREAKPOINTS} hardware breakpoints in use")
        out = self.mdb.command(f"break *0x{addr:08x}")
        m = re.search(r"Breakpoint (\d+) at", "\n".join(out))  # "-1" does not match
        if not m:
            raise MdbError(f"break: {out}")
        self.breakpoints[addr] = int(m.group(1))

    def remove_breakpoint(self, addr):
        num = self.breakpoints.pop(addr, None)
        if num is not None:
            self.mdb.command(f"delete {num}")

    def set_watchpoint(self, kind, addr, length):
        if (kind, addr) in self.watchpoints:
            return
        if len(self.watchpoints) >= MAX_WATCHPOINTS:
            raise MdbError(f"all {MAX_WATCHPOINTS} data breakpoints in use")
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
        insn_addr = depc + 4 if debug & DEBUG_DBD else depc
        how = "first"
        if not depc & 1:  # MIPS32, not microMIPS
            try:
                insn = self.read_words(insn_addr, 1)[0]
                access = decode_access(insn)
                if access and access[0] == is_store:
                    _, base, offset, size = access
                    daddr = (regs[base] + offset) & 0xFFFFFFFF
                    for k, a, n in candidates:
                        if daddr < a + max(n, 1) and a < daddr + size:
                            chosen = (k, a, n)
                            how = f"decoded 0x{daddr:08x}"
                            break
            except MdbError as e:
                self.log.write(f"!! {e}\n")
        kind, addr, _ = chosen
        self.log.write(f"## watchpoint {self.WATCH_NAMES[kind]} 0x{addr:08x} ({how}), Debug=0x{debug:08x}\n")
        return f"{self.WATCH_NAMES[kind]}:{addr:x};"

    def program(self, elf=None):
        """Program ELF (default: the current one) into the target.

        A second "program" in the same MDB debug session fails ("Failed to
        program device"), a new "hwtool ICD3" before it makes it work.
        """
        if elf:
            self.elf = os.path.abspath(elf)
        self.remove_all()
        out = self.mdb.command("hwtool ICD3", timeout=120)
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
        return f"T{signal:02x}{PC_REGNUM:02x}:{le32(self.target.pc)};{reason}"

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
                return self.qxfer(MEMORY_MAP, offset, length)
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
                    # FPU and other registers GDB knows for MIPS, the PIC32MZ EC
                    # has no FPU
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3333)
    ap.add_argument("--device", default="PIC32MZ2048ECM100")
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
    for cmd in (f"device {args.device}", "set AutoSelectMemRanges auto", "hwtool ICD3",
                f"LoadScript {helper}"):
        mdb.command(cmd, timeout=120)
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
    target = Target(mdb, log, elf)
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
