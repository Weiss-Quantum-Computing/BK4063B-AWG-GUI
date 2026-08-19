# BK4063B AWG GUI

Panel control for a B&K Precision 4063B arbitrary waveform generator. The mirror
image of [Scope Grab](../scope-grab): instead of pulling a capture off an
instrument, this pushes a setup onto one. Same shape - single file, PyVISA over
the rear-panel USB-B port, no vendor software.

Edit the panel, press **Apply changes**, and the generator follows. Press
**Save setup** and the whole instrument state lands in a timestamped JSON you can
recall later or file alongside the data it produced.

| File | Contents |
|------|----------|
| `<prefix>_<timestamp>.json` | Full instrument state - every SCPI block for both channels, verbatim. This is what Recall reads. |
| `<prefix>_<timestamp>.txt` | The same thing laid out for reading: output state, load, wave, modulation |

## Requirements

- NI-VISA, or any VISA runtime (Keysight IO Libraries works too - the 4063B shows
  up either way)
- Python 3.9+
- `pip install pyvisa numpy matplotlib`

Matplotlib is optional; without it you lose the waveform preview and nothing else.

## Usage

```
pythonw bk4063b_awg_gui.py
```

`pythonw` keeps the console window from appearing. The app auto-connects to the
first 4063B it finds on USB; hit **Connect** to retry.

## Outputs are never switched for you

This is the one place where a generator panel differs from a capture tool: a wrong
click here puts a voltage into something.

- **Apply changes**, **Recall setup** and closing the window never touch an output
  switch. Only the **ON** / **OFF** buttons do.
- **confirm before switching an output on** (on by default) puts a confirmation in
  front of anything that changes what a live channel is emitting: switching an
  output on, applying to a channel that is already on, or uploading an arb to one.
  The dialog quotes the generator's own last-read setting, not whatever unapplied
  edit is sitting in the panel.
- The lamp beside each channel reads **ON** in red or **OFF** in green, taken from
  the generator on every read - not from what the panel last asked for.

Closing the app leaves a running channel running. That is deliberate: shutting a
control panel should not interrupt something the bench is in the middle of.

## The panel

Each channel gets output state, load and polarity; a wave type with its
parameters; arb selection and sample clock; and one modulation/sweep/burst row.

- **Greyed cells** are the parameters the selected wave type has no use for. They
  are ignored by Apply even if they still hold a number from a previous type.
- **`*` markers** flag a cell you have edited but not applied yet, and the status
  line beside the buttons counts them. It reads `in sync (hh:mm:ss)` when the
  panel matches the generator.
- **Apply sends only what you edited.** Untouched settings are not rewritten,
  which matters on a channel that is currently driving something.
- **Mode** is one row because the instrument allows only one of modulation, sweep
  and burst at a time. Picking a mode relabels the parameter slots beneath it.
- **Preview** draws what Apply *would* produce, computed locally from the panel -
  not read back from the generator. An arb already loaded on the instrument
  cannot be previewed (its samples are not readable over USB); load the file in
  the upload box and the preview picks it up.

## Uploading an arbitrary waveform

**Load file...**, pick the column if you are asked, set a name and a channel, then
**Upload**. The waveform goes into the generator's user memory and is selected on
that channel.

### What the file should look like

`.csv`, `.txt`, `.dat` (text) or `.npy` (a NumPy array). Text files may use commas
or whitespace, and **a header row is optional** - if the first line will not parse
as numbers it is read as column names instead. All of these work:

| Layout | Example |
|--------|---------|
| One sample per line | `0.0`⏎`0.707`⏎`1.0` |
| One row of samples | `0.0, 0.707, 1.0` |
| time, volts | `0.0,-1.0`⏎`1e-6,-0.99` |
| Named columns | `time_s,volts`⏎`0.0,-1.0` |
| Full scope capture | `time_s,CH1_V,CH2_V,...` |
| `.npy` | `np.save("w.npy", samples)` |

With more than one column the **Column** dropdown next to the file name chooses
which one holds the samples, listing the header names when the file has them. The
default skips an obvious x-axis (`time`, `time_s`, `t`, `index`, ...) and takes
the first real data column. Change it and the preview follows, so you can see
which trace you are about to upload.

That default matters for a Scope Grab capture, whose columns are
`time_s,CH1_...,CH2_...,CH3_...,CH4_...`: taking the *last* column would upload
the trigger trace rather than the signal.

### What the numbers mean

