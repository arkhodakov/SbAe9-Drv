#!/usr/bin/env python3
"""Read the AE-9 DAC and codec state through the SbAe9-Drv debugfs file (root).

The 3xHelix driver only writes the two DAC devices on the CA0113 command engine:
the ES9038Q2M at group 0x48 and the second device at group 0x49. This tool reads
them back with our own driver's read sequence (driver/ca0132/ca0132.c,
ca0113_mmio_command_get; it read the expected values on this card on
2026-11-09), sent as the driver's "w2"/"r2" debugfs commands. The codec state
comes from "verb" GET commands, whose replies the driver writes to the kernel
log.

  engine --card N             the command engine's registers, one traced DAC
                              read, the I2S and power registers (ChipIO), codec
                              GPIO and codec 2; read-only, refused while a
                              stream of card N runs
  pin0 --card N               Windows' GPIO pin 0 pulse (0, 15 ms, then 1), then
                              whether the DAC answers; if it does, both DAC
                              devices are muted as Windows does next. Refused
                              while a stream runs or when the DAC already answers
  dump --master M             DAC registers and codec state; read-only
  probe --card N --master M   while a steady tone plays on card N: the key DAC
                              registers, then the automute probe. DAC registers
                              0x02, 0x04 and 0x05 change for 0.4 s and are
                              restored to the values read before.
  tone --out DIR              write the steady tone for the probe

M is the raw Master Playback Volume (182 = -36 dB): the DAC volume registers
should hold the attenuation 254 - M, in 0.5 dB steps.

The debugfs commands bypass the driver's DAC lock: run this only while nothing
else changes the DAC (no knob, no output switch, no stream start or stop).
"""

import argparse
import math
import os
import re
import struct
import sys
import time
import wave
from pathlib import Path

DEBUGFS = "/sys/kernel/debug/creative_ae9/cmd"
KMSG = "/dev/kmsg"
PCM_STATUS = "/proc/asound/card{card}/pcm0p/sub0/status"

STEADY_NAME = "ae9-steady-m40dbfs.wav"
STEADY_LEVEL = -40
STEADY_SECONDS = 12
RATE = 48000
FREQUENCY = 1000
RAMP = RATE * 50 // 1000

REPLY_R2 = re.compile(r"r2 0x([0-9a-f]+) = 0x([0-9a-f]+)")
REPLY_W2 = re.compile(r"w2 0x([0-9a-f]+) <= 0x([0-9a-f]+) \(rb 0x([0-9a-f]+)\)")
REPLY_CHIPIO = re.compile(r"chipio\[0x([0-9a-f]+)\]=0x([0-9a-f]+) ret=(-?\d+)")
KLOG_VERB = re.compile(r"dbg verb 0x([0-9a-f]{8}) -> 0x([0-9a-f]{8}) \((-?\d+)\)")
STREAM_STATUS = "/proc/asound/card{card}"

# Windows' engine lock (docs/reverse-engineering/ctxhda-acm-analysis.md 11.1):
# 0x210 = 7e, 5a, then the register must read aa, or the driver gives up.
UNLOCKED = 0xAA
DONE = 1 << 23  # 0x20c bit 23: the transfer finished (Windows polls it)

# ca0113 GPIO: BAR2 0x320, a 16-bit command "pin | value << 8". Windows' first
# D0 step on this card (CtxHda 0x35de4, once per start; our capture
# docs/captures/ae9-20260903-210512.trace.ops.txt lines 1280-1283): pin 5 = 1,
# pin 0 = 0, 1 ms (15 ms in the capture), pin 0 = 1, then the first DAC access.
# Its post-DSP block, which 3xHelix replays, has no pin 0: pin 0 is already 1.
# 3xHelix never sets pin 0 = 1, and its shutdown and suspend set it to 0.
GPIO = 0x320
PIN0_LOW = 0x0000
PIN0_HIGH = 0x0100
PULSE = 0.015

# ES9038Q2M automute probe (driver/ca0132/ca0132.c, ae9_automute_probe):
# level -60 dB, time ~0.17 s, mute the automuted channels.
AUTOMUTE_LEVEL = 0x3C
AUTOMUTE_TIME = 0xFF
AUTOMUTE_MODE = 0x40
# The probe writes only over these values: the ES9038Q2M defaults of 0x02 and
# 0x05 (the driver never writes them) and the driver's 0x04 = 0x00. Any other
# reading means the reads cannot be trusted, and nothing is written.
PROBE_SAVED = {0x02: 0x34, 0x04: 0x00, 0x05: 0x68}
CHIP_ID = 0x1C  # register 0x40 bits 7:2; read 0x71 on this card on 2026-11-09


