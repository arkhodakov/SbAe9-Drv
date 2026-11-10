#!/usr/bin/env python3
"""Deterministic attenuated test tones for the AE-9 playback experiment.

Each file: 48 kHz, stereo, S16_LE. 0.5 s silence, 1 kHz on the LEFT channel
only for 1.0 s, 0.5 s silence, 1 kHz on the RIGHT channel only for 1.0 s,
0.5 s silence. Tone edges use 50 ms raised-cosine ramps. The integer peak is
rounded DOWN, so every sample satisfies |x| <= 32768 * 10^(level/20).

Without --out, only print the SHA-256 of each file. With --out DIR, write new
files there and refuse to overwrite anything. No audio device is touched.
"""

import argparse
import hashlib
import io
import math
import os
import struct
import wave
from pathlib import Path

RATE = 48000
FREQUENCY = 1000
LEVELS = (-60, -50, -40)
SILENCE = RATE // 2
TONE = RATE
RAMP = RATE * 50 // 1000


def name(level):
    return f"ae9-tone-m{-level}dbfs.wav"


def peak(level):
    """Largest permitted integer amplitude for a dBFS level (full scale 32768)."""
    return math.floor(32768 * 10 ** (level / 20))


def tone_samples(amplitude):
    samples = []
    for n in range(TONE):
        if n < RAMP:
            envelope = 0.5 - 0.5 * math.cos(math.pi * n / RAMP)
        elif n >= TONE - RAMP:
            envelope = 0.5 - 0.5 * math.cos(math.pi * (TONE - 1 - n) / RAMP)
        else:
            envelope = 1.0
        value = round(amplitude * envelope * math.sin(2 * math.pi * FREQUENCY * n / RATE))
        samples.append(max(-amplitude, min(amplitude, value)))
    return samples


def render(level):
    if level not in LEVELS:
        raise ValueError(f"Unsupported level: {level}")
    tone = tone_samples(peak(level))
    silence = [(0, 0)] * SILENCE
    frames = (silence + [(value, 0) for value in tone] + silence
              + [(0, value) for value in tone] + silence)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(b"".join(struct.pack("<hh", left, right) for left, right in frames))
    return buffer.getvalue()


def digests():
    return {name(level): hashlib.sha256(render(level)).hexdigest() for level in LEVELS}


def write(directory):
    directory = Path(directory)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError(f"Output must be an existing real directory: {directory}")
    for level in LEVELS:
        target = directory / name(level)
        # O_EXCL: never overwrite or follow an existing file/symlink.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(render(level))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    if args.out:
        write(args.out)
    for filename, digest in digests().items():
        print(f"{digest}  {filename}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
