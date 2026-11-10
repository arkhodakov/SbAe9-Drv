# Workbench tools (not part of the driver)

Test tools for running this driver on the owner's card, kept on the fork's
`workbench` branch next to the driver they test. The driver build ignores this
directory. Run card, history and findings: [docs/SBAE9-DRV.md](../docs/SBAE9-DRV.md).

| Tool | What it does |
|---|---|
| `sbae9drv_test.sh` | The test stages: `check`, `load`, `level`, `headphones`, `tones`, `ladder`, `mic`, `dac`, `pin0`, `unload`. The module is loaded with `insmod` from this repository's top directory and never installed. Stages that touch the card run only with the owner's OK. |
| `sbae9drv_dac.py` | Reads the DAC devices, the command engine, ChipIO and codec state through the driver's debugfs; the automute probe; the GPIO pin 0 test. |
| `sbae9drv_firmware.py` | Extracts the DSP firmware from Creative's `CtxHda.sys` (kept in the git-ignored `local/`). |
| `ae9_test_tones.py` | The -60/-50/-40 dBFS left-then-right test tones. |
| `ae9_wav_stats.py` | Levels of a recording and the mic verdict (250 ms windows). |

Host-only tests, which never touch the card:

```sh
for t in tools/tests/test_*.py; do python3 -B "$t" || break; done
```