def attenuation(master):
    """DAC volume register value for a raw Master Playback Volume (0..254)."""
    if not 0 <= master <= 254:
        raise ValueError(f"Master must be 0..254, not {master}")
    return 254 - master


def dac_registers(volume):
    """(group, register, meaning, (mask, value) expected on headphones, or None)."""
    full = 0xFF
    rows = [
        (0x48, 0x01, "input select", (full, 0xC0)),
        (0x48, 0x02, "serial format, automute mode", (full, 0x34)),
        (0x48, 0x04, "automute time (0 = off)", (full, 0x00)),
        (0x48, 0x05, "automute level", (full, 0x68)),
        (0x48, 0x06, "volume ramp", (full, 0x40)),
        (0x48, 0x07, "filter; bit 0 = mute", (0x01, 0x00)),
        (0x48, 0x08, "GPIO 1-2", (full, 0xFF)),
        (0x48, 0x0A, "master mode", (full, 0x02)),
        (0x48, 0x0B, "", (full, 0x20)),
        (0x48, 0x0C, "DPLL bandwidth", (full, 0x5F)),
        (0x48, 0x0D, "output table", (full, 0x00)),
        (0x48, 0x0E, "soft start", (full, 0x8A)),
        (0x48, 0x0F, "volume 1", (full, volume)),
        (0x48, 0x10, "volume 2", (full, volume)),
        (0x48, 0x11, "master trim", (full, 0xFF)),
        (0x48, 0x12, "master trim", (full, 0xFF)),
        (0x48, 0x13, "master trim", (full, 0xFF)),
        (0x48, 0x14, "master trim", (full, 0x7F)),
        (0x48, 0x15, "GPIO input, volume mode", None),
        (0x48, 0x16, "output table", (full, 0x00)),
        (0x48, 0x17, "output table", (full, 0x00)),
        (0x48, 0x18, "output table", (full, 0x00)),
        (0x48, 0x19, "output table (headphones 01)", (full, 0x01)),
        (0x48, 0x1D, "line-out mute (headphones 80)", (full, 0x80)),
        (0x48, 0x40, "chip ID; bit 1 automute, bit 0 lock", (0x01, 0x01)),
        (0x48, 0x41, "DPLL ratio", None),
        (0x48, 0x42, "DPLL ratio", None),
        (0x48, 0x43, "DPLL ratio", None),
        (0x48, 0x44, "DPLL ratio", None),
        (0x49, 0x00, "", (full, 0xFF)),
        (0x49, 0x01, "", (full, 0xFF)),
        (0x49, 0x02, "volume", (full, volume)),
        (0x49, 0x03, "volume", (full, volume)),
        (0x49, 0x04, "volume", (full, volume)),
        (0x49, 0x05, "volume", (full, volume)),
        (0x49, 0x06, "", (full, 0xFF)),
        (0x49, 0x07, "", (full, 0xFF)),
        (0x49, 0x0A, "mute (06 on, 07 muted)", (full, 0x06)),
        (0x49, 0x0E, "", None),
        (0x49, 0x10, "", (full, 0x7F)),
        (0x49, 0x18, "", (full, 0xFF)),
        (0x49, 0x19, "", (full, 0xFF)),
        (0x49, 0x1A, "", (full, 0x00)),
        (0x49, 0x1B, "", (full, 0x70)),
    ]
    return rows


KEY_REGISTERS = ((0x48, 0x01), (0x48, 0x07), (0x48, 0x0A), (0x48, 0x0F), (0x48, 0x10),
                 (0x48, 0x19), (0x48, 0x1D), (0x48, 0x40), (0x48, 0x41), (0x48, 0x42),
                 (0x48, 0x43), (0x48, 0x44), (0x49, 0x02), (0x49, 0x04), (0x49, 0x0A))


def verb(nid, command, payload=0, cad=1):
    """A codec verb: 12-bit commands (0xf..) take an 8-bit payload, 4-bit ones 16 bits."""
    if command > 0xF:
        return cad << 28 | nid << 20 | command << 8 | payload
    return cad << 28 | nid << 20 | command << 16 | payload


