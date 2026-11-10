#!/usr/bin/env python3
"""Host-only tests for tools/ae9_test_tones.py, the tones the runner plays.

Run: python3 tools/tests/test_ae9_test_tones.py
"""
import importlib.util
import io
import math
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.dont_write_bytecode = True
TOOL = Path(__file__).resolve().parents[1] / "ae9_test_tones.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("ae9_test_tones", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tones = load_tool()


def frames(level):
    with wave.open(io.BytesIO(tones.render(level)), "rb") as wav:
        layout = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate())
        data = memoryview(wav.readframes(wav.getnframes())).cast("h")
    return layout, data


class TestTonesTests(unittest.TestCase):
    def test_levels_and_layout(self):
        self.assertEqual(tones.LEVELS, (-60, -50, -40))
        for level in tones.LEVELS:
            layout, data = frames(level)
            self.assertEqual(layout, (2, 2, 48000))
            self.assertEqual(len(data) // 2, 48000 * 7 // 2)  # 0.5 + 1 + 0.5 + 1 + 0.5 s

    def test_peak_never_exceeds_the_level(self):
        for level in tones.LEVELS:
            _, data = frames(level)
            limit = math.floor(32768 * 10 ** (level / 20))
            self.assertLessEqual(max(abs(x) for x in data), limit)
            self.assertGreaterEqual(max(abs(x) for x in data), limit - 1)

    def test_left_then_right(self):
        _, data = frames(-40)
        left, right = data[0::2], data[1::2]
        second = 48000
        self.assertTrue(any(left[second:2 * second]) and not any(right[second:2 * second]))
        self.assertTrue(any(right[2 * second + second // 2:3 * second + second // 2]))
        self.assertFalse(any(left[2 * second + second // 2:3 * second + second // 2]))
        self.assertFalse(any(left[:second // 2]) or any(right[:second // 2]))

    def test_deterministic_and_refuses_to_overwrite(self):
        self.assertEqual(tones.render(-50), tones.render(-50))
        with tempfile.TemporaryDirectory(prefix="ae9-tones-test-") as directory:
            self.assertEqual(tones.main(["--out", directory]), 0)
            written = sorted(p.name for p in Path(directory).iterdir())
            self.assertEqual(written, sorted(tones.name(level) for level in tones.LEVELS))
            with self.assertRaises(FileExistsError):
                tones.main(["--out", directory])


if __name__ == "__main__":
    unittest.main(verbosity=2)
