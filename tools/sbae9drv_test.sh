#!/bin/sh
# Test stages for the 3xHelix SbAe9-Drv driver (module snd-hda-codec-creative-ae9,
# built in this repository, with the board_d0 option, the GPIO pin 0 fix) in a NORMAL
# Fedora boot. The module is loaded by hand and never installed: the next boot does not
# load it. Moved here from sb-ae9 (4ae84b2) on 2026-11-10.
# The stages after check touch the card: run them only with the owner's OK.
#   sh tools/sbae9drv_test.sh check         read-only: kernel, blacklist, card, bridge, module, firmware, PipeWire
#   sudo sh tools/sbae9drv_test.sh load     insmod (no options), then the bring-up log with the board-D0 line
#   sh tools/sbae9drv_test.sh level         the Master range must end at $CEILING dB; then Master -36 dB
#   sh tools/sbae9drv_test.sh headphones    head unit output to Headphones, Master at most -36 dB
#   sh tools/sbae9drv_test.sh tones         -60, -50, -40 dBFS 1 kHz tones, left then right
#   sh tools/sbae9drv_test.sh ladder        a -40 dBFS tone at Master -36, -26, -16, -6 dB; back to -36
#   sh tools/sbae9drv_test.sh mic           8 s from the head unit's mic input (3 s silence, then speech)
#   sudo sh tools/sbae9drv_test.sh dac      through the driver's debugfs, idle: the command engine, I2S/power
#                                           registers, codec 2, DAC registers, codec state; then a steady
#                                           -40 dBFS tone, the key registers and the automute probe
#   sudo sh tools/sbae9drv_test.sh pin0     GPIO pin 0 pulse as Windows starts the card (0, 15 ms, 1), then
#                                           whether the DAC answers; if it does, both DAC devices are muted
#   sudo sh tools/sbae9drv_test.sh unload   rmmod (the driver's Windows shutdown sequence), then the log
# PipeWire must be stopped for load and unload (systemctl --user stop pipewire.socket
# pipewire-pulse.socket pipewire pipewire-pulse wireplumber). Writes only the mixer
# controls named below, the recordings and its log under ~/.local/state/sb-ae9/. The dac
# stage also writes the driver's debugfs file: register reads (BAR2, ChipIO, GET verbs),
# DAC reads, and the automute probe (DAC registers 0x02, 0x04 and 0x05 for 0.4 s, then
# restored; only over their expected values). The pin0 stage writes GPIO 0x320 (pin 0 only)
# and, if the DAC then answers, mutes it (48/07 = 81, 49/0a = 07).
PATH=/usr/sbin:/usr/bin:/sbin:/bin
LC_ALL=C
export PATH LC_ALL
unset ALSA_CONFIG_PATH ALSA_CONFIG_DIR
set -f
IFS=$(printf ' \t\n.')
IFS=${IFS%.}

KERNEL=7.2.13-200.fc44.x86_64
PCI=/sys/bus/pci/devices/0000:04:00.0
BRIDGE=/sys/bus/pci/devices/0000:03:00.0
MODULE=snd_hda_codec_creative_ae9
DRIVER=snd-hda-codec-creative-ae9
SYSMOD=/sys/module/$MODULE
FIRMWARE=/lib/firmware/creative-ae9
DEBUGFS=/sys/kernel/debug/creative_ae9/cmd
MAIN_MD5=85c8f2b47180cd6623a2b0e8b8911fe0
OVERLAY_MD5=0e222c941f708a2caf93d50d2aba5ef8
# The owner's maximum level: 0 dB since 2026-11-10 (it was -35 dB with a local
# hu_max_db patch). 0 dB is the card's own maximum: no ceiling option is needed.
CEILING=0
# Master Playback Volume: 0..254 at 0.5 dB from -127 dB; the ceiling ends it at
# 254 + 2 * CEILING. The tests run at 182 (-36 dB), the driver's boot volume.
CEILING_RAW=254
TEST_RAW=182
# ladder: -36, -26, -16, -6 dB
LADDER_RAW='182 202 222 242'
SILENCE=3
SECONDS_TOTAL=8
REPO=$(cd "$(dirname "$0")/.." && pwd) || exit 1
# the driver is built in this repository's top directory
DRV=$REPO
KO=$DRV/$DRIVER.ko
STOP_PW='systemctl --user stop pipewire.socket pipewire-pulse.socket pipewire pipewire-pulse wireplumber'

