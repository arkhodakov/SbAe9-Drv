#!/usr/bin/env python3
"""Unprivileged, host-only tests for the SbAe9-Drv first-contact tools: NEVER touch the card.

tools/sbae9drv_test.sh runs as a private copy with every literal filesystem root
and PATH relocated into a fixture tree; amixer, aplay, arecord, insmod, rmmod,
modinfo, pgrep, journalctl, logger, id and sleep are Python stubs that model the
driver's sysfs, ALSA and kernel-log behaviour. tools/sbae9drv_firmware.py is
tested on synthetic FLS images.
Run: python3 tools/tests/test_sbae9drv.py
"""
import hashlib
import importlib.util
import json
import math
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.dont_write_bytecode = True

TOOLS = Path(__file__).resolve().parents[1]
TOOL = TOOLS / "sbae9drv_test.sh"
FIRMWARE_TOOL = TOOLS / "sbae9drv_firmware.py"
KERNEL = "7.2.13-200.fc44.x86_64"
CMDLINE = ("BOOT_IMAGE=(hd1,gpt2)/vmlinuz-7.2.13-200.fc44.x86_64 root=UUID=fixture ro "
           "module_blacklist=snd_hda_intel,snd_hda_code_c_ca0132 quiet")
DEVICE = "sys/devices/pci0000:00/0000:00:1c.0/0000:03:00.0/0000:04:00.0"
BRIDGE = "sys/devices/pci0000:00/0000:00:1c.0/0000:03:00.0"
KO = "SbAe9-Drv/snd-hda-codec-creative-ae9.ko"
# Absolute host paths in the script, relocated into the test root.
ROOT_PATTERN = re.compile(
    r"(?<![a-zA-Z0-9_/.-])/(?:sysroot|sys|proc|etc|usr|lib|run|dev)(?=[/\s\"':)]|$)")
MODULE_DIR = "sys/module/snd_hda_codec_creative_ae9"


