#!/usr/bin/env python3
"""Level statistics of a PCM WAV recording, per channel; read-only, no dependencies.

  python3 -B tools/ae9_wav_stats.py [--split SECONDS] FILE.wav

With --split, the part before SECONDS (silence) and after it (speech) are
reported separately, which gives the noise floor and the speech level of one
XLR test recording. Levels are dBFS: 0 dBFS is a full-scale square wave peak.

The verdict compares 250 ms windows: the quietest one of the silent part (the
noise floor) with the loudest one of the speech part, so a cough or a word in
the silent part does not hide the speech; a NOTE line says when that happened.
"""

import argparse
import math
import struct
import sys
import wave


def read(path):
    with wave.open(str(path), "rb") as wav:
        channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if width not in (2, 3, 4):
        raise ValueError(f"unsupported sample width {width} bytes")
    if width == 3:
        raw = [int.from_bytes(frames[i:i + 3], "little", signed=True) for i in range(0, len(frames), 3)]
    else:
        raw = list(struct.unpack("<%d%s" % (len(frames) // width, "h" if width == 2 else "i"), frames))
    full = float(1 << (8 * width - 1))
    return rate, [[sample / full for sample in raw[c::channels]] for c in range(channels)]


def db(value):
    return -math.inf if value <= 0 else 20 * math.log10(value)


def stats(samples):
    if not samples:
        return None
    peak = max(abs(s) for s in samples)
    mean = sum(samples) / len(samples)
    rms = math.sqrt(sum((s - mean) ** 2 for s in samples) / len(samples))
    return {"peak": db(peak), "rms": db(rms), "dc": mean,
            "zero": sum(1 for s in samples if s == 0) / len(samples),
            "clipped": sum(1 for s in samples if abs(s) >= 0.999)}


def describe(label, rate, channels, start, end):
    lines = []
    for index, data in enumerate(channels):
        part = data[int(start * rate):None if end is None else int(end * rate)]
        result = stats(part)
        if result is None:
            continue
        name = "L" if index == 0 else "R" if index == 1 else str(index)
        lines.append(f"{label} {name}: peak {result['peak']:7.1f} dBFS, RMS {result['rms']:7.1f} dBFS, "
                     f"DC {result['dc']:+.5f}, zero samples {result['zero']:.0%}, clipped {result['clipped']}")
    return lines


WINDOW = 0.25  # seconds per level window of the verdict
# Window levels of plain noise spread by a few dB (the dead-mic recording of
# 2026-11-10 11:27: +3.6 dB), so the thresholds sit above that.
CLEAR_DB = 15.0
WEAK_DB = 6.0


def windows(samples, rate, start):
    """(start second, RMS dBFS) of every whole window; one short part counts as one window."""
    size = max(1, int(rate * WINDOW))
    if len(samples) < size:
        return [(start, stats(samples)["rms"])] if samples else []
    return [(start + i / rate, stats(samples[i:i + size])["rms"])
            for i in range(0, len(samples) - size + 1, size)]


def gap_db(top, floor):
    if top == -math.inf:
        return 0.0  # constant signal, nothing above it
    if floor == -math.inf:
        return math.inf
    return top - floor


def verdict(rate, channels, split):
    if all(all(s == 0 for s in data) for data in channels):
        return "VERDICT: digital silence (all samples zero): no signal reached the stream."
    if split is None:
        return "VERDICT: non-zero data; compare against a recording with speech."
    cut = int(split * rate)
    best = None
    for data in channels:
        quiet = windows(data[:cut], rate, 0.0)
        loud = windows(data[cut:], rate, split)
        if not quiet or not loud:
            continue
        floor = min(quiet, key=lambda w: w[1])
        busy = max(quiet, key=lambda w: w[1])
        top = max(loud, key=lambda w: w[1])
        gap = gap_db(top[1], floor[1])
        if best is None or gap > best[0]:
            best = (gap, floor, busy, top, stats(data[cut:])["clipped"])
    if best is None:
        return "VERDICT: the recording is too short to compare its parts."
    gap, floor, busy, top, clipped = best
    lines = []
    if gap_db(busy[1], floor[1]) >= 10:
        lines.append(f"NOTE: the silent part was not silent: {busy[1]:.1f} dBFS at {busy[0]:.2f} s, "
                     f"its quietest {floor[1]:.1f} dBFS at {floor[0]:.2f} s.")
    where = f"loudest {top[1]:.1f} dBFS at {top[0]:.2f} s over the noise floor {floor[1]:.1f} dBFS"
    if clipped:
        lines.append(f"VERDICT: speech present (+{gap:.1f} dB, {where}) but CLIPPED: lower Mic Boost.")
    elif gap >= CLEAR_DB:
        lines.append(f"VERDICT: speech clearly present: +{gap:.1f} dB ({where}).")
    elif gap >= WEAK_DB:
        lines.append(f"VERDICT: weak signal: speech only +{gap:.1f} dB ({where}); try Mic Boost 20.")
    else:
        lines.append(f"VERDICT: no speech found (+{gap:.1f} dB): noise or a constant signal only.")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", type=float, help="seconds of silence at the start")
    parser.add_argument("file")
    args = parser.parse_args(argv)
    try:
        rate, channels = read(args.file)
    except (OSError, EOFError, ValueError, wave.Error) as error:
        print(f"Cannot read {args.file}: {error}", file=sys.stderr)
        return 1
    seconds = len(channels[0]) / rate if channels and rate else 0
    print(f"{args.file}: {len(channels)} ch, {rate} Hz, {seconds:.2f} s")
    if args.split is not None and 0 < args.split < seconds:
        lines = describe("silence", rate, channels, 0, args.split)
        lines += describe("speech ", rate, channels, args.split, None)
        split = args.split
    else:
        lines = describe("all", rate, channels, 0, None)
        split = None
    print("\n".join(lines))
    print(verdict(rate, channels, split))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