uid=$(id -u)
log=
if [ "$uid" != 0 ]; then
    state_dir=${XDG_STATE_HOME:-${HOME:?}/.local/state}/sb-ae9
    out_dir=$state_dir/sbae9drv
    mkdir -p "$out_dir" 2>/dev/null && log=$state_dir/sbae9drv.log
fi
say() {
    printf '%s\n' "$*"
    if [ -n "$log" ]; then
        printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$log" 2>/dev/null
    elif [ "$uid" = 0 ]; then
        logger -t sbae9drv -- "$*" 2>/dev/null
    fi
    return 0
}
fail() {
    say "REFUSED: $*" >&2
    exit 1
}

case "${1:-}" in
    check|load|level|headphones|tones|ladder|mic|dac|pin0|unload) action=$1; [ "$#" = 1 ] || fail 'Exactly one argument.' ;;
    *) printf 'Usage: sh %s check|load|level|headphones|tones|ladder|mic|dac|pin0|unload\n' "$0" >&2; exit 2 ;;
esac
case "$action" in
    load|unload|dac|pin0) [ "$uid" = 0 ] || fail "$action needs root: sudo sh $0 $action" ;;
    check) ;;
    *) [ "$uid" != 0 ] || fail "Run $action as your user, not root." ;;
esac
say "=== sbae9drv_test.sh $action ==="

