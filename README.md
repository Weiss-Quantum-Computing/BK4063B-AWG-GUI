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

- **Greyed cells** are the parameters the current settings give no meaning to,
  and Apply ignores them even if they still hold a number from a previous type.
  This works at two levels: the arb row only applies to `ARB`, and within it
  **Sa/s** and **Interp** only apply to the **TrueArb** clock, since DDS derives
  its timing from the frequency instead. A greyed cell still shows what the
  generator reports - it just cannot be edited or sent.
- **`*` markers** flag a cell you have edited but not applied yet, and the status
  line beside the buttons counts them. It reads `in sync (hh:mm:ss)` when the
  panel matches the generator.
- **Apply sends only what you edited.** Untouched settings are not rewritten,
  which matters on a channel that is currently driving something.
- **Mode** is one row because the instrument allows only one of modulation, sweep
  and burst at a time. Picking a mode relabels the parameter slots beneath it.
- **Preview** draws what Apply *would* produce, computed locally from the panel -
  not read back from the generator, which cannot report its samples. An arb is
  drawn from the local copy kept in `Waveforms/`, so anything uploaded through
  this app previews even sessions later; one put there by other means shows a
  note instead of a guess.

## Building a waveform in the panel

**Build waveform** makes the samples for you: pick a **Shape**, set **Points**,
press **Build**. The result becomes the pending waveform, the preview switches to
**pending** to show it, and **Upload** sends it. No file needed.

| Shape | Parameters | What it is for |
|-------|-----------|----------------|
| Gaussian | truncation in σ | The default pulse. σ=3 puts the ends at 1%, so there is no step to ring. |
| Blackman | – | Lowest spectral sidelobes of the windows here; the usual choice for Raman and Rabi pulses where off-resonant excitation matters. |
| Hann | – | Gentler than Blackman, wider main lobe. |
| Tukey flat-top | flat fraction | Flat top with cosine shoulders. `flat=0` is a Hann, `flat=1` a square. |
| Sech (ARP) | truncation | The adiabatic-rapid-passage amplitude profile, and the analytically solvable Rosen–Zener case. Pair with **Chirp**. |
| Sinc | zero crossings | A rectangle in frequency — the start of a flat-topped spectral profile. |
| Square pulse | width fraction | Hard-edged gate. |
| Trapezoid | rise, fall | Linear edges with a settable hold. |
| Tanh flat-top | edge, flat | Smooth switch-on with no corner: the AOM/EOM intensity ramp. |
| Linear ramp | start, end | Plain sweep — the EOM ramp. |
| Exponential ramp | start, end, τ | Evaporative-cooling ramp. `1 → 0` with small τ gives the hard early knee. |
| Smoothstep ramp | start, end | Minimum-jerk: zero slope *and* zero curvature at both ends, which is what keeps transport or a trap handover adiabatic. |
| Chirp | start/end cycles, envelope | Linear frequency sweep. Sech envelope + chirp is the standard ARP recipe. |
| Multitone | comma list of cycles, envelope | Sum of sines for multi-frequency addressing. Cycle counts are per record, so every tone closes cleanly on the repeat. |
| Gaussian deriv | truncation, β | The quadrature half of a DRAG pulse — Gaussian on one channel, this × β on the other. |

**Carrier cycles** turns any envelope into a burst: the shape becomes the outline
and a sine at that many cycles per record fills it. Leave it `0` for a bare
envelope. This is how the Blackman example above is specified — Blackman shape,
1000 carrier cycles.

Envelopes come out unipolar (0…1) because that is what an intensity control
wants; anything oscillating comes out bipolar (−1…+1). A unipolar shape therefore
uses the upper half of the DAC, so it swings from `Offset` to `Offset + Ampl/2` —
for a 0→5 V ramp set `Ampl = 10 V`, `Offset = 0`.

### Typing or pasting values

**Type/paste values...** takes numbers straight from the clipboard or the
keyboard — one per line, a single row, or columns separated by commas,
semicolons or whitespace. A non-numeric first line is read as column names,
blank lines and `#` comments are skipped, and multi-column input gets the same
column picker as a file. Ragged rows are rejected rather than silently
misaligned.

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

### DDS or TrueArb

Both play the same stored points; they differ in what the DAC clock is doing.

**DDS** runs the DAC at a fixed high rate and steps through the stored record
with a phase accumulator, so one pass through the record is one output period at
whatever frequency you ask for. The frequency is an independent setting with very
fine resolution, and it does not care how long the record is. The cost is that at
speed the accumulator does not land on every stored point — it skips and repeats
to hit the frequency, so fine detail between samples is not guaranteed to appear.

**TrueArb** clocks the DAC at exactly the sample rate you set and emits every
stored point, in order, once per period. Nothing is skipped, so a sequence of
values comes out exactly as written. The price is that frequency is no longer
independent: it is `rate / points`.

Measured on this unit with the same 50,000-point waveform:

