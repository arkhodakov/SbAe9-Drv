#!/usr/bin/env python3
"""Extract the AE-9 DSP firmware for the 3xHelix SbAe9-Drv driver from CtxHda.sys.

  python3 -B tools/sbae9drv_firmware.py --sys local/CtxHda.sys --out local/sbae9drv-fw

CtxHda.sys (Creative's Windows driver) embeds several FLS images: chains of
{u32 magic "MXFL", u32 chip address, u32 count, u32 data[count]} segments,
ended by a segment with count 0. The SbAe9-Drv driver loads two of them from
/lib/firmware/creative-ae9/:

- ctefx-desktop.bin: the main DSP image, the first one of 655436 bytes;
- ctefx-ae9-overlay.bin: the second image, 432 bytes, segments 78 words at
  chip 0x0b7b10 and 21 words at 0x03f3a0.

Both must match the MD5 of the images the driver is tested with (its
extract_firmware.py and extract_overlay.py); anything else is refused. This is
our own reader of the format: no code from the driver's repository runs.
Writes OUT/creative-ae9/<name> and refuses to replace a different existing
file. Reads and writes only these files; no device is touched. Install with:

  sudo install -D -m 0644 -t /lib/firmware/creative-ae9 OUT/creative-ae9/*.bin
"""

import argparse
import hashlib
import struct
import sys
from pathlib import Path

MAGIC = 0x4C46584D  # "MXFL"
MAIN = ("ctefx-desktop.bin", 655436, "85c8f2b47180cd6623a2b0e8b8911fe0")
OVERLAY = ("ctefx-ae9-overlay.bin", 432, "0e222c941f708a2caf93d50d2aba5ef8")
OVERLAY_SEGMENTS = [(0x0B7B10, 78), (0x03F3A0, 21)]


def images(data):
    """Every complete FLS image as (offset, bytes, [(chip address, count), ...])."""
    found, position, pattern = [], 0, struct.pack("<I", MAGIC)
    while True:
        start = data.find(pattern, position)
        if start < 0:
            return found
        offset, segments, complete = start, [], False
        while offset + 12 <= len(data):
            magic, address, count = struct.unpack_from("<III", data, offset)
            if magic != MAGIC:
                break
            segments.append((address, count))
            offset += 12 + count * 4
            if count == 0:
                complete = offset <= len(data)
                break
        if complete:
            found.append((start, data[start:offset], segments))
            position = offset
        else:
            position = start + 4


def select(data):
    """The (name, bytes) pairs to install; raises ValueError for anything unexpected."""
    found = images(data)
    main = [blob for _, blob, _ in found if len(blob) == MAIN[1]]
    if not main:
        raise ValueError(f"no {MAIN[1]}-byte FLS image (the main DSP image) in the file")
    if len(found) < 2:
        raise ValueError("fewer than two FLS images in the file")
    overlay, segments = found[1][1], found[1][2]
    if segments[:2] != OVERLAY_SEGMENTS or len(overlay) != OVERLAY[1]:
        raise ValueError(f"the second FLS image is not the overlay: {len(overlay)} bytes, "
                         f"segments {[(hex(a), c) for a, c in segments[:3]]}")
    chosen = []
    for (name, _, want), blob in ((MAIN, main[0]), (OVERLAY, overlay)):
        md5 = hashlib.md5(blob).hexdigest()
        if md5 != want:
            raise ValueError(f"{name}: MD5 {md5} is not the tested image ({want})")
        chosen.append((name, blob))
    return chosen


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sys", required=True, type=Path, help="CtxHda.sys from the Windows driver")
    parser.add_argument("--out", required=True, type=Path, help="output directory")
    args = parser.parse_args(argv)
    try:
        chosen = select(args.sys.read_bytes())
    except (OSError, ValueError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 1
    folder = args.out / "creative-ae9"
    for name, blob in chosen:
        target = folder / name
        if target.exists() and target.read_bytes() != blob:
            print(f"REFUSED: {target} exists with different content", file=sys.stderr)
            return 1
    folder.mkdir(parents=True, exist_ok=True)
    for name, blob in chosen:
        target = folder / name
        if not target.exists():
            target.write_bytes(blob)
        print(f"{target}: {len(blob)} bytes, md5 {hashlib.md5(blob).hexdigest()}, "
              f"sha256 {hashlib.sha256(blob).hexdigest()}")
    print(f"Install: sudo install -D -m 0644 -t /lib/firmware/creative-ae9 {folder}/*.bin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