read_file() {
    value=
    read -r value < "$1" 2>/dev/null
    printf '%s\n' "$value"
}
bound_driver() {
    link=$(readlink "$PCI/driver" 2>/dev/null) || { printf 'none\n'; return; }
    printf '%s\n' "${link##*/}"
}
card_index() {
    set +f
    set -- "$PCI"/sound/card*
    set -f
    [ "$#" = 1 ] && [ -d "$1" ] || return 1
    case "${1##*/card}" in ''|*[!0-9]*) return 1 ;; esac
    printf '%s\n' "${1##*/card}"
}
pcm_states() {
    # One "pcmXY/subZ: state" line per substream of the card.
    set +f
    for status in "/proc/asound/card$1"/pcm*/sub*/status; do
        set -f
        [ -f "$status" ] || continue
        state=$(cat "$status" 2>/dev/null)
        case "$state" in closed*) state=closed ;; *) state=$(printf '%s\n' "$state" | sed -n 's/^state: *//p' | head -n 1) ;; esac
        sub=${status%/status}
        printf '%s/%s: %s\n' "$(basename "$(dirname "$sub")")" "$(basename "$sub")" "${state:-open}"
    done
    set -f
}
pipewire_running() {
    for name in pipewire wireplumber pipewire-pulse; do
        pgrep -x "$name" > /dev/null 2>&1 && return 0
    done
    return 1
}
cget() {
    amixer -c "$card" cget "name=$1" 2>&1
}
values() {
    printf '%s\n' "$1" | grep '^ *: values=' | tr -d ' ' | tr -d ':'
}
show() {
    out=$(cget "$1") || { say "$1: cannot read"; return 1; }
    say "$1: $(values "$out")"
}
expect_set() {
    say "cset $1 = $2"
    amixer -c "$card" cset "name=$1" "$2" > /dev/null 2>&1 || fail "Cannot write $1."
    out=$(cget "$1") || fail "Cannot read $1."
    got=$(values "$out")
    [ "$got" = "values=$3" ] || fail "$1 is '$got' after the write, expected values=$3."
}
master_raw() {
    out=$(cget 'Master Playback Volume') || fail 'Cannot read Master Playback Volume.'
    raw=$(values "$out")
    raw=${raw#values=}
    case "$raw" in ''|*[!0-9]*) fail "Unexpected Master Playback Volume value: '$raw'." ;; esac
    printf '%s\n' "$raw"
}
kernel_lines() {
    # The driver's kernel messages since epoch second $1.
    journalctl -k --since "@$1" -o cat --no-pager 2>/dev/null |
        grep -e '0000:04:00\.0' -e "$DRIVER" -e "$MODULE"
}
module_report() {
    # Prints problems with the built module, nothing when it is fine.
    if [ ! -f "$KO" ]; then
        printf 'module %s is missing (build it in %s)\n' "$KO" "$DRV"
        return
    fi
    vermagic=$(modinfo -F vermagic "$KO" 2>/dev/null)
    [ "${vermagic%% *}" = "$KERNEL" ] || printf 'module vermagic is "%s", not %s\n' "${vermagic%% *}" "$KERNEL"
    modinfo -F parm "$KO" 2>/dev/null | grep -q '^board_d0:' ||
        printf 'module has no board_d0 option (not the GPIO pin 0 build)\n'
}
firmware_report() {
    for pair in "ctefx-desktop.bin $MAIN_MD5" "ctefx-ae9-overlay.bin $OVERLAY_MD5"; do
        name=${pair%% *}
        want=${pair#* }
        sum=$(md5sum "$FIRMWARE/$name" 2>/dev/null)
        [ "${sum%% *}" = "$want" ] || printf 'firmware %s/%s is missing or not the tested image\n' "$FIRMWARE" "$name"
    done
}
preflight() {
    # Every reason the card is not ready for a first load, one per line.
    [ "$(read_file /proc/sys/kernel/osrelease)" = "$KERNEL" ] || printf 'kernel is not %s\n' "$KERNEL"
    blacklisted=no
    for word in $(cat /proc/cmdline); do
        case "$word" in
            module_blacklist=*) case ",${word#module_blacklist=}," in *,snd_hda_intel,*) blacklisted=yes ;; esac ;;
        esac
    done
    [ "$blacklisted" = yes ] || printf 'snd_hda_intel is not in module_blacklist= on the kernel command line\n'
    ids="$(read_file "$PCI/vendor"):$(read_file "$PCI/device") $(read_file "$PCI/subsystem_vendor"):$(read_file "$PCI/subsystem_device")"
    [ "$ids" = '0x1102:0x0010 0x1102:0x0071' ] || printf 'no AE-9 at %s (%s)\n' "$PCI" "$ids"
    case "$(readlink -f "$PCI")" in
        */0000:03:00.0/0000:04:00.0) ;;
        *) printf 'the AE-9 is not behind %s\n' "${BRIDGE##*/}" ;;
    esac
    [ "$(bound_driver)" = none ] || printf 'the AE-9 is already bound to %s\n' "$(bound_driver)"
    [ ! -e "$SYSMOD" ] || printf '%s is already loaded\n' "$MODULE"
    module_report
    firmware_report
    if pipewire_running; then printf 'PipeWire is running (as your user: %s)\n' "$STOP_PW"; fi
}
need_card() {
    [ "$(bound_driver)" = "$DRIVER" ] || fail "The AE-9 is not bound to $DRIVER (driver: $(bound_driver)); run load first."
    [ "$(read_file "$SYSMOD/parameters/board_d0")" = 1 ] ||
        fail "board_d0 is '$(read_file "$SYSMOD/parameters/board_d0")', not 1 (the GPIO pin 0 step is off or missing)."
    card=$(card_index) || fail 'Expected exactly one ALSA card under the AE-9 PCI device.'
}
need_idle() {
    busy=$(pcm_states "$card" | grep -v ': closed$')
    [ -z "$busy" ] || fail "A stream of card $card is open: $busy"
}

case "$action" in
check)
    say "Kernel $(read_file /proc/sys/kernel/osrelease); AE-9 driver: $(bound_driver)"
    say "Bridge ${BRIDGE##*/}: $(read_file "$BRIDGE/vendor"):$(read_file "$BRIDGE/device") rev $(read_file "$BRIDGE/revision") (0x1b21:0x1080 = ASMedia: posted_flush, 64 KB DMA pad, no bus reset)"
    if [ -f "$KO" ]; then
        sum=$(sha256sum "$KO")
        say "Module $KO: version $(modinfo -F version "$KO" 2>/dev/null), sha256 ${sum%% *}"
    fi
    if [ -e "$SYSMOD" ]; then
        say "Loaded: board_d0=$(read_file "$SYSMOD/parameters/board_d0")"
        if card=$(card_index); then
            say "Card $card ($(read_file "/proc/asound/card$card/id"))"
            pcm_states "$card" | while IFS= read -r line; do say "$line"; done
            for control in 'Master Playback Volume' 'Master Playback Switch' 'Head Unit Output' \
                           'Head Unit Phantom Power' 'Mic Gain Capture Volume' 'Capture Volume'; do
                show "$control"
            done
        fi
    else
        problems=$(preflight)
        if [ -n "$problems" ]; then
            printf '%s\n' "$problems" | while IFS= read -r line; do say "NOT READY: $line"; done
        else
            say 'Ready for: sudo sh tools/sbae9drv_test.sh load'
        fi
    fi ;;

