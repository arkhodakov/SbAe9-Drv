#!/usr/bin/env python3
"""Host-only tests for tools/sbae9drv_dac.py: a modelled debugfs file, never the card.

The fake models the SbAe9-Drv debugfs replies ("r2", "w2", "verb") over a
CA0113 command engine with two register files (groups 0x48 and 0x49).
Run: python3 tools/tests/test_sbae9drv_dac.py
"""
import importlib.util
import io
import math
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path

sys.dont_write_bytecode = True
TOOL = Path(__file__).resolve().parents[1] / "sbae9drv_dac.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("sbae9drv_dac", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dac = load_tool()


def headphone_registers(volume=0x48):
    """Every register of the dump at its expected headphone value."""
    registers = {0x48: {}, 0x49: {}}
    for group, register, _, expected in dac.dac_registers(volume):
        registers[group][register] = expected[1] if expected else 0x00
    registers[0x48][0x07] = 0x80
    registers[0x48][0x40] = 0x71
    return registers


def healthy_bar2():
    """BAR2 as Windows' init leaves it (0x20c and 0x210 are modelled)."""
    return {0x004: 0x7172000B, 0x01C: 0x00880480, 0x100: 0x40000700, 0x104: 0x0, 0x304: 0xFF,
            0x320: 0x00000105, 0x800: 0x6B, 0x804: 0x48, 0x830: 0xAFF, 0x840: 0x0, 0x854: 0x0,
            0x860: 0x0, 0x86C: 0x1, 0x89C: 0x1}


def healthy_chipio():
    return {0x189000: 0x0001F100, 0x189004: 0x0001F100, 0x189008: 0x0001F100, 0x189024: 0x00008005,
            0x189028: 0x0002000F, 0x18902C: 0xF, 0x189030: 0x81, 0x18B008: 0xF0, 0x18B030: 0x0,
            0x18B0A4: 0x2}


class FakeDriver(dac.Debugfs):
    """The driver's debugfs replies over a modelled CA0113 command engine."""

    def __init__(self, registers, zeros=False, dead=False, stuck=False, lock_reply=None, wake_on_pin0=False):
        super().__init__(path="unused", sleep=lambda seconds: self.slept.append(seconds))
        self.registers = registers
        self.zeros = zeros
        self.dead = dead  # no DAC answers: every read is ff
        self.stuck = stuck  # 0x20c bit 23 never comes back
        self.lock_reply = lock_reply  # what 0x210 reads when unlocked (aa)
        self.wake_on_pin0 = wake_on_pin0  # GPIO pin 0 = 0, then 1, brings a dead DAC up
        self.gpio = []
        self.bar2 = healthy_bar2()
        self.chipio = healthy_chipio()
        self.slept = []
        self.commands = []
        self.writes = []
        self.verbs = []
        self.lock = []
        self.mode = self.group = self.latched = None
        self.busy = False
        self.data = 0

    def status(self):
        value = self.registers[0x48][0x40]
        automute = self.registers[0x48][0x04] != 0 and self.registers[0x48][0x02] >> 6 == 1
        return value | 2 if automute and self.zeros else value & ~2

    def unlocked(self):
        return self.lock[-2:] == [0x7E, 0x5A]

    def read(self, offset):
        if offset == 0x208:
            return self.data
        if offset == 0x210:
            if not self.unlocked():
                return 0
            return dac.UNLOCKED if self.lock_reply is None else self.lock_reply
        if offset == 0x20C:
            mode = 0x800004 if self.mode is None else self.mode
            if self.stuck or self.busy:
                self.busy = False
                return mode & ~dac.DONE
            return mode | dac.DONE
        return self.bar2.get(offset, 0)

    def command(self, text):
        self.commands.append(text)
        words = text.split()
        if words[0] == "verb":
            self.verbs.append(int(words[1], 16))
            return "stale reply"
        if words[0] == "chipior":
            address = int(words[1], 16)
            return f"chipio[0x{address:x}]=0x{self.chipio.get(address, 0):x} ret=0"
        offset = int(words[1], 16)
        if words[0] == "r2":
            return f"r2 0x{offset:x} = 0x{self.read(offset):x}"
        value = int(words[2], 16)
        if offset == 0x210:
            self.lock.append(value)
        elif offset == 0x20C:
            self.mode = value
        elif offset == 0x804:
            self.group = value
        elif offset == 0x204:
            assert self.unlocked(), "command engine used while locked"
            self.busy = True
            if self.mode == 0x800005:
                if not self.dead:
                    self.registers[self.group][value & 0xFF] = value >> 8
                self.writes.append((self.group, value & 0xFF, value >> 8))
            elif self.mode == 0x800003:
                self.latched = (self.group, value & 0xFF)
        elif offset == 0x208 and self.mode == 0x800003 and self.latched:
            group, register = self.latched
            self.busy = True
            if self.dead:
                self.data = 0xFF
            else:
                self.data = self.status() if self.latched == (0x48, 0x40) else self.registers[group][register]
                self.data |= 0x100  # upper bits the tool must drop
        else:
            if offset == 0x320:
                if self.wake_on_pin0 and self.gpio and self.gpio[-1] & 0xFFFF == 0x0000 \
                        and value & 0xFFFF == 0x0100:
                    self.dead = False
                    self.registers = headphone_registers()
                    self.registers[0x48][0x07] = 0x80  # the ES9038's power-on default
                self.gpio.append(value)
            self.bar2[offset] = value
        rb = self.data if offset == 0x208 else (self.group if offset == 0x804 else self.peek(offset))
        return f"w2 0x{offset:x} <= 0x{value:x} (rb 0x{rb:x})"

    def peek(self, offset):
        """A read-back that does not consume the busy flag (the driver's own rb)."""
        busy = self.busy
        value = self.read(offset)
        self.busy = busy
        return value


