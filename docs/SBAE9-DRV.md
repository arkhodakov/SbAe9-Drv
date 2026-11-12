# 3xHelix SbAe9-Drv: review and first contact

> **Where this lives.** The `workbench` branch of the owner's fork
> (`arkhodakov/SbAe9-Drv`): upstream `main` (0.12.6 since 2026-11-12) + the fixes of 3xHelix/SbAe9-Drv#3 +
> our test tools (`tools/`, `tools/tests/`) and this run card. The tools, their
> tests and this file came from sb-ae9 at `4ae84b2` on 2026-11-10; sb-ae9 itself
> is no longer developed. "sb-ae9's" below names files that stayed there. Private
> files go in the git-ignored `local/` of this repository. Host-only tests:
> `for t in tools/tests/test_*.py; do python3 -B "$t"; done`.

<https://github.com/3xHelix/SbAe9-Drv> is an out-of-tree AE-9 driver written
from scratch. Its module is `snd-hda-codec-creative-ae9`.

- **Based on Windows MMIO captures.** It replays what Creative's Windows driver
  does in VFIO MMIO captures, and adapts parts of the kernel's ca0132 code.
- **Its own PCI driver.** It binds the PCI device itself and runs its own
  CORB/RIRB. It does not use `snd_hda_intel`. From `snd-hda-core` it imports one
  function, `snd_hdac_stream_format()`, which only computes a stream format word.
  It registers no HDA bus, so the state of the shared HDA core is never touched.
- **Release reviewed:** v0.12.4 (2026-11-09, commit `9f989d1`), license
  GPL-2.0-or-later. Only the code was read; none of its scripts were run.