load)
    problems=$(preflight)
    [ -z "$problems" ] || fail "$(printf '%s\n' "$problems" | tr '\n' ';')"
    sum=$(sha256sum "$KO")
    say "Module $KO, sha256 ${sum%% *}"
    mark=$(date +%s)
    say "insmod $KO: the bring-up takes 10-30 s (firmware download, DSP handshake)."
    journalctl --sync > /dev/null 2>&1
    sync
    if insmod "$KO"; then rc=0; else rc=$?; fi
    sleep 2
    journalctl --sync > /dev/null 2>&1
    lines=$(kernel_lines "$mark")
    printf '%s\n' "$lines" | while IFS= read -r line; do [ -n "$line" ] && say "kernel: $line"; done
    [ "$rc" = 0 ] || fail "insmod exited with $rc."
    for expect in 'posted_flush: every register write is read back (ASMedia bridge)' \
                  '(ASMedia ASM1083/1085): 64 KB spare after each DMA buffer' \
                  'ASMedia bridge: skipping the secondary bus reset' \
                  'board D0: GPIO pin 0 pulsed, the DAC answers' \
                  'DSP firmware load: 0'; do
        case "$lines" in *"$expect"*) say "seen: $expect" ;; *) say "MISSING: $expect" ;; esac
    done
    case "$lines" in
        *'but the DAC does not answer'*) fail 'The DAC does not answer after the board-D0 step: the analog outputs stay silent (the module stays loaded).' ;;
    esac
    need_card
    case "$lines" in *'Init complete: card '*) ;; *) fail 'No "Init complete" from the driver; see the kernel lines above.' ;; esac
    say "LOADED: card $card ($(read_file "/proc/asound/card$card/id")). Next, as your user: sh tools/sbae9drv_test.sh level" ;;

level)
    need_card
    need_idle
    out=$(cget 'Master Playback Volume') || fail 'Cannot read Master Playback Volume.'
    max=$(printf '%s\n' "$out" | sed -n 's/.*,min=0,max=\([0-9]*\),.*/\1/p')
    [ "$max" = "$CEILING_RAW" ] || fail "Master Playback Volume ends at '$max', not $CEILING_RAW ($CEILING dB)."
    printf '%s\n' "$out" | grep -q 'dBscale-min=-127.00dB,step=0.50dB' || fail 'Master Playback Volume has an unexpected dB scale.'
    say "Master Playback Volume: 0..$max = -127..$CEILING dB, now $(values "$out")"
    expect_set 'Master Playback Volume' 100% "$CEILING_RAW"
    say "100% = $CEILING dB: the range ends at the owner's level."
    expect_set 'Master Playback Volume' "$TEST_RAW" "$TEST_RAW"
    say 'Master at -36 dB.'
    for control in 'Master Playback Switch' 'Head Unit Output' 'Head Unit Phantom Power'; do show "$control"; done
    say 'Next: sh tools/sbae9drv_test.sh headphones' ;;

headphones)
    need_card
    need_idle
    raw=$(master_raw) || exit 1
    [ "$raw" -le "$TEST_RAW" ] || fail "Master is $raw, above $TEST_RAW (-36 dB): run level first."
    out=$(cget 'Head Unit Output') || fail 'Cannot read Head Unit Output.'
    if [ "$(values "$out")" = values=1 ]; then
        # The driver restores the head unit's own output at load; setting the
        # same value is no switch, and the driver logs nothing for it.
        say 'Head Unit Output: already Headphones (the driver brought the head unit up on them); no switch.'
    else
        mark=$(date +%s)
        expect_set 'Head Unit Output' Headphones 1
        switched=no
        tries=0
        while [ "$tries" -lt 10 ]; do
            sleep 1
            tries=$((tries + 1))
            case "$(kernel_lines "$mark")" in *'head unit: output headphones'*) switched=yes; break ;; esac
        done
        [ "$switched" = yes ] || fail 'The driver did not report the switch to headphones within 10 s (journalctl -k).'
    fi
    raw=$(master_raw) || exit 1
    if [ "$raw" -gt "$TEST_RAW" ]; then
        expect_set 'Master Playback Volume' "$TEST_RAW" "$TEST_RAW"
    else
        say "Master Playback Volume: $raw (headphone volume, at most -36 dB)"
    fi
    show 'Master Playback Switch'
    say 'Head unit on headphones. Next: sh tools/sbae9drv_test.sh tones' ;;

