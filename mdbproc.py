"""Drive the stock MPLAB X mdb.sh as a child process over stdin/stdout.

MDB has no machine interface. Commands go to its stdin as text, the output
is parsed. Each command is followed by "echo <marker>", which MDB prints as
"/*<marker>*/" when it has processed everything before it, so the output of
one command is the text up to that marker.

Halts are asynchronous: after "run"/"continue" MDB later prints
"Stop at" with "address:0x..." (and "Target Halted" for a halt by
breakpoint). The reader thread watches every line for them, independent of
the command that is running.
"""
import itertools
import os
import queue
import re
import subprocess
import threading
import time

# default location of an MPLAB X v5.45 install on Linux; the environment
# variable MDB_SH or the --mdb option of mdb_gdbserver.py overrides it
MDB_SH = os.environ.get("MDB_SH", "/opt/microchip/mplabx/v5.45/mplab_platform/bin/mdb.sh")

STOP_ADDR = re.compile(r"^\s*address:\s*0x([0-9a-fA-F]+)")


class MdbError(Exception):
    pass


class Mdb:
    def __init__(self, mdb_sh=MDB_SH, log=None, cwd=None):
        self.log = log
        self.proc = subprocess.Popen(
            [mdb_sh], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, cwd=cwd, bufsize=0)
        self.lines = queue.Queue()
        self.marker = itertools.count(1)
        self.lock = threading.Lock()
        # stop events: set when MDB reports a stop, with the stop address
        self.stopped = threading.Event()
        self.stop_addr = None
        self._expect_addr = False
        # "@@" lines printed by the Jython helper (mdbhelper.py)
        self.script_lines = queue.Queue()
        self.reader =threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _log(self, direction, text):
        if self.log:
            self.log.write(f"{time.monotonic():10.3f} {direction} {text}\n")
            self.log.flush()

    def _read(self):
        buf = b""
        while True:
            chunk = self.proc.stdout.read1(4096) if hasattr(self.proc.stdout, "read1") \
                else os.read(self.proc.stdout.fileno(), 4096)
            if not chunk:
                self.lines.put(None)
                return
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").rstrip("\r")
                # the prompt ">" is printed without a newline before the next output
                line = line.lstrip(">")
                self._log("<", line)
                if not self._watch(line):
                    self.lines.put(line)

    def _watch(self, line):
        """Note stop events; return True for lines that only the helper uses."""
        if line.startswith("@@"):
            self.script_lines.put(line[2:])
            return True
        if line.strip() == "Stop at":
            self._expect_addr = True
            return False
        m = STOP_ADDR.match(line)
        if m and self._expect_addr:
            self._expect_addr = False
            self.stop_addr = int(m.group(1), 16)
            self.stopped.set()
        return False

    def command(self, text, timeout=60):
        """Run one MDB command and return its output lines."""
        with self.lock:
            n = next(self.marker)
            mark = f"__mdb_{n}__"
            self._log(">", text)
            self.proc.stdin.write(f"{text}\necho {mark}\n".encode())
            self.proc.stdin.flush()
            out = []
            end = time.monotonic() + timeout
            while True:
                try:
                    line = self.lines.get(timeout=max(0.0, end - time.monotonic()))
                except queue.Empty:
                    raise MdbError(f"timeout in {text!r}, output so far: {out}")
                if line is None:
                    raise MdbError(f"mdb exited during {text!r}, output: {out}")
                if f"/*{mark}*/" in line:
                    return out
                out.append(line)

    def script(self, expr, timeout=30):
        """Evaluate a helper call with "Execute" and return its "@@" lines.

        The helper prints its result after MDB has already echoed the
        marker, so the lines are collected up to "@@end".
        """
        while not self.script_lines.empty():
            self.script_lines.get_nowait()
        self.command(f"Execute {expr}", timeout)
        out = []
        end = time.monotonic() + timeout
        while True:
            try:
                line = self.script_lines.get(timeout=max(0.0, end - time.monotonic()))
            except queue.Empty:
                raise MdbError(f"no result from {expr!r}: {out}")
            if line == "end":
                return out
            out.append(line)

    def quit(self):
        try:
            self.proc.stdin.write(b"quit\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=30)
        except Exception:
            self.proc.kill()