def load_firmware_tool():
    spec = importlib.util.spec_from_file_location("sbae9drv_firmware", FIRMWARE_TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fls(segments):
    """An FLS image: (chip address, words) segments, then the count-0 terminator."""
    blob = b""
    for address, words in segments:
        blob += struct.pack("<III", 0x4C46584D, address, len(words)) + b"".join(
            struct.pack("<I", word) for word in words)
    return blob + struct.pack("<III", 0x4C46584D, 0, 0)


def main_image(seed=1):
    # 12 + 4 * 163853 + 12 = 655436 bytes, the size of the tested main image
    return fls([(0x80020, [(seed * 2654435761 + n) & 0xFFFFFFFF for n in range(163853)])])


def overlay_image(seed=7):
    return fls([(0x0B7B10, [seed + n for n in range(78)]), (0x03F3A0, [seed * 3 + n for n in range(21)])])


def ctxhda(main, overlay, extra=b""):
    return b"MZ\x90\x00" + b"\x00" * 60 + main + b"\x11" * 32 + overlay + extra + b"\x00" * 16


STUB = r'''
import json
import os
from pathlib import Path
import shutil
import sys
import time

root = Path(ROOT_LITERAL)
name = Path(sys.argv[0]).name
args = sys.argv[1:]
state_path = root / "state.json"
state = json.loads(state_path.read_text())
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps([name, *args]) + "\n")
DEVICE = root / DEVICE_LITERAL
MODULE_DIR = root / MODULE_DIR_LITERAL
DRIVER_DIR = root / "sys/bus/pci/drivers/snd-hda-codec-creative-ae9"
ENUMS = {"Head Unit Output": ["Speakers", "Headphones"]}
PREFIX = "snd-hda-codec-creative-ae9 0000:04:00.0: "


def save():
    state_path.write_text(json.dumps(state))


def kernel(*lines):
    now = int(time.time())
    state["kernel"].extend([now, line] for line in lines)


def control_max(control):
    return {"Master Playback Volume": state["master_max"], "Mic Gain Capture Volume": 2,
            "Capture Volume": 99}.get(control)


if name == "id":
    assert args == ["-u"], args
    print(state["uid"])
elif name in ("sleep", "logger"):
    pass
elif name == "pgrep":
    assert args[0] == "-x" and len(args) == 2, args
    sys.exit(0 if args[1] in state["running"] else 1)
elif name == "modinfo":
    assert args[:1] == ["-F"] and args[2].endswith("/snd-hda-codec-creative-ae9.ko"), args
    parm = state["plain_parm"] if args[2] in state["plain"] else state["parm"]
    print({"vermagic": state["vermagic"], "parm": parm, "version": "0.12.4"}[args[1]])
elif name == "journalctl":
    if args == ["--sync"]:
        sys.exit(0)
    assert args[:2] == ["-k", "--since"] and args[3:] == ["-o", "cat", "--no-pager"], args
    since = int(args[2].lstrip("@"))
    for stamp, line in state["kernel"]:
        if stamp >= since:
            print(line)
elif name == "insmod":
    assert len(args) == 1 and args[0].endswith("/snd-hda-codec-creative-ae9.ko"), args
    plain = args[0] in state["plain"]
    (MODULE_DIR / "parameters").mkdir(parents=True)
    if not plain:
        (MODULE_DIR / "parameters/board_d0").write_text("1\n")
    mode = state["probe"]
    if mode == "fail":
        kernel(PREFIX + "DSP did not start after 3 attempt(s); not registering the card")
    else:
        DRIVER_DIR.mkdir(parents=True, exist_ok=True)
        (DEVICE / "driver").symlink_to(DRIVER_DIR)
        (DEVICE / "sound/card0").mkdir(parents=True)
        asound = root / "proc/asound/card0"
        for pcm in ("pcm0p", "pcm0c", "pcm1c", "pcm2c", "pcm3c", "pcm4p"):
            (asound / pcm / "sub0").mkdir(parents=True)
            (asound / pcm / "sub0/status").write_text("closed\n")
        (asound / "id").write_text("AE9\n")
        lines = [PREFIX + "posted_flush: every register write is read back (ASMedia bridge)",
                 PREFIX + "bridge 0000:03:00.0 (ASMedia ASM1083/1085): 64 KB spare after each DMA buffer",
                 PREFIX + ("secondary bus reset via 0000:03:00.0: 0, ready after 120 ms (id 0x00101102)" if plain else
                           "ASMedia bridge: skipping the secondary bus reset (it takes the bridge down at ring start; bus_reset=1 forces it)"),
                 PREFIX + ("post_windows: 246 SPI frames (10 without reply), 49 CA0113 commands, 45 writes" if plain else
                           "post_windows: 246 SPI frames (0 without reply), 49 CA0113 commands, 45 writes")]
        if not plain:
            lines.append(PREFIX + ("board D0: GPIO pin 0 pulsed, but the DAC does not answer (48/40 = 0xff): "
                                   "the analog outputs will be silent" if mode == "deaf" else
                                   "board D0: GPIO pin 0 pulsed, the DAC answers (48/40 = 0x72)"))
        lines += [PREFIX + "DSP firmware load: 0", "unrelated: usb 1-1: new device"]
        if mode in ("ok", "deaf"):
            lines.append(PREFIX + "Init complete: card Creative Labs Sound Blaster AE-9 at 0x0 irq 16")
        kernel(*lines)
    save()
elif name == "rmmod":
    assert args == ["snd_hda_codec_creative_ae9"], args
    shutil.rmtree(MODULE_DIR)
    if (DEVICE / "driver").is_symlink():
        (DEVICE / "driver").unlink()
    shutil.rmtree(DEVICE / "sound", ignore_errors=True)
    shutil.rmtree(root / "proc/asound/card0", ignore_errors=True)
    kernel(PREFIX + "shutdown (unload): Windows power-down sequence", PREFIX + "shutdown (unload): done")
    save()
elif name == "amixer":
    assert args[:2] == ["-c", "0"] and args[3].startswith("name="), args
    control = args[3][5:]
    mixer = state["mixer"]
    assert control in mixer, "Unexpected control: " + control
    if args[2] == "cset":
        assert len(args) == 5, args
        value = args[4]
        if control in ENUMS:
            value = str(ENUMS[control].index(value)) if value in ENUMS[control] else value
            assert value.isdigit() and int(value) < len(ENUMS[control]), args
            if control == "Head Unit Output" and value == "1" and mixer[control] != "1":
                mixer["Master Playback Volume"] = str(state["hp_master"])
                if state["switch_logged"]:
                    kernel(PREFIX + "head unit: output headphones, volume att 0x48")
        elif control_max(control) is not None:
            top = control_max(control)
            value = str(top) if value == "100%" else str(max(0, min(int(value), top)))
        else:
            assert value in ("on", "off"), args
        mixer[control] = value
        state["writes"].append([control, args[4]])
        save()
        sys.exit(0)
    assert args[2] == "cget", args
    print(f"numid=1,iface=MIXER,name='{control}'")
    if control in ENUMS:
        print(f"  ; type=ENUMERATED,access=rw------,values=1,items={len(ENUMS[control])}")
        for index, item in enumerate(ENUMS[control]):
            print(f"  ; Item #{index} '{item}'")
    elif control_max(control) is not None:
        print(f"  ; type=INTEGER,access=rw---R--,values=1,min=0,max={control_max(control)},step=0")
    else:
        print("  ; type=BOOLEAN,access=rw------,values=1")
    print("  : values=" + mixer[control])
    if control == "Master Playback Volume":
        print("  | dBscale-min=-127.00dB,step=0.50dB,mute=0")
elif name == "aplay":
    assert args[:3] == ["-q", "-D", "plughw:0,0"] and len(args) == 4, args
    assert Path(args[3]).is_file(), args
    state["played"].append([Path(args[3]).name, dict(state["mixer"])])
    save()
elif name == "arecord":
    assert args[:-1] == ["-q", "-D", "hw:0,0", "-f", "S32_LE", "-r", "48000", "-c", "2", "-d", "8"], args
    shutil.copyfile(root / "fixture.wav", args[-1])
else:
    raise AssertionError("Unexpected command: " + name)
'''


def write_wav(path, seconds=8, rate=48000):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(4)
        wav.setframerate(rate)
        frames = bytearray()
        for n in range(seconds * rate):
            t = n / rate
            value = 1e-4 * math.sin(2 * math.pi * 7919 * t) + (0.05 * math.sin(2 * math.pi * 300 * t) if t >= 3 else 0)
            frames += struct.pack("<ii", int(value * 2**31), int(value * 2**31))
        wav.writeframes(bytes(frames))


class FirstContactToolTests(unittest.TestCase):
    REAL_TOOLS = ("cat", "grep", "tr", "sed", "head", "mkdir", "date", "dirname", "basename",
                  "readlink", "md5sum", "sha256sum", "mktemp", "rm", "sync", "python3", "tail")
    STUBS = ("amixer", "aplay", "arecord", "insmod", "rmmod", "modinfo", "pgrep", "journalctl",
             "logger", "id", "sleep")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sbae9drv-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.shell = shutil.which("sh")
        self.write("proc/sys/kernel/osrelease", KERNEL + "\n")
        self.write("proc/cmdline", CMDLINE + "\n")
        self.write("dev/null", "")
        for key, value in (("vendor", "0x1102"), ("device", "0x0010"), ("subsystem_vendor", "0x1102"),
                           ("subsystem_device", "0x0071")):
            self.write(f"{DEVICE}/{key}", value + "\n")
        for key, value in (("vendor", "0x1b21"), ("device", "0x1080"), ("revision", "0x03")):
            self.write(f"{BRIDGE}/{key}", value + "\n")
        links = self.path("sys/bus/pci/devices")
        links.mkdir(parents=True)
        (links / "0000:04:00.0").symlink_to(self.path(DEVICE))
        (links / "0000:03:00.0").symlink_to(self.path(BRIDGE))
        self.write(KO, "fixture module\n")
        self.main_fw, self.overlay_fw = b"main image fixture", b"overlay fixture"
        self.write_bytes("lib/firmware/creative-ae9/ctefx-desktop.bin", self.main_fw)
        self.write_bytes("lib/firmware/creative-ae9/ctefx-ae9-overlay.bin", self.overlay_fw)
        write_wav(self.path("fixture.wav"))
        tools = self.path("SbAe9-Drv/tools")
        tools.mkdir(parents=True)
        for helper in ("ae9_test_tones.py", "ae9_wav_stats.py"):
            shutil.copyfile(TOOLS / helper, tools / helper)
        self.bin = self.path("commands")
        self.bin.mkdir()
        for tool in self.REAL_TOOLS:
            executable = shutil.which(tool)
            self.assertIsNotNone(executable, tool)
            (self.bin / tool).symlink_to(executable)
        stub = (STUB.replace("ROOT_LITERAL", repr(str(self.root))).replace("DEVICE_LITERAL", repr(DEVICE))
                .replace("MODULE_DIR_LITERAL", repr(MODULE_DIR)).replace("KO_LITERAL", repr(KO)))
        for tool in self.STUBS:
            (self.bin / tool).write_text("#!" + sys.executable + " -IS\n" + stub)
            (self.bin / tool).chmod(0o700)
        self.state = {"uid": 1000, "running": [], "vermagic": KERNEL + " SMP preempt mod_unload modversions",
                      "parm": "board_d0:Windows' board-D0 GPIO step before codec 1's set-up (uint)\n"
                              "debug:Log detail (uint)",
                      "plain": [], "plain_parm": "debug:Log detail (uint)",
                      "probe": "ok", "kernel": [], "writes": [], "played": [], "master_max": 254,
                      "hp_master": 182, "switch_logged": True,
                      "mixer": {"Master Playback Volume": "214", "Master Playback Switch": "on",
                                "Head Unit Output": "0", "Head Unit Phantom Power": "off",
                                "Mic Gain Capture Volume": "1", "Capture Volume": "99"}}
        self.save()
        script = TOOL.read_text()
        path_line = "PATH=/usr/sbin:/usr/bin:/sbin:/bin"
        self.assertEqual(script.count(path_line), 1)
        script = script.replace(path_line, "PATH=" + str(self.bin))
        for constant, blob in (("MAIN_MD5", self.main_fw), ("OVERLAY_MD5", self.overlay_fw)):
            line = re.search(rf"^{constant}=([0-9a-f]{{32}})$", script, re.M)
            self.assertIsNotNone(line, constant)
            script = script.replace(line[0], f"{constant}={hashlib.md5(blob).hexdigest()}")
        relocated = ROOT_PATTERN.sub(lambda match: str(self.root) + match[0], script)
        self.assertIsNone(ROOT_PATTERN.search(relocated), "Unrelocated host path in test script")
        self.script = tools / "sbae9drv_test.sh"
        self.script.write_text(relocated)

    def path(self, relative):
        return self.root / relative

    def write(self, relative, data):
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)

    def write_bytes(self, relative, data):
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def save(self):
        self.path("state.json").write_text(json.dumps(self.state))

    def load(self):
        self.state = json.loads(self.path("state.json").read_text())
        return self.state

    def set_state(self, **values):
        self.load()
        self.state.update(values)
        self.save()

    def calls(self, name=None):
        path = self.path("calls.jsonl")
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if name is None or row[0] == name]

    def invoke(self, *args):
        return subprocess.run([self.shell, str(self.script), *args], cwd=self.root,
                              env={"PATH": str(self.bin), "LC_ALL": "C", "HOME": str(self.path("home"))},
                              capture_output=True, text=True, timeout=120)

    def ok(self, *args):
        result = self.invoke(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def refused(self, *args, message):
        result = self.invoke(*args)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("REFUSED: ", result.stderr)
        self.assertIn(message, result.stderr)
        return result

    def as_root(self):
        self.set_state(uid=0)

    def as_user(self):
        self.set_state(uid=1000)

    def loaded(self):
        self.as_root()
        self.ok("load")
        self.as_user()

    def test_constants_match_the_firmware_tool_and_the_ceiling(self):
        firmware = load_firmware_tool()
        script = TOOL.read_text()
        self.assertIn(f"MAIN_MD5={firmware.MAIN[2]}\n", script)
        self.assertIn(f"OVERLAY_MD5={firmware.OVERLAY[2]}\n", script)
        self.assertIn("CEILING=0\n", script)
        # 0.5 dB steps from -127 dB: 0 dB is 254, -36 dB (the boot volume) 254 - 72
        self.assertIn("CEILING_RAW=254\n", script)
        self.assertIn(f"TEST_RAW={254 - 36 * 2}\n", script)

    def test_check_reports_ready_and_writes_nothing(self):
        result = self.ok("check")
        self.assertIn("Ready for: sudo sh tools/sbae9drv_test.sh load", result.stdout)
        self.assertIn("Bridge 0000:03:00.0: 0x1b21:0x1080 rev 0x03", result.stdout)
        self.assertIn("version 0.12.4, sha256 " + hashlib.sha256(b"fixture module\n").hexdigest(), result.stdout)
        self.assertEqual(self.load()["writes"], [])
        self.assertEqual(self.calls("insmod"), [])

    def test_check_names_every_blocker(self):
        self.write("proc/sys/kernel/osrelease", "7.2.14-200.fc44.x86_64\n")
        self.write("proc/cmdline", "root=UUID=fixture module_blacklist=nouveau rd.driver.blacklist=snd_hda_intel\n")
        self.write_bytes("lib/firmware/creative-ae9/ctefx-desktop.bin", b"other image")
        self.path("lib/firmware/creative-ae9/ctefx-ae9-overlay.bin").unlink()
        self.set_state(running=["wireplumber"], vermagic="7.2.14-200.fc44.x86_64 SMP", parm="debug:Log detail")
        result = self.ok("check")
        firmware = str(self.path("lib/firmware/creative-ae9"))
        for blocker in ("kernel is not " + KERNEL,
                        "snd_hda_intel is not in module_blacklist=",
                        "module vermagic is \"7.2.14-200.fc44.x86_64\"",
                        f"firmware {firmware}/ctefx-desktop.bin is missing or not the tested image",
                        f"firmware {firmware}/ctefx-ae9-overlay.bin is missing or not the tested image",
                        "PipeWire is running"):
            self.assertIn("NOT READY: " + blocker, result.stdout)
        self.assertEqual(result.stdout.count("NOT READY: "), 6)
        self.assertNotIn("Ready for:", result.stdout)

    def test_load_refuses_a_user_and_every_blocker(self):
        self.refused("load", message="load needs root")
        self.as_root()
        self.set_state(running=["pipewire"])
        self.refused("load", message="PipeWire is running")
        self.set_state(running=[])
        self.write("proc/cmdline", "root=UUID=fixture quiet\n")
        self.refused("load", message="snd_hda_intel is not in module_blacklist=")
        self.write("proc/cmdline", CMDLINE + "\n")
        self.path(KO).unlink()
        self.refused("load", message="is missing (build it in")
        self.assertEqual(self.calls("insmod"), [])

    def test_load_refuses_a_bound_card_or_a_loaded_module(self):
        self.as_root()
        driver = self.path("sys/bus/pci/drivers/snd_hda_intel")
        driver.mkdir(parents=True)
        (self.path(DEVICE) / "driver").symlink_to(driver)
        self.refused("load", message="already bound to snd_hda_intel")
        (self.path(DEVICE) / "driver").unlink()
        self.path(MODULE_DIR).mkdir(parents=True)
        self.refused("load", message="snd_hda_codec_creative_ae9 is already loaded")
        self.assertEqual(self.calls("insmod"), [])

    def test_load_inserts_without_options_and_reports(self):
        self.as_root()
        result = self.ok("load")
        self.assertEqual(self.calls("insmod"), [["insmod", str(self.path(KO))]])
        self.assertIn("seen: board D0: GPIO pin 0 pulsed, the DAC answers", result.stdout)
        names = [row[0] for row in self.calls()]
        self.assertLess(names.index("journalctl"), names.index("insmod"), "journal synced before insmod")
        for seen in ("posted_flush: every register write is read back (ASMedia bridge)",
                     "64 KB spare after each DMA buffer", "skipping the secondary bus reset",
                     "DSP firmware load: 0"):
            self.assertIn("seen: ", result.stdout)
            self.assertIn(seen, result.stdout)
        self.assertNotIn("MISSING", result.stdout)
        self.assertNotIn("unrelated", result.stdout)
        self.assertIn("LOADED: card 0 (AE9)", result.stdout)
        self.assertEqual(self.load()["writes"], [])

    def plain_module(self):
        relative = "SbAe9-Drv-0.12.5/snd-hda-codec-creative-ae9.ko"
        self.write(relative, "plain upstream module\n")
        self.set_state(plain=[str(self.path(relative))])
        return self.path(relative)

    def test_check_and_load_take_a_plain_build_by_path(self):
        plain = self.plain_module()
        result = self.ok("check", str(plain))
        self.assertIn(f"Module {plain}: version", result.stdout)
        self.assertIn("without the GPIO pin 0 step (plain upstream)", result.stdout)
        self.assertIn("Ready for:", result.stdout)
        self.as_root()
        result = self.ok("load", str(plain))
        self.assertEqual(self.calls("insmod"), [["insmod", str(plain)]])
        self.assertNotIn("MISSING", result.stdout)
        self.assertNotIn("seen: board D0", result.stdout)
        self.assertIn("bus reset: done", result.stdout)
        self.assertIn("head unit: 246 SPI frames (10 without reply)", result.stdout)
        self.assertIn("LOADED: card 0 (AE9)", result.stdout)
        self.assertFalse(self.path(MODULE_DIR + "/parameters/board_d0").exists())
        self.as_user()
        self.ok("level")  # a module without board_d0 is accepted

    def test_load_reports_the_fixed_build(self):
        self.as_root()
        result = self.ok("load")
        self.assertIn("seen: board D0: GPIO pin 0 pulsed, the DAC answers", result.stdout)
        self.assertIn("bus reset: skipped", result.stdout)
        self.assertIn("head unit: 246 SPI frames (0 without reply)", result.stdout)
        self.assertIn("with the GPIO pin 0 step (board_d0)", self.ok("check").stdout)

    def test_load_refuses_a_wrong_module_path(self):
        self.as_root()
        self.refused("load", str(self.path("other.ko")), message="must be a snd-hda-codec-creative-ae9.ko")
        self.refused("load", str(self.path("missing/snd-hda-codec-creative-ae9.ko")), message="No module at")
        self.refused("load", "a", "b", message="At most one module path")
        self.refused("level", "x", message="Exactly one argument")
        self.assertEqual(self.calls("insmod"), [])

    def test_a_loaded_module_with_the_step_off_is_refused(self):
        self.loaded()
        self.write(MODULE_DIR + "/parameters/board_d0", "0\n")
        self.refused("level", message="board_d0 is '0', not 1")

    def test_load_fails_when_the_dac_does_not_answer(self):
        self.as_root()
        self.set_state(probe="deaf")
        self.refused("load", message="The DAC does not answer after the board-D0 step")

    def test_load_fails_without_a_card_or_without_init_complete(self):
        self.as_root()
        self.set_state(probe="fail")
        result = self.refused("load", message="not bound to snd-hda-codec-creative-ae9")
        self.assertIn("DSP did not start", result.stdout)

    def test_load_fails_when_the_driver_never_completes(self):
        self.as_root()
        self.set_state(probe="incomplete")
        self.refused("load", message='No "Init complete" from the driver')

    def test_level_needs_the_loaded_driver(self):
        self.refused("level", message="not bound to snd-hda-codec-creative-ae9")
        self.assertEqual(self.calls("amixer"), [])

    def test_level_refuses_an_unexpected_range(self):
        self.loaded()
        self.set_state(master_max=184)
        self.refused("level", message="ends at '184', not 254")
        self.assertEqual(self.load()["writes"], [])

    def test_level_proves_the_ceiling_then_sets_minus_36(self):
        self.loaded()
        result = self.ok("level")
        self.assertEqual(self.load()["writes"], [["Master Playback Volume", "100%"],
                                                 ["Master Playback Volume", "182"]])
        self.assertEqual(self.load()["mixer"]["Master Playback Volume"], "182")
        self.assertIn("100% = 0 dB: the range ends at the owner's level.", result.stdout)
        self.assertEqual(self.state["mixer"]["Master Playback Volume"], "182")

    def test_level_refuses_while_a_stream_is_open(self):
        self.loaded()
        self.write("proc/asound/card0/pcm0p/sub0/status", "state: RUNNING\nowner_pid: 42\n")
        self.refused("level", message="pcm0p/sub0: RUNNING")
        self.assertEqual(self.load()["writes"], [])

    def test_headphones_needs_level_first(self):
        self.loaded()
        self.refused("headphones", message="Master is 214, above 182")
        self.assertEqual(self.load()["writes"], [])

    def test_headphones_switches_and_caps_the_headphone_volume(self):
        self.loaded()
        self.ok("level")
        self.set_state(hp_master=190)
        result = self.ok("headphones")
        writes = self.load()["writes"][2:]
        self.assertEqual(writes, [["Head Unit Output", "Headphones"], ["Master Playback Volume", "182"]])
        self.assertIn("Head unit on headphones.", result.stdout)

    def test_headphones_refuses_without_the_drivers_confirmation(self):
        self.loaded()
        self.ok("level")
        self.set_state(switch_logged=False)
        self.refused("headphones", message="did not report the switch to headphones within 10 s")
        self.assertEqual(len(self.calls("sleep")), 10 + 1)  # 10 polls, plus the one after insmod

    def test_headphones_accepts_a_head_unit_already_on_headphones(self):
        self.loaded()
        self.ok("level")
        self.load()
        self.state["mixer"]["Head Unit Output"] = "1"
        self.save()
        self.set_state(switch_logged=False)
        result = self.ok("headphones")
        self.assertEqual(self.load()["writes"][2:], [], "no switch when it is already on headphones")
        self.assertIn("already Headphones", result.stdout)
        self.assertIn("Head unit on headphones.", result.stdout)

    def test_tones_need_headphones_and_the_test_level(self):
        self.loaded()
        self.refused("tones", message="not on Headphones")
        self.ok("level")
        self.ok("headphones")
        self.load()
        self.state["mixer"]["Master Playback Volume"] = "184"
        self.save()
        self.refused("tones", message="Master is 184, above 182")
        self.assertEqual(self.calls("aplay"), [])

    def test_tones_play_three_levels_quietest_first(self):
        self.loaded()
        self.ok("level")
        self.ok("headphones")
        self.ok("tones")
        played = self.load()["played"]
        self.assertEqual([name for name, _ in played],
                         ["ae9-tone-m60dbfs.wav", "ae9-tone-m50dbfs.wav", "ae9-tone-m40dbfs.wav"])
        for _, mixer in played:
            self.assertEqual(mixer["Head Unit Output"], "1")
            self.assertLessEqual(int(mixer["Master Playback Volume"]), 182)

    def test_ladder_needs_headphones_then_steps_up_and_returns(self):
        self.loaded()
        self.refused("ladder", message="not on Headphones")
        self.ok("level")
        self.ok("headphones")
        result = self.ok("ladder")
        played = self.load()["played"]
        self.assertEqual([name for name, _ in played], ["ae9-tone-m40dbfs.wav"] * 4)
        self.assertEqual([mixer["Master Playback Volume"] for _, mixer in played], ["182", "202", "222", "242"])
        for _, mixer in played:
            self.assertEqual(mixer["Head Unit Output"], "1")
        self.assertEqual(self.state["mixer"]["Master Playback Volume"], "182")
        for db in ("-36", "-26", "-16", "-6"):
            self.assertIn(f"Master {db} dB, -40 dBFS", result.stdout)
        self.assertIn("Master back at -36 dB.", result.stdout)

    def test_mic_refuses_phantom_power_then_records_and_analyses(self):
        self.loaded()
        self.load()
        self.state["mixer"]["Head Unit Phantom Power"] = "on"
        self.save()
        self.refused("mic", message="Phantom power (48 V) is on")
        self.load()
        self.state["mixer"]["Head Unit Phantom Power"] = "off"
        self.save()
        result = self.ok("mic")
        self.assertEqual(len(self.calls("arecord")), 1)
        self.assertIn(">>> SPEAK NOW", result.stdout)
        self.assertIn("VERDICT: speech clearly present", result.stdout)
        recordings = list(self.path("home/.local/state/sb-ae9/sbae9drv").glob("mic-*.wav"))
        self.assertEqual(len(recordings), 1)
        self.assertEqual(self.load()["writes"], [])

    def test_unload_needs_root_no_pipewire_and_no_stream(self):
        self.loaded()
        self.refused("unload", message="unload needs root")
        self.as_root()
        self.set_state(running=["pipewire-pulse"])
        self.refused("unload", message="PipeWire is running")
        self.set_state(running=[])
        self.write("proc/asound/card0/pcm0c/sub0/status", "state: RUNNING\n")
        self.refused("unload", message="pcm0c/sub0: RUNNING")
        self.assertEqual(self.calls("rmmod"), [])

    def test_unload_removes_the_module_and_reports_the_shutdown(self):
        self.loaded()
        self.as_root()
        result = self.ok("unload")
        self.assertEqual(self.calls("rmmod"), [["rmmod", "snd_hda_codec_creative_ae9"]])
        self.assertIn("UNLOADED: shutdown sequence done.", result.stdout)
        self.assertFalse(self.path(MODULE_DIR).exists())

    def test_dac_needs_root_the_card_its_debugfs_headphones_and_no_stream(self):
        self.as_root()
        self.refused("dac", message="run load first")
        self.loaded()
        self.refused("dac", message="dac needs root")
        self.as_root()
        self.refused("dac", message="Cannot write")
        self.write("sys/kernel/debug/creative_ae9/cmd", "")
        self.refused("dac", message="not on Headphones")
        self.write("proc/asound/card0/pcm0p/sub0/status", "state: RUNNING\n")
        self.refused("dac", message="pcm0p/sub0: RUNNING")
        self.write("proc/asound/card0/pcm0p/sub0/status", "closed\n")
        self.load()
        self.state["mixer"]["Head Unit Output"] = "1"
        self.state["mixer"]["Master Playback Volume"] = "214"
        self.save()
        self.refused("dac", message="Master is 214, above 182")
        self.assertEqual(self.calls("aplay"), [])
        self.assertEqual(self.load()["writes"], [])

    def test_pin0_needs_root_the_card_its_debugfs_and_no_stream(self):
        self.as_root()
        self.refused("pin0", message="run load first")
        self.loaded()
        self.refused("pin0", message="pin0 needs root")
        self.as_root()
        self.refused("pin0", message="Cannot write")
        self.write("sys/kernel/debug/creative_ae9/cmd", "")
        self.write("proc/asound/card0/pcm0p/sub0/status", "state: RUNNING\n")
        self.refused("pin0", message="pcm0p/sub0: RUNNING")
        self.assertEqual(self.calls("aplay"), [])
        self.assertEqual(self.load()["writes"], [])

    def test_card_stages_refuse_root(self):
        self.loaded()
        self.as_root()
        for stage in ("level", "headphones", "tones", "ladder", "mic"):
            self.refused(stage, message=f"Run {stage} as your user")


class FirmwareToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sbae9drv-fw-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tool = load_firmware_tool()
        self.main, self.overlay = main_image(), overlay_image()
        self.assertEqual(len(self.main), 655436)
        self.assertEqual(len(self.overlay), 432)
        # synthetic images: pin their MD5s in place of the real ones
        self.tool.MAIN = (self.tool.MAIN[0], self.tool.MAIN[1], hashlib.md5(self.main).hexdigest())
        self.tool.OVERLAY = (self.tool.OVERLAY[0], self.tool.OVERLAY[1], hashlib.md5(self.overlay).hexdigest())

    def run_tool(self, data):
        source = self.root / "CtxHda.sys"
        source.write_bytes(data)
        return self.tool.main(["--sys", str(source), "--out", str(self.root / "out")])

    def test_extracts_both_images_and_reruns_cleanly(self):
        trailer = fls([(0x0B7B10, list(range(7618))), (0x03F3A0, list(range(44)))])
        self.assertEqual(self.run_tool(ctxhda(self.main, self.overlay, trailer)), 0)
        folder = self.root / "out/creative-ae9"
        self.assertEqual((folder / "ctefx-desktop.bin").read_bytes(), self.main)
        self.assertEqual((folder / "ctefx-ae9-overlay.bin").read_bytes(), self.overlay)
        self.assertEqual(self.run_tool(ctxhda(self.main, self.overlay, trailer)), 0)

    def test_refuses_an_untested_image(self):
        self.assertEqual(self.run_tool(ctxhda(main_image(seed=2), self.overlay)), 1)
        self.assertEqual(self.run_tool(ctxhda(self.main, overlay_image(seed=8))), 1)
        self.assertFalse((self.root / "out").exists())

    def test_refuses_a_second_image_that_is_not_the_overlay(self):
        wrong = fls([(0x0B7B10, list(range(77))), (0x03F3A0, list(range(22)))])
        self.assertEqual(self.run_tool(ctxhda(self.main, wrong)), 1)
        self.assertEqual(self.run_tool(self.main), 1)  # one image only

    def test_refuses_to_replace_a_different_file(self):
        folder = self.root / "out/creative-ae9"
        folder.mkdir(parents=True)
        (folder / "ctefx-desktop.bin").write_bytes(b"something else")
        self.assertEqual(self.run_tool(ctxhda(self.main, self.overlay)), 1)
        self.assertEqual((folder / "ctefx-desktop.bin").read_bytes(), b"something else")
        self.assertFalse((folder / "ctefx-ae9-overlay.bin").exists())

    def test_truncated_images_are_skipped(self):
        found = self.tool.images(self.main[:-8] + b"\x00" * 4)
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