# (meaning, verb, expected reply on headphones or None)
CODEC_VERBS = (
    ("AFG power state", verb(0x01, 0xF05), None),
    ("DAC 0x02 stream, channel", verb(0x02, 0xF06), None),
    ("DAC 0x02 format", verb(0x02, 0xA), None),
    ("DAC 0x02 amp out left", verb(0x02, 0xB, 0xA000), 0x5A),
    ("DAC 0x02 amp out right", verb(0x02, 0xB, 0x8000), 0x5A),
    ("DAC 0x02 power state", verb(0x02, 0xF05), None),
    ("DAC 0x03 stream, channel", verb(0x03, 0xF06), None),
    ("DAC 0x04 stream, channel", verb(0x04, 0xF06), None),
    ("pin 0x0b control", verb(0x0B, 0xF07), 0x00),
    ("pin 0x0f control", verb(0x0F, 0xF07), 0x00),
    ("pin 0x10 control", verb(0x10, 0xF07), 0xC0),
    ("pin 0x10 EAPD", verb(0x10, 0xF0C), None),
    ("pin 0x10 connection", verb(0x10, 0xF01), None),
    ("pin 0x10 power state", verb(0x10, 0xF05), None),
)

# The CA0113 command engine and its neighbours in BAR2: (offset, meaning,
# (mask, value) after Windows' init, or None). Windows reads every one of them
# (read-modify-write or status), so reading them changes nothing.
# Sources: docs/reverse-engineering/ctxhdb-analysis.md B4a-B4d (HdIs, HdIc,
# HdUa init), ctxhda-acm-analysis.md 11.1, driver/ca0132/ca0132.c (0x1c).
ENGINE_REGISTERS = (
    (0x004, "card revision", None),
    (0x01C, "hardware control (Windows 00880480; bit 9 = codec 2 off)", (0x00880280, 0x00880080)),
    (0x100, "I2S enables and card pins", None),
    (0x104, "I2S channel setup", None),
    (0x304, "card pin directions", (0xFF, 0xFF)),
    (0x20C, "engine control (bit 23 = transfer done)", (DONE, DONE)),
    (0x210, "engine lock (aa = unlocked)", None),
    (0x800, "engine mode (Windows 6b)", (0x27, 0x23)),
    (0x804, "engine address (bit 12 = 0)", (0x1000, 0x0000)),
    (0x830, "engine setup (Windows sets 240)", (0x240, 0x240)),
    (0x840, "engine status", None),
    (0x854, "engine status", None),
    (0x860, "engine status", None),
    (0x86C, "engine enable (bit 0)", (0x1, 0x1)),
    (0x89C, "engine enable acknowledged (bit 0)", (0x1, 0x1)),
)

# The card's GPIO block (HdGp, sb-ae9 docs/reverse-engineering/
# ctxhda-acm-analysis.md 3.1): which register holds a pin's level is not
# known, so all of them are read. 0x320 takes commands (pin | value << 8)
# and reads back the last one, not the pin levels.
GPIO_REGISTERS = (
    (0x300, "bit per pin (HdGp +0x78/+0x80)"),
    (0x304, "pin enables, byte (+0x28/+0x30; Windows sets ff)"),
    (0x308, "bytes 0x308/0x309, bit per pin (+0x38..+0x50)"),
    (0x30C, "1 << pin (+0x68/+0x70)"),
    (0x310, "16 bits per pin: pins 0 and 1 (+0x58/+0x60)"),
    (0x314, "16 bits per pin: pins 2 and 3"),
    (0x318, "16 bits per pin: pins 4 and 5"),
    (0x31C, "16 bits per pin: pins 6 and 7"),
    (0x320, "command register (reads the last command)"),
)