- **Tested by the author** on Fedora 44 with kernels 7.2.13/14/15, on two cards.
  One of them (Kaiba's) sits behind the same bridge as ours: ASMedia `1b21:1080`
  rev 03.
- **Community:** DarkNikGr (sb-ae9's `docs/FORK-DARKNIKGR.md`), s3boun3t and
  Kaiba now contribute here. DarkNikGr's sb-ae9 has had no push since
  2026-11-06.
- **Features:** everything in our plan (headphones through the ACM, XLR capture,
  knob and display, 48 V, suspend). Beyond our plan: SBX/EQ, 5.1, optical, What U
  Hear, Voice Clarity, and the [SbAe9-SBC](https://github.com/3xHelix/SbAe9-SBC)
  control panel.

On 2026-11-10 the owner decided to try it before desktop run 3 of our ca0132 path.

**First contact, 2026-11-10 (session 1):** the load was clean (every expected line,
firmware on the first try, no freeze). The −35 dB ceiling held; udev's `alsactl` had
set Master to the top of the range. But nothing was heard: neither the tones
(−60/−50/−40 dBFS at Master −36 dB, HD 650 on the 1/4" jack, ACM gain switch at
High) nor speech on the mic (FIFINE K669D, dynamic, XLR, no 48 V; the recording held
only the noise floor, −67.6 dBFS RMS). The relay click was not heard. After the
session the owner set the maximum level to **0 dB** (no ceiling: `hu_max_db=0`);
the runner follows it, and its `ladder` stage steps a −40 dBFS tone through Master
−36, −26, −16 and −6 dB. The session table below still shows the −35 dB values.

**Session 1, continued** (same boot; module reloaded with `hu_max_db=0`):

- **Mic: the cause is found and was fixed by hand.**
  - The driver's boot replay sends the ACM's init frames right after it powers the
    ACM on (GPIO 5), with no wait. On this card the ACM answered none of the first
    10 frames (`post_windows: 246 SPI frames (10 without reply)`). They are the
    probe, the unlocks, the marker and the handshake `F0 03 03 05 03 03`.
  - Result: reg 5 read `00`, and reg 2 read `c1`, i.e. both 3.5 mm jacks
    "plugged".
  - The handshake sent through debugfs (`huframe`) gave reg 5 `03` and reg 2 `f1`
    (both jacks empty). The XLR mic then recorded speech, 36.9 dB above the
    silence. At Mic Gain +20 dB the peaks reached −0.1 dBFS.
  - Our own driver waited ~350 ms after GPIO 5 and retried the probe
    (2026-11-09). The fix belongs in the driver: wait for the probe's reply
    before the init frames.
- **Headphones: still silent.**
  - The ACM clicks when its headphone amp switches, in both directions.
  - The log has every switch and stream-start step, with no errors.
  - The −40 dBFS tone at Master −36, −26, −16 and −6 dB (−76 to −46 dB below full
    scale) was not heard. On 2026-11-09 our driver's tone was heard at −75 dB.
- **The `dac` stage, first run** ([below](#diagnostics-the-dac-stage)): **every
  DAC register of both devices read `ff`**, idle and while the tone played.
  - The codec side was as the driver sets it: pin 0x10 `c0`, DAC 0x02 amps `5a`,
    format `31` (48 kHz, 24-bit, stereo), AFG in D0.
  - The automute probe saw `ff` instead of `34`/`00`/`68` and wrote nothing.
  - The same read sequence read the expected values on this card on 2026-11-09
    (our driver). So in this state neither DAC answers on the command engine's
    bus: no power, reset or clock at the DACs, or the engine does not reach them.
    None of the driver's DAC writes (unmute, volume, routing) can have landed,
    which explains the silence.
  - Unknown yet: whether the reload caused it (the first, cold load was not read).
- **The `dac` stage, second run, with the engine section:** the engine is fine and
  the DAC does not answer.
  - Engine registers as Windows leaves them (0x1c `00880480`, 0x800 `6b`, 0x830
    `aff`, 0x86c and 0x89c bit 0 set); it unlocks (`aa`); both transfers go busy,
    then done. 0x20c then reads `00810003` (bit 16 set, status 0x840/0x854/0x860
    `1`), and 0x208 reads `ffffffff`: no device answers at 0x48.
  - The codec's I2S check and power registers read the 3xHelix author's healthy
    values (0x18902c `f`, 0x189030 `81`). Codec 2 is in D3 (`233`), but Windows
    keeps it in D3 during analog playback too (our capture analysis), so that is
    not it.
- **Likely cause: GPIO pin 0 is never raised.** Windows' first D0 step on this
  card (CtxHda 0x35de4, "AntiPop"; our capture lines 1280-1283) is pin 5 = 1,
  pin 0 = 0, 1 ms, pin 0 = 1, and only then the first DAC access. Its post-DSP
  block has no pin 0, because pin 0 is already 1 by then. 3xHelix's replay
  (`ae9_cold_bar2_post`) is that post-DSP block (its GPIO order matches it
  exactly). Nothing in 3xHelix sets pin 0 = 1, while its shutdown and suspend set
  it to 0. Our driver pulsed pin 0 on 2026-11-09, and its DAC answered.
- **`pin0` stage: confirmed.** Before the pulse 48/40 read `ff`. After it, 48/07
  read `80` (the power-on default, as Windows reads it) and 48/40 `72` (chip ID
  `1c`); 0x20c bit 16 and 0x854 stayed clear (so those flag a device that does not
  answer), and the mutes (48/07 = `81`, 49/0a = `07`) read back.
- **The fix** (branch `arkhodakov/volume-ceiling`, local):
  - `5dfa9bc` Windows' board-D0 GPIO step before codec 1's set-up: pins 4-7 to
    GPIO, all pins enabled, pin 5 = 1, pin 0 = 0, 1 ms, pin 0 = 1, both DAC
    devices muted, pin 2 = 0, 48/1d = `40`, pin 3 = 1; then the chip ID is read
    and logged (`board D0: ... the DAC answers`). `board_d0=0` turns it off.
  - `0746800` the post-boot replay retries the head unit's first frame for up to
    ~3 s until it answers, after reading out bytes already waiting in the UART.
    With the board-D0 step the head unit is powered seconds before its frames, as
    in Windows, so the handshake should land without the manual `huframe`.
- **Reload with the fix (same boot):** `board D0: GPIO pin 0 pulsed, the DAC
  answers (48/40 = 0x72)` and `post_windows: 246 SPI frames (0 without reply)`:
  the head unit answered its very first frame (no retry line), and its plug bits
  came right (`3.5 mm headphone plug out`, `mic plug out`), so the handshake
  landed. The driver brought the head unit up on headphones by itself, so the
  `headphones` stage's switch was a no-op and it refused while waiting for a
  switch line; it now accepts a head unit already on headphones.
- **Headphones and mic work** (same reload, no manual step): the owner hears the
  −40 dBFS tones in `dac` and `tones` at Master −36 dB, and the XLR mic records
  well (heard back through the card). The `mic` stage still said "no speech"
  because the 3 s "silent" part had a loud sound at 0-1.75 s; the real noise floor
  was −61 dBFS and the speech 40 dB above it. `ae9_wav_stats.py` now compares
  250 ms windows (the quietest of the silent part with the loudest of the speech
  part) and adds a NOTE when the silent part was not silent: that recording now
  reads +41.8 dB, the 12:19 one +42.2 dB, the dead-mic 11:27 one "no speech"
  (+3.6 dB).

## Why: the ASMedia bridge

Our card is `0000:04:00.0` behind an ASMedia ASM1083 bridge (`0000:03:00.0`,
`1b21:1080` rev 03) on the Intel root port `00:1c.0`. The driver detects this
bridge and switches on three things. Each one matches one of our freezes; this is
a hypothesis, not proof:

| Our freeze (sb-ae9's `docs/INVESTIGATION.md`, memory notes) | Their finding | Behind ASMedia |
|---|---|---|
| 1-2: VFIO reset (bus reset) | the secondary bus reset takes the bridge down at ring start | no bus reset (`bus_reset=-1`) |
| 3: VM, during the DSP firmware download (IOMMU translating) | the bridge reads past the end of the 256 KB firmware buffer: IOMMU fault (kernel bugzilla 219552) | 64 KB spare after every DMA buffer (`dma_pad`) |
| 4: bare metal, PCM 10 playback, IOMMU off | the bridge wedges after 14-18 posted writes with no read in between | every register write read back (`posted_flush`) |

Our tree flushes posted writes only in `azx_int_clear()` (patch 0001) and after
BAR2 writes. The rest of `snd-hda-core` writes unflushed.

## Firmware

The driver loads two files from `/lib/firmware/creative-ae9/`, both extracted from
`CtxHda.sys`:

- `ctefx-desktop.bin`: the Windows main DSP image, 655436 bytes;
- `ctefx-ae9-overlay.bin`: 432 bytes.

It refuses the root-directory `ctefx-desktop.bin`, which is alsa-firmware's
655856-byte AE-5/AE-7 image. Our `local/CtxHda.sys` (driver 6.0.105.65) holds
exactly the images it is tested with: main MD5 `85c8f2b4…`, overlay MD5
`0e222c94…`. The overlay is byte-identical to our
`local/ae9-dsp-output-overlay.bin`.

`tools/sbae9drv_firmware.py` is our own reader of the FLS format. It extracts both
files, checks those MD5s and refuses anything else:

```sh
python3 -B tools/sbae9drv_firmware.py --sys local/CtxHda.sys --out local/sbae9drv-fw
```

## Where it differs from our rules, and what we do about it

- **No volume ceiling.** The hardware volume is driven by the desktop slider and by
  the knob, up to 0 dB:
  - "Master Playback Volume" is the DAC attenuation, −127..0 dB in 0.5 dB steps.
    The driver starts at −36 dB, on Speakers.
  - Our patch `hu_max_db` (below) ends the range at −35 dB, the owner's level.
    PipeWire's 100% then is −35 dB, and the knob stops there.
  - Every DAC volume write is held at or below the ceiling, including Windows'
    replayed tables. The boot replay writes 0 dB before −36 dB.
  - Without the patch, `alsactl init` would set a new card's Master to −20 dB.
- **Autoload.** Each of its install routes (COPR akmod, DKMS, `.deb`) loads the
  module at every boot, through modalias and `modules-load.d`. For the first
  contact the module is never installed: root runs `insmod` on the built file,
  and the next boot is untouched. The `module_blacklist=snd_hda_intel,...` entry
  stays; this driver does not need `snd_hda_intel` at all.
- **Mixer writers.**
  - PipeWire is stopped while the module loads and unloads.
  - On the first card add, udev's `alsactl` runs `init`; the ceiling bounds it.
  - Our old `/etc/wireplumber/wireplumber.conf.d/51-sb-ae9-desktop.conf` matches
    the same PCI device. It turns ACP off and disables the capture node 0.0, so it
    is removed before the PipeWire step. sb-ae9's
    `tools/install_ae9_desktop_service.py` reinstalls it if we return to run 3.
- **Output switching** during playback is normal in this driver (Windows' switch
  sequences). Our "no output switch while playing" rule came from our own driver.
- **Known issues** (its README): some stuck states clear only with a PSU
  power-off, and a warm reboot can rarely leave the card dark. Most of them were
  seen in DSP mode (Direct Mode off) or with heavy switching. Direct Mode (DSP
  bypass) is the default.

## Working copy and patch

- **Location:** `~/Software/Personal/SbAe9-Drv`, since 2026-11-10 a clone of the
  owner's fork (`origin` = `arkhodakov/SbAe9-Drv`, `upstream` = 3xHelix). The
  earlier working copy, with the local branch `arkhodakov/volume-ceiling`
  (`7b0322b` hu_max_db, `5dfa9bc`, `0746800`), was replaced by it.
- **Branch for the PR:** `fix/dac-gpio0-head-unit-start`, since 2026-11-12 (pushed
  that day) = upstream 0.12.6 (`a44ba37`) + `42b47d1` (board-D0 GPIO step) +
  `a893fce` (the head unit's first frame). First opened on 0.12.4 (`7b52a2e`) as
  `a685026` + `f0bd2ae`: the same code as `5dfa9bc`/`0746800`, comments
  reworded. No `hu_max_db`: with the 0 dB maximum it is not needed, and the
  runner now loads the module without options and wants `board_d0` = 1.
- **Review PR** into the fork's own `main`: arkhodakov/SbAe9-Drv#1.
- **Upstream PR:** 3xHelix/SbAe9-Drv#3, the same branch (opened 2026-11-10 on
  the owner's go). On 2026-11-12 the 0.12.6 rebase was force-pushed (mergeable),
  the description replaced, and the owner posted the comparison below with
  three dumps. First build of the branch (0.12.4): sha256 `c6a461ff…`,
  srcversion `E3B3480E6B130B65E964075`. Current build, 0.12.6 + #3 (2026-11-12,
  the copy in this working tree): sha256
  `a2952682c7c77534b22f15de7c66af19867c654d7a6ad4a27fccea301b3a4842`, srcversion
  `A864F52E10FDC5288B9ED70`, `board_d0`. Its cold boot passed on the evening of
  2026-11-12 (below); it is installed with DKMS since then.
- **Merged** (the author, 2026-11-12, comment on #3): the race shows on his card
  too, milder (his head unit needed 3 tries without the step; the retry caught
  it); with the step 0 of 246 lost in 3 reloads and a PSU-off cold boot. Both
  commits are in his tree on 0.12.7, with the owner's name, for 0.12.8. His
  changes: the board-D0 check uses 0.12.7's own DAC read helper, three reads,
  the last kept ("a single read returns the previous result"); the retry is
  cut to about 1 s.
- **Build** (the owner, no root), for the running kernel:

  ```sh
  make -C /lib/modules/$(uname -r)/build M=$HOME/Software/Personal/SbAe9-Drv modules
  ```

  The runner wants a module built for the running kernel (no longer pinned to
  7.2.13 since 2026-11-12).

- **2026-11-11, the author's review of #3:** he agrees (Windows raises pin 0 at
  every boot; his captures show it too; his unload/shutdown leave it low, as
  Windows) and wants it in, rebased onto 0.12.5 and with Windows' ~16 ms wait
  before raising pin 0 (we had 1 ms). His guess: we hit it hard because 0.12.4
  skipped the card reset behind ASMedia, so pin 0 stayed low after a reload;
  0.12.5 resets every card at load (`bus_reset`, default on). On his two cards
  (IDT and ASMedia bridges) the DACs answer and the handshake gets through. He
  asks for plain 0.12.5 without our patch after a PSU-off cold boot and after a
  reload (do the DACs answer, does the mic work), an `ae9-full-dump` of that
  version, and how the GPIOs look.
  - Against the guess, for the mic: on 0.12.4 the head unit missed its first 10
    frames at the first load after a power-off too, not only after the reload.
    The DAC was read only after the reload.
  - Done locally: both commits rebased onto upstream `main` (`3094f27`, 0.12.5 +
    README): `c2593ed` (pin 0, now `msleep(16)`), `f4cf122`; `workbench`
    replayed on top. Not pushed yet (needs the owner's build check).
  - Plain 0.12.5 for the test: worktree `~/Software/Personal/SbAe9-Drv-0.12.5`
    (detached at `upstream/main`, no `board_d0`).
  - The runner takes another module (`check [KO]`, `load [KO]`), accepts a build
    without `board_d0`, reports the bus-reset and head-unit-frames lines at
    load, and the engine section and the pin0 stage read the whole GPIO block
    (0x300-0x320; 0x320 only echoes the last command).
  - Risk: 0.12.5 resets the card through our ASM1083 at every load. Our old
    wedges (sb-ae9 `docs/INVESTIGATION.md`) came from unflushed ring writes, not
    from a reset, and Kaiba's same bridge passed 0.12.5's tests.

- **2026-11-12, the comparison** (no wedge from the bus resets; dumps in
  `local/sbae9drv-dumps/`):

  | | plain 0.12.5, PSU-off cold boot | plain 0.12.6, reload | 0.12.6 + #3, reload |
  |---|---|---|---|
  | DACs (48/40) | answer (`71`) | answer | answer |
  | head-unit frames without reply | 10 of 246 | 9 of 246 | 0 |
  | headphones, XLR mic | silent | work | work |
  | GPIO after bring-up (0x300 / 0x304 / 0x320) | 31 / ff / 105 | 31 / ff / 105 | 31 / ff / 105 |

  - The author's guess holds for the DACs: with 0.12.5's bus reset they answer
    without the pin 0 pulse, cold and after a reload.
  - The head unit is a race. Plain raises pin 5 (its power, it seems: the
    shutdown table drops it) about 40 steps before the first frame, and the
    head unit answers only from about frame 10 on (20 ms per unanswered frame).
    Frame 10 is the handshake: 9 lost works, 10 lost is silent. With the
    board-D0 step pin 5 goes high seconds earlier: 0 lost in all three runs
    with it (0.12.4, 0.12.5, 0.12.6; all reloads). The first-frame retry never
    fired.
  - 0.12.6 came out on 2026-11-11. Its dump adds a "Driver state" section
    (debugfs `state`: GPIO pin levels, the unanswered head-unit frames). It
    conflicted with #3, so both commits were rebased again, onto `a44ba37`:
    `42b47d1` (unchanged) and `a893fce` (the retry merged with 0.12.6's
    no-reply counters); `workbench` on top. The 0.12.5 states are kept as
    `backup/fix-on-0.12.5` and `backup/workbench-on-0.12.5`. Plain 0.12.6
    worktree: `~/Software/Personal/SbAe9-Drv-0.12.6`.
  - The cold boot with #3 passed the same evening (next section but one).

- **Built** by the owner on 2026-11-10 from `7b0322b` (clean tree), with no
  compiler warnings:
  - `snd-hda-codec-creative-ae9.ko`: sha256 `656be219…`, srcversion
    `68B8A419025C4C7C1F4C592`, vermagic `7.2.13-200.fc44.x86_64`, with the
    `hu_max_db` option.
  - Kbuild warns that the compiler differs: Fedora's gcc 16.2.1-3 versus 16.2.1-2
    for the kernel. That is harmless: the same GCC release, and the kernel uses no
    GCC plugins and no randstruct.
- **Rebuilt** by the owner on 2026-11-10 from `0746800` (clean tree), again with no
  compiler warnings: sha256 `72d7dbbc5acb4fb759dbe76e77e6fc988b8f09ace2082010221a9ed48626b9bd`,
  srcversion `22D8D63384BBF7E5F16833B`, vermagic `7.2.13-200.fc44.x86_64`, with
  the `hu_max_db` and `board_d0` options and the new log lines.
- **Dependencies:** `snd`, `snd-pcm` and `snd-hda-core`. `insmod` does not load
  dependencies. On this machine the onboard audio (SOF) loads all three at boot.

## First contact (one session, no test boot entry)

`tools/sbae9drv_test.sh` runs the stages. Each stage checks its preconditions and
refuses otherwise. Host-only tests: `tools/tests/test_sbae9drv.py`.

### Preparation (no card access)

1. Build the module (above). `sh tools/sbae9drv_test.sh check` then shows it.
2. Root, once:

   ```sh
   cd ~/Software/Personal/SbAe9-Drv
   sudo install -D -m 0644 -t /lib/firmware/creative-ae9 local/sbae9drv-fw/creative-ae9/*.bin
   sudo rm /etc/wireplumber/wireplumber.conf.d/51-sb-ae9-desktop.conf
   sudo grubby --update-kernel=/boot/vmlinuz-7.2.13-200.fc44.x86_64 --remove-args="intel_iommu=on iommu=pt"
   ```

   The last line removes the IOMMU options added for the VM path, which is gone.
   The platform then matches our passing bare-metal runs, which had the IOMMU off.
3. Power off fully: shut down, switch the PSU off for about 30 s, boot the normal
   entry. The card then starts cold, which the driver's README recommends.

### Session (touches the card: owner's OK for this run)

| Step | Command | Expected |
|---|---|---|
| 0 | `sh tools/sbae9drv_test.sh check` | `Ready for: ... load` |
| 1 | `systemctl --user stop pipewire.socket pipewire-pulse.socket pipewire pipewire-pulse wireplumber` | no audio on the desktop until step 7 |
| 2 | `sudo sh tools/sbae9drv_test.sh load` | `seen:` for posted_flush, 64 KB pad, no bus reset, ceiling −35 dB and firmware load 0; `LOADED: card N (AE9)`. The ACM clicks and shows `-SP-`. Riskiest step. |
| 3 | `sh tools/sbae9drv_test.sh level` | range ends at −35 dB, 100% = −35 dB, then −36 dB; output still Speakers |
| 4 | `sh tools/sbae9drv_test.sh headphones` | the driver logs the switch; `-HP-` on the display; Master at most −36 dB |
| 5 | `sh tools/sbae9drv_test.sh tones` | headphones on only now: −60/−50/−40 dBFS, left then right |
| 6 | `sh tools/sbae9drv_test.sh mic` | 3 s silence, then count to five; the verdict line |
| 7 | `systemctl --user start pipewire.socket pipewire-pulse.socket pipewire pipewire-pulse wireplumber` | ACP builds the AE-9 card (stereo fallback profiles) |
| 8 | Hyprland volume keys, the knob, then music at a low slider | `amixer -c AE9 cget name='Master Playback Volume'` follows the slider; slider 100% reads 184 (−35 dB) |
| 9 | the AE-9 source in an application (or `pw-record`) | the mic works for applications |
| 10 | stop PipeWire (step 1), then `sudo sh tools/sbae9drv_test.sh unload` | `UNLOADED: shutdown sequence done.`; or just reboot, the driver runs the same sequence |

If anything fails, stop and report. If the machine freezes, power-cycle: the normal
boot does not load the module. Evidence afterwards:

```sh
journalctl -b -1 -k --no-pager | grep -e 0000:04:00.0 -e creative-ae9
```

**Fallback:** desktop run 3 of our ca0132 path, with image `16e9ad10…` pinned on
2026-11-10 (sb-ae9's `docs/DESKTOP-TEST.md`).

### Diagnostics: the dac stage

`sudo sh tools/sbae9drv_test.sh dac` needs the loaded card, Headphones, Master at
most −36 dB and no open stream. It runs `tools/sbae9drv_dac.py` (host tests:
`tools/tests/test_sbae9drv_dac.py`) through the driver's debugfs file
`/sys/kernel/debug/creative_ae9/cmd`:

1. **Engine section, idle, read-only** (`engine --card N`; refused while any
   stream of the card runs, as 3xHelix's own dump script does for ChipIO).
   - BAR2: the CA0113 command engine (0x20c, 0x210, 0x800, 0x804, 0x830, 0x86c,
     0x89c, status 0x840/0x854/0x860) and its neighbours (0x1c, 0x100, 0x104,
     0x304), next to what Windows' init leaves (our CtxHdb notes, B4a-B4d). Windows
     reads each of them itself.
   - One traced DAC read of 48/40, the chip ID: whether the engine unlocks (0x210
     must read `aa`, Windows' check), whether 0x20c bit 23 goes busy and then done
     for both transfers, and what 0x208 returns. It adds only reads of 0x20c to the
     proven sequence. Its `ENGINE:` line tells a locked engine, a stuck transfer or
     a device that does not answer apart.
   - ChipIO: the I2S clock descriptors and the I2S check and power registers that
     3xHelix's `scripts/ae9-full-dump.sh` reads (healthy AE-9: 0x18902c `f`,
     0x189030 `81`).
   - GET verbs: codec 1 and codec 2 GPIO, codec 2's ID and power state.
2. **Idle dump, read-only.**
   - The DAC registers of groups 0x48 (ES9038Q2M) and 0x49, each next to the value
     the driver's tables give for headphones.
   - The read sequence is our driver's `ca0113_mmio_command_get`, sent as
     `w2`/`r2`. It read the expected values on this card on 2026-11-09.
   - The codec state, from GET verbs only: pin controls, DAC 0x02 amps, stream
     binding. Their replies are in the kernel log.
3. **A steady tone:** −40 dBFS, 1 kHz, both channels, 12 s.
4. **While the tone plays:**
   - the key DAC registers: mute, volume, routing, lock;
   - the ES9038 automute probe (from our dormant `ae9_automute_probe`). For 0.4 s
     it sets automute (level −60 dB), then reads register 0x40: bit 0 lock, bit 1
     automute.
   - It writes 0x02, 0x04 and 0x05, but only over the expected values `34`, `00`
     and `68` with chip ID `1c`. Otherwise it writes nothing. It restores them and
     reads them back.

The verdict line:

- automute off: the DAC receives the tone, and the break is after it (analog
  stage, cable or ACM);
- automute on: it receives zeros, and the break is before it (codec, DSP or
  routing);
- no lock: no usable I2S;
- every register `ff`: the DAC does not answer on its control bus, and the
  probe writes nothing.

The debugfs commands bypass the driver's DAC lock. Leave the knob and the outputs
alone while it runs.

### Diagnostics: the pin0 stage

`sudo sh tools/sbae9drv_test.sh pin0` (no stream may run; headphones off the
head, as releasing the pin may click):

- reads 0x320 (the debugfs write is 32-bit; it keeps the upper half), then the
  DAC chip ID; if the DAC already answers, it writes nothing;
- sets GPIO pin 0 to 0, waits 15 ms (the capture's spacing), sets it to 1;
- reads 48/07 (Windows read `80` there, the power-on default) and the chip ID;
- if the DAC answers, mutes both devices as Windows does next (48/07 = `81`,
  49/0a = `07`): they come up unset, and nothing may play before the driver sets
  them up again.

## The cold boot with #3, then the install (2026-11-12 evening)

The last open test of #3, under plain 0.12.5's failing conditions (PSU-off cold
boot, the module loaded by hand as the first driver to touch the card). It is
also the check before an install (`local/TODO.md`, step 1).

**Result: passed, and installed.** The owner: "Всё работает!"

- Cold boot (20:23; the load logged no "DSP firmware flag set from an earlier
  run", so the card had been off): `board D0: GPIO pin 0 pulsed, the DAC
  answers (48/40 = 0x72)`, `246 SPI frames (0 without reply)`, no retry line,
  both 3.5 mm plugs out. `level` ends at 0 dB; the head unit was already on
  Headphones; the dac tone was heard; `mic` +36.3 dB (speech −11 dBFS peak).
  Dump: `local/sbae9drv-dumps/ae9-dump-20261112-202836-pr-0.12.6-cold.txt`
  (`head unit boot frames: 246, without reply 0`; GPIO `31`/`ff`/`105`).
- DKMS install at 20:29: 0.12.6 for 7.2.13, srcversion `A864F52E10FDC5288B9ED70`
  (the tested build's), both config files in `/etc`.
- Next boot (a reboot, 20:30): loaded early (23 s after the kernel start, the
  LUKS prompt included), DAC answers, 0 of 246 without reply, plugs out.
- The `load` and `dac` stages run as root and log under root's home, so their
  output is not in `~/.local/state/sb-ae9/sbae9drv.log`; the kernel log has
  the load lines.

### Before the reboot (no card access)

- Optional: a test build for kernel 7.3.3 (in updates; the author tested 7.2.x
  only). It builds in a copy, so the 7.2.13 module for the session stays:

  ```sh
  sudo dnf install kernel-devel-7.3.3-200.fc44
  rm -rf /tmp/ae9-k733 && mkdir /tmp/ae9-k733
  cd ~/Software/Personal/SbAe9-Drv
  cp Makefile creative_ae9*.c creative_ae9*.h ca0132_regs.h /tmp/ae9-k733/
  make -C /usr/src/kernels/7.3.3-200.fc44.x86_64 M=/tmp/ae9-k733 modules
  modinfo -F vermagic /tmp/ae9-k733/snd-hda-codec-creative-ae9.ko
  ```

  The copied files are the ones `scripts/dkms-install.sh` copies. Until the
  module builds and runs on 7.3.3: `sudo dnf upgrade --exclude='kernel*'`.
- Power off fully: shut down, switch the PSU off for about 30 s, boot the normal
  entry (7.2.13).

### Session (touches the card: owner's OK for this run)

| Step | Command | Expected |
|---|---|---|
| 0 | `sh tools/sbae9drv_test.sh check` | `Ready for: ... load`; the module with the GPIO pin 0 step |
| 1 | stop PipeWire (First contact, step 1) | |
| 2 | `sudo sh tools/sbae9drv_test.sh load` | `board D0: ... the DAC answers`; `head unit: 246 SPI frames (0 without reply)`; no `head unit ... after N tries` line (the retry); `LOADED` |
| 3 | `sh tools/sbae9drv_test.sh level`, then `headphones` | the Master range ends at 0 dB, then −36 dB; `-HP-` |
| 4 | `sudo sh tools/sbae9drv_test.sh dac` | the −40 dBFS tone is heard; the DAC registers as expected |
| 5 | `sh tools/sbae9drv_test.sh mic` | the verdict finds speech |
| 6 | `cd ~/Software/Personal/SbAe9-Drv-0.12.6 && sudo scripts/ae9-full-dump.sh` | `ae9-dump-<time>.txt`; its Driver state: `head unit boot frames: 246, without reply 0` |
| 7 | start PipeWire (First contact, step 7) | |

### Then: the install (DKMS)

The owner decided on 2026-11-12 to load the driver at boot. Only if steps 2-5
passed, in the same session (the card is not touched):

```sh
cd ~/Software/Personal/SbAe9-Drv
sudo scripts/dkms-install.sh
sudo install -m 644 config/ae9-modules-load.conf /etc/modules-load.d/creative-ae9.conf
sudo install -m 644 config/ae9-omit.conf /etc/dracut.conf.d/ae9-omit.conf
scripts/dkms-install.sh --check
modinfo -F srcversion snd-hda-codec-creative-ae9
```

- The script copies only this tree's driver files (on `workbench` they equal
  #3's) to `/usr/src/snd-hda-codec-creative-ae9-0.12.6/` and builds them for
  every kernel with headers: 7.2.13 only (6.19.10 has none). Expected:
  `built for 1 kernel(s)`; `--check` shows one copy, under `extra/`; srcversion
  `A864F52E10FDC5288B9ED70`, the tested build's.
- modules-load.d loads it early at boot (the author: the DSP start failed on
  11 of 26 cold boots with udev loading alone, 0 of 150 with early loading).
  The dracut file keeps it out of the initramfs, so it never starts before its
  firmware is reachable. No `dracut -f`: today's initramfs has no copy.
- The module loaded by hand stays until the reboot; nothing is reloaded.
- The next ordinary boot is the check:
  `journalctl -b -k | grep -e 'board D0' -e 'SPI frames'` (the DAC answers,
  0 frames without reply), then music at a low volume.
- Rollback: `sudo scripts/dkms-install.sh --remove` and delete the two files.
  A boot that hangs: GRUB `e`, append `,snd_hda_codec_creative_ae9` to
  `module_blacklist=`, Ctrl+X.
- Experiments afterwards: `unload` first, then `load` of the other build; a
  reload or another build still needs the owner's OK per run.

## Working with the author

- **Releases only in public.** The public repository holds release snapshots
  ("build files only"). The author develops in a private repository that also
  holds the captures and decoders.
- **PRs are ported by hand.** The author re-applies a PR in the private tree and
  keeps its authorship, as with DarkNikGr's PR #2 (seven DAC filters, in 0.11.0).
- **Discord:** linked in the repository's issue #1.
- **Our model:**
  - a small patch set on a reviewed release tag;
  - PRs for what is useful upstream, the ceiling first;
  - our own host tests and run tools stay in this repository.
- **Not run on the host:** their scripts and installers. The firmware is
  extracted by our own tool.