**The file sets the shape; the panel sets the volts.** Sample values are not
output voltages - the amplitude and offset actually produced come from **Ampl
(Vpp)** and **Offset (V)** on the channel, exactly as for a built-in wave. A
waveform captured at 0 to 5.8 V and one at -1 to +1 upload to the same thing.

`normalise to full scale` (on by default) divides by the largest absolute sample,
so the biggest excursion reaches the DAC's full scale. Asymmetry is kept: a
capture sitting entirely above zero still comes out entirely above the offset,
using half the range. Turn normalising off if your samples are already in
-1.0..+1.0 and you want that headroom preserved exactly; anything beyond that
range is clipped.

Samples go out as signed 16-bit little-endian, matching the 4063B's 16-bit DAC.
An odd point count is rejected by the instrument, so the last sample is dropped.
There is no small size limit to design around - a 1,000,000-point record uploads
in one go.

### A worked example

`Waveforms/` holds one, with the script that made it:

```
python Waveforms/make_blackman_burst.py
```

10 ms of 100 kHz carrier under a Blackman envelope - 1000 carrier cycles,
50,000 points at 50 samples per cycle, the window tapering to zero at both ends
so the record joins onto itself when it repeats.

![Blackman-windowed 100 kHz burst](Waveforms/blackman_100kHz_10ms.png)

The sample rate baked into the file (5 MSa/s) is a resolution choice, nothing
more - the file carries no timing. Setting Clock = TrueArb, Sa/s = 5e6 makes the
generator report `PERI,0.01S`, which is the 10 ms back again.

### How fast it plays back

Set by **Clock** on the channel, not by the file:

- **DDS** - the whole record plays once per **Freq (Hz)** period, however many
  points it holds. Use this to replay a captured transient at a chosen rate.
- **TrueArb** - points clock out at the **Sa/s** you set, so the period is
  points ÷ sample rate. Use this to preserve the original timing: a capture taken
  at 1 GSa/s replays at its true speed if you set the same rate.

**Deleting** an uploaded waveform has to be done at the front panel
(Utility -> Store/Recall) - the firmware exposes no SCPI for it.

## Command ordering

The 4063B accepts these without error and then silently ignores or overrides
them. `Awg.apply_channel` sequences around all four; they are worth knowing if you
drive the instrument from your own scripts.

- **The declared load rescales amplitude.** Set the load *after* the amplitude and
  a 1.5 Vpp request typed against HiZ quietly becomes 0.75 Vpp when the load
  changes to 50 ohm. Load goes first.
- **`ARWV` and `SRATE` both force `WVTP,ARB`.** Selecting an arb or touching the
  sample-clock mode rewrites the wave type, so both must precede `BSWV`.
- **`BSWV` clears active modulation**, so the carrier must be set before the mode
  block, never after.
- **`MDWV STATE,ON` and the type tag must be separate commands.** Combined, as in
  `MDWV STATE,ON,FM,...`, the instrument applies the state and drops the type
  switch, leaving you on the previous modulation.

The carrier also constrains what is legal: PWM needs a PULSE carrier, and the rest
need SINE/SQUARE/RAMP/ARB. Ask for AM on a pulse carrier and you quietly get PWM.

## Reading replies

Query responses need more than a comma split. `MDWV`/`SWWV`/`BTWV` carry a bare
modulation-type tag mid-string, which shifts every later key/value pair by one,
and a trailing `CARR,...` carrier block whose keys collide with the modulation's
own - flattened, `FRQ` reports the carrier frequency instead of the modulating
one. `parse_reply` handles both; the carrier comes back as a nested dict.

## Setups

**Save setup** writes both channels' full state. **Recall setup** applies one back,
in the order above, leaving the output switches alone. Recall is byte-exact: every
block reads back identical to what was saved, across sine, ramp, modulation, sweep,
burst and TrueArb.

Config - folder, prefix, arb name and the safety toggle - lives in
`%APPDATA%\BK4063B-AWG-GUI\config.json`, out of the program folder so a `git pull` cannot
clobber it.

## Scripting

[`bk4063b.py`](bk4063b.py) is a standalone scripting library for the same
instrument. The GUI does not import it - `bk4063b_awg_gui.py` carries its own
copy of the instrument layer so it stays a single droppable file - but the
library adds per-waveform convenience methods the GUI's internal `Awg` class
does not have, plus a scoped `snapshot`/`restore` pair.

```python
from bk4063b import BK4063B

with BK4063B() as awg:
    snap = awg.snapshot(channels=(2,))   # scope it so CH1 is never rewritten
    awg.sine(2, freq=1e3, amp=2.0)
    awg.output(2, True, load=50)
    awg.restore(snap)
```