# ChipIO (codec 1 memory): the I2S clocks and the power registers that
# 3xHelix's scripts/ae9-full-dump.sh reads, idle only. 0x18902c = f and
# 0x189030 = 81 are what its author reads on a healthy AE-9 (2026-11-06, idle).
CHIPIO_REGISTERS = (
    (0x189000, "I2S clock descriptor (48 kHz start: 0001f100)", None),
    (0x189004, "I2S clock descriptor (48 kHz start: 0001f100)", None),
    (0x189008, "I2S clock descriptor (48 kHz start: 0001f100)", None),
    (0x189024, "I2S rate (48 kHz start: 00008005)", None),
    (0x189028, "I2S", None),
    (0x18902C, "I2S output check (healthy AE-9: f)", (0xFFFFFFFF, 0xF)),
    (0x189030, "I2S output check (healthy AE-9: 81)", (0xFFFFFFFF, 0x81)),
    (0x18B008, "stream start (bit 3 cleared at every start)", None),
    (0x18B030, "PMR1 (bit 4 PDSNS, bit 3 SNSP)", None),
    (0x18B0A4, "power (bit 1 set at codec start-up)", (0x2, 0x2)),
)

# (meaning, verb, expected reply or None)
ENGINE_VERBS = (
    ("codec 1 GPIO data", verb(0x01, 0xF15), None),
    ("codec 1 GPIO enable", verb(0x01, 0xF16), None),
    ("codec 1 GPIO direction", verb(0x01, 0xF17), None),
    ("codec 2 vendor ID", verb(0x00, 0xF00, 0x00, cad=2), 0x11020011),
    ("codec 2 AFG power state", verb(0x01, 0xF05, cad=2), None),
    ("codec 2 GPIO data", verb(0x01, 0xF15, cad=2), None),
    ("codec 2 GPIO enable", verb(0x01, 0xF16, cad=2), None),
    ("codec 2 GPIO direction", verb(0x01, 0xF17, cad=2), None),
)


class Debugfs:
    """The driver's command file: write one command, then read its reply."""

    def __init__(self, path=DEBUGFS, sleep=time.sleep):
        self.path = path
        self.sleep = sleep

    def command(self, text):
        fd = os.open(self.path, os.O_WRONLY)
        try:
            os.write(fd, text.encode() + b"\n")
        finally:
            os.close(fd)
        fd = os.open(self.path, os.O_RDONLY)
        try:
            return os.read(fd, 256).decode(errors="replace").strip()
        finally:
            os.close(fd)

    def r2(self, offset):
        reply = self.command(f"r2 0x{offset:x}")
        match = REPLY_R2.fullmatch(reply)
        if not match or int(match[1], 16) != offset:
            raise RuntimeError(f"unexpected reply to r2 0x{offset:x}: {reply!r}")
        return int(match[2], 16)

    def w2(self, offset, value):
        """Write, then return the driver's read-back of the same register."""
        reply = self.command(f"w2 0x{offset:x} 0x{value:x}")
        match = REPLY_W2.fullmatch(reply)
        if not match or int(match[1], 16) != offset or int(match[2], 16) != value:
            raise RuntimeError(f"unexpected reply to w2 0x{offset:x} 0x{value:x}: {reply!r}")
        return int(match[3], 16)

    def chipior(self, address):
        reply = self.command(f"chipior {address:x}")
        match = REPLY_CHIPIO.fullmatch(reply)
        if not match or int(match[1], 16) != address:
            raise RuntimeError(f"unexpected reply to chipior {address:x}: {reply!r}")
        if match[3] != "0":
            raise RuntimeError(f"chipior {address:x} failed: {reply!r}")
        return int(match[2], 16)

    def verb(self, value):
        self.command(f"verb {value:08x}")


def _status(dbg):
    return [dbg.r2(offset) for offset in (0x860, 0x854, 0x840)]


def _phase(dbg, trace, label):
    """The 20 ms wait after a transfer starts. Traced, 0x20c is read right
    after the start and after the wait, as Windows' poll reads it."""
    early = dbg.r2(0x20C) if trace is not None else None
    dbg.sleep(0.02)
    late = dbg.r2(0x20C) if trace is not None else None
    status = _status(dbg)
    if trace is not None:
        trace.append(f"{label}: 0x20c {early:08x} right after the start, {late:08x} after 20 ms; "
                     f"status 860={status[0]:x} 854={status[1]:x} 840={status[2]:x}")
    return early, late


