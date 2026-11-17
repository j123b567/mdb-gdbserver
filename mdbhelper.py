# Jython 2.7 helper loaded into MDB with "LoadScript". MDB's "Execute <expr>"
# evaluates an expression in the same interpreter, so the functions here are
# called as "Execute regs()". Results go to System.out as lines starting with
# "@@", which mdbproc.py picks out of the MDB output.
from java.lang import System
from com.microchip.mplab.mdb.debugapi import CommandInterpreter
from com.microchip.mplab.mdbcore.registerview.interfaces import RegisterViewProvider

_MemType = RegisterViewProvider.eRegisterMemType
_Filter = RegisterViewProvider.eRegisterMemFilter


def _out(text):
    System.out.println("@@" + text)
    System.out.flush()


def _assembly():
    ci = CommandInterpreter.getCommandInterpreter()
    field = ci.getClass().getDeclaredField("debugger")
    field.setAccessible(True)
    return field.get(ci).getAssembly()


def _rv():
    return _assembly().getLookup().lookup(RegisterViewProvider)


GPR = ["zero", "at", "v0", "v1", "a0", "a1", "a2", "a3",
       "t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7",
       "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7",
       "t8", "t9", "k0", "k1", "gp", "sp", "fp", "ra"]
# GDB's MIPS register numbers 0..37: r0..r31, sr, lo, hi, bad, cause, pc.
# While the CPU is in debug mode, DEPC holds the PC to resume at.
GDB_REGS = GPR + ["Status", "lo", "hi", "BadVAddr", "Cause", "DEPC"]

_cache = {}


def _reg(name):
    r = _cache.get(name)
    if r is None:
        regs = _rv().getInitializedRegisters(_Filter.eAllRegisters)
        r = regs.FindName(name)
        _cache[name] = r
    return r


def _cmd_tool_name(name):
    """MDB's "hwtool" type for a tool name ("ICD 3" -> "icd3"), as ToolFinder
    in lib/mdb.jar derives it."""
    n = name.lower().replace(" ", "").replace("-", "")
    return {"simulator": "sim", "starterkits(pkob)": "sk",
            "curiosity/starterkits(pkob4)": "pkob4"}.get(n, n)


def tools():
    """Print the connected tools as "tool TYPE INDEX SERIAL DESCRIPTION".

    "hwtool TYPE INDEX" selects the tool. MDB's ToolFinder lists the USB
    tools of the vendor IDs 04D8 and 03EB, and INDEX counts only the tools
    whose ID contains the product ID of TYPE, in the order of that list.
    This repeats it, so the index is the one MDB uses.
    """
    try:
        from org.openide.util import Lookup
        from com.microchip.mplab.comm import MPLABCommProviderInterface
        from com.microchip.mplab.mdbcore.platformtool import PlatformToolMetaManager
        comm = Lookup.getDefault().lookup(MPLABCommProviderInterface)
        found = comm.GetCurrentToolList(None, "USB", "04D8", None)
        other = comm.GetCurrentToolList(None, "USB", "03EB", None)
        if other is not None and not other.isEmpty():
            found.putAll(other)
        pids = {}  # product ID without "0x" -> hwtool type
        for meta in PlatformToolMetaManager.getAllTools():
            pid = meta.getUSBProductID()
            if pid and pid.startswith("0x"):
                pids[pid[2:]] = _cmd_tool_name(meta.getName())
        ids = [found.get(k) for k in found.keySet()]
        for tid in ids:
            fields = tid.split(":=")
            serial = fields[7] if len(fields) > 7 else "?"
            descr = fields[6] if len(fields) > 6 else ""
            kind, index = "?", 0
            for pid, name in pids.items():
                if pid.upper() == fields[3].upper():
                    kind = name
                    index = [t for t in ids if pid in t].index(tid)
            _out("tool %s %d %s %s" % (kind, index, serial, descr))
    except Exception, e:
        _out("error %s" % e)
    _out("end")


def tool():
    """Print the tool MDB is connected to: "tool CLASS ID"."""
    try:
        a = _assembly()
        _out("tool %s %s" % (a.GetToolClass(), a.GetToolID()))
    except Exception, e:
        _out("error %s" % e)
    _out("end")