tones)
    need_card
    need_idle
    out=$(cget 'Head Unit Output') || fail 'Cannot read Head Unit Output.'
    [ "$(values "$out")" = values=1 ] || fail 'The head unit is not on Headphones: run headphones first.'
    raw=$(master_raw) || exit 1
    [ "$raw" -le "$TEST_RAW" ] || fail "Master is $raw, above $TEST_RAW (-36 dB)."
    show 'Master Playback Switch'
    tones=$(mktemp -d) || fail 'mktemp failed.'
    python3 -B "$REPO/tools/ae9_test_tones.py" --out "$tones" > /dev/null || fail 'Cannot write the test tones.'
    for level in 60 50 40; do
        say ">>> -$level dBFS: 1 kHz LEFT, then RIGHT (3.5 s) <<<"
        aplay -q -D "plughw:$card,0" "$tones/ae9-tone-m${level}dbfs.wav" || fail "aplay failed at -$level dBFS."
        sleep 2
    done
    rm -rf "$tones"
    say 'Done. Report which levels you heard, left and right, and any click or noise.' ;;

ladder)
    need_card
    need_idle
    out=$(cget 'Head Unit Output') || fail 'Cannot read Head Unit Output.'
    [ "$(values "$out")" = values=1 ] || fail 'The head unit is not on Headphones: run headphones first.'
    raw=$(master_raw) || exit 1
    [ "$raw" -le "$TEST_RAW" ] || fail "Master is $raw, above $TEST_RAW (-36 dB)."
    show 'Master Playback Switch'
    tones=$(mktemp -d) || fail 'mktemp failed.'
    python3 -B "$REPO/tools/ae9_test_tones.py" --out "$tones" > /dev/null || fail 'Cannot write the test tones.'
    for step in $LADDER_RAW; do
        [ "$step" -le "$CEILING_RAW" ] || break
        expect_set 'Master Playback Volume' "$step" "$step"
        say ">>> Master $(( (step - 254) / 2 )) dB, -40 dBFS: 1 kHz LEFT, then RIGHT (3.5 s) <<<"
        if ! aplay -q -D "plughw:$card,0" "$tones/ae9-tone-m40dbfs.wav"; then
            amixer -c "$card" cset 'name=Master Playback Volume' "$TEST_RAW" > /dev/null 2>&1
            fail "aplay failed at Master step $step."
        fi
        sleep 2
    done
    rm -rf "$tones"
    expect_set 'Master Playback Volume' "$TEST_RAW" "$TEST_RAW"
    say 'Master back at -36 dB. Report the first step you heard, left and right.' ;;

mic)
    need_card
    need_idle
    out=$(cget 'Head Unit Phantom Power') || fail 'Cannot read Head Unit Phantom Power.'
    [ "$(values "$out")" = values=off ] || fail 'Phantom power (48 V) is on; switch it off on the head unit first.'
    show 'Mic Gain Capture Volume'
    show 'Capture Volume'
    file=$out_dir/mic-$(date '+%Y%m%d-%H%M%S').wav
    say "Recording to $file"
    sync
    arecord -q -D "hw:$card,0" -f S32_LE -r 48000 -c 2 -d "$SECONDS_TOTAL" "$file" &
    recorder=$!
    say "Recording $SECONDS_TOTAL s: stay SILENT now..."
    sleep "$SILENCE"
    say '>>> SPEAK NOW: count from one to five at your normal voice. <<<'
    if wait "$recorder"; then rc=0; else rc=$?; fi
    sync
    [ "$rc" = 0 ] || fail "arecord exited with $rc."
    python3 -B "$REPO/tools/ae9_wav_stats.py" --split "$SILENCE" "$file" 2>&1 |
        while IFS= read -r line; do say "$line"; done ;;