def dac_read(dbg, group, register, trace=None):
    """One DAC register over the CA0113 command engine (ca0113_mmio_command_get).

    With a trace list, it also records the engine's replies, and reads 0x20c
    twice per transfer: the only accesses it adds to the proven sequence."""
    dbg.w2(0x210, 0x7E)
    unlocked = dbg.w2(0x210, 0x5A)
    lock = dbg.r2(0x210)
    if trace is not None:
        trace.append(f"unlock: 0x210 reads {unlocked:x}, then {lock:x} (Windows needs aa)")
    try:
        mode = dbg.w2(0x20C, 0x800001)
        address = dbg.w2(0x804, group)
        dbg.w2(0x20C, 0x800003)
        dbg.w2(0x204, register)
        if trace is not None:
            trace.append(f"setup: 0x20c reads {mode:08x}, 0x804 reads {address:x}")
        register_phase = _phase(dbg, trace, f"register {register:02x} sent")
        dbg.w2(0x20C, 0x800003)
        strobe = dbg.w2(0x208, 0xFFFF)
        data_phase = _phase(dbg, trace, "read strobe")
        value = dbg.r2(0x208)
        if trace is not None:
            trace.append(f"data: 0x208 reads {strobe:x} right after the strobe, {value:x} after 20 ms")
            trace.append(engine_verdict(unlocked & 0xFF, lock & 0xFF, (register_phase, data_phase), value & 0xFF))
        return value & 0xFF
    finally:
        dbg.w2(0x20C, 0x800002)
        dbg.w2(0x210, 0)
        dbg.r2(0x210)


def engine_verdict(unlocked, lock, phases, value):
    """One line from a traced read: lock, transfers, then the device."""
    if UNLOCKED not in (unlocked, lock):
        return (f"ENGINE: it does NOT UNLOCK (0x210 reads {unlocked:02x}/{lock:02x}, Windows needs aa): "
                "no DAC command gets through")
    if any(not late & DONE for _, late in phases):
        return "ENGINE: a transfer did NOT FINISH (0x20c bit 23 still 0 after 20 ms): the engine is stuck"
    ran = all(not early & DONE for early, _ in phases)
    how = "busy, then done: it ran both transfers" if ran else "done each time it was read"
    if value == 0xFF:
        return (f"ENGINE: unlocked, bit 23 {how}; the DAC ANSWERS ff: no device replies "
                "(DAC power, reset or clock), or the bus is cut")
    return f"ENGINE: unlocked, bit 23 {how}; the DAC answers {value:02x}"


def dac_write(dbg, group, register, value):
    """One DAC register write, as the driver's ca0113_cmd sends it."""
    dbg.w2(0x210, 0x7E)
    dbg.w2(0x210, 0x5A)
    try:
        dbg.w2(0x20C, 0x800005)
        dbg.w2(0x804, group)
        dbg.w2(0x20C, 0x800005)
        dbg.w2(0x204, register | value << 8)
        dbg.sleep(0.02)
        _status(dbg)
    finally:
        dbg.w2(0x20C, 0x800004)
        dbg.w2(0x210, 0)


def judge(value, expected):
    if expected is None:
        return "info"
    mask, want = expected
    return "ok" if value & mask == want else "DIFFERS"


def describe(group, register, value, meaning, expected):
    want = ""
    if expected is not None:
        mask, wanted = expected
        want = f"want {wanted:02x}" if mask == 0xFF else f"want bits {wanted:02x}/{mask:02x}"
    return f"{group:02x}/{register:02x} = {value:02x}  {judge(value, expected):7} {want:16} {meaning}".rstrip()


class KernelLog:
    """New kernel log records since mark(), from /dev/kmsg."""

    def __init__(self, path=KMSG):
        self.path = path
        self.fd = None

    def mark(self):
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        os.lseek(self.fd, 0, os.SEEK_END)

    def lines(self):
        records = []
        while True:
            try:
                record = os.read(self.fd, 8192)
            except BlockingIOError:
                break
            except BrokenPipeError:  # overwritten records: skip them
                continue
            if not record:
                break
            records.append(record.decode(errors="replace").split(";", 1)[-1].rstrip("\n"))
        os.close(self.fd)
        self.fd = None
        return records


def codec_state(dbg, klog, table=CODEC_VERBS):
    """(meaning, verb, reply or None, expected) for every GET verb of the table."""
    klog.mark()
    for _, value, _ in table:
        dbg.verb(value)
    replies = {}
    for line in klog.lines():
        match = KLOG_VERB.search(line)
        if match and match[3] == "0":
            replies[int(match[1], 16)] = int(match[2], 16)
    return [(meaning, value, replies.get(value), expected) for meaning, value, expected in table]


