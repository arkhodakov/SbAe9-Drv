#!/usr/bin/env python3
"""Host-only tests for tools/ae9_wav_stats.py (level statistics and the mic verdict).

Moved here from sb-ae9's tools/tests/test_ae9_mic.py (4ae84b2) with the tool.
Run: python3 tools/tests/test_ae9_wav_stats.py
"""
import math
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.dont_write_bytecode = True
STATS = Path(__file__).resolve().parents[1] / "ae9_wav_stats.py"


def write_wav(path, signal, seconds=8, rate=48000):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(4)
        wav.setframerate(rate)
        frames = bytearray()
        for n in range(seconds * rate):
            value = max(-1.0, min(signal(n / rate), 0.999999))
            frames += struct.pack("<ii", int(value * 2**31), int(value * 2**31))
        wav.writeframes(bytes(frames))


def speech(t):
    noise = 1e-4 * math.sin(2 * math.pi * 7919 * t)
    return noise + (0.05 * math.sin(2 * math.pi * 300 * t) if t >= 3 else 0)


class WavStatsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ae9-wav-test-")
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)

    def stats(self, signal, *extra):
        path = self.dir / "x.wav"
        write_wav(path, signal)
        result = subprocess.run([sys.executable, "-B", str(STATS), *extra, str(path)],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_verdicts(self):
        self.assertIn("speech clearly present", self.stats(speech, "--split", "3"))
        self.assertIn("digital silence", self.stats(lambda t: 0.0, "--split", "3"))
        self.assertIn("no speech found", self.stats(lambda t: 1e-4, "--split", "3"))
        self.assertIn("weak signal", self.stats(
            lambda t: 1e-3 * math.sin(2 * math.pi * 300 * t) * (2 if t >= 3 else 1), "--split", "3"))
        self.assertIn("CLIPPED", self.stats(lambda t: 1.0 if t >= 3 else 1e-4, "--split", "3"))

    def test_a_sound_in_the_silent_part_does_not_hide_the_speech(self):
        def recording(t):
            if t < 1.5:
                return 0.3 * math.sin(2 * math.pi * 200 * t)   # a word before the prompt
            if t < 3:
                return 1e-3 * math.sin(2 * math.pi * 50 * t)   # the real silence
            return 0.05 * math.sin(2 * math.pi * 300 * t) if t < 5 else 1e-3 * math.sin(2 * math.pi * 50 * t)

        out = self.stats(recording, "--split", "3")
        self.assertIn("NOTE: the silent part was not silent", out)
        self.assertIn("VERDICT: speech clearly present: +34.0 dB", out)

    def test_a_steady_noise_has_no_speech_even_with_windows(self):
        out = self.stats(lambda t: 1e-2 * math.sin(2 * math.pi * 1000 * t), "--split", "3")
        self.assertIn("no speech found (+0.0 dB)", out)
        self.assertNotIn("NOTE:", out)

    def test_levels(self):
        out = self.stats(speech, "--split", "3")
        self.assertRegex(out, r"speech  L: peak +-26\.0 dBFS, RMS +-29\.0 dBFS")

    def test_unreadable_file(self):
        path = self.dir / "bad.wav"
        path.write_bytes(b"not a wav")
        result = subprocess.run([sys.executable, "-B", str(STATS), str(path)],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