dac)
    need_card
    need_idle
    [ -w "$DEBUGFS" ] || fail "Cannot write $DEBUGFS (debugfs not mounted, or the module is not loaded)."
    out=$(cget 'Head Unit Output') || fail 'Cannot read Head Unit Output.'
    [ "$(values "$out")" = values=1 ] || fail 'The head unit is not on Headphones: run headphones first.'
    raw=$(master_raw) || exit 1
    [ "$raw" -le "$TEST_RAW" ] || fail "Master is $raw, above $TEST_RAW (-36 dB)."
    show 'Master Playback Switch'
    work=$(mktemp -d) || fail 'mktemp failed.'
    say 'Reading the command engine, one traced DAC read, the I2S and power registers and codec 2 (idle, about 1 s).'
    if python3 -B -I "$REPO/tools/sbae9drv_dac.py" --debugfs "$DEBUGFS" engine --card "$card" > "$work/engine.txt" 2>&1; then rc=0; else rc=$?; fi
    while IFS= read -r line; do say "$line"; done < "$work/engine.txt"
    [ "$rc" = 0 ] || { rm -rf "$work"; fail "The engine reads failed ($rc)."; }
    say 'Reading the DAC registers and the codec state (idle, about 3 s). Hands off the knob.'
    if python3 -B -I "$REPO/tools/sbae9drv_dac.py" --debugfs "$DEBUGFS" dump --master "$raw" > "$work/dump.txt" 2>&1; then rc=0; else rc=$?; fi
    while IFS= read -r line; do say "$line"; done < "$work/dump.txt"
    [ "$rc" = 0 ] || { rm -rf "$work"; fail "The idle dump failed ($rc)."; }
    python3 -B -I "$REPO/tools/sbae9drv_dac.py" tone --out "$work" > /dev/null || { rm -rf "$work"; fail 'Cannot write the steady tone.'; }
    say '>>> A steady -40 dBFS 1 kHz tone on both channels, 12 s. Hands off the knob. <<<'
    aplay -q -D "plughw:$card,0" "$work/ae9-steady-m40dbfs.wav" &
    player=$!
    sleep 3
    if python3 -B -I "$REPO/tools/sbae9drv_dac.py" --debugfs "$DEBUGFS" probe --card "$card" --master "$raw" > "$work/probe.txt" 2>&1; then rc=0; else rc=$?; fi
    if wait "$player"; then prc=0; else prc=$?; fi
    while IFS= read -r line; do say "$line"; done < "$work/probe.txt"
    rm -rf "$work"
    [ "$rc" = 0 ] || fail "The probe failed ($rc)."
    [ "$prc" = 0 ] || fail "aplay exited with $prc."
    say 'Done. Report whether you heard the steady tone.' ;;

pin0)
    need_card
    need_idle
    [ -w "$DEBUGFS" ] || fail "Cannot write $DEBUGFS (debugfs not mounted, or the module is not loaded)."
    say 'GPIO pin 0: 0 for 15 ms, then 1, as Windows starts the card; then whether the DAC answers.'
    say '>>> Headphones OFF your head now: releasing the pin may click. Starting in 5 s. <<<'
    sleep 5
    work=$(mktemp -d) || fail 'mktemp failed.'
    if python3 -B -I "$REPO/tools/sbae9drv_dac.py" --debugfs "$DEBUGFS" pin0 --card "$card" > "$work/pin0.txt" 2>&1; then rc=0; else rc=$?; fi
    while IFS= read -r line; do say "$line"; done < "$work/pin0.txt"
    rm -rf "$work"
    [ "$rc" = 0 ] || fail "The pin 0 test failed ($rc)."
    say 'Done. Play nothing on the card until the next build is loaded.' ;;

unload)
    [ -e "$SYSMOD" ] || fail "$MODULE is not loaded."
    if pipewire_running; then fail "PipeWire is running; as your user: $STOP_PW"; fi
    if card=$(card_index); then need_idle; fi
    mark=$(date +%s)
    say "rmmod $MODULE: the driver runs its Windows shutdown sequence."
    journalctl --sync > /dev/null 2>&1
    sync
    rmmod "$MODULE" || fail "rmmod failed."
    sleep 2
    journalctl --sync > /dev/null 2>&1
    lines=$(kernel_lines "$mark")
    printf '%s\n' "$lines" | while IFS= read -r line; do [ -n "$line" ] && say "kernel: $line"; done
    [ ! -e "$SYSMOD" ] || fail "$MODULE is still loaded."
    case "$lines" in
        *'shutdown (unload): done'*) say 'UNLOADED: shutdown sequence done.' ;;
        *) say 'UNLOADED, but the log has no "shutdown (unload): done".' ;;
    esac ;;
esac
exit 0