def codec_lines(rows):
    out = []
    for meaning, value, reply, expected in rows:
        if reply is None:
            out.append(f"verb {value:08x} -> no reply in the kernel log  {meaning}")
            continue
        verdict = "info" if expected is None else ("ok" if reply == expected else "DIFFERS")
        want = "" if expected is None else f"want {expected:08x}"
        out.append(f"verb {value:08x} -> {reply:08x}  {verdict:7} {want:13}  {meaning}".rstrip())
    return out


def dump(dbg, klog, master, out=None):
    """Lines go to out as they are read, so a failure keeps what came before."""
    out = [] if out is None else out
    volume = attenuation(master)
    out.append(f"DAC registers (Master {master}: volume registers want {volume:02x}):")
    values = []
    for group, register, meaning, expected in dac_registers(volume):
        value = dac_read(dbg, group, register)
        values.append(value)
        out.append("  " + describe(group, register, value, meaning, expected))
    if set(values) == {0xFF}:
        out.append(f"  All {len(values)} DAC reads came back ff: neither DAC device answers")
    out.append("Codec 1 (GET verbs):")
    out.extend("  " + line for line in codec_lines(codec_state(dbg, klog)))
    return out


def running_streams(card, root=STREAM_STATUS):
    """Every substream of the card whose status says RUNNING."""
    found = []
    for status in sorted(Path(root.format(card=card)).glob("pcm*/sub*/status")):
        try:
            text = status.read_text()
        except OSError:
            continue
        if any(line.split(":", 1)[-1].strip() == "RUNNING"
               for line in text.splitlines() if line.startswith("state:")):
            found.append(f"{status.parent.parent.name}/{status.parent.name}")
    return found


def describe_wide(name, value, meaning, expected, width=8):
    verdict = judge(value, expected)
    want = ""
    if expected is not None:
        mask, wanted = expected
        full = (1 << 4 * width) - 1
        want = f"want {wanted:x}" if mask == full else f"want bits {wanted:x}/{mask:x}"
    return f"{name} = {value:0{width}x}  {verdict:7} {want:26} {meaning}".rstrip()


def gpio_values(dbg):
    return {offset: dbg.r2(offset) for offset, _ in GPIO_REGISTERS}


def gpio_lines(values, before=None):
    """One line per GPIO register; with before, the ones that changed are marked."""
    lines = []
    for offset, meaning in GPIO_REGISTERS:
        mark = ""
        if before is not None and before[offset] != values[offset]:
            mark = f"  CHANGED from {before[offset]:08x}"
        lines.append(f"{offset:03x} = {values[offset]:08x}  {meaning}{mark}")
    return lines


def engine(dbg, klog, card, root=STREAM_STATUS, out=None):
    """Read-only: the command engine, one traced DAC read, ChipIO, codec GPIO
    and codec 2. Lines go to out as they are read, as in dump()."""
    out = [] if out is None else out
    playing = running_streams(card, root)
    if playing:
        raise RuntimeError(f"stream(s) running on card {card} ({' '.join(playing)}): ChipIO reads "
                           "during playback can freeze the card; stop playback first")
    out.append("Command engine (BAR2):")
    for offset, meaning, expected in ENGINE_REGISTERS:
        out.append("  " + describe_wide(f"{offset:03x}", dbg.r2(offset), meaning, expected))
    out.append("GPIO block (BAR2 0x300-0x320):")
    out.extend("  " + line for line in gpio_lines(gpio_values(dbg)))
    out.append("One traced DAC read (48/40, the chip ID; 2026-11-09 on this card: 71):")
    trace = []
    try:
        value = dac_read(dbg, 0x48, 0x40, trace)
    finally:
        out.extend("  " + line for line in trace)
    out.append(f"  value {value:02x}")
    out.append("ChipIO (I2S clocks and power; idle):")
    for address, meaning, expected in CHIPIO_REGISTERS:
        out.append("  " + describe_wide(f"{address:06x}", dbg.chipior(address), meaning, expected))
    out.append("Codec GPIO and codec 2 (GET verbs):")
    out.extend("  " + line for line in codec_lines(codec_state(dbg, klog, ENGINE_VERBS)))
    return out