class FakeKernelLog:
    def __init__(self, driver, replies):
        self.driver = driver
        self.replies = replies
        self.start = None

    def mark(self):
        self.start = len(self.driver.verbs)

    def lines(self):
        return ["unrelated line"] + [f"snd-hda-codec-creative-ae9 0000:04:00.0: dbg verb 0x{value:08x} -> "
                                     f"0x{self.replies[value]:08x} (0)"
                                     for value in self.driver.verbs[self.start:] if value in self.replies]


def codec_replies(table=None):
    table = dac.CODEC_VERBS if table is None else table
    return {value: (expected if expected is not None else 0x10) for _, value, expected in table}


def dead_registers():
    return {group: {register: 0xFF for register in values} for group, values in headphone_registers().items()}


class DacToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sbae9drv-dac-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.status = self.root / "status"
        self.status.write_text("state: RUNNING\nowner_pid   : 1\n")

    def probe(self, driver, master=182):
        return dac.probe(driver, master, 1, status_path=str(self.status))

    def test_attenuation_is_half_db_steps_below_zero(self):
        self.assertEqual(dac.attenuation(182), 0x48)
        self.assertEqual(dac.attenuation(202), 0x34)
        self.assertEqual(dac.attenuation(254), 0x00)
        for bad in (-1, 255):
            with self.assertRaises(ValueError):
                dac.attenuation(bad)

    def test_verbs_are_codec_1_get_verbs(self):
        verbs = {meaning: value for meaning, value, _ in dac.CODEC_VERBS}
        self.assertEqual(verbs["pin 0x10 control"], 0x110F0700)
        self.assertEqual(verbs["DAC 0x02 amp out left"], 0x102BA000)
        self.assertEqual(verbs["DAC 0x02 amp out right"], 0x102B8000)
        self.assertEqual(verbs["DAC 0x02 format"], 0x102A0000)
        self.assertEqual(verbs["AFG power state"], 0x101F0500)
        for value in verbs.values():
            self.assertEqual(value >> 28, 1)
            command = value >> 8 & 0xFFF
            self.assertTrue(command >> 8 == 0xF or value >> 16 & 0xF in (0xA, 0xB), hex(value))

    def test_dump_is_read_only_and_flags_what_differs(self):
        registers = headphone_registers()
        registers[0x48][0x07] = 0x81  # muted
        registers[0x49][0x0A] = 0x07  # muted
        driver = FakeDriver(registers)
        replies = codec_replies()
        replies[0x110F0700] = 0x00
        out = dac.dump(driver, FakeKernelLog(driver, replies), 182)
        self.assertEqual(driver.writes, [], "the dump must not write the DAC")
        self.assertEqual(driver.lock[-1], 0, "the command engine must end locked")
        rows = {line.split()[0]: line for line in out if line.startswith("  4")}
        self.assertEqual(len(rows), len(dac.dac_registers(0x48)))
        self.assertIn("48/07 = 81  DIFFERS want bits 00/01", rows["48/07"])
        self.assertIn("49/0a = 07  DIFFERS want 06", rows["49/0a"])
        self.assertIn("48/0f = 48  ok", rows["48/0f"])
        self.assertIn("48/40 = 71  ok", rows["48/40"])
        self.assertIn("48/41 = 00  info", rows["48/41"])
        self.assertEqual(sum("DIFFERS" in line for line in out), 3)
        self.assertIn("  verb 110f0700 -> 00000000  DIFFERS want 000000c0  pin 0x10 control", out)
        self.assertEqual(driver.verbs, [value for _, value, _ in dac.CODEC_VERBS])

    def test_dump_reports_a_missing_verb_reply(self):
        driver = FakeDriver(headphone_registers())
        replies = codec_replies()
        del replies[0x110F0700]
        out = dac.dump(driver, FakeKernelLog(driver, replies), 182)
        self.assertIn("  verb 110f0700 -> no reply in the kernel log  pin 0x10 control", out)

    def test_read_sequence_matches_our_driver(self):
        driver = FakeDriver(headphone_registers())
        self.assertEqual(dac.dac_read(driver, 0x48, 0x0F), 0x48)
        self.assertEqual(driver.commands, [
            "w2 0x210 0x7e", "w2 0x210 0x5a", "r2 0x210",
            "w2 0x20c 0x800001", "w2 0x804 0x48", "w2 0x20c 0x800003", "w2 0x204 0xf",
            "r2 0x860", "r2 0x854", "r2 0x840",
            "w2 0x20c 0x800003", "w2 0x208 0xffff",
            "r2 0x860", "r2 0x854", "r2 0x840", "r2 0x208",
            "w2 0x20c 0x800002", "w2 0x210 0x0", "r2 0x210"])
        self.assertEqual(driver.slept, [0.02, 0.02])

    def test_write_sequence_matches_the_driver(self):
        driver = FakeDriver(headphone_registers())
        dac.dac_write(driver, 0x48, 0x05, 0x3C)
        self.assertEqual(driver.commands, [
            "w2 0x210 0x7e", "w2 0x210 0x5a",
            "w2 0x20c 0x800005", "w2 0x804 0x48", "w2 0x20c 0x800005", "w2 0x204 0x3c05",
            "r2 0x860", "r2 0x854", "r2 0x840",
            "w2 0x20c 0x800004", "w2 0x210 0x0"])
        self.assertEqual(driver.writes, [(0x48, 0x05, 0x3C)])

    def test_bad_replies_raise(self):
        driver = FakeDriver(headphone_registers())
        driver.command = lambda text: "something else"
        with self.assertRaises(RuntimeError):
            driver.r2(0x208)
        with self.assertRaises(RuntimeError):
            driver.w2(0x20C, 0x800001)
        with self.assertRaises(RuntimeError):
            driver.chipior(0x189030)
        driver.command = lambda text: "chipio[0x189030]=0x0 ret=-110"
        with self.assertRaisesRegex(RuntimeError, "failed"):
            driver.chipior(0x189030)
        driver.command = lambda text: "chipio[0x189034]=0x81 ret=0"
        with self.assertRaisesRegex(RuntimeError, "unexpected reply"):
            driver.chipior(0x189030)

    def test_probe_refuses_without_a_running_stream(self):
        driver = FakeDriver(headphone_registers())
        for text in ("closed\n", "state: PREPARED\n"):
            self.status.write_text(text)
            with self.assertRaisesRegex(RuntimeError, "no running playback stream"):
                self.probe(driver)
        self.status.unlink()
        with self.assertRaises(RuntimeError):
            self.probe(driver)
        self.assertEqual(driver.commands, [])

    def test_probe_restores_the_automute_registers(self):
        for zeros, verdict in ((False, "the DAC receives the TONE"), (True, "the DAC receives SILENCE")):
            with self.subTest(zeros=zeros):
                registers = headphone_registers()
                before = {group: dict(values) for group, values in registers.items()}
                driver = FakeDriver(registers, zeros=zeros)
                out, ok = self.probe(driver)
                self.assertTrue(ok, out)
                self.assertEqual(driver.registers, before)
                self.assertEqual(driver.writes, [(0x48, 0x05, 0x3C), (0x48, 0x04, 0xFF), (0x48, 0x02, 0x74),
                                                 (0x48, 0x02, 0x34), (0x48, 0x04, 0x00), (0x48, 0x05, 0x68)])
                self.assertIn(0.4, driver.slept)
                self.assertEqual(driver.lock[-1], 0)
                self.assertIn("Automute probe: 02, 04, 05 restored", out)
                self.assertTrue(out[-1].startswith("VERDICT: " + verdict), out[-1])
                rows = [line for line in out if line.startswith("  4")]
                self.assertEqual(len(rows), len(dac.KEY_REGISTERS))

    def test_probe_writes_nothing_over_unexpected_values(self):
        for group, register, value in ((0x48, 0x02, 0x00), (0x48, 0x04, 0x10), (0x48, 0x05, 0xFF),
                                       (0x48, 0x40, 0x01)):
            with self.subTest(register=register, value=value):
                registers = headphone_registers()
                registers[group][register] = value
                driver = FakeDriver(registers)
                out, ok = self.probe(driver)
                self.assertFalse(ok)
                self.assertEqual(driver.writes, [])
                self.assertIn("Automute probe SKIPPED", out[-1])

    def test_probe_restores_when_a_step_fails(self):
        registers = headphone_registers()
        driver = FakeDriver(registers)
        real_read = dac.dac_read
        reads = []

        def failing_read(dbg, group, register):
            reads.append((group, register))
            if len(reads) == len(dac.KEY_REGISTERS) + 5:  # the read after the automute writes
                raise RuntimeError("read failed")
            return real_read(dbg, group, register)

        dac.dac_read = failing_read
        self.addCleanup(setattr, dac, "dac_read", real_read)
        with self.assertRaisesRegex(RuntimeError, "read failed"):
            self.probe(driver)
        self.assertEqual(driver.registers[0x48][0x02], 0x34)
        self.assertEqual(driver.registers[0x48][0x04], 0x00)
        self.assertEqual(driver.registers[0x48][0x05], 0x68)

    def engine(self, driver, replies=None):
        idle = self.root / "idle" / "card1" / "pcm0p" / "sub0"
        idle.mkdir(parents=True, exist_ok=True)
        (idle / "status").write_text("closed\n")
        klog = FakeKernelLog(driver, codec_replies(dac.ENGINE_VERBS) if replies is None else replies)
        return dac.engine(driver, klog, 1, root=str(self.root / "idle" / "card{card}"))

    def engine_line(self, out):
        lines = [line for line in out if line.startswith("  ENGINE: ")]
        self.assertEqual(len(lines), 1, out)
        return lines[0]

    def test_engine_refuses_while_a_stream_runs(self):
        card = self.root / "busy" / "card1"
        for sub, text in (("pcm0p/sub0", "state: RUNNING\n"), ("pcm0c/sub0", "closed\n")):
            (card / sub).mkdir(parents=True)
            (card / sub / "status").write_text(text)
        driver = FakeDriver(headphone_registers())
        with self.assertRaisesRegex(RuntimeError, r"\(pcm0p/sub0\).*stop playback first"):
            dac.engine(driver, FakeKernelLog(driver, {}), 1, root=str(self.root / "busy" / "card{card}"))
        self.assertEqual(driver.commands, [])

    def test_engine_reads_only_and_reports_a_working_engine(self):
        driver = FakeDriver(headphone_registers())
        out = self.engine(driver)
        self.assertEqual(driver.writes, [], "the engine section must not write the DAC")
        self.assertEqual(driver.lock[-1], 0, "the command engine must end locked")
        written = {text.split()[1] for text in driver.commands if text.startswith("w2 ")}
        self.assertEqual(written, {"0x210", "0x20c", "0x804", "0x204", "0x208"})
        self.assertFalse([line for line in out if "DIFFERS" in line], out)
        self.assertIn("  unlock: 0x210 reads aa, then aa (Windows needs aa)", out)
        self.assertEqual(self.engine_line(out),
                         "  ENGINE: unlocked, bit 23 busy, then done: it ran both transfers; the DAC answers 71")
        self.assertIn("  value 71", out)
        rows = {line.split()[0]: line for line in out if " = " in line and not line.startswith("  verb")}
        self.assertEqual(len(rows), len(dac.ENGINE_REGISTERS) + len(dac.CHIPIO_REGISTERS))
        self.assertTrue(rows["01c"].startswith("  01c = 00880480  ok"), rows["01c"])
        self.assertTrue(rows["189030"].startswith("  189030 = 00000081  ok"), rows["189030"])
        self.assertEqual(driver.verbs, [value for _, value, _ in dac.ENGINE_VERBS])
        self.assertIn("  verb 200f0000 -> 11020011  ok      want 11020011  codec 2 vendor ID", out)

    def test_engine_names_what_is_off(self):
        driver = FakeDriver(headphone_registers())
        driver.bar2.update({0x01C: 0x00880680, 0x89C: 0x0})
        driver.chipio[0x189030] = 0x82
        out = self.engine(driver)
        flagged = sorted(line.split()[0] for line in out if "DIFFERS" in line)
        self.assertEqual(flagged, ["01c", "189030", "89c"])

    def test_engine_reports_a_dac_that_does_not_answer(self):
        driver = FakeDriver(dead_registers(), dead=True)
        out = self.engine(driver)
        self.assertIn("the DAC ANSWERS ff", self.engine_line(out))
        self.assertIn("busy, then done", self.engine_line(out))
        self.assertIn("  value ff", out)

    def test_engine_reports_a_locked_engine(self):
        driver = FakeDriver(headphone_registers(), lock_reply=0x00)
        self.assertIn("does NOT UNLOCK (0x210 reads 00/00", self.engine_line(self.engine(driver)))

    def test_engine_reports_a_stuck_transfer(self):
        driver = FakeDriver(headphone_registers(), stuck=True)
        out = self.engine(driver)
        self.assertIn("did NOT FINISH", self.engine_line(out))
        self.assertTrue(any(line.startswith("  20c = ") and "DIFFERS" in line for line in out), out)

    def test_traced_read_adds_only_reads_of_0x20c(self):
        driver = FakeDriver(headphone_registers())
        trace = []
        self.assertEqual(dac.dac_read(driver, 0x48, 0x40, trace), 0x71)
        self.assertEqual(driver.commands, [
            "w2 0x210 0x7e", "w2 0x210 0x5a", "r2 0x210",
            "w2 0x20c 0x800001", "w2 0x804 0x48", "w2 0x20c 0x800003", "w2 0x204 0x40",
            "r2 0x20c", "r2 0x20c", "r2 0x860", "r2 0x854", "r2 0x840",
            "w2 0x20c 0x800003", "w2 0x208 0xffff",
            "r2 0x20c", "r2 0x20c", "r2 0x860", "r2 0x854", "r2 0x840", "r2 0x208",
            "w2 0x20c 0x800002", "w2 0x210 0x0", "r2 0x210"])
        self.assertEqual(driver.slept, [0.02, 0.02])
        self.assertIn("register 40 sent: 0x20c 00000003 right after the start, 00800003 after 20 ms; "
                      "status 860=0 854=0 840=0", trace)

    def test_dump_says_when_no_dac_answers(self):
        driver = FakeDriver(dead_registers(), dead=True)
        out = dac.dump(driver, FakeKernelLog(driver, codec_replies()), 182)
        self.assertIn(f"  All {len(dac.dac_registers(0x48))} DAC reads came back ff: neither DAC device answers",
                      out)
        healthy = FakeDriver(headphone_registers())
        self.assertFalse([line for line in dac.dump(healthy, FakeKernelLog(healthy, codec_replies()), 182)
                          if "came back ff" in line])

    def test_probe_reports_a_dac_that_does_not_answer(self):
        driver = FakeDriver(dead_registers(), dead=True)
        out, ok = self.probe(driver)
        self.assertTrue(ok, out)
        self.assertEqual(driver.writes, [])
        self.assertIn("Automute probe SKIPPED: the DAC answers ff to everything; nothing was written", out)
        self.assertTrue(out[-1].startswith("VERDICT: the DAC does NOT ANSWER on its control bus"), out[-1])

    def pin0(self, driver):
        idle = self.root / "idle" / "card1" / "pcm0p" / "sub0"
        idle.mkdir(parents=True, exist_ok=True)
        (idle / "status").write_text("closed\n")
        return dac.pin0(driver, 1, root=str(self.root / "idle" / "card{card}"))

    def test_pin0_refuses_while_a_stream_runs_or_the_card_is_gone(self):
        card = self.root / "busy" / "card1" / "pcm0p" / "sub0"
        card.mkdir(parents=True)
        (card / "status").write_text("state: RUNNING\n")
        driver = FakeDriver(dead_registers(), dead=True, wake_on_pin0=True)
        with self.assertRaisesRegex(RuntimeError, "stop playback first"):
            dac.pin0(driver, 1, root=str(self.root / "busy" / "card{card}"))
        self.assertEqual(driver.commands, [])
        driver.bar2[0x320] = 0xFFFFFFFF
        with self.assertRaisesRegex(RuntimeError, "does not answer"):
            self.pin0(driver)
        self.assertEqual(driver.gpio, [])
        self.assertEqual(driver.writes, [])

    def test_pin0_wakes_a_dac_that_pin0_holds_and_mutes_it(self):
        driver = FakeDriver(dead_registers(), dead=True, wake_on_pin0=True)
        out, ok = self.pin0(driver)
        self.assertTrue(ok, out)
        self.assertEqual(driver.gpio, [0x0000, 0x0100])
        self.assertIn(dac.PULSE, driver.slept)
        self.assertIn("Before: 48/40 = ff", out)
        self.assertIn("After: 48/07 = 80 (Windows reads 80 here, the power-on default)", out)
        self.assertEqual(driver.writes, [(0x48, 0x07, 0x81), (0x49, 0x0A, 0x07)])
        self.assertIn("Muted as Windows does next: 48/07 = 81 (want 81), 49/0a = 07 (want 07)", out)
        self.assertTrue(out[-1].startswith("VERDICT: the DAC ANSWERS after the pin 0 pulse"), out[-1])
        self.assertEqual(driver.lock[-1], 0)

    def test_pin0_reports_a_dac_that_still_does_not_answer(self):
        driver = FakeDriver(dead_registers(), dead=True)
        out, ok = self.pin0(driver)
        self.assertTrue(ok, out)
        self.assertEqual(driver.gpio, [0x0000, 0x0100])
        self.assertEqual(driver.writes, [], "no DAC write when the DAC does not answer")
        self.assertTrue(out[-1].startswith("VERDICT: the DAC still does NOT answer"), out[-1])

    def test_pin0_leaves_an_answering_dac_alone(self):
        driver = FakeDriver(headphone_registers())
        out, ok = self.pin0(driver)
        self.assertTrue(ok, out)
        self.assertEqual(driver.gpio, [])
        self.assertEqual(driver.writes, [])
        self.assertIn("The DAC already answers: no pulse, nothing was written", out)

    def test_pin0_keeps_the_upper_half_of_the_gpio_register(self):
        driver = FakeDriver(dead_registers(), dead=True, wake_on_pin0=True)
        driver.bar2[0x320] = 0x00AB0105
        self.pin0(driver)
        self.assertEqual(driver.gpio, [0x00AB0000, 0x00AB0100])

    def test_verdicts(self):
        self.assertIn("NOT LOCKED", dac.verdict(0x70))
        self.assertIn("NOT LOCKED", dac.verdict(0x72))
        self.assertIn("SILENCE", dac.verdict(0x73))
        self.assertIn("TONE", dac.verdict(0x71))

    def test_steady_tone(self):
        target = dac.write_tone(self.root)
        self.assertEqual(target.name, "ae9-steady-m40dbfs.wav")
        with wave.open(str(target), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (2, 2, 48000))
            self.assertEqual(wav.getnframes(), 48000 * dac.STEADY_SECONDS)
            frames = wav.readframes(wav.getnframes())
        samples = memoryview(frames).cast("h")
        limit = math.floor(32768 * 10 ** (-40 / 20))
        self.assertEqual(max(abs(value) for value in samples), limit)
        self.assertEqual(samples[0::2].tolist(), samples[1::2].tolist())
        self.assertEqual(samples[0], 0)
        with self.assertRaises(FileExistsError):
            dac.write_tone(self.root)

    def test_main_reports_errors_after_what_was_read(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = dac.main(["--debugfs", str(self.root / "missing"), "dump", "--master", "182"])
        self.assertEqual(code, 1)
        lines = stdout.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("DAC registers (Master 182"), lines)
        self.assertTrue(lines[-1].startswith("ERROR: "), lines)


if __name__ == "__main__":
    unittest.main()