def caps():
    """Print the number of instruction and data breakpoints of the tool on
    this device: "caps INSTRUCTION DATA"."""
    try:
        from com.microchip.mplab.mdbcore.ControlPointMediator import ControlPointMediator
        c = _assembly().getLookup().lookup(ControlPointMediator).getEmulationCapabilities()
        _out("caps %d %d" % (c.getMaxInstructionBPs(), c.getMaxDataBPs()))
    except Exception, e:
        _out("error %s" % e)
    _out("end")


def memmap():
    """Print the physical memory ranges of the device (EDC of the device
    pack): "memmap KIND BEGIN END" for program flash, boot flash, RAM and
    SFRs, END exclusive."""
    try:
        part = _assembly().GetDevice().getMainPartition()
        for kind, getter in (("code", part.getCodeRange), ("boot", part.getBootConfigRange),
                             ("ram", part.getGPRRange), ("sfr", part.getSFRRange)):
            r = getter()
            if r is not None and r.second > r.first:
                _out("memmap %s %x %x" % (kind, r.first, r.second))
    except Exception, e:
        _out("error %s" % e)
    _out("end")


def regs():
    """Print the 38 GDB registers as hex words, in GDB order, then CP0 Debug."""
    try:
        rv = _rv()
        vals = []
        for name in GDB_REGS + ["Debug"]:
            r = rv.readSFRValue(_reg(name), True)
            vals.append("%08x" % (r.getCurrentValue() & 0xffffffff))
        _out("regs " + " ".join(vals))
    except Exception, e:
        _out("error %s" % e)
    _out("end")


def debugregs():
    """Print the EJTAG related CP0 registers Debug and Debug2."""
    try:
        rv = _rv()
        vals = []
        for name in ("Debug", "Debug2", "DEPC"):
            r = rv.readSFRValue(_reg(name), True)
            vals.append("%s=%08x" % (name, r.getCurrentValue() & 0xffffffff))
        _out(" ".join(vals))
    except Exception, e:
        _out("error %s" % e)
    _out("end")


def setreg(num, value):
    """Write GDB register number num."""
    try:
        r = _reg(GDB_REGS[num])
        r.setCurrentValue(value & 0xffffffff)
        _rv().writeSFRValue(r)
        _out("ok")
    except Exception, e:
        _out("error %s" % e)
    _out("end")


def setreg2(num, value):
    """Write GDB register number num through the memory model."""
    try:
        from com.microchip.mplab.mdbcore.memory import MemoryModel
        mm = _assembly().getLookup().lookup(MemoryModel)
        r = _reg(GDB_REGS[num])
        r.setCurrentValue(value & 0xffffffff)
        r.WriteValue(mm)
        _out("ok")
    except Exception, e:
        _out("error %s" % e)
    _out("end")


def models():
    """Print every MemoryModel in the lookup, with its word at 0x8 (v0)."""
    try:
        from com.microchip.mplab.mdbcore.memory import MemoryModel
        for mm in _assembly().getLookup().lookupAll(MemoryModel):
            _out("%s %s" % (mm.getClass().getName(),
                            " ".join(i.getName() for i in mm.getClass().getInterfaces())))
    except Exception, e:
        _out("error %s" % e)
    _out("end")


def regmodel_write(cls, addr, value):
    """Write a word to the physical memory of the MemoryModel named cls."""
    try:
        from com.microchip.mplab.mdbcore.memory import MemoryModel
        for mm in _assembly().getLookup().lookupAll(MemoryModel):
            if mm.getClass().getName().endswith(cls):
                mm.GetPhysicalMemory().WriteWord(addr, value & 0xffffffff)
                _out("ok %s" % mm.getClass().getName())
    except Exception, e:
        _out("error %s" % e)
    _out("end")


def listregs():
    """Print every register that is not a peripheral SFR."""
    try:
        regs = _rv().getInitializedRegisters(_Filter.eAllRegisters)
        for r in regs.getAllRegisters():
            t = r.getMemoryType()
            if t != _MemType.ePeripheral:
                _out("%s %s 0x%x %d" % (r.getName(), t, r.getAddress(), r.getByteLen()))
        _out("end")
    except Exception, e:
        _out("error %s" % e)