def pin0(dbg, card, root=STREAM_STATUS, out=None):
    """Windows' pin 0 pulse, then whether the DAC answers; (lines, ok).

    The debugfs write is 32-bit, so it keeps the upper half of 0x320 as read.
    If the DAC answers afterwards, both devices are muted the way Windows
    continues (48/07 = 81, 49/0a = 07): they come up unset, at their power-on
    defaults, and must not play before the driver sets them up."""
    out = [] if out is None else out
    playing = running_streams(card, root)
    if playing:
        raise RuntimeError(f"stream(s) running on card {card} ({' '.join(playing)}): stop playback first")
    gpio = dbg.r2(GPIO)
    if gpio == 0xFFFFFFFF:
        raise RuntimeError("BAR2 0x320 reads ffffffff: the card does not answer")
    keep = gpio & 0xFFFF0000
    out.append(f"GPIO 0x320 reads {gpio:08x}; the 32-bit writes keep its upper half ({keep >> 16:04x})")
    before = dac_read(dbg, 0x48, 0x40)
    out.append(f"Before: 48/40 = {before:02x}")
    if before != 0xFF:
        out.append("The DAC already answers: no pulse, nothing was written")
        return out, True
    gpio_before = gpio_values(dbg)
    out.append("GPIO block before the pulse:")
    out.extend("  " + line for line in gpio_lines(gpio_before))
    dbg.w2(GPIO, keep | PIN0_LOW)
    dbg.sleep(PULSE)
    dbg.w2(GPIO, keep | PIN0_HIGH)
    dbg.sleep(0.02)
    out.append("GPIO pin 0 = 0 for 15 ms, then 1 (Windows' first D0 step)")
    out.append("GPIO block after the pulse:")
    out.extend("  " + line for line in gpio_lines(gpio_values(dbg), gpio_before))
    first = dac_read(dbg, 0x48, 0x07)
    out.append(f"After: 48/07 = {first:02x} (Windows reads 80 here, the power-on default)")
    trace = []
    chip = dac_read(dbg, 0x48, 0x40, trace)
    out.extend("  " + line for line in trace)
    if first == 0xFF and chip == 0xFF:
        out.append("VERDICT: the DAC still does NOT answer, so pin 0 is not what holds it "
                   "(pin 0 stays 1, Windows' state); nothing else was written")
        return out, True
    dac_write(dbg, 0x48, 0x07, 0x81)
    dac_write(dbg, 0x49, 0x0A, 0x07)
    muted = (dac_read(dbg, 0x48, 0x07), dac_read(dbg, 0x49, 0x0A))
    out.append(f"Muted as Windows does next: 48/07 = {muted[0]:02x} (want 81), 49/0a = {muted[1]:02x} (want 07)")
    if chip >> 2 == CHIP_ID and muted == (0x81, 0x07):
        out.append("VERDICT: the DAC ANSWERS after the pin 0 pulse: GPIO pin 0 was holding both DAC "
                   "devices. They are muted and unset now; nothing plays until the driver sets them up")
    else:
        out.append("VERDICT: after the pin 0 pulse the DAC answers only in part (see above)")
    return out, True


def stream_running(card, status_path=PCM_STATUS):
    try:
        text = Path(status_path.format(card=card)).read_text()
    except OSError:
        return False
    return any(line.split(":", 1)[-1].strip() == "RUNNING"
               for line in text.splitlines() if line.startswith("state:"))


def verdict(status):
    lock, automute = status & 1, status >> 1 & 1
    if not lock:
        return "the DAC is NOT LOCKED: no usable I2S from the codec"
    if automute:
        return "the DAC receives SILENCE (zeros): the break is before the DAC (codec, DSP or routing)"
    return "the DAC receives the TONE: the break is after the DAC (analog stage, cable or head unit)"