| | DDS | TrueArb |
|---|---|---|
| Frequency | independent, set directly | `rate / points` |
| Range reached | up to **20 MHz** | 75 MSa/s ÷ 50,000 = **1.5 kHz** |
| Resolution | ≤ 0.0001 Hz confirmed | sample rate 0.001 Sa/s … 75 MSa/s |
| Every sample output? | not guaranteed | yes |

That 20 MHz against 1.5 kHz is the whole trade in one line, and it is the same
record either way.

**Use TrueArb** for a pulse sequence, a captured transient, or a typed list of
values — anything where the individual samples *are* the waveform, and where you
want the timing to be exactly what you computed. **Use DDS** for a repetitive
shape you want at a precise, high, or finely-tuned frequency.

### Why the frequency moves when you change waveform

In TrueArb the frequency is not an independent setting. The points leave at the
sample clock, so

```
freq = sample rate / number of points
```

Select a waveform of a different length and the frequency *has* to change - the
generator is not overriding you, it is doing arithmetic. Measured on this unit at
600 Sa/s: a 1,000-point waveform gives 0.6 Hz, a 50,000-point one gives 0.012 Hz.
Selecting a stored waveform can also reset the sample rate itself.

Every Apply that changes the arb selection now logs what happened, so the number
is visible rather than mysterious:

```
CH2 TrueArb: 600 Sa/s over 50,000 points -> 0.012 Hz
```

To hold the frequency across a waveform change, either use **DDS** - where the
whole record is one period whatever its length - or set the sample rate to
`freq x points` for the new waveform.

One more wrinkle: a waveform can carry a frequency stored with it at upload time,
which the generator restores when you select it. This app never writes one, but
waveforms put there by other software may have one.

### Held or interpolated - the clock decides

The same stored points come out as two different shapes depending on the clock,
confirmed on a scope:

![TrueArb holds each sample; DDS ramps between them](Waveforms/dds_vs_truearb.png)

- **TrueArb** clocks each point out and holds it until the next, so the output is
  a staircase and every value you wrote is a real flat level.
- **DDS** resamples the record to land on the frequency you asked for, and ramps
  from point to point rather than stepping.

The preview follows the channel's **Clock** setting and says which it is drawing
(`held` or `interpolated` in the title), so switching DDS ↔ TrueArb changes the
picture. On a smooth 50,000-point record the difference is invisible; on a
ten-value list it is the entire shape.

The **Interp** box writes the instrument's `INTER` parameter, which chooses how
the DAC gets from one sample to the next in TrueArb:

- **HOLD** — zero-order hold. Each sample is held for a full clock period and the
  output is a staircase. Every level you wrote is a real, flat, measurable level,
  which is what you want when the samples *are* the setpoints.
- **LINE** — linear interpolation. The output ramps from each sample to the next
  instead of stepping, so a coarsely-sampled curve comes out smooth. Fewer
  high-frequency steps to filter, but no flat dwell at each value.

At a high sample rate the difference disappears into the analogue bandwidth; at a
low one it is the whole character of the output.

**Unverified on this unit, and probably not the setting you want anyway.**
`SRATE?` never echoes `INTER` back whichever value is written, so the box clears
after Apply and the app cannot show which mode is in force; `LINE` and `HOLD`
produced identical readback. Note that the hold-versus-ramp behaviour you can
actually see on a scope tracks the **Clock** setting, not this box - TrueArb
holds, DDS ramps - so reach for Clock first. Whether `INTER` does anything on top
of that is untested.

## Waveforms in generator memory

The list at the bottom left shows what the generator holds, and whether this app
has a local copy of the samples.

**There is no remote delete.** Confirmed by probing rather than assumed: `WVDT
DEL`, `STL DEL`, `DELETE` and the whole SCPI `MMEM` subsystem all return
`-113 "Undefined header"`, and `C1:WVDT DEL,<name>` is accepted with no error and
no effect. Waveforms come off at the front panel, Utility > Store/Recall.
Re-uploading a name overwrites it, so reusing names keeps the list from growing.

**Forget local copy** deletes only this app's copy of the samples; the waveform
stays on the generator. **Use on channel** stages the selected waveform on the
channel named in the upload row — it does not send anything until you Apply.

### Why a local copy is needed at all

The generator will not read a stored waveform back out over USB: `WVDT?` times
out and wedges the session. So a waveform uploaded in an earlier session is a
name and nothing else, and cannot be drawn.

Every upload therefore saves a copy into `Waveforms/` as `.npy`, and those are
loaded at startup, which is what lets the preview draw a waveform you uploaded
days ago. What is stored is the normalised samples, i.e. what the DAC actually
received, not the raw file.

The folder works in both directions: any `.npy` in it is offered as a local copy
under its filename, so dropping one in named to match a waveform already on the
generator makes that waveform previewable without re-uploading it. Waveforms
that predate this app show as `no local copy` until you do one or the other.

Only `.csv` and `.png` in `Waveforms/` are tracked by git; uploaded `.npy` copies
are bench artefacts and stay out, the same way captured data does.

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