def probe(dbg, master, card, status_path=PCM_STATUS, out=None):
    """(lines, ok); lines go to out as they are read, as in dump()."""
    out = [] if out is None else out
    if not stream_running(card, status_path):
        raise RuntimeError(f"no running playback stream on card {card}: start the steady tone first")
    volume = attenuation(master)
    table = {(group, register): (meaning, expected)
             for group, register, meaning, expected in dac_registers(volume)}
    out.append("DAC registers while the tone plays:")
    for group, register in KEY_REGISTERS:
        meaning, expected = table[group, register]
        value = dac_read(dbg, group, register)
        out.append("  " + describe(group, register, value, meaning, expected))
    saved = {register: dac_read(dbg, 0x48, register) for register in (0x02, 0x04, 0x05)}
    before = dac_read(dbg, 0x48, 0x40)
    out.append(f"Automute probe: saved 02={saved[0x02]:02x} 04={saved[0x04]:02x} 05={saved[0x05]:02x}, "
               f"status {before:02x} before")
    if set(saved.values()) | {before} == {0xFF}:
        out.append("Automute probe SKIPPED: the DAC answers ff to everything; nothing was written")
        out.append("VERDICT: the DAC does NOT ANSWER on its control bus (no power, reset or clock "
                   "at the DAC, or the command engine does not reach it), so none of the driver's "
                   "DAC settings can have landed")
        return out, True
    if saved != PROBE_SAVED or before >> 2 != CHIP_ID:
        out.append("Automute probe SKIPPED: these are not the expected 02=34 04=00 05=68 and chip ID "
                   f"{CHIP_ID:02x}; nothing was written")
        return out, False
    try:
        dac_write(dbg, 0x48, 0x05, AUTOMUTE_LEVEL)
        dac_write(dbg, 0x48, 0x04, AUTOMUTE_TIME)
        dac_write(dbg, 0x48, 0x02, saved[0x02] & 0x3F | AUTOMUTE_MODE)
        dbg.sleep(0.4)
        after = dac_read(dbg, 0x48, 0x40)
    finally:
        for register in (0x02, 0x04, 0x05):
            dac_write(dbg, 0x48, register, saved[register])
    restored = {register: dac_read(dbg, 0x48, register) for register in (0x02, 0x04, 0x05)}
    if restored != saved:
        for register in (0x02, 0x04, 0x05):
            dac_write(dbg, 0x48, register, saved[register])
        restored = {register: dac_read(dbg, 0x48, register) for register in (0x02, 0x04, 0x05)}
    out.append(f"Automute probe: status {after:02x} with automute on (bit 1 automute {after >> 1 & 1}, "
               f"bit 0 lock {after & 1})")
    if restored != saved:
        out.append("RESTORE FAILED: " + " ".join(f"{r:02x}={restored[r]:02x}" for r in sorted(restored)))
        return out, False
    out.append("Automute probe: 02, 04, 05 restored")
    out.append("VERDICT: " + verdict(after))
    return out, True


def steady_tone(level=STEADY_LEVEL, seconds=STEADY_SECONDS):
    """1 kHz on both channels, 50 ms raised-cosine ramps; |x| <= 32768 * 10^(level/20)."""
    amplitude = math.floor(32768 * 10 ** (level / 20))
    total = RATE * seconds
    frames = bytearray()
    for n in range(total):
        if n < RAMP:
            envelope = 0.5 - 0.5 * math.cos(math.pi * n / RAMP)
        elif n >= total - RAMP:
            envelope = 0.5 - 0.5 * math.cos(math.pi * (total - 1 - n) / RAMP)
        else:
            envelope = 1.0
        value = round(amplitude * envelope * math.sin(2 * math.pi * FREQUENCY * n / RATE))
        value = max(-amplitude, min(amplitude, value))
        frames += struct.pack("<hh", value, value)
    return bytes(frames)


def write_tone(directory):
    directory = Path(directory)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError(f"Output must be an existing real directory: {directory}")
    target = directory / STEADY_NAME
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as stream, wave.open(stream, "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(steady_tone())
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--debugfs", default=DEBUGFS)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("engine").add_argument("--card", type=int, required=True)
    sub.add_parser("pin0").add_argument("--card", type=int, required=True)
    for name in ("dump", "probe"):
        action = sub.add_parser(name)
        action.add_argument("--master", type=int, required=True)
        if name == "probe":
            action.add_argument("--card", type=int, required=True)
    sub.add_parser("tone").add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.action == "tone":
        print(write_tone(args.out))
        return 0
    dbg = Debugfs(args.debugfs)
    out = []
    try:
        if args.action == "engine":
            engine(dbg, KernelLog(), args.card, out=out)
            ok = True
        elif args.action == "pin0":
            _, ok = pin0(dbg, args.card, out=out)
        elif args.action == "dump":
            dump(dbg, KernelLog(), args.master, out)
            ok = True
        else:
            _, ok = probe(dbg, args.master, args.card, out=out)
    except (OSError, RuntimeError, ValueError) as error:
        print("\n".join(out + [f"ERROR: {error}"]))
        return 1
    print("\n".join(out))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
