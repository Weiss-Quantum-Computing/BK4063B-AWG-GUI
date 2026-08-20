#!/usr/bin/env python3
"""
BK4063B AWG GUI - panel control for a B&K Precision 4063B arbitrary
waveform generator.

The mirror image of Scope Grab: instead of pulling a capture off an instrument,
this pushes a setup onto one. Edit the panel, press Apply, and the generator
follows; press Save setup and the whole instrument state lands in a timestamped
JSON you can recall later or keep with the data it produced.

Outputs are never switched by Apply, by Recall, or by closing the window - only
the ON/OFF buttons do that, and by default they ask first. A channel that is
already driving something stays driving it.

Requires: NI-VISA (or any VISA) + `pip install pyvisa numpy matplotlib`
          (matplotlib only draws the waveform preview - everything else works
          without it)
Run with:  pythonw bk4063b_awg_gui.py    (pythonw = no console window)
"""

import datetime
import json
import os
import queue
import re
import struct
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
import pyvisa

try:
    import matplotlib
    matplotlib.use("TkAgg")
    matplotlib.rcParams["font.size"] = 8
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
except ImportError:                       # everything but the preview still works
    Figure = None

# USB vendor/product of the B&K 4060B series, used to pick the generator out of
# a bench that also has a scope and an analyzer on the bus.
USB_ID = "0xF4EC::0xEE38"

# Remembered between sessions: folder, prefix, arb settings, safety toggles.
# Kept out of the program folder so a git pull cannot clobber it.
CONFIG_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                           "BK4063B-AWG-GUI", "config.json")

# A copy of every waveform this app uploads, kept because the generator cannot
# read a stored waveform back out: without it, a waveform uploaded in an earlier
# session can never be drawn again. It lives beside the program rather than in
# %APPDATA% so the samples are somewhere you can actually get at them, which
# also makes the folder work in both directions - drop a .npy in here named to
# match a waveform on the generator and the preview will use it.
WAVE_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Waveforms")

CHANNELS = (1, 2)
# One colour per channel, used for the trace, the checkbox and the split
# y axis, so a glance at any of them identifies the others.
CH_COLOUR = {1: "#1f77b4", 2: "#d62728"}      # blue, red


def _lighten(colour, amount):
    """A paler version of a hex colour, mixed towards white."""
    red, green, blue = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
    mix = [int(round(c + (255 - c) * amount)) for c in (red, green, blue)]
    return "#%02x%02x%02x" % tuple(mix)


# The pending trace is the channel it is aimed at, but not yet: a paler shade
# of that channel's colour says both at once, where the same colour dashed left
# it looking like the channel's own trace drawn twice.
PENDING_COLOUR = {ch: _lighten(colour, 0.42) for ch, colour in CH_COLOUR.items()}

# A value the panel worked out rather than one that was set. Light enough to
# read black text through, since it sits behind an entry box.
COMPUTED_BG = "#fdf3b4"
WAVE_TYPES = ("SINE", "SQUARE", "RAMP", "PULSE", "NOISE", "DC", "ARB")
# The front panel only offers these two, so they are what the dropdown shows.
# The remote interface will actually take anything from 50 ohm to 100k (49 gets
# clamped to 50), and the cell stays typeable so an odd value can still be set -
# it just will not be visible on the generator's own screen.
LOADS = ("50", "HZ")

BAD_NAME_CHARS = r'<>:"/\|?*'

# Widgets that already use Space/arrows themselves, so the global shortcuts stay
# out of their way - typing in the prefix box must not toggle an output.
KEY_OWNERS = {
    "Entry", "TEntry", "Text", "Spinbox", "TSpinbox", "TCombobox",
    "Checkbutton", "TCheckbutton", "Radiobutton", "TRadiobutton",
    "Button", "TButton",
}

# BSWV parameters: (key, label, wave types it applies to). The panel shows them
# all and greys out the ones the selected type has no use for, so the layout
# does not jump around when the type changes.
# Laid out three to a row, so the first two rows are the same three settings
# twice: frequency, amplitude and offset across the top, and underneath each
# one the other way of saying it - period, high level, low level. The generator
# offers all six on its own display, and which row you would rather type
# depends on what you are setting up.
OSCILLATING = {"SINE", "SQUARE", "RAMP", "PULSE", "ARB"}
WAVE_PARAMS = [
    ("FRQ",   "Freq (Hz)",    OSCILLATING),
    ("AMP",   "Ampl (Vpp)",   OSCILLATING),
    ("OFST",  "Offset (V)",   OSCILLATING | {"DC"}),
    ("PERI",  "Period (s)",   OSCILLATING),
    ("HLEV",  "High (V)",     OSCILLATING),
    ("LLEV",  "Low (V)",      OSCILLATING),
    ("PHSE",  "Phase (deg)",  {"SINE", "SQUARE", "RAMP", "ARB"}),
    ("DUTY",  "Duty (%)",     {"SQUARE", "PULSE"}),
    ("SYM",   "Symmetry (%)", {"RAMP"}),
    ("WIDTH", "Width (s)",    {"PULSE"}),
    ("RISE",  "Rise (s)",     {"PULSE"}),
    ("FALL",  "Fall (s)",     {"PULSE"}),
    ("DLY",   "Delay (s)",    {"PULSE"}),
    ("STDEV", "Std dev (V)",  {"NOISE"}),
    ("MEAN",  "Mean (V)",     {"NOISE"}),
]

MOD_SHAPES = ("SINE", "SQUARE", "TRIANGLE", "UPRAMP", "DNRAMP", "NOISE", "ARB")
SOURCES = ("INT", "EXT")

# The one carrier the generator really insists on: PWM widens a pulse, so there
# is nothing for it to act on unless the wave type is PULSE. The rest of the
# modes take any of SINE/SQUARE/RAMP/ARB.
PWM_CARRIER = "PULSE"

# Modulation, sweep and burst are one panel row because the instrument treats
# them as one setting: only one can be active at a time. Each entry is
# (label, SCPI key, choices or None); choices make the cell a fixed dropdown.
MODES = ("Off", "AM", "DSBAM", "FM", "PM", "PWM", "ASK", "FSK", "PSK",
         "Sweep", "Burst")
MODE_PARAMS = {
    "Off":   [],
    "AM":    [("Mod freq (Hz)", "FRQ", None), ("Depth (%)", "DEPTH", None),
              ("Shape", "MDSP", MOD_SHAPES), ("Source", "SRC", SOURCES)],
    "DSBAM": [("Mod freq (Hz)", "FRQ", None),
              ("Shape", "MDSP", MOD_SHAPES), ("Source", "SRC", SOURCES)],
    "FM":    [("Mod freq (Hz)", "FRQ", None), ("Deviation (Hz)", "DEVI", None),
              ("Shape", "MDSP", MOD_SHAPES), ("Source", "SRC", SOURCES)],
    "PM":    [("Mod freq (Hz)", "FRQ", None), ("Deviation (deg)", "DEVI", None),
              ("Shape", "MDSP", MOD_SHAPES), ("Source", "SRC", SOURCES)],
    "PWM":   [("Mod freq (Hz)", "FRQ", None), ("Width dev (s)", "DEVI", None),
              ("Shape", "MDSP", MOD_SHAPES), ("Source", "SRC", SOURCES)],
    "ASK":   [("Key freq (Hz)", "KFRQ", None), ("Source", "SRC", SOURCES)],
    "FSK":   [("Key freq (Hz)", "KFRQ", None), ("Hop freq (Hz)", "HFRQ", None),
              ("Source", "SRC", SOURCES)],
    "PSK":   [("Key freq (Hz)", "KFRQ", None), ("Source", "SRC", SOURCES)],
    "Sweep": [("Time (s)", "TIME", None), ("Start (Hz)", "START", None),
              ("Stop (Hz)", "STOP", None), ("Shape", "SWMD", ("LINE", "LOG")),
              ("Direction", "DIR", ("UP", "DOWN")),
              ("Trigger", "TRSR", ("INT", "EXT", "MAN"))],
    "Burst": [("Period (s)", "PRD", None), ("Cycles", "TIME", None),
              ("Delay (s)", "DLAY", None),
              ("Shape", "GATE_NCYC", ("NCYC", "GATE")),
              ("Trigger", "TRSR", ("INT", "EXT", "MAN")),
              ("Start phase", "STPS", None)],
}
MODE_SLOTS = max(len(p) for p in MODE_PARAMS.values())

# Which SCPI block each mode lives in. All three are cleared before one is
# enabled, because the instrument allows only one at a time.
MODE_VERB = {"Sweep": "SWWV", "Burst": "BTWV"}
MODE_VERBS = ("MDWV", "SWWV", "BTWV")

# The burst parameters depend on one another, and the 4063B does not quietly
# ignore one that does not apply in the state it is in - it rejects the whole
# command. Since the state was switched on a line earlier and the parameters
# never landed, the burst comes straight back off with nothing said about why.
#
# From the programming guide's BTWV table:
#   PRD   not valid when the carrier is NOISE, or TRSR is EXT
#   STPS  not valid when the carrier is NOISE or PULSE
#   DLAY  available when GATE_NCYC is NCYC; not valid on a NOISE carrier
#   TIME  available when GATE_NCYC is NCYC; not valid on a NOISE carrier
# so the two parameters that decide the others are sent first and the rest are
# dropped where they cannot apply. MDWV and SWWV have no dependencies among
# the parameters this panel sends, which is why only burst misbehaved.
MODE_LEADS = {"Burst": ("GATE_NCYC", "TRSR")}


def mode_block(mode, params, wvtp=""):
    """A mode block's parameters as (key, value) pairs, filtered and ordered.

    `wvtp` is the carrier wave type, which decides some of it.
    """
    wvtp = str(wvtp or "").strip().upper()
    keep = dict(params)
    if mode == "Burst":
        gate = str(keep.get("GATE_NCYC", "NCYC")).strip().upper()
        trsr = str(keep.get("TRSR", "INT")).strip().upper()
        drop = set()
        if wvtp == "NOISE":
            # Noise has no cycles to count, no phase to start at and no shape
            # to gate, so only the trigger source survives.
            drop |= {"PRD", "STPS", "GATE_NCYC", "DLAY", "TIME"}
        if wvtp in ("NOISE", "PULSE"):
            drop.add("STPS")
        if gate != "NCYC":
            drop |= {"DLAY", "TIME"}
        if trsr == "EXT":
            drop.add("PRD")           # the period is whatever the trigger says
        keep = {k: v for k, v in keep.items() if k not in drop}
    leads = MODE_LEADS.get(mode, ())
    ordered = [(k, keep[k]) for k in leads if k in keep]
    return ordered + [(k, v) for k, v in keep.items() if k not in leads]


# Bits of the standard event status register worth repeating back. This
# generator has no SYST:ERR? queue - it answers that query with a complaint
# about the query itself - but *ESR? works, and reading it clears it.
ESR_BITS = ((32, "command error - the generator did not understand it"),
            (16, "execution error - understood, but not allowed in this state"),
            (8, "device error"),
            (4, "query error"))

# Modulation types appear as a bare tag with no value, mid-response.
_BARE_TAGS = {"AM", "DSBAM", "FM", "PM", "PWM", "ASK", "FSK", "PSK"}

# Keys a recalled setup does not send back. The generator takes PERI, HLEV and
# LLEV perfectly well - they are documented BSWV parameters and the panel offers
# them - but they say the same thing as FRQ, AMP and OFST, which a recall is
# already sending. Two descriptions of one setting in a single command is two
# instructions, and the last one wins. The rest are genuinely readback-only.
READ_ONLY_KEYS = {"PERI", "MAX_OUTPUT_AMP", "HLEV", "LLEV", "AMPVRMS",
                  "AMPDBM", "TRMD"}


# ---------------------------------------------------------------------------
# Waveform library
#
# Every shape is a pure function of a point count and a parameter lookup, so it
# can be built, previewed and tested without an instrument attached. Envelopes
# come out unipolar (0..1) because that is what an intensity control wants; the
# oscillating shapes come out bipolar (-1..+1). Either way the numbers are a
# shape, and the volts come from Ampl/Offset on the channel.
# ---------------------------------------------------------------------------

ENV_CHOICES = ("None", "Blackman-Harris", "Gaussian", "Hann", "Tukey")


class _Params:
    """Panel strings read as numbers, with a fallback when a box is empty."""

    def __init__(self, values):
        self.values = values

    def num(self, key, default):
        try:
            text = str(self.values.get(key, "")).strip()
            return float(text) if text else default
        except ValueError:
            return default

    def txt(self, key, default=""):
        return str(self.values.get(key, "")).strip() or default

    def tones(self, key, default=(10.0,)):
        out = []
        for token in str(self.values.get(key, "")).replace(";", ",").split(","):
            token = token.strip()
            if token:
                try:
                    out.append(float(token))
                except ValueError:
                    pass
        return out or list(default)


def _unit(n):
    """0 .. 1 across the record."""
    return np.linspace(0.0, 1.0, n)


def _centred(n):
    """-1 .. +1 across the record."""
    return np.linspace(-1.0, 1.0, n)


def _gaussian(n, trunc):
    """Truncated Gaussian. trunc is the half-width of the record in sigma, so 3
    puts the ends at exp(-4.5) ~ 1% rather than cutting a visible step."""
    return np.exp(-0.5 * (_centred(n) * max(trunc, 1e-6)) ** 2)


def _tukey(n, flat):
    """Flat top with raised-cosine shoulders. flat=0 is a Hann, flat=1 a square."""
    flat = min(max(flat, 0.0), 1.0)
    x = _unit(n)
    taper = (1.0 - flat) / 2.0
    w = np.ones(n)
    if taper > 0:
        left, right = x < taper, x > 1.0 - taper
        w[left] = 0.5 * (1 - np.cos(np.pi * x[left] / taper))
        w[right] = 0.5 * (1 - np.cos(np.pi * (1.0 - x[right]) / taper))
    return w


def _trapezoid(n, rise, fall):
    x = _unit(n)
    rise, fall = max(rise, 0.0), max(fall, 0.0)
    if rise + fall > 1.0:                       # keep a sane shape if over-specified
        rise, fall = rise / (rise + fall), fall / (rise + fall)
    w = np.ones(n)
    if rise > 0:
        m = x < rise
        w[m] = x[m] / rise
    if fall > 0:
        m = x > 1.0 - fall
        w[m] = (1.0 - x[m]) / fall
    return w


def _tanh_top(n, edge, flat):
    """Flat top with tanh shoulders - a smooth switch-on with no corner, which is
    what an AOM or EOM intensity ramp usually wants."""
    x = _centred(n)
    a = min(max(flat, 0.0), 1.0)
    w = max(edge, 1e-4)
    y = 0.5 * (np.tanh((x + a) / w) - np.tanh((x - a) / w))
    peak = float(y.max())
    return y / peak if peak > 0 else y


def _envelope(name, n, trunc=3.0, flat=0.5):
    if name in ("Blackman-Harris", "Blackman"):
        return _blackman_harris(n)
    if name == "Gaussian":
        return _gaussian(n, trunc)
    if name == "Hann":
        return np.hanning(n)
    if name == "Tukey":
        return _tukey(n, flat)
    return np.ones(n)


def _normalise(y):
    peak = float(np.max(np.abs(y)))
    return y / peak if peak > 0 else y


def _build_gaussian(n, p):
    return _gaussian(n, p.num("trunc", 3.0))


def _blackman_harris(n):
    """The four-term Blackman-Harris window.

    Sidelobes at about -92 dB where the classic three-term Blackman manages
    -58, which is the whole reason to reach for a window here: the sidelobes
    are what drives the line you are trying not to drive.
    """
    x = 2 * np.pi * _unit(n)
    return (0.35875 - 0.48829 * np.cos(x) + 0.14128 * np.cos(2 * x)
            - 0.01168 * np.cos(3 * x))


def _build_blackman(n, p):
    return _blackman_harris(n)


def _build_hann(n, p):
    return np.hanning(n)


def _build_tukey(n, p):
    return _tukey(n, p.num("flat", 0.5))


def _build_sech(n, p):
    """Hyperbolic secant - the amplitude profile for adiabatic rapid passage,
    and analytically solvable as the Rosen-Zener model."""
    return 1.0 / np.cosh(_centred(n) * max(p.num("trunc", 4.0), 1e-6))


def _build_sinc(n, p):
    """Bipolar. A sinc in time is a rectangle in frequency, so this is the
    starting point for a flat-topped spectral profile."""
    return np.sinc(_centred(n) * max(p.num("lobes", 4.0), 1e-6))


def _build_square(n, p):
    width = min(max(p.num("width", 0.5), 0.0), 1.0)
    return (np.abs(_centred(n)) <= width).astype(float)


def _build_trapezoid(n, p):
    return _trapezoid(n, p.num("rise", 0.1), p.num("fall", 0.1))


def _build_tanh_top(n, p):
    return _tanh_top(n, p.num("edge", 0.1), p.num("flat", 0.6))


def _build_dc(n, p):
    """A flat level. On its own it is the hold between two ramps; with a
    carrier it is a rectangular-envelope burst."""
    return np.full(n, p.num("level", 1.0))


def _build_linear(n, p):
    start, end = p.num("start", 0.0), p.num("end", 1.0)
    return start + (end - start) * _unit(n)


def _build_exp(n, p):
    """Exponential approach from start to end. The usual evaporative-cooling
    ramp is start 1, end 0, with tau setting how hard the knee is."""
    start, end = p.num("start", 1.0), p.num("end", 0.0)
    tau = max(p.num("tau", 0.3), 1e-4)
    t = _unit(n)
    k = (1.0 - np.exp(-t / tau)) / (1.0 - np.exp(-1.0 / tau))
    return start + (end - start) * k


def _build_smoothstep(n, p):
    """Minimum-jerk ramp: zero slope and zero curvature at both ends, which is
    what keeps a transport or a trap handover adiabatic."""
    start, end = p.num("start", 0.0), p.num("end", 1.0)
    t = _unit(n)
    return start + (end - start) * (t ** 3) * (10 - 15 * t + 6 * t * t)


def _build_chirp(n, p):
    """Linear frequency sweep across the record, in cycles. Pair a chirp with a
    sech envelope for adiabatic rapid passage."""
    c0, c1 = p.num("c0", 10.0), p.num("c1", 100.0)
    t = _unit(n)
    y = np.sin(2 * np.pi * (c0 * t + 0.5 * (c1 - c0) * t * t))
    return y * _envelope(p.txt("env", "None"), n)


def _build_multitone(n, p):
    """Sum of sines, each given as a whole number of cycles across the record so
    every tone closes cleanly when the waveform repeats."""
    t = _unit(n)
    y = np.zeros(n)
    for cycles in p.tones("tones"):
        y += np.sin(2 * np.pi * cycles * t)
    return _normalise(y) * _envelope(p.txt("env", "None"), n)


def _build_dgauss(n, p):
    """Derivative of a Gaussian: the quadrature half of a DRAG pulse. Put a
    Gaussian on one channel and this, scaled by beta, on the other."""
    x = _centred(n)
    trunc = max(p.num("trunc", 3.0), 1e-6)
    y = -x * trunc ** 2 * np.exp(-0.5 * (x * trunc) ** 2)
    return _normalise(y) * p.num("beta", 1.0)


# name -> (builder, [(label, key, default, choices or None)], takes a carrier)
_CARRIER = [("Carrier cycles", "cycles", "0", None),
            ("Carrier phase (deg)", "cphase", "0", None)]

BUILD_SHAPES = {
    "Gaussian":        (_build_gaussian,
                        [("Truncate (+/-sigma)", "trunc", "3", None)] + _CARRIER),
    "Blackman-Harris": (_build_blackman, list(_CARRIER)),
    "Hann":            (_build_hann, list(_CARRIER)),
    "Tukey flat-top":  (_build_tukey,
                        [("Flat fraction", "flat", "0.5", None)] + _CARRIER),
    "Sech (ARP)":      (_build_sech,
                        [("Truncate (+/-units)", "trunc", "4", None)] + _CARRIER),
    "Sinc":            (_build_sinc,
                        [("Zero crossings", "lobes", "4", None)] + _CARRIER),
    "Square pulse":    (_build_square,
                        [("Width fraction", "width", "0.5", None)] + _CARRIER),
    "Trapezoid":       (_build_trapezoid,
                        [("Rise fraction", "rise", "0.1", None),
                         ("Fall fraction", "fall", "0.1", None)] + _CARRIER),
    "Tanh flat-top":   (_build_tanh_top,
                        [("Edge fraction", "edge", "0.1", None),
                         ("Flat fraction", "flat", "0.6", None)] + _CARRIER),
    "Hold (DC)":       (_build_dc,
                        [("Level", "level", "1", None)] + _CARRIER),
    "Linear ramp":     (_build_linear,
                        [("Start", "start", "0", None),
                         ("End", "end", "1", None)] + _CARRIER),
    "Exponential ramp": (_build_exp,
                         [("Start", "start", "1", None), ("End", "end", "0", None),
                          ("Time constant", "tau", "0.3", None)] + _CARRIER),
    "Smoothstep ramp": (_build_smoothstep,
                        [("Start", "start", "0", None),
                         ("End", "end", "1", None)] + _CARRIER),
    "Chirp":           (_build_chirp,
                        [("Start cycles", "c0", "10", None),
                         ("End cycles", "c1", "100", None),
                         ("Envelope", "env", "Blackman-Harris", ENV_CHOICES)]),
    "Multitone":       (_build_multitone,
                        [("Cycles (comma list)", "tones", "10, 20, 35", None),
                         ("Envelope", "env", "None", ENV_CHOICES)]),
    "Gaussian deriv":  (_build_dgauss,
                        [("Truncate (+/-sigma)", "trunc", "3", None),
                         ("Beta", "beta", "1", None)]),
}
BUILD_SLOTS = max(len(spec) for _, spec in BUILD_SHAPES.values())


def build_waveform(shape, n_points, values):
    """Make the samples for one shape. Pure - no instrument, no widgets."""
    if shape not in BUILD_SHAPES:
        raise ValueError(f"unknown shape {shape!r}")
    n = int(n_points)
    if n < 2:
        raise ValueError("need at least 2 points")
    builder, _ = BUILD_SHAPES[shape]
    p = _Params(values)
    y = np.asarray(builder(n, p), dtype=np.float64)

    cycles = p.num("cycles", 0.0)
    if cycles > 0:
        # An envelope times a carrier: the shape becomes the burst outline and
        # the carrier fills it, which is how a Raman or Rabi pulse is specified.
        phase = np.deg2rad(p.num("cphase", 0.0))
        y = y * np.sin(2 * np.pi * cycles * _unit(n) + phase)
    return y


def build_warnings(shape, n_points, values, rate=0.0, carrier_hz=None):
    """Ways a record of `n_points` is too coarse for what is being asked of it.

    A built record is a list of numbers with no time in it until a clock is
    picked, so most of this is about the point count alone: how many points
    each carrier cycle gets, and whether a shape's own detail survives. `rate`
    only matters where the answer is in hertz - the carrier against Nyquist,
    and what the record comes to in seconds.
    """
    lines = []
    n = int(n_points) if n_points else 0
    if n < 2:
        return ["a record needs at least 2 points"]
    p = _Params(values)

    cycles = p.num("cycles", 0.0)
    if cycles > 0:
        per_cycle = n / cycles
        if per_cycle < ALIAS_LIMIT:
            lines.append(
                f"{cycles:g} carrier cycles across {n:,} points is "
                f"{per_cycle:.2g} points each - below two the carrier is "
                f"aliased, and what comes out is not the tone you asked for")
        elif per_cycle < DENSITY_LIMITS["cycle"]:
            lines.append(
                f"{cycles:g} carrier cycles across {n:,} points is "
                f"{per_cycle:.2g} points each - the carrier is only just "
                f"resolved, so the peaks will read low and the shape will be "
                f"visibly stepped")
        if rate > 0 and carrier_hz and carrier_hz > rate / 2.0:
            lines.append(
                f"a {_hz(carrier_hz)} carrier is above the Nyquist limit of "
                f"{_hz(rate / 2.0)} for {_hz(rate)} - it will come out as a "
                f"lower frequency, not as itself")

    # A shape's own detail is a fraction of the record, so it is the point
    # count that decides whether it survives - the clock never enters into it.
    for key, name, floor in (("rise", "rise", 0.0), ("fall", "fall", 0.0),
                             ("edge", "edge", 0.0), ("flat", "flat top", 0.0),
                             ("width", "width", 0.0)):
        fraction = p.num(key, floor)
        if 0 < fraction < 1 and fraction * n < 8:
            lines.append(f"the {name} is {fraction:g} of the record, which is "
                         f"{fraction * n:.2g} points - too few to shape it")
    return lines


# A sequence is built in real time against a sample clock, not in fractions of
# a record: the whole point is that its segments differ in length, and "2 us at
# 5 MHz" is how a pulse is specified at the bench. That makes the clock part of
# the specification rather than something chosen afterwards.
SEQ_LOCAL = "Local waveform"
SEQ_UNITS = ("ns", "us", "ms", "s")
_TIME_UNITS = {"s": 1.0, "ms": 1e-3, "m": 1e-3, "us": 1e-6, "u": 1e-6,
               "ns": 1e-9, "n": 1e-9}

# (label, key, default, entry width). One row of these per segment.
SEQ_COLUMNS = [("Shape", "shape", "Blackman-Harris", 16),
               ("Time", "time", "1", 8),
               ("Ampl", "ampl", "1", 6),
               ("Carrier (Hz)", "freq", "0", 10),
               ("Phase (deg)", "phase", "0", 8),
               ("Gap after", "gap", "0", 8),
               ("Extra (key=value)", "extra", "", 20)]


def shape_extras(shape):
    """A shape's own parameters as `key=value`, the carrier aside.

    The builder gives each of them a labelled box. A sequence row has one free
    text field instead, so the defaults are written into it when the shape is
    picked - otherwise they are reachable but invisible, and you would have to
    already know that a Tukey takes `flat=` to discover that it does.
    """
    if shape == SEQ_LOCAL:
        return "name="
    if shape not in BUILD_SHAPES:
        return ""
    return " ".join(f"{key}={default}"
                    for _, key, default, _ in BUILD_SHAPES[shape][1]
                    if key not in ("cycles", "cphase"))


SEQ_DEFAULT = {key: default for _, key, default, _ in SEQ_COLUMNS}
SEQ_DEFAULT["extra"] = shape_extras(SEQ_DEFAULT["shape"])

# Settings a spec carries besides its segments. Without them a pasted sequence
# is only half of itself: the same rows at the wrong clock are a different
# waveform, and there is nothing in a row that says which.
SEQ_SETTINGS = ("rate", "unit", "baseline", "coherent", "clock")


def _as_bool(text, default=False):
    value = str(text or "").strip().lower()
    if value in ("on", "yes", "true", "1"):
        return True
    if value in ("off", "no", "false", "0"):
        return False
    return default


def _hz(value):
    for scale, suffix in ((1e6, " MHz"), (1e3, " kHz"), (1.0, " Hz")):
        if abs(value) >= scale:
            return f"{value / scale:.4g}{suffix}"
    return f"{value:.4g} Hz"


def _secs(value):
    for scale, suffix in ((1.0, " s"), (1e-3, " ms"), (1e-6, " us"),
                          (1e-9, " ns")):
        if abs(value) >= scale:
            return f"{value / scale:.4g}{suffix}"
    return f"{value * 1e12:.4g} ps"


def parse_time(text, unit="us", default=0.0):
    """Seconds from '2', '2u', '2us', '1.5ms', '200m' or '2e-6s'.

    A bare number is in `unit`, the sequence's own setting, so microsecond
    pulses are typed as 2 rather than 0.000002 - and a 200 ms hold in the
    middle of that same sequence is still typed as 200m rather than as 200000.
    """
    raw = str(text if text is not None else "").strip().lower()
    raw = raw.replace("\u00b5", "u").replace("sec", "s")
    if not raw:
        return default
    match = re.fullmatch(r"([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)\s*([a-z]*)",
                         raw)
    if not match:
        raise ValueError(f"cannot read {text!r} as a time")
    value, suffix = float(match.group(1)), match.group(2) or unit
    if suffix not in _TIME_UNITS:
        raise ValueError(f"unknown time unit {suffix!r} in {text!r}")
    return value * _TIME_UNITS[suffix]


def parse_kv(text):
    """'trunc=3 flat=0.6' -> {'trunc': '3', 'flat': '0.6'}.

    Split on whitespace and semicolons but never on commas, so a value that is
    itself a list - Multitone's 'tones=10,20,35' - survives intact.
    """
    out = {}
    for token in re.split(r"[;\s]+", str(text or "").strip()):
        if not token:
            continue
        key, sep, value = token.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"extra parameter {token!r} should be key=value")
        out[key.strip()] = value.strip()
    return out


def _as_float(text, default=0.0):
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return default


def seq_shapes():
    """Every shape a segment can be, in the order the dropdown offers them."""
    return list(BUILD_SHAPES) + [SEQ_LOCAL]


# Shapes that have been renamed. Kept so a sequence spec written before the
# rename still reads: the window really did change, but the file should not
# have to be edited to find that out.
SHAPE_ALIASES = {"blackman": "Blackman-Harris"}


def resolve_shape(name):
    """Match a typed shape name case-insensitively, or say what is on offer."""
    text = str(name or "").strip()
    if text.lower() in SHAPE_ALIASES:
        return SHAPE_ALIASES[text.lower()]
    for candidate in seq_shapes():
        if candidate.lower() == text.lower():
            return candidate
    raise ValueError(f"unknown shape {text!r} - one of: "
                     + ", ".join(seq_shapes()))


def _carrier(n, cycles, phase):
    """`cycles` cycles across n samples, sampled half-open.

    Not `_unit(n)`, which runs 0..1 inclusive and so spends n samples covering
    n-1 intervals: a segment built that way carries its frequency about
    n/(n-1) high and hands the next segment a phase one sample out. Fine for a
    record that loops on itself, wrong for one that is butted against another,
    which is the whole of what a sequence is.

    The shape keeps the inclusive convention - a ramp typed 0 to 1 should
    reach 1 - because a shape's endpoints are specified where a carrier's are
    merely where the window fell.
    """
    return np.sin(2 * np.pi * cycles * (np.arange(n) / n) + np.deg2rad(phase))


def _segment_samples(shape, n, extra, cycles, phase, waves):
    """One segment's samples, unit amplitude."""
    for key in ("cycles", "cphase"):
        if key in extra:
            raise ValueError(
                f"put the carrier in the Carrier (Hz) and Phase columns, not "
                f"in {key}= - a sequence knows how long the segment is, so it "
                f"can work in hertz where the builder has to work in cycles")
    if shape != SEQ_LOCAL:
        y = build_waveform(shape, n, extra)
        return y * _carrier(n, cycles, phase) if cycles > 0 else y
    name = str(extra.get("name", "")).strip()
    src = (waves or {}).get(name)
    if src is None:
        known = ", ".join(sorted(waves or {})) or "nothing loaded"
        raise ValueError(f"no waveform called {name!r} - give the segment "
                         f"'name=<waveform>', one of: {known}")
    src = np.asarray(src, dtype=np.float64).ravel()
    if src.size < 2:
        raise ValueError(f"{name!r} has too few samples to use")
    # Stretched or squeezed into whatever time the sequence gives it, so one
    # stored record can appear at two lengths in the same sequence.
    y = np.interp(np.linspace(0.0, src.size - 1.0, n), np.arange(src.size), src)
    return y * _carrier(n, cycles, phase) if cycles > 0 else y


def build_sequence(segments, rate, unit="us", baseline=0.0, coherent=False,
                   waves=None):
    """Concatenate differently-specified segments into one record.

    Segments that need not resemble each other, laid end to end: a
    Blackman-Harris at 5 MHz, a gap, the same envelope at half the amplitude
    and ninety degrees, a slow ramp, a hold, a ramp back down. A plain train of
    identical pulses is the case where every row is the same.
    Each segment carries its own shape, duration, amplitude, carrier frequency
    and phase, and its own gap to whatever follows.

    `rate` is the sample clock the record is meant to be played at, in Sa/s -
    it is what turns a duration in seconds into a point count and a carrier in
    hertz into cycles across the segment, so a sequence is only true at the
    rate it was built for.

    `coherent` references every carrier to the start of the sequence rather
    than to its own segment, which is what makes the second pulse of a Ramsey
    pair arrive with a defined phase relative to the first. Off, the typed
    phase is exactly the phase the segment starts at.

    Returns (samples, [(index, shape, points, seconds, gap seconds), ...]).
    """
    rate = _as_float(rate, 0.0)
    if rate <= 0:
        raise ValueError("sample rate must be a positive number of Sa/s")

    pieces, rows, elapsed = [], [], 0.0
    for index, seg in enumerate(segments, 1):
        if not str(seg.get("shape", "")).strip():
            continue
        shape = resolve_shape(seg.get("shape"))
        span = parse_time(seg.get("time"), unit, 0.0)
        gap = parse_time(seg.get("gap"), unit, 0.0)
        if span < 0 or gap < 0:
            raise ValueError(f"segment {index} ({shape}) has a negative time")
        n = int(round(span * rate))
        if n < 2:
            raise ValueError(
                f"segment {index} ({shape}) comes to {n} point(s) at "
                f"{rate:.10g} Sa/s - give it a longer time or a faster clock")
        freq = _as_float(seg.get("freq"), 0.0)
        phase = _as_float(seg.get("phase"), 0.0)
        if coherent:
            # As though a reference oscillator at this segment's frequency had
            # been running since the sequence began.
            phase += 360.0 * freq * elapsed
        y = _segment_samples(shape, n, parse_kv(seg.get("extra")),
                             freq * span, phase, waves)
        pieces.append(np.asarray(y, dtype=np.float64) * _as_float(
            seg.get("ampl"), 1.0))
        rows.append((index, shape, n, span, gap))
        elapsed += span

        gap_n = int(round(gap * rate))
        if gap_n > 0:
            pieces.append(np.full(gap_n, float(baseline)))
        elapsed += gap

    if not pieces:
        raise ValueError("no segments to build - add one first")
    return np.concatenate(pieces), rows


def sequence_extent(segments, rate, unit="us"):
    """(segments, seconds, points) without building the samples.

    Wanted on every keystroke to keep the running total honest, which is why it
    tolerates a half-typed number instead of raising at one.
    """
    rate = _as_float(rate, 0.0)
    count, span, points = 0, 0.0, 0
    for seg in segments:
        if not str(seg.get("shape", "")).strip():
            continue
        count += 1
        for key in ("time", "gap"):
            try:
                seconds = parse_time(seg.get(key), unit, 0.0)
            except ValueError:
                continue
            span += seconds
            points += int(round(seconds * rate)) if rate > 0 else 0
    return count, span, points


def parse_sequence_spec(text):
    """Read a typed or pasted sequence back as (segments, settings).

    One segment per line, fields in the order of SEQ_COLUMNS. Everything after
    the sixth comma is the extra field, so a value with commas of its own
    survives. Blank lines and # comments are skipped, as they are everywhere
    else numbers are pasted in - except a comment of the form `# rate: 1e8`,
    which is how the spec carries the settings that are not per-segment. They
    stay comments so that a spec written by hand without them still reads, and
    so that the whole thing is still a file somebody can follow.

    `settings` holds only the keys the text actually mentioned, and holds them
    as the strings they were written as: a rate typed 1e8 comes back 1e8 rather
    than 100000000.
    """
    keys = [key for _, key, _, _ in SEQ_COLUMNS]
    out, settings = [], {}
    for number, line in enumerate(str(text or "").splitlines(), 1):
        line = line.strip().lstrip("\ufeff")
        if not line:
            continue
        if line.startswith("#"):
            name, sep, value = line.lstrip("#").strip().partition(":")
            if sep and name.strip().lower() in SEQ_SETTINGS:
                settings[name.strip().lower()] = value.strip()
            continue
        fields = [f.strip() for f in line.split(",", len(keys) - 1)]
        try:
            seg = {"shape": resolve_shape(fields[0])}
        except ValueError as exc:
            raise ValueError(f"line {number}: {exc}") from None
        for key, value in zip(keys[1:], fields[1:]):
            seg[key] = value
        out.append({key: seg.get(key, "") for key in keys})
    if not out:
        raise ValueError("no segments found in that text")
    return out, settings


def format_sequence_spec(segments, rate, unit="us", baseline="0",
                         coherent=False, clock=True):
    """Segments and settings back out as the text `parse_sequence_spec` reads.

    `rate` and `baseline` are written through as the strings they were typed
    as, so a spec copied out and pasted back leaves the boxes exactly as they
    were rather than normalising 1e8 into 100000000.
    """
    keys = [key for _, key, _, _ in SEQ_COLUMNS]
    lines = ["# BK4063B sequence",
             f"# rate: {rate}",
             f"# unit: {unit}",
             f"# baseline: {baseline}",
             f"# coherent: {'on' if coherent else 'off'}",
             f"# clock: {'on' if clock else 'off'}",
             "#",
             "# " + ", ".join(keys)]
    for seg in segments:
        if not str(seg.get("shape", "")).strip():
            continue
        lines.append(", ".join(str(seg.get(key, "")).strip() for key in keys)
                     .rstrip(", "))
    return "\n".join(lines) + "\n"


def parse_pasted(text):
    """Read typed or pasted numbers into (2-D array, column names or None).

    Same contract as read_table, so pasted data goes through the same column
    picker: rows split on newlines, columns on commas, semicolons or whitespace.
    A first row that is not numeric is taken as the column names.
    """
    rows, names = [], None
    for line in text.strip().splitlines():
        line = line.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        fields = [f for f in re.split(r"[,;\s]+", line) if f]
        try:
            rows.append([float(f) for f in fields])
        except ValueError:
            if not rows and names is None:
                names = [f.strip('"') for f in fields]
                continue
            raise ValueError(f"cannot read this as numbers: {line[:60]}")
    if not rows:
        raise ValueError("no numbers found")

    width = len(rows[0])
    if any(len(r) != width for r in rows):
        raise ValueError("rows do not all have the same number of columns")
    data = np.asarray(rows, dtype=np.float64)
    # Numbers typed across one line are a waveform, not one sample of many
    # channels.
    if data.shape[0] == 1 and data.shape[1] > 2:
        data, names = data.T, None
    return data, names


# Column headings that are an x-axis rather than samples, so a file written as
# time,volts picks the volts by default.
TIME_NAMES = {"time", "time_s", "times", "t", "t_s", "sec", "secs", "seconds",
              "s", "x", "index", "n", "sample", "samples"}


def read_table(path):
    """Read a sample file into (2-D array of columns, column names or None).

    Accepts .npy, or text with any delimiter: one sample per line, time,volts
    pairs, or a full multi-column capture. A header row is detected by the
    numeric parse failing on it and skipped - Scope Grab's own CSVs are headed
    `time_s,CH1_V,...`, and replaying one of those here is the whole point.
    """
    if path.lower().endswith(".npy"):
        data = np.asarray(np.load(path), dtype=np.float64)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        return data, None

    delim = "," if path.lower().endswith(".csv") else None
    names = None
    try:
        data = np.loadtxt(path, delimiter=delim, ndmin=2)
    except ValueError:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            header = fh.readline()
        names = [c.strip().strip('"') for c in header.split(delim or None)]
        data = np.loadtxt(path, delimiter=delim, ndmin=2, skiprows=1)
    data = np.asarray(data, dtype=np.float64)

    # A single row of many values is a waveform written across the line, not a
    # one-sample capture of many channels.
    if data.shape[0] == 1 and data.shape[1] > 2:
        data, names = data.T, None
    return data, names


def default_column(names, ncols):
    """Which column is most likely to hold the samples."""
    if names and len(names) == ncols:
        for i, name in enumerate(names):
            if name.strip().lstrip("#").strip().lower() not in TIME_NAMES:
                return i
    # Unnamed: a lone column is the data, and a pair is time,volts.
    return 0 if ncols == 1 else 1


def dac_samples(samples, normalize=True):
    """The samples exactly as the DAC will receive them, in -1..+1.

    One definition on purpose. This used to be written out separately where the
    bytes were packed, where the local copy was kept, and not at all where the
    pending trace was drawn - so the preview showed raw file values while
    everything after the upload showed normalised ones, and the same waveform
    changed height the moment it was sent.
    """
    data = np.asarray(samples, dtype=np.float64).ravel()
    if normalize:
        peak = float(np.max(np.abs(data)))
        if peak == 0:
            raise ValueError("all samples are zero; nothing to normalise")
        data = data / peak
    return np.clip(data, -1.0, 1.0)


def cache_file(name):
    return os.path.join(WAVE_CACHE, safe_name(name) + ".npy")


def cache_save(name, samples):
    """Keep a copy of what was just uploaded, so it can be previewed later."""
    os.makedirs(WAVE_CACHE, exist_ok=True)
    np.save(cache_file(name), np.asarray(samples, dtype=np.float32))


def cache_load():
    """Every waveform this app has uploaded, as {name: samples}."""
    out = {}
    try:
        entries = sorted(os.listdir(WAVE_CACHE))
    except OSError:
        return out
    for entry in entries:
        if not entry.endswith(".npy"):
            continue
        try:
            out[entry[:-4]] = np.asarray(np.load(os.path.join(WAVE_CACHE, entry)),
                                         dtype=np.float64).ravel()
        except Exception:
            pass                      # a corrupt cache entry is not worth a crash
    return out


def cache_forget(name):
    try:
        os.remove(cache_file(name))
        return True
    except OSError:
        return False


def safe_name(text):
    """Trim a typed prefix down to something legal in a filename."""
    out = "".join("_" if c in BAD_NAME_CHARS else c for c in text.strip())
    return out.strip() or "awg"


def fmt_value(raw):
    """Normalise an instrument reply into what the panel displays."""
    raw = str(raw).strip()
    try:
        return f"{float(raw):g}"
    except ValueError:
        return raw


def _strip_unit(text):
    """'1000HZ' -> 1000.0, '0.001S' -> 0.001, '1000000Sa/s' -> 1000000.0,
    'NOR' -> 'NOR'."""
    body = text.rstrip("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ%/")
    try:
        return float(body)
    except ValueError:
        return text


def parse_reply(response):
    """Turn 'C1:BSWV WVTP,SINE,FRQ,1000HZ' into {'WVTP': 'SINE', 'FRQ': 1000.0}.

    Three wrinkles in this dialect stop a plain pairwise split from working:

    * A leading value with no key ('C1:OUTP ON,LOAD,HZ') -- filed under 'STATE',
      which is how the matching set-command spells it.
    * A bare modulation tag with no value ('MDWV STATE,ON,AM,MDSP,SINE,...') --
      filed under 'TYPE'. Left in place it shifts every later pair by one.
    * A trailing 'CARR,...' carrier block on MDWV/SWWV/BTWV -- nested under
      'CARR'. Flattened it would clobber same-named modulation keys, so that
      'FRQ' would report the carrier frequency rather than the modulating one.
    """
    _, _, payload = response.strip().partition(" ")
    fields = [f.strip() for f in payload.split(",") if f.strip() != ""]

    out = {}
    i = 0
    if fields and fields[0] in ("ON", "OFF"):
        out["STATE"] = fields[0]
        i = 1
    while i < len(fields):
        key = fields[i]
        if key == "CARR":
            out["CARR"] = parse_reply("CARR " + ",".join(fields[i + 1:]))
            break
        if key in _BARE_TAGS:
            out["TYPE"] = key
            i += 1
            continue
        if i + 1 < len(fields):
            out[key] = _strip_unit(fields[i + 1])
            i += 2
        else:
            out[key] = None
            i += 1
    return out


def _mod_wave(name, u):
    """The modulating signal, -1..1, from its own phase u (in cycles)."""
    u = u % 1.0
    name = (name or "SINE").upper()
    if name == "SQUARE":
        return np.where(u < 0.5, 1.0, -1.0)
    if name == "TRIANGLE":
        return 1.0 - 4.0 * np.abs(((u + 0.25) % 1.0) - 0.5)
    if name == "UPRAMP":
        return 2.0 * u - 1.0
    if name == "DNRAMP":
        return 1.0 - 2.0 * u
    if name == "NOISE":
        return np.clip(np.random.default_rng(0).standard_normal(u.size) / 3.0,
                       -1.0, 1.0)
    return np.sin(2.0 * np.pi * u)


def _shape(wvtp, ph, num, period, arb, hold, duty_over=None):
    """Unit shape, -1..1, from carrier phase ph (in cycles)."""
    x = ph % 1.0
    if wvtp == "SINE":
        return np.sin(2.0 * np.pi * x)
    if wvtp == "SQUARE":
        duty = duty_over if duty_over is not None else num("DUTY", 50.0) / 100.0
        return np.where(x < np.clip(duty, 1e-6, 1 - 1e-6), 1.0, -1.0)
    if wvtp == "RAMP":
        sym = np.clip(num("SYM", 50.0) / 100.0, 1e-6, 1 - 1e-6)
        return np.where(x < sym, 2 * x / sym - 1, 1 - 2 * (x - sym) / (1 - sym))
    if wvtp == "PULSE":
        # Delay slides the pulse inside its own period; rise and fall are real
        # times, so they only mean anything as a fraction of that period.
        x = (x - num("DLY") / period) % 1.0
        width = num("WIDTH")
        duty = (width / period if width > 0
                else (duty_over if duty_over is not None
                      else num("DUTY", 20.0) / 100.0))
        duty = np.clip(duty, 1e-6, 1 - 1e-6)
        rise = max(num("RISE") / period, 1e-9)
        fall = max(num("FALL") / period, 1e-9)
        y = np.zeros_like(x)
        y = np.where(x < rise, x / rise, y)
        y = np.where((x >= rise) & (x < duty), 1.0, y)
        y = np.where((x >= duty) & (x < duty + fall), 1.0 - (x - duty) / fall, y)
        return 2.0 * y - 1.0
    if wvtp == "ARB":
        if arb is None or len(arb) < 2:
            return None
        pos = x * len(arb)
        if hold:
            # TrueArb: each stored point is held to the next clock, so floor the
            # index and the staircase is in the samples themselves.
            return arb[pos.astype(int) % len(arb)]
        # DDS: the generator ramps from point to point. Interpolating has to
        # happen here rather than by drawing style, because a floored index
        # would bake the steps into the data and no line style could undo it.
        wrapped = np.append(arb, arb[0])         # the record joins back on itself
        return np.interp(pos % len(arb), np.arange(len(wrapped)), wrapped)
    return None


def _numget(source):
    """Read panel strings as numbers, falling back when a box is empty."""
    def num(key, default=0.0):
        try:
            return float(str(source.get(key, "")).strip() or default)
        except (ValueError, AttributeError):
            return default
    return num


def _repeat_freq(wvtp, freq, arb_period):
    """How often the record on screen actually repeats, in hertz.

    An arb repeats at the channel's FRQ only while the record loaded on the
    channel is the one being drawn. Under TrueArb the points come out at the
    sample clock, so a record of a different length repeats at rate / points -
    and a record that has not been sent yet is exactly the case where the
    caller knows that length and FRQ still describes the old one. So an
    `arb_period` handed in for an arb is the repeat time, and FRQ is stale.

    DDS resamples whatever it holds into one period, so there the length says
    nothing about the rate and the channel's frequency stands: callers pass no
    `arb_period` for it.
    """
    if wvtp == "ARB" and arb_period and arb_period > 0:
        return 1.0 / arb_period
    return freq


def preview_period(wvtp, vals, mode="Off", mod=None, arb_period=None):
    """The repeat time worth drawing two of.

    With a mode running that is the envelope, not the carrier: two carrier
    cycles of a 1 kHz tone under 10 Hz AM is a flat sine with no modulation
    visible, which is how the preview managed to look plausible while ignoring
    the whole modulation row.
    """
    num, mnum = _numget(vals), _numget(mod or {})
    freq = _repeat_freq(wvtp, num("FRQ", 1000.0), arb_period)
    carrier = 1.0 / (freq if freq > 0 else 1000.0)
    if mode == "Sweep":
        return max(mnum("TIME", 1.0), 1e-9)
    if mode == "Burst":
        period = mnum("PRD", 0.0)
        return period if period > 0 else carrier * max(mnum("TIME", 1.0), 1.0)
    if mode in ("AM", "DSBAM", "FM", "PM", "PWM"):
        rate = mnum("FRQ", 0.0)
        return 1.0 / rate if rate > 0 else carrier
    if mode in ("ASK", "FSK", "PSK"):
        rate = mnum("KFRQ", 0.0)
        return 1.0 / rate if rate > 0 else carrier
    return carrier


# How finely the preview is sampled. The window is sized by the modulating
# envelope rather than by the carrier, so a slow modulation on a fast carrier
# can put thousands of carrier cycles inside one picture, and a fixed point
# budget then draws an alias instead of the waveform. The count is chosen per
# trace from what is actually in it, between a floor that keeps a plain tone
# smooth and a ceiling that keeps a redraw-per-keystroke responsive.
PREVIEW_MIN_POINTS = 2000
PREVIEW_MAX_POINTS = 20000
PTS_PER_CYCLE = 40          # what a smooth oscillating trace wants
PTS_PER_ARB_SAMPLE = 3      # a stored point cannot be resolved finer than itself

# Points per feature below which the preview stops being a faithful picture.
# A dozen points across a cycle already puts the drawn peak about 3% low, and
# below two the shape itself is wrong. A stored arb point is its own limit -
# one preview point each already shows every sample there is - so its threshold
# sits at one rather than above it.
DENSITY_LIMITS = {"cycle": 12.0, "pulse": 8.0, "edge": 4.0,
                  "stored point": 1.0}

# Below this the shape is not merely coarse, it is a different shape: two
# points per cycle is where a sampled sine stops carrying its own frequency.
ALIAS_LIMIT = 2.0


def preview_features(wvtp, vals, mode="Off", mod=None, arb=None,
                     arb_period=None):
    """Everything in the trace that has to be resolved, as
    (per second, what one of them is called, points wanted each).

    Separate from `preview_period`, which answers how *long* to draw. How
    *finely* is a different question with a different answer: the window
    follows the modulating envelope while the detail follows the carrier, its
    deviation, a pulse edge, or the stored points of an arb.

    `arb_period` is the arb's own repeat time where the caller knows it; its
    stored points then come out at that rate rather than at FRQ, which is what
    the trace is drawn at.
    """
    num, mnum = _numget(vals), _numget(mod or {})
    freq = num("FRQ", 1000.0)
    if freq <= 0:
        freq = 1000.0
    freq = _repeat_freq(wvtp, freq, arb_period)

    rate = freq
    if mode == "FM":
        rate = freq + abs(mnum("DEVI"))
    elif mode == "FSK":
        rate = max(rate, mnum("HFRQ", freq))
    elif mode == "Sweep":
        rate = max(rate, mnum("START", freq), mnum("STOP", freq))
    out = [(rate, "cycle", PTS_PER_CYCLE)]

    if wvtp == "PULSE":
        width = num("WIDTH")
        if width > 0:
            out.append((1.0 / width, "pulse", PTS_PER_CYCLE))
        for key in ("RISE", "FALL"):
            edge = num(key)
            # An edge thousands of times shorter than the period is drawn as
            # the vertical step it may as well be. Only an edge long enough to
            # show as a slope is worth resolving.
            if edge > 0.002 / freq:
                out.append((1.0 / edge, "edge", PTS_PER_CYCLE))
    elif wvtp == "ARB" and arb is not None and len(arb) >= 2:
        out.append((freq * len(arb), "stored point", PTS_PER_ARB_SAMPLE))
    return out


def _key_rate(mode, mod):
    """Edges per second of whatever gates the trace on and off, or 0."""
    mnum = _numget(mod or {})
    if mode in ("ASK", "FSK", "PSK"):
        return 2.0 * mnum("KFRQ", 0.0)          # one edge per keyed state
    if mode == "Burst" and \
            str((mod or {}).get("GATE_NCYC", "NCYC")).upper() != "GATE":
        prd = mnum("PRD", 0.0)
        return 1.0 / prd if prd > 0 else 0.0
    return 0.0


def preview_points(span, wvtp, vals, mode="Off", mod=None, arb=None,
                   arb_period=None):
    """How many samples to draw `span` seconds with.

    Deterministic, so the plot and the warning about the plot agree without
    having to pass the count between them.
    """
    want = max(per * span * rate for rate, _, per
               in preview_features(wvtp, vals, mode, mod, arb, arb_period))
    n = int(min(max(PREVIEW_MIN_POINTS, want), PREVIEW_MAX_POINTS))

    # Land the grid exactly on the keying edges. A gate that switches between
    # two samples slopes into its off state and leaves it a sample late, which
    # on ASK - the one mode whose off state is a flat zero - reads as points
    # missing from the zero rather than as a square edge.
    edges = _key_rate(mode, mod) * span
    if 1.0 <= edges <= n:
        n = int(round(round((n - 1) / edges) * edges)) + 1
    return max(2, min(n, PREVIEW_MAX_POINTS))


def preview_aliasing(span, n, wvtp, vals, mode="Off", mod=None, arb=None,
                     arb_period=None):
    """True when `n` points across `span` seconds cannot draw this faithfully.

    Asked of a whole trace rather than of one feature: the panel says which
    channel to distrust, not which parameter of it, so anything too fine for
    the budget is enough to answer yes.
    """
    if span <= 0:
        return False
    return any(rate > 0 and n / (span * rate) < DENSITY_LIMITS.get(unit, 8.0)
               for rate, unit, _
               in preview_features(wvtp, vals, mode, mod, arb, arb_period))


def preview_curve(wvtp, vals, arb=None, periods=2.0, n=None, hold=True,
                  period=None, span=None, mode="Off", mod=None, invert=False,
                  arb_period=None):
    """What the panel currently describes, in volts against seconds.

    Computed here rather than read back from the generator: the point is to show
    what Apply would produce, before it is applied. `hold` picks how an arb gets
    from one stored point to the next - held under TrueArb, ramped under DDS.

    `period` sizes the window that would otherwise come from FRQ, and `span`
    sets how much time to draw. Both exist so several traces can be put on one
    shared time axis: two channels running at different frequencies are
    simultaneous in the real world, and drawing each over its own private window
    would imply they line up when they do not.

    `arb_period` is a different thing and the two are not interchangeable: it
    says how often the *record* repeats, which is what the trace is drawn at.
    Under a mode `period` is the envelope, so feeding that to the carrier would
    draw a 1 kHz arb under 100 Hz AM as a 100 Hz one. See `_repeat_freq` for
    when a caller has an `arb_period` to give.

    `n` is the number of samples to draw with; left out, it is chosen from
    what the trace contains, because the window is sized by the envelope and a
    fixed budget across it leaves a fast carrier undersampled.

    `mode` and `mod` carry the modulation/sweep/burst row. Everything that
    varies with time is folded into an instantaneous frequency, a phase offset
    and an amplitude scale, then integrated once - which is what lets FM, FSK
    and a sweep share a path with a plain carrier instead of each being a
    special case.

    Returns (t, volts, note) or None when there is nothing meaningful to draw.
    """
    mod = mod or {}
    num, mnum = _numget(vals), _numget(mod)
    amp, ofst = num("AMP", 1.0), num("OFST")
    freq = num("FRQ", 1000.0)
    if freq <= 0:
        freq = 1000.0
    # Substituted before f_inst is built, so the one accumulator below carries
    # the record's real repeat rate and FM, FSK and a sweep still deviate about
    # it rather than about a frequency the record is not coming out at.
    freq = _repeat_freq(wvtp, freq, arb_period)

    if period is None:
        period = preview_period(wvtp, vals, mode, mod, arb_period)
    if span is None:
        span = periods * period
    if n is None:
        n = preview_points(span, wvtp, vals, mode, mod, arb, arb_period)
    t = np.linspace(0.0, span, n)
    dt = t[1] - t[0] if n > 1 else 1.0

    f_inst = np.full(n, freq)                    # cycles per second
    ph_off = np.full(n, num("PHSE") / 360.0)     # cycles
    scale = np.ones(n)                           # amplitude multiplier
    duty_over = None
    note = ""

    if mode in ("AM", "DSBAM"):
        m = _mod_wave(mod.get("MDSP"), t * mnum("FRQ", 100.0))
        if mode == "AM":
            # Measured on the instrument, not assumed: 0% depth leaves the
            # amplitude alone and 100% doubles the peak, so the scale is
            # 1 + m, not the (1 + m)/2 that Keysight-style generators use and
            # that this drew at half height until it was checked on a scope.
            scale = 1.0 + mnum("DEPTH", 100.0) / 100.0 * m
        else:
            scale = m                            # suppressed carrier
    elif mode == "FM":
        f_inst = freq + mnum("DEVI", 0.0) * _mod_wave(
            mod.get("MDSP"), t * mnum("FRQ", 100.0))
    elif mode == "PM":
        ph_off = ph_off + mnum("DEVI", 0.0) / 360.0 * _mod_wave(
            mod.get("MDSP"), t * mnum("FRQ", 100.0))
    elif mode == "PWM":
        base = (num("WIDTH") * freq if num("WIDTH") > 0
                else num("DUTY", 20.0) / 100.0)
        duty_over = np.clip(
            base + mnum("DEVI", 0.0) * freq * _mod_wave(
                mod.get("MDSP"), t * mnum("FRQ", 100.0)), 1e-6, 1 - 1e-6)
    elif mode in ("ASK", "FSK", "PSK"):
        key = _mod_wave("SQUARE", t * mnum("KFRQ", 100.0)) > 0
        if mode == "ASK":
            scale = np.where(key, 1.0, 0.0)
        elif mode == "FSK":
            f_inst = np.where(key, freq, mnum("HFRQ", freq))
        else:
            ph_off = ph_off + np.where(key, 0.0, 0.5)      # 180 degrees
    elif mode == "Sweep":
        sweep_t = max(mnum("TIME", 1.0), 1e-9)
        start, stop = mnum("START", freq), mnum("STOP", freq)
        u = (t % sweep_t) / sweep_t
        if str(mod.get("DIR", "UP")).upper() == "DOWN":
            u = 1.0 - u
        if str(mod.get("SWMD", "LINE")).upper() == "LOG" and start > 0 and stop > 0:
            f_inst = start * (stop / start) ** u
        else:
            f_inst = start + (stop - start) * u
    elif mode == "Burst":
        if str(mod.get("GATE_NCYC", "NCYC")).upper() == "GATE":
            # Gated burst follows an external signal we know nothing about.
            note = "gated burst: gate not modelled"
        else:
            cycles = max(mnum("TIME", 1.0), 0.0)
            prd = mnum("PRD", 0.0) or (cycles / freq)
            scale = np.where(np.mod(t - mnum("DLAY", 0.0), max(prd, 1e-12))
                             < cycles / freq, 1.0, 0.0)

    if wvtp == "DC":
        return t, np.full(n, ofst), note
    if wvtp == "NOISE":
        rng = np.random.default_rng(0)           # fixed seed: a still picture
        y = num("STDEV", 0.5) * rng.standard_normal(n) * scale
        return t, num("MEAN") + (-y if invert else y), note

    # One phase accumulator for the lot: a plain carrier is the constant case,
    # and FM, FSK and a sweep are the same integral with f varying.
    ph = np.cumsum(f_inst) * dt + ph_off
    y = _shape(wvtp, ph, num, period, arb, hold, duty_over=duty_over)
    if y is None:
        return None
    y = y * scale
    if invert:
        y = -y                                   # polarity flips the AC part only
    return t, ofst + (amp / 2.0) * y, note


# ---------------------------------------------------------------------------
# Instrument layer
# ---------------------------------------------------------------------------

class Awg:
    def __init__(self):
        self.rm = None
        self.inst = None
        self.idn = ""
        self.addr = ""

    def connect(self, addr=None):
        self.close()
        self.rm = pyvisa.ResourceManager()
        if addr:
            candidates = [addr]
        else:
            # The generator's VID/PID first, then anything else on USB, so an
            # unexpected address still connects if it answers to the right *IDN?.
            resources = self.rm.list_resources()
            candidates = [r for r in resources if USB_ID in r]
            candidates += [r for r in resources
                           if r.startswith("USB") and r not in candidates]
        for res in candidates:
            try:
                dev = self.rm.open_resource(res)
                dev.timeout = 5000
                dev.read_termination = "\n"
                dev.write_termination = "\n"
                idn = dev.query("*IDN?").strip()
            except Exception:
                continue
            if "4063B" in idn:
                dev.timeout = 20000
                dev.chunk_size = 1024 * 1024
                self.inst, self.idn, self.addr = dev, idn, res
                return idn
            dev.close()
        raise RuntimeError("No B&K 4063B found on USB. Check the rear-panel "
                           "USB-B cable and that NI MAX still sees the generator.")

    def close(self):
        for obj in (self.inst, self.rm):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self.inst = self.rm = None

    def query(self, command):
        return self.inst.query(command).strip()

    def write(self, command):
        self.inst.write(command)

    def wait(self):
        self.query("*OPC?")

    def complaint(self):
        """What the generator has objected to since this was last read, or "".

        Reading clears it, so a check before a batch of writes starts them from
        a clean slate. Without this an Apply goes out blind: a refused command
        looks exactly like an accepted one, which is how a burst could come
        back switched off with nothing said.
        """
        try:
            status = int(self.query("*ESR?"))
        except Exception:
            return ""                 # no status register is not a failure
        return "; ".join(text for bit, text in ESR_BITS if status & bit)

    # -- reading -----------------------------------------------------------

    def read_channel(self, ch):
        """Every block the panel shows, as the instrument's own raw strings."""
        return {verb: self.query(f"C{ch}:{verb}?")
                for verb in ("OUTP", "BSWV", "ARWV", "SRATE",
                             "MDWV", "SWWV", "BTWV")}

    def user_waveforms(self):
        """Names of the waveforms uploaded into user memory."""
        _, _, payload = self.query("STL? USER").partition(" ")
        return [f.strip() for f in payload.split(",")
                if f.strip() and f.strip() != "WVNM"]

    # -- writing -----------------------------------------------------------

    def set_output(self, ch, on):
        self.write(f"C{ch}:OUTP {'ON' if on else 'OFF'}")

    def apply_channel(self, ch, blocks, log=lambda s: None):
        """Push one channel's settings, in the only order the 4063B accepts.

        `blocks` is {verb: {key: value}} holding just what should be written.
        The ordering is not cosmetic - the instrument silently overrides
        earlier writes with later ones:

          The declared load rescales amplitude, so it must precede BSWV -- set
            it after and a 1.5 Vpp request typed against HiZ silently becomes
            0.75 Vpp when the load is then changed to 50 ohm.
          ARWV and SRATE both force WVTP,ARB, so both must precede BSWV.
          BSWV clears any active modulation, so it must precede the mode block.

        Get this wrong and the commands are all accepted, with the panel and the
        generator quietly disagreeing about what is being output.

        Returns the commands the generator refused, which is nothing on a good
        day and the whole point on a bad one.
        """
        prefix = f"C{ch}"
        refused = []
        self.complaint()              # start from a clean status register

        def send(command):
            self.write(command)
            log(f"  {command}")
            problem = self.complaint()
            if problem:
                refused.append(command)
                log(f"    ^ refused: {problem}")

        outp = blocks.get("OUTP") or {}
        parts = [f"{k},{v}" for k, v in outp.items() if k != "STATE" and v != ""]
        if parts:
            send(f"{prefix}:OUTP {','.join(parts)}")

        arb = blocks.get("ARWV") or {}
        if arb.get("NAME"):
            send(f"{prefix}:ARWV NAME,{arb['NAME']}")
        elif arb.get("INDEX") not in (None, ""):
            send(f"{prefix}:ARWV INDEX,{int(float(arb['INDEX']))}")

        srate = blocks.get("SRATE") or {}
        if srate:
            args = ",".join(f"{k},{v}" for k, v in srate.items() if v != "")
            if args:
                send(f"{prefix}:SRATE {args}")

        bswv = blocks.get("BSWV") or {}
        if bswv:
            args = ",".join(f"{k},{v}" for k, v in bswv.items() if v != "")
            if args:
                send(f"{prefix}:BSWV {args}")

        if bswv.get("WVTP") == "ARB" and srate.get("MODE") == "TARB":
            # TrueArb quantises its clock against the loaded waveform, so the
            # pre-BSWV write lands a few ppm off. Re-asserting it afterwards is
            # only safe here because SRATE forces WVTP,ARB - which is what this
            # channel is already set to.
            args = ",".join(f"{k},{v}" for k, v in srate.items() if v != "")
            self.write(f"{prefix}:SRATE {args}")

        if "MODE" in blocks:
            mode, params = blocks["MODE"]
            for verb in MODE_VERBS:
                self.write(f"{prefix}:{verb} STATE,OFF")
            if mode != "Off":
                verb = MODE_VERB.get(mode, "MDWV")
                # STATE and the type tag have to be sent separately: combined,
                # the instrument applies the state and drops the type switch.
                # The manual is explicit that STATE must be ON before any other
                # parameter of a burst is set, so this ordering is required
                # rather than merely tidy.
                send(f"{prefix}:{verb} STATE,ON")
                # Which burst parameters are legal depends on the carrier, so
                # the wave type has to be known - from what is being sent where
                # that is part of this apply, and from the generator otherwise.
                carrier = (blocks.get("BSWV") or {}).get("WVTP", "")
                if not carrier and mode == "Burst":
                    try:
                        carrier = str(parse_reply(
                            self.query(f"{prefix}:BSWV?")).get("WVTP", ""))
                    except Exception:
                        carrier = ""
                args = ",".join(f"{k},{v}" for k, v
                                in mode_block(mode, params, carrier) if v != "")
                lead = f"{mode}," if verb == "MDWV" else ""
                if lead or args:
                    send(f"{prefix}:{verb} {lead}{args}")
            else:
                log(f"  {prefix}: modulation, sweep and burst all off")
        return refused

    def upload_arb(self, ch, name, samples, normalize=True):
        """Upload a waveform into user memory and select it on this channel.

        Sent as signed 16-bit little-endian, which is what the 16-bit DAC in the
        4063B expects. Any point count from 2 upwards is fine - odd included,
        which was measured rather than assumed: 3, 7 and 101 points all store
        and read back at exactly their length.
        """
        data = np.asarray(samples, dtype=np.float64).ravel()
        if data.size < 2:
            raise ValueError("need at least 2 samples")
        data = dac_samples(data, normalize=normalize)
        codes = np.round(data * 32767).astype("<i2")

        header = f"C{ch}:WVDT WVNM,{name},WAVEDATA,".encode("ascii")
        # One write_raw so the USBTMC END bit lands after the last data byte; a
        # trailing terminator here would be swallowed as waveform data.
        self.inst.write_raw(header + codes.tobytes())
        self.wait()
        self.write(f"C{ch}:ARWV NAME,{name}")
        return data.size

    def snapshot(self):
        return {
            "captured": datetime.datetime.now().isoformat(timespec="seconds"),
            "instrument": self.idn,
            "address": self.addr,
            "channels": {str(ch): self.read_channel(ch) for ch in CHANNELS},
        }


def describe(snap):
    """Human-readable form of a snapshot, for the .txt written beside it."""
    lines = [
        f"saved              : {snap.get('captured', '')}",
        f"instrument         : {snap.get('instrument', '')}",
        f"visa address       : {snap.get('address', '')}",
        "",
    ]
    for ch in CHANNELS:
        blocks = snap.get("channels", {}).get(str(ch), {})
        out = parse_reply(blocks.get("OUTP", ""))
        lines.append(f"CH{ch} output         : {out.get('STATE', '?')}"
                     f"   load {out.get('LOAD', '?')}"
                     f"   polarity {out.get('PLRT', '?')}")
        for verb in ("BSWV", "ARWV", "SRATE", "MDWV", "SWWV", "BTWV"):
            if blocks.get(verb):
                lines.append(f"CH{ch} {verb:<14}: {blocks[verb]}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        self.awg = Awg()
        self.msgs = queue.Queue()
        self.busy = False
        self.arb_samples = None       # samples loaded from disk, not yet uploaded
        self.arb_table = None         # every column of that file, for the picker
        self.arb_element = None       # the last built/loaded/pasted waveform,
                                      # kept so a train is never built on a train
        self.known_waves = cache_load()   # name -> samples, from earlier sessions
        self.device_waves = []        # what the generator last said it holds
        self.arb_source = ""
        self.read_stamp = ""

        # panel key -> widgets/state, exactly one entry per editable cell
        self.vars = {}                # key -> StringVar shown in the panel
        self.marks = {}               # key -> "edited" marker label
        self.inst_vals = {}           # key -> value the generator last reported
        self.widgets = {}             # key -> the entry/combobox itself
        self.natural = {}             # key -> state to restore when re-enabled
        self.arb_labels = {}          # key -> its caption, for greying out
        self.halos = {}               # key -> the frame behind it, for the tint
        self.halo_off = None          # whatever Tk's default frame colour is
        self.computed = set()         # keys this app worked out and filled in
        self.driver = {}              # linked group -> the cell now driving it
        self.out_state = {ch: None for ch in CHANNELS}

        # The mode row's boxes are shared by every mode, so what each one means
        # is remembered here: without it a switch of mode leaves the old values
        # sitting under the new labels.
        self.mode_shown = {ch: "Off" for ch in CHANNELS}
        self.pwm_bad = {ch: False for ch in CHANNELS}
        self.loading = False          # panel being filled from the generator
        self.quiet = False            # a var write that is not a user edit

        # The sequence lives on the app rather than in its window, so closing
        # the window and opening it again does not throw the sequence away.
        # Held here rather than in the window that edits them, so that the
        # saved config still loads and a save still works whether or not the
        # setups window has ever been opened.
        self.setup_win = None
        self.outdir = tk.StringVar(value=os.path.join(
            os.path.expanduser("~"), "Desktop", "awg_setups"))
        self.prefix = tk.StringVar(value="awg")
        self.confirm_output = tk.BooleanVar(value=True)
        self.save_btn = self.recall_btn = None

        self.seq_win = None
        self.seq_data = [dict(SEQ_DEFAULT)]
        self.seq_vars = []
        self.seq_note = None
        self.seq_canvas = None
        self.seq_rate = tk.StringVar()
        self.seq_unit = tk.StringVar(value="us")
        self.seq_baseline = tk.StringVar(value="0")
        self.seq_coherent = tk.BooleanVar(value=False)
        self.seq_set_clock = tk.BooleanVar(value=True)
        for var in (self.seq_rate, self.seq_unit, self.seq_baseline):
            var.trace_add("write", lambda *_: self._seq_info())

        root.title("BK4063B AWG GUI")
        win_w = min(1330, root.winfo_screenwidth() - 80)
        win_h = min(900, root.winfo_screenheight() - 120)
        root.geometry(f"{win_w}x{win_h}+40+20")

        pad = dict(padx=8, pady=4)

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        # --- connection row
        top = ttk.Frame(left)
        top.pack(fill="x", **pad)
        self.status = ttk.Label(top, text="Not connected", foreground="#a00")
        self.status.pack(side="left")
        ttk.Button(top, text="Connect", command=self.do_connect).pack(side="right")
        ttk.Button(top, text="Load/save setups...",
                   command=self.do_setups).pack(side="right", padx=(0, 6))

        # --- one panel per channel
        for ch in CHANNELS:
            self.build_channel(left, ch, pad)

        # --- read / apply bar
        bar = ttk.Frame(left)
        bar.pack(fill="x", **pad)
        self.read_btn = ttk.Button(bar, text="Read from generator",
                                   command=self.do_read, state="disabled")
        self.read_btn.pack(side="left")
        self.apply_btn = ttk.Button(bar, text="Apply changes",
                                    command=self.do_apply, state="disabled")
        self.apply_btn.pack(side="left", padx=6)
        # Not gated on an instrument being attached: putting the panel back to
        # what was last read is a thing the panel does to itself.
        ttk.Button(bar, text="Discard changes",
                   command=self.do_discard).pack(side="left", padx=(0, 6))
        self.sync = ttk.Label(bar, text="not read yet", foreground="#666")
        self.sync.pack(side="left", padx=6)

        hint = ttk.Frame(left)
        hint.pack(fill="x", padx=8)
        tk.Label(hint, text="   ", bg=COMPUTED_BG, relief="solid",
                 borderwidth=1).pack(side="left")
        ttk.Label(hint, foreground="#666",
                  text=" computed from other settings, not set directly"
                  ).pack(side="left", padx=(4, 0))

        # The waveform tools sit under the preview rather than in the left
        # column: they are what the preview is showing, and the channel panels
        # already fill the height on a laptop screen.
        self.build_preview(right, pad)
        self.build_builder(right, pad)
        self.build_arb(right, pad)
        # Beside the upload that fills it rather than under the channels, which
        # is both where it belongs and where there is room: in the left column
        # it was the section that got squeezed to nothing at the default size.
        self.build_memory(right, pad)

        # --- log
        lf = ttk.LabelFrame(right, text="Log")
        lf.pack(fill="both", expand=True, **pad)
        self.logbox = tk.Text(lf, height=8, wrap="word", font=("Consolas", 9))
        # Wrapped continuations are indented, so a long message reads as one
        # entry rather than as several.
        self.logbox.tag_configure("entry", lmargin2=30)
        self.logbox.pack(fill="both", expand=True, padx=4, pady=4)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.pump)
        self.root.after(300, self.do_connect)
        self.saved_cfg = None
        self.load_config()
        self.refresh_marks()
        self.draw_preview()

    # -- panel construction ------------------------------------------------

    def cell(self, parent, key, choices, row, col, width, on_change=None):
        """One editable panel cell plus its unapplied-edit marker."""
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=col, sticky="w", padx=2, pady=1)
        var = tk.StringVar()
        # A plain tk.Frame behind the widget, showing two pixels all the way
        # round. That is the tint for a computed value: the vista theme ignores
        # a ttk field background outright, and swapping in a tk.Entry for the
        # cells that need one would leave the panel with two kinds of box. The
        # frame is always there, so turning the tint on shifts no layout.
        halo = tk.Frame(holder)
        halo.pack(side="left")
        if self.halo_off is None:
            self.halo_off = halo.cget("bg")
        # None -> plain entry. A list of choices -> fixed dropdown. An empty
        # tuple -> dropdown that is also typeable, for the arb name, whose
        # suggestions are filled in from the generator once it is read.
        if choices is not None:
            w = ttk.Combobox(halo, textvariable=var, values=list(choices),
                             width=max(4, width - 3),
                             state="readonly" if choices else "normal")
        else:
            w = ttk.Entry(halo, textvariable=var, width=width)
        w.pack(padx=2, pady=2)
        mark = ttk.Label(holder, text=" ", width=1, foreground="#c60")
        mark.pack(side="left")

        self.halos[key] = halo
        self.vars[key] = var
        self.marks[key] = mark
        self.widgets[key] = w
        self.inst_vals[key] = ""
        # Remembered so re-enabling a cell restores the state it was built with:
        # a fixed-list combobox must come back readonly, not as free text.
        self.natural[key] = "readonly" if choices else "normal"
        var.trace_add("write", lambda *_: self.on_edit(key, on_change))
        return w

    def enable(self, key, on):
        self.widgets[key].configure(state=self.natural[key] if on else "disabled")

    def build_channel(self, parent, ch, pad):
        f = ttk.LabelFrame(parent, text=f"Channel {ch}")
        f.pack(fill="x", **pad)

        # --- output row: the only controls that switch the output live
        o = ttk.Frame(f)
        o.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(o, text="Output:").pack(side="left")
        lamp = ttk.Label(o, text="?", width=5, foreground="#666")
        lamp.pack(side="left", padx=(4, 8))
        setattr(self, f"lamp{ch}", lamp)
        on_btn = ttk.Button(o, text="ON", width=5,
                            command=lambda c=ch: self.toggle_output(c, True),
                            state="disabled")
        on_btn.pack(side="left")
        off_btn = ttk.Button(o, text="OFF", width=5,
                             command=lambda c=ch: self.toggle_output(c, False),
                             state="disabled")
        off_btn.pack(side="left", padx=4)
        setattr(self, f"on_btn{ch}", on_btn)
        setattr(self, f"off_btn{ch}", off_btn)
        ttk.Label(o, text="Load:").pack(side="left", padx=(12, 0))
        # Typeable, not a fixed list: the generator takes any load from 50
        # ohm up, even though only these two appear on its own screen.
        load = self.cell(self._grid(o), f"C{ch}:OUTP:LOAD", LOADS, 0, 0, 9)
        load.configure(state="normal")
        self.natural[f"C{ch}:OUTP:LOAD"] = "normal"
        ttk.Label(o, text="Polarity:").pack(side="left", padx=(8, 0))
        self.cell(self._grid(o), f"C{ch}:OUTP:PLRT", ("NOR", "INVT"), 0, 0, 9)

        # --- wave type + its parameters
        w = ttk.Frame(f)
        w.pack(fill="x", padx=6, pady=2)
        ttk.Label(w, text="Wave:").grid(row=0, column=0, sticky="e", padx=(0, 4))
        self.cell(w, f"C{ch}:BSWV:WVTP", WAVE_TYPES, 0, 1, 11,
                  on_change=lambda c=ch: self.on_wave_type(c))
        for i, (key, label, _) in enumerate(WAVE_PARAMS):
            row, col = divmod(i, 3)
            row += 1
            lab = ttk.Label(w, text=label + ":")
            lab.grid(row=row, column=col * 2, sticky="e", padx=(0, 4), pady=1)
            setattr(self, f"lab{ch}_{key}", lab)
            self.cell(w, f"C{ch}:BSWV:{key}", None, row, col * 2 + 1, 11)

        # --- arb selection and sample clock, only live for WVTP,ARB
        a = ttk.Frame(f)
        a.pack(fill="x", padx=6, pady=2)
        for col, (text, key, choices, width) in enumerate((
                ("Arb wave:", f"C{ch}:ARWV:NAME", (), 16),
                ("Clock:", f"C{ch}:SRATE:MODE", ("DDS", "TARB"), 8),
                ("Sa/s:", f"C{ch}:SRATE:VALUE", None, 11))):
            label = ttk.Label(a, text=text)
            label.grid(row=0, column=col * 2, sticky="e",
                       padx=((0, 4) if col == 0 else (8, 4)))
            self.arb_labels[key] = label
            # The clock decides whether Sa/s and Interp mean anything, so it
            # re-runs the same greying pass the wave type does.
            widget = self.cell(a, key, choices, 0, col * 2 + 1, width,
                               on_change=(lambda c=ch: self.on_wave_type(c))
                               if key.endswith("SRATE:MODE") else None)
            if key.endswith("ARWV:NAME"):
                setattr(self, f"arbcombo{ch}", widget)

        # --- modulation / sweep / burst
        m = ttk.Frame(f)
        m.pack(fill="x", padx=6, pady=(2, 6))
        ttk.Label(m, text="Mode:").grid(row=0, column=0, sticky="e", padx=(0, 4))
        self.cell(m, f"C{ch}:MODE", MODES, 0, 1, 11,
                  on_change=lambda c=ch: self.on_mode(c))
        for slot in range(MODE_SLOTS):
            row, col = divmod(slot, 3)
            row += 1
            lab = ttk.Label(m, text="")
            lab.grid(row=row, column=col * 2, sticky="e", padx=(0, 4), pady=1)
            setattr(self, f"modlab{ch}_{slot}", lab)
            holder = ttk.Frame(m)
            holder.grid(row=row, column=col * 2 + 1, sticky="w", padx=2, pady=1)
            var = tk.StringVar()
            # A Combobox with state="normal" and no values is an entry box, so
            # one widget covers both the free-number and the fixed-list slots.
            cb = ttk.Combobox(holder, textvariable=var, width=8, state="normal")
            cb.pack(side="left")
            mark = ttk.Label(holder, text=" ", width=1, foreground="#c60")
            mark.pack(side="left")
            key = f"C{ch}:MODE:{slot}"
            self.vars[key] = var
            self.marks[key] = mark
            self.widgets[key] = cb
            self.inst_vals[key] = ""
            self.natural[key] = "normal"
            var.trace_add("write", lambda *_, k=key: self.on_edit(k, None))

    @staticmethod
    def _grid(parent):
        """A one-cell grid container, so `cell` can grid inside a packed row."""
        holder = ttk.Frame(parent)
        holder.pack(side="left")
        return holder

    def build_builder(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Build waveform")
        f.pack(fill="x", **pad)

        r = ttk.Frame(f)
        r.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(r, text="Shape:").pack(side="left")
        self.shape = tk.StringVar(value="Gaussian")
        cb = ttk.Combobox(r, textvariable=self.shape, values=list(BUILD_SHAPES),
                          width=17, state="readonly")
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.on_shape())
        self.build_len_label = ttk.Label(r, text="Points:")
        self.build_len_label.pack(side="left", padx=(10, 2))
        self.build_pts = tk.StringVar(value="10000")
        ttk.Entry(r, textvariable=self.build_pts, width=9).pack(side="left")
        self.build_unit = tk.StringVar(value="us")
        self.build_unit_box = ttk.Combobox(
            r, textvariable=self.build_unit, values=list(SEQ_UNITS), width=4,
            state="disabled")
        self.build_unit_box.pack(side="left", padx=(2, 0))
        self.build_unit_box.bind("<<ComboboxSelected>>",
                                 lambda e: self.on_build_change())
        ttk.Button(r, text="Build", command=self.do_build).pack(side="left", padx=(10, 4))
        ttk.Button(r, text="Type/paste values...",
                   command=self.do_paste).pack(side="left")
        ttk.Button(r, text="Sequence...",
                   command=self.do_sequence).pack(side="left", padx=(4, 0))

        # A carrier in cycles is rate-free and survives being replayed at any
        # clock, which is why the builder works that way. But a carrier is
        # usually known in hertz, and a record has a duration the moment a
        # clock is named, so the panel will do the conversion either way.
        c = ttk.Frame(f)
        c.pack(fill="x", padx=6, pady=(0, 2))
        self.build_real = tk.BooleanVar(value=False)
        ttk.Checkbutton(c, text="Using real units with",
                        variable=self.build_real,
                        command=self.on_build_units).pack(side="left")
        self.build_rate = tk.StringVar(value="1e6")
        ttk.Entry(c, textvariable=self.build_rate, width=10).pack(side="left",
                                                                 padx=(4, 2))
        ttk.Label(c, text="Sa/s").pack(side="left")
        # Wrapped rather than clipped: a warning that runs off the edge of the
        # window is a warning nobody reads. The full text goes to the log on
        # every build too, since this line is replaced by the next one.
        self.build_note = ttk.Label(c, text="", foreground="#666",
                                    justify="left", wraplength=520)
        self.build_note.pack(side="left", padx=(10, 0))
        for var in (self.build_pts, self.build_rate):
            var.trace_add("write", lambda *_: self.on_build_change())

        g = ttk.Frame(f)
        g.pack(fill="x", padx=6, pady=(2, 6))
        self.shape_labels, self.shape_vars, self.shape_boxes = [], [], []
        for slot in range(BUILD_SLOTS):
            row, col = divmod(slot, 3)
            lab = ttk.Label(g, text="")
            lab.grid(row=row, column=col * 2, sticky="e", padx=(0, 4), pady=1)
            var = tk.StringVar()
            box = ttk.Combobox(g, textvariable=var, width=13, state="normal")
            box.grid(row=row, column=col * 2 + 1, sticky="w", padx=2, pady=1)
            var.trace_add("write", lambda *_: self.on_build_change())
            self.shape_labels.append(lab)
            self.shape_vars.append(var)
            self.shape_boxes.append(box)
        self.on_shape()

    def on_shape(self, keep=False):
        """Relabel the parameter slots for the shape now selected.

        `keep` leaves the values alone, for the case where only the carrier's
        units changed and the shape did not.
        """
        spec = BUILD_SHAPES[self.shape.get()][1]
        for slot in range(BUILD_SLOTS):
            lab = self.shape_labels[slot]
            var = self.shape_vars[slot]
            box = self.shape_boxes[slot]
            if slot < len(spec):
                label, key, default, choices = spec[slot]
                if key == "cycles" and self.build_real.get():
                    label = "Carrier (Hz)"
                lab.configure(text=label + ":", foreground="#000")
                box.configure(values=list(choices) if choices else (),
                              state="readonly" if choices else "normal")
                if not keep:
                    var.set(default)
            else:
                lab.configure(text="")
                box.configure(values=(), state="disabled")
                if not keep:
                    var.set("")
        self.on_build_change()

    def build_rate_value(self):
        rate = _as_float(self.build_rate.get(), 0.0)
        if rate <= 0:
            raise ValueError("real units need a positive sample rate")
        return rate

    def build_points(self):
        """How many samples the record will have, however it was asked for.

        In real units the length box holds a time, because that is what setting
        it actually changes: at a fixed clock a longer record is a longer pulse,
        not a finer one. The point count falls out of the clock.
        """
        if not self.build_real.get():
            return int(_as_float(self.build_pts.get(), 0.0))
        span = parse_time(self.build_pts.get(), self.build_unit.get(), 0.0)
        return int(round(span * self.build_rate_value()))

    def build_values(self):
        """The shape's parameters, with a carrier in hertz turned into cycles.

        The library only knows cycles across the record, that being the one
        description of a carrier which needs no clock. Hertz is the useful one
        to type, so the conversion happens here: cycles = frequency x points /
        rate.
        """
        spec = BUILD_SHAPES[self.shape.get()][1]
        values = {spec[i][1]: self.shape_vars[i].get()
                  for i in range(len(spec))}
        carrier = None
        if self.build_real.get() and "cycles" in values:
            freq = _as_float(values["cycles"], 0.0)
            if freq > 0:
                points = self.build_points()
                carrier = freq
                values["cycles"] = f"{freq * points / self.build_rate_value():.10g}"
        return values, carrier

    def on_build_units(self):
        """Switch the length and the carrier between points and real time.

        The record itself does not change: the same 10000 points at 1 MSa/s is
        10 ms either way, and the same 50 cycles across it is 5 kHz. Converting
        rather than clearing means the box that was right a moment ago is still
        right.
        """
        try:
            rate = self.build_rate_value()
        except ValueError as exc:
            self.build_real.set(not self.build_real.get())
            messagebox.showerror("Cannot use real units", str(exc))
            return
        spec = BUILD_SHAPES[self.shape.get()][1]
        cycles_at = next((i for i in range(len(spec))
                          if spec[i][1] == "cycles"), None)
        real = self.build_real.get()

        if real:
            points = int(_as_float(self.build_pts.get(), 0.0))
            unit = _TIME_UNITS[self.build_unit.get()]
            self.build_pts.set(f"{points / rate / unit:.10g}" if points else "")
            if cycles_at is not None and points:
                cycles = _as_float(self.shape_vars[cycles_at].get(), 0.0)
                self.shape_vars[cycles_at].set(f"{cycles * rate / points:.10g}")
        else:
            # Not build_points(): the checkbox has already flipped, so that
            # would read the box as a point count when it is still a time.
            points = int(round(parse_time(self.build_pts.get(),
                                          self.build_unit.get(), 0.0) * rate))
            if cycles_at is not None and points:
                freq = _as_float(self.shape_vars[cycles_at].get(), 0.0)
                self.shape_vars[cycles_at].set(f"{freq * points / rate:.10g}")
            self.build_pts.set(str(points))

        self.build_len_label.configure(text="Length:" if real else "Points:")
        self.build_unit_box.configure(state="readonly" if real else "disabled")
        self.on_shape(keep=True)

    def on_build_change(self):
        """Say what the record will come to, and what it cannot resolve."""
        if not hasattr(self, "build_note"):
            return                     # still building the panel
        try:
            values, carrier = self.build_values()
            points = self.build_points()
        except ValueError as exc:
            self.build_note.configure(text=str(exc), foreground="#c60")
            return
        rate = _as_float(self.build_rate.get(), 0.0)
        parts = []
        if points >= 2 and rate > 0:
            # Played straight through at this clock, a record of n points lasts
            # n/rate and repeats at rate/n. Both are worth knowing before it is
            # built rather than after it is on the generator.
            parts.append(f"{points:,} pts = {_secs(points / rate)}, "
                         f"repeats at {_hz(rate / points)}")
        warnings = build_warnings(self.shape.get(), points, values, rate, carrier)
        if warnings:
            self.build_note.configure(
                text=" | ".join(parts + warnings[:1]), foreground="#c60")
        else:
            self.build_note.configure(text=" | ".join(parts), foreground="#666")

    def do_build(self):
        shape = self.shape.get()
        spec = BUILD_SHAPES[shape][1]
        try:
            values, carrier = self.build_values()
            points = self.build_points()
            data = build_waveform(shape, points, values)
        except Exception as exc:
            self.log(f"Could not build {shape}: {exc}")
            messagebox.showerror("Cannot build waveform", str(exc))
            return
        detail = ", ".join(f"{spec[i][0]}={self.shape_vars[i].get()}"
                           for i in range(len(spec)) if self.shape_vars[i].get())
        self.log(f"Built {shape}: {data.size} pts" + (f" ({detail})" if detail else ""))
        rate = _as_float(self.build_rate.get(), 0.0)
        if carrier:
            self.log(f"  carrier {_hz(carrier)} at {rate:.10g} Sa/s "
                     f"= {values['cycles']} cycles across the record")
        # Logged as well as shown, because the label is one line and goes away
        # the moment the next thing is built.
        for line in build_warnings(shape, points, values, rate, carrier):
            self.log(f"  warning: {line}")
        self.take_table(data.reshape(-1, 1), None, f"built {shape}",
                        shape.split("(")[0].strip().replace(" ", "_").lower())

    def do_paste(self):
        """Type or paste numbers straight in, instead of loading a file."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Type or paste values")
        dlg.transient(self.root)
        dlg.grab_set()
        ttk.Label(dlg, justify="left", text=(
            "One sample per line, a single row of numbers, or columns separated\n"
            "by commas, semicolons or spaces. A non-numeric first line is read\n"
            "as column names. Blank lines and # comments are skipped.")
        ).pack(anchor="w", padx=8, pady=(8, 4))

        box = ttk.Frame(dlg)
        box.pack(fill="both", expand=True, padx=8)
        txt = tk.Text(box, width=58, height=16, wrap="none", font=("Consolas", 9))
        bar = ttk.Scrollbar(box, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=bar.set)
        txt.pack(side="left", fill="both", expand=True)
        bar.pack(side="left", fill="y")

        row = ttk.Frame(dlg)
        row.pack(fill="x", padx=8, pady=8)

        def use():
            try:
                table, names = parse_pasted(txt.get("1.0", "end"))
            except Exception as exc:
                messagebox.showerror("Cannot read values", str(exc), parent=dlg)
                return
            dlg.destroy()
            self.take_table(table, names, "pasted values", "pasted")

        ttk.Button(row, text="Use these values", command=use).pack(side="left")
        ttk.Button(row, text="Cancel", command=dlg.destroy).pack(side="left", padx=6)
        txt.focus_set()

    # -- sequence editor ---------------------------------------------------

    def do_sequence(self):
        """A run of differently-specified segments, laid end to end.

        Its own window rather than another panel row, because a sequence is a
        table and the panel has nowhere to put one. Deliberately not modal: the
        main preview is right behind it, so each Build redraws the picture the
        sequence is being tuned against.
        """
        if self.seq_win is not None and self.seq_win.winfo_exists():
            self.seq_win.lift()
            self.seq_win.focus_force()
            return
        if not self.seq_rate.get().strip():
            # Whatever the target channel is clocked at is the likeliest
            # answer, since that is the rate the record will play at.
            rate = self.vars[f"C{self.arb_ch.get()}:SRATE:VALUE"].get().strip()
            self.seq_rate.set(rate if _as_float(rate, 0.0) > 0 else "1e6")

        win = self.seq_win = tk.Toplevel(self.root)
        win.title("Build a sequence")
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self._seq_close)

        ttk.Label(win, justify="left", foreground="#444", text=(
            "Segments are laid end to end into one record, each with its own "
            "shape, length, amplitude and carrier.\n"
            "Times are in the unit below; a suffix overrides it for one field "
            "(2u, 200m, 1.5s). Carrier is in hertz,\n"
            "which only means anything at the sample rate given. Extra takes "
            "the shape's own parameters as\n"
            "key=value. Picking a shape fills in that shape's own defaults,\n"
            "so the row says what it will accept. name=pending reaches "
            "whatever was last built, loaded or pasted.")
        ).pack(anchor="w", padx=8, pady=(8, 4))

        top = ttk.Frame(win)
        top.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(top, text="Sample rate:").pack(side="left")
        ttk.Entry(top, textvariable=self.seq_rate, width=10).pack(side="left",
                                                                 padx=(3, 1))
        ttk.Label(top, text="Sa/s").pack(side="left", padx=(0, 10))
        ttk.Label(top, text="Times in:").pack(side="left")
        ttk.Combobox(top, textvariable=self.seq_unit, values=list(SEQ_UNITS),
                     width=4, state="readonly").pack(side="left", padx=(3, 10))
        ttk.Label(top, text="Baseline:").pack(side="left")
        ttk.Entry(top, textvariable=self.seq_baseline, width=6).pack(
            side="left", padx=(3, 10))

        opts = ttk.Frame(win)
        opts.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Checkbutton(
            opts, variable=self.seq_coherent,
            text="phase coherent (reference every carrier to the sequence "
                 "start, not to its own segment)").pack(anchor="w")
        ttk.Checkbutton(
            opts, variable=self.seq_set_clock,
            text="set the target channel to TrueArb at this rate when "
                 "building").pack(anchor="w")

        holder = ttk.Frame(win)
        holder.pack(fill="both", expand=True, padx=8, pady=(2, 2))
        canvas = self.seq_canvas = tk.Canvas(holder, highlightthickness=0,
                                             height=200)
        bar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.seq_body = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=self.seq_body, anchor="nw")
        self.seq_body.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        # Bound only while the pointer is over the list, so the wheel still
        # belongs to whatever is under it everywhere else.
        canvas.bind("<Enter>", lambda e: canvas.bind_all(
            "<MouseWheel>",
            lambda ev: canvas.yview_scroll(-1 * (ev.delta // 120), "units")))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        ttk.Label(win, foreground="#666",
                  text="^ v move a segment    D duplicate    X delete").pack(
                      anchor="w", padx=10, pady=(0, 2))

        tools = ttk.Frame(win)
        tools.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Button(tools, text="+ Add segment",
                   command=self._seq_add).pack(side="left")
        ttk.Button(tools, text="Paste spec...",
                   command=self._seq_paste).pack(side="left", padx=(6, 0))
        ttk.Button(tools, text="Copy spec",
                   command=self._seq_copy).pack(side="left", padx=(6, 0))
        self.seq_note = ttk.Label(tools, text="", foreground="#666")
        self.seq_note.pack(side="left", padx=12)

        buttons = ttk.Frame(win)
        buttons.pack(fill="x", padx=8, pady=(2, 8))
        ttk.Button(buttons, text="Build", command=self._seq_build).pack(
            side="left")
        ttk.Button(buttons, text="Close", command=self._seq_close).pack(
            side="left", padx=6)

        self._seq_redraw()
        win.update_idletasks()
        win.minsize(win.winfo_reqwidth(), win.winfo_reqheight())

    def _seq_close(self):
        if self.seq_win is not None:
            self.seq_win.destroy()
        self.seq_win, self.seq_note, self.seq_canvas = None, None, None
        self.seq_vars = []

    def _seq_redraw(self):
        """Rebuild the rows from the data.

        Cheaper to think about than patching widgets in place: reorder, insert
        and delete all become one list operation followed by a redraw, and the
        row numbers cannot drift out of step with the list.
        """
        for child in self.seq_body.winfo_children():
            child.destroy()
        # The variables have to be held here: a StringVar that is garbage
        # collected takes its Tcl variable with it, and the row goes blank.
        self.seq_vars = []

        heads = ["#"] + [label for label, _, _, _ in SEQ_COLUMNS]
        for col, text in enumerate(heads):
            ttk.Label(self.seq_body, text=text, foreground="#444").grid(
                row=0, column=col, sticky="w", padx=2, pady=(0, 2))

        for index, seg in enumerate(self.seq_data):
            ttk.Label(self.seq_body, text=str(index + 1),
                      foreground="#666").grid(row=index + 1, column=0, padx=(2, 4))
            for col, (_, key, _, width) in enumerate(SEQ_COLUMNS, start=1):
                var = tk.StringVar(value=str(seg.get(key, "")))
                var.trace_add("write", lambda *_, i=index, k=key, v=var:
                              self._seq_edit(i, k, v))
                self.seq_vars.append(var)
                if key == "shape":
                    widget = ttk.Combobox(self.seq_body, textvariable=var,
                                          values=seq_shapes(), width=width,
                                          state="readonly")
                else:
                    widget = ttk.Entry(self.seq_body, textvariable=var,
                                       width=width)
                widget.grid(row=index + 1, column=col, sticky="w", padx=2, pady=1)
            row_tools = ttk.Frame(self.seq_body)
            row_tools.grid(row=index + 1, column=len(SEQ_COLUMNS) + 1,
                           padx=(8, 2))
            for text, call in (("^", self._seq_up), ("v", self._seq_down),
                               ("D", self._seq_dup), ("X", self._seq_del)):
                ttk.Button(row_tools, text=text, width=2,
                           command=lambda f=call, i=index: f(i)).pack(side="left")

        # A canvas has no opinion about how wide the frame scrolling inside it
        # is, so without this the row buttons sit past the right edge with no
        # way to reach them - the scrollbar only moves the view up and down.
        if self.seq_canvas is not None:
            self.seq_body.update_idletasks()
            self.seq_canvas.configure(width=self.seq_body.winfo_reqwidth())
        self._seq_info()

    def _seq_edit(self, index, key, var):
        if not 0 <= index < len(self.seq_data):
            return
        self.seq_data[index][key] = var.get()
        if key == "shape":
            # A new shape takes different parameters, so the ones the last
            # shape took are not just stale, they are meaningless. Filling in
            # the new defaults is also the only place the row says what the
            # shape will accept.
            self.seq_data[index]["extra"] = shape_extras(var.get())
            self._seq_redraw()
            return
        self._seq_info()

    def _seq_add(self):
        self.seq_data.append(dict(SEQ_DEFAULT))
        self._seq_redraw()

    def _seq_dup(self, index):
        self.seq_data.insert(index + 1, dict(self.seq_data[index]))
        self._seq_redraw()

    def _seq_del(self, index):
        self.seq_data.pop(index)
        if not self.seq_data:
            self.seq_data.append(dict(SEQ_DEFAULT))
        self._seq_redraw()

    def _seq_up(self, index):
        if index > 0:
            self.seq_data[index - 1:index + 1] = [self.seq_data[index],
                                                  self.seq_data[index - 1]]
            self._seq_redraw()

    def _seq_down(self, index):
        if index < len(self.seq_data) - 1:
            self.seq_data[index:index + 2] = [self.seq_data[index + 1],
                                              self.seq_data[index]]
            self._seq_redraw()

    def _seq_info(self):
        """The running total, recomputed on every keystroke."""
        if self.seq_note is None:
            return
        count, span, points = sequence_extent(self.seq_data,
                                              self.seq_rate.get(),
                                              self.seq_unit.get())
        text = (f"{count} segment{'' if count == 1 else 's'}, "
                f"{_secs(span)}, {points} pts")
        colour = "#666"
        if points > 1000000:
            text += " - long enough to be slow to send and slow to draw"
            colour = "#c60"
        self.seq_note.configure(text=text, foreground=colour)

    def _seq_build(self):
        """Make the record and hand it to the pending waveform."""
        try:
            # `pending` names whatever was last built, loaded or pasted, so a
            # record that has never been uploaded can still be a segment - the
            # case the pulse train used to cover.
            waves = dict(self.known_waves)
            if self.arb_element is not None:
                waves["pending"] = self.arb_element
            out, rows = build_sequence(
                self.seq_data, self.seq_rate.get(), unit=self.seq_unit.get(),
                baseline=_as_float(self.seq_baseline.get(), 0.0),
                coherent=bool(self.seq_coherent.get()), waves=waves)
        except Exception as exc:
            self.log(f"Could not build the sequence: {exc}")
            messagebox.showerror("Cannot build sequence", str(exc),
                                 parent=self.seq_win)
            return

        rate = _as_float(self.seq_rate.get(), 0.0)
        self.log(f"Sequence: {len(rows)} segments, {out.size} pts, "
                 f"{_secs(out.size / rate)} at {rate:.10g} Sa/s"
                 + (" (phase coherent)" if self.seq_coherent.get() else ""))
        for index, shape, n, span, gap in rows:
            self.log(f"  {index}. {shape}, {_secs(span)} ({n} pts)"
                     + (f", then {_secs(gap)} gap" if gap > 0 else ""))
        element = self.arb_element
        self.take_table(out.reshape(-1, 1), None, "sequence", "sequence")
        # A sequence must never become its own source. Left alone, a segment
        # naming `pending` would fold the last result into the next one and the
        # record would double every time Build was pressed.
        self.arb_element = element
        if self.seq_set_clock.get():
            self._seq_set_clock(rate)

    def _seq_set_clock(self, rate):
        """Put the clock the sequence was designed for on the target channel.

        A sequence is specified in seconds, and seconds only exist on this
        generator under TrueArb at a stated rate: under DDS the record is one
        period at whatever frequency the channel happens to hold, and every
        duration in the sequence becomes a fiction. Left as unapplied edits
        rather than sent, so it carries the same asterisks as anything typed by
        hand and Apply is still the thing that changes the instrument.
        """
        ch = int(self.arb_ch.get())
        # Wave type first: the sample-rate cells are greyed out under any other
        # type, and a greyed cell is not collected for Apply.
        cells = ((f"C{ch}:BSWV:WVTP", "ARB"), (f"C{ch}:SRATE:MODE", "TARB"),
                 (f"C{ch}:SRATE:VALUE", f"{rate:.10g}"))
        for key, value in cells:
            self.vars[key].set(value)
        # Marked afterwards, not as each one is written: setting a var runs
        # on_edit, which clears the mark on the assumption a write is somebody
        # typing.
        self.computed.update(key for key, _ in cells)
        self.refresh_marks()
        self.log(f"  CH{ch} panel set to ARB / TrueArb / {rate:.10g} Sa/s "
                 "- not applied yet")

    def _seq_spec(self):
        """The sequence as text: the rows, and the settings that frame them."""
        return format_sequence_spec(
            self.seq_data, self.seq_rate.get().strip(), self.seq_unit.get(),
            baseline=self.seq_baseline.get().strip(),
            coherent=bool(self.seq_coherent.get()),
            clock=bool(self.seq_set_clock.get()))

    def _seq_apply_settings(self, settings):
        """Put a pasted spec's settings into the boxes that hold them.

        Only what the text actually mentioned: a spec written by hand with
        nothing but rows leaves the window as it was, rather than resetting it
        to defaults nobody asked for.
        """
        for name, var in (("rate", self.seq_rate),
                          ("baseline", self.seq_baseline)):
            if settings.get(name):
                var.set(settings[name])
        unit = settings.get("unit", "").strip().lower()
        if unit in SEQ_UNITS:
            self.seq_unit.set(unit)
        for name, var in (("coherent", self.seq_coherent),
                          ("clock", self.seq_set_clock)):
            if name in settings:
                var.set(_as_bool(settings[name], bool(var.get())))

    def _seq_copy(self):
        text = self._seq_spec()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.log(f"Sequence spec copied to the clipboard "
                 f"({len(self.seq_data)} segments)")

    def _seq_paste(self):
        """Type or paste a whole sequence at once.

        Opens holding the sequence that is there now, so the same box is both
        how a sequence gets in and how it gets read out to keep beside the data
        it produced.
        """
        dlg = tk.Toplevel(self.seq_win)
        dlg.title("Sequence spec")
        dlg.transient(self.seq_win)
        dlg.grab_set()
        ttk.Label(dlg, justify="left", text=(
            "One segment per line: " + ", ".join(
                key for _, key, _, _ in SEQ_COLUMNS) + ".\n"
            "Everything after the sixth comma is the extra field, so a value "
            "with commas of its own survives.\n"
            "The `# rate:` lines carry the settings above the list and are "
            "read back with the rows; other\n"
            "# comments and blank lines are skipped. Using this replaces the "
            "whole sequence.")
        ).pack(anchor="w", padx=8, pady=(8, 4))

        box = ttk.Frame(dlg)
        box.pack(fill="both", expand=True, padx=8)
        txt = tk.Text(box, width=76, height=16, wrap="none",
                      font=("Consolas", 9))
        scroll = ttk.Scrollbar(box, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=scroll.set)
        txt.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        txt.insert("1.0", self._seq_spec())

        row = ttk.Frame(dlg)
        row.pack(fill="x", padx=8, pady=8)

        def use():
            try:
                data, settings = parse_sequence_spec(txt.get("1.0", "end"))
            except Exception as exc:
                messagebox.showerror("Cannot read sequence", str(exc),
                                     parent=dlg)
                return
            dlg.destroy()
            self.seq_data = data
            # Settings before the redraw: the running total under the list is
            # computed against the rate and the unit, so landing the rows first
            # would show a length that was never true.
            self._seq_apply_settings(settings)
            self._seq_redraw()
            named = ", ".join(sorted(settings)) if settings else "no settings"
            self.log(f"Sequence spec read: {len(data)} segments ({named})")

        ttk.Button(row, text="Use this sequence", command=use).pack(side="left")
        ttk.Button(row, text="Cancel", command=dlg.destroy).pack(side="left",
                                                                 padx=6)
        txt.focus_set()

    def build_arb(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Upload arbitrary waveform")
        f.pack(fill="x", **pad)
        r1 = ttk.Frame(f)
        r1.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(r1, text="Load file...", command=self.pick_arb).pack(side="left")
        ttk.Label(r1, text="Column:").pack(side="left", padx=(10, 2))
        # Which column holds the samples. A multi-channel scope capture has
        # several plausible answers, so it is shown rather than guessed at
        # silently - picking the wrong one uploads the trigger trace.
        self.arb_col = tk.StringVar()
        self.arb_col_box = ttk.Combobox(r1, textvariable=self.arb_col, width=16,
                                        state="disabled")
        self.arb_col_box.pack(side="left")
        self.arb_col_box.bind("<<ComboboxSelected>>", lambda e: self.pick_column())
        self.arb_info = ttk.Label(r1, text="no file loaded", foreground="#666")
        self.arb_info.pack(side="left", padx=8)

        r2 = ttk.Frame(f)
        r2.pack(fill="x", padx=6, pady=(2, 6))
        ttk.Label(r2, text="Name:").pack(side="left")
        self.arb_name = tk.StringVar(value="wave2")
        ttk.Entry(r2, textvariable=self.arb_name, width=14).pack(side="left", padx=4)
        ttk.Label(r2, text="to CH:").pack(side="left", padx=(8, 0))
        self.arb_ch = tk.StringVar(value="2")
        chbox = ttk.Combobox(r2, textvariable=self.arb_ch,
                             values=[str(c) for c in CHANNELS], width=3,
                             state="readonly")
        chbox.pack(side="left", padx=4)
        # The pending trace takes its colour and its timebase from this channel.
        chbox.bind("<<ComboboxSelected>>", lambda e: self.draw_preview())
        self.norm = tk.BooleanVar(value=True)
        ttk.Checkbutton(r2, text="normalise to full scale", variable=self.norm,
                        command=self.draw_preview).pack(side="left", padx=8)
        self.upload_btn = ttk.Button(r2, text="Upload", command=self.do_upload,
                                     state="disabled")
        self.upload_btn.pack(side="left", padx=4)
        ttk.Button(r2, text="Clear pending",
                   command=self.do_clear_pending).pack(side="left")

    def build_memory(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Waveforms in generator memory")
        f.pack(fill="both", expand=True, **pad)

        # Controls first, from the bottom up: the list is the part that should
        # give up space when the column runs short, not the buttons.
        ttk.Label(f, foreground="#666", justify="left", text=(
            "No remote delete on this firmware - waveforms come off at the front\n"
            "panel (Utility > Store/Recall). Re-uploading a name overwrites it.")
        ).pack(side="bottom", anchor="w", padx=6, pady=(0, 6))

        row = ttk.Frame(f)
        row.pack(side="bottom", fill="x", padx=6, pady=2)
        self.mem_btn = ttk.Button(row, text="Refresh", command=self.do_read,
                                  state="disabled")
        self.mem_btn.pack(side="left")
        ttk.Button(row, text="Forget local copy",
                   command=self.do_forget).pack(side="left", padx=6)
        ttk.Button(row, text="Use on channel",
                   command=self.do_use_wave).pack(side="left")

        box = ttk.Frame(f)
        box.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        self.mem_list = tk.Listbox(box, height=4, font=("Consolas", 9),
                                   exportselection=False)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.mem_list.yview)
        self.mem_list.configure(yscrollcommand=sb.set)
        self.mem_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

    def refresh_memory(self, names):
        """Show what is on the generator and what we hold a local copy of."""
        self.device_waves = list(names or [])
        self.mem_list.delete(0, "end")
        for name in self.device_waves:
            samples = self.known_waves.get(name)
            note = (f"{samples.size:>9,} pts" if samples is not None
                    else "  no local copy")
            self.mem_list.insert("end", f"{name:<18}{note}")
        # A cached waveform the generator no longer lists was deleted at the
        # front panel, or uploaded from another machine. Say so rather than
        # leaving a name that looks live.
        for name in sorted(set(self.known_waves) - set(self.device_waves)):
            self.mem_list.insert("end", f"{name:<18}  local copy only")

    def selected_wave(self):
        picked = self.mem_list.curselection()
        if not picked:
            return None
        return self.mem_list.get(picked[0]).split()[0]

    def do_forget(self):
        """Drop this app's local copy. The generator keeps its own."""
        name = self.selected_wave()
        if not name:
            self.log("Pick a waveform in the list first.")
            return
        if name not in self.known_waves:
            self.log(f"No local copy of '{name}' to forget.")
            return
        if not messagebox.askokcancel(
                "Forget local copy?",
                f"Delete this app's local copy of '{name}'?\n\n"
                "The waveform stays in the generator's memory - this only means "
                "the preview can no longer draw it. There is no way to delete it "
                "from the generator over USB; that is done at the front panel."):
            return
        self.known_waves.pop(name, None)
        cache_forget(name)
        self.log(f"Forgot the local copy of '{name}' (still on the generator)")
        self.refresh_memory(self.device_waves)
        self.draw_preview()

    def do_use_wave(self):
        """Put the selected waveform into the channel chosen in the upload row."""
        name = self.selected_wave()
        if not name:
            self.log("Pick a waveform in the list first.")
            return
        if name not in self.device_waves:
            self.log(f"'{name}' is not in the generator's memory - upload it first.")
            return
        ch = int(self.arb_ch.get())
        self.vars[f"C{ch}:BSWV:WVTP"].set("ARB")
        self.vars[f"C{ch}:ARWV:NAME"].set(name)
        self.show_ch[ch].set(True)
        self.log(f"CH{ch} set to '{name}' - press Apply changes to send it")

    def do_setups(self):
        """Saving and recalling a whole instrument state, in its own window.

        A window rather than a panel row because it is not something touched
        while tuning - it is what happens once at the start and once at the
        end. The row it used to occupy was the height the generator's waveform
        list needed and could not get.

        Not modal: Save and Recall both go off to the instrument on a thread,
        and a grab here would freeze the panel that reports what they did.
        """
        if self.setup_win is not None and self.setup_win.winfo_exists():
            self.setup_win.lift()
            self.setup_win.focus_force()
            return
        win = self.setup_win = tk.Toplevel(self.root)
        win.title("Load / save setups")
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self._setups_close)

        ttk.Label(win, justify="left", foreground="#444", text=(
            "Save setup writes both channels' full state to a timestamped "
            "JSON and a readable .txt beside it.\n"
            "Recall setup applies one back, leaving the output switches alone.")
        ).pack(anchor="w", padx=8, pady=(8, 4))

        of = ttk.Frame(win)
        of.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Label(of, text="Folder:").pack(side="left", padx=(0, 4))
        ttk.Entry(of, textvariable=self.outdir, width=52).pack(
            side="left", fill="x", expand=True)
        ttk.Button(of, text="...", width=3,
                   command=self.pick_dir).pack(side="left", padx=6)

        pf = ttk.Frame(win)
        pf.pack(fill="x", padx=8, pady=2)
        ttk.Label(pf, text="Prefix:").pack(side="left")
        ttk.Entry(pf, textvariable=self.prefix, width=16).pack(side="left", padx=4)
        self.save_btn = ttk.Button(pf, text="Save setup",
                                   command=self.do_save_setup, state="disabled")
        self.save_btn.pack(side="left", padx=(8, 4))
        self.recall_btn = ttk.Button(pf, text="Recall setup...",
                                     command=self.do_recall_setup,
                                     state="disabled")
        self.recall_btn.pack(side="left")

        gf = ttk.Frame(win)
        gf.pack(fill="x", padx=8, pady=(6, 4))
        # Defaults on. Turning an output on is the one thing in this app that
        # can put a voltage into something that was not expecting it.
        ttk.Checkbutton(gf, text="confirm before switching an output on",
                        variable=self.confirm_output).pack(side="left")

        ttk.Button(win, text="Close", command=self._setups_close).pack(
            anchor="w", padx=8, pady=(4, 8))
        # The two buttons are only live with an instrument attached, and this
        # window may well have been opened before one was.
        self.set_busy(self.busy)

    def _setups_close(self):
        if self.setup_win is not None:
            self.setup_win.destroy()
        self.setup_win = None
        self.save_btn = self.recall_btn = None

    def build_preview(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Preview (what Apply would produce)")
        f.pack(fill="x", **pad)
        if Figure is None:
            ttk.Label(f, text="matplotlib not installed - no preview",
                      foreground="#666").pack(padx=8, pady=8)
            self.canvas = None
            return
        self.fig = Figure(figsize=(5.6, 3.0), dpi=100)
        # Built on the first draw and rebuilt only when the layout changes -
        # how many panels there are, and whether one of them is twinned.
        self.axes, self.ax, self.ax2, self.axes_key = [], None, None, None
        self.canvas = FigureCanvasTkAgg(self.fig, master=f)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=4, pady=4)

        r = ttk.Frame(f)
        r.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(r, text="Show:").pack(side="left")
        self.show_ch = {}
        for ch in CHANNELS:
            var = tk.BooleanVar(value=True)
            self.show_ch[ch] = var
            box = tk.Checkbutton(r, text=f"CH{ch}", variable=var,
                                 command=self.draw_preview,
                                 foreground=CH_COLOUR[ch],
                                 activeforeground=CH_COLOUR[ch],
                                 selectcolor="white")
            box.pack(side="left", padx=(4, 0))
        self.show_pending = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="pending", variable=self.show_pending,
                        command=self.draw_preview).pack(side="left", padx=(8, 0))
        # Sits with the trace switches because that is what it is about: a
        # caveat on the picture belongs beside the picture, where it can change
        # on every keystroke without a log filling up behind it.
        self.alias_note = ttk.Label(r, text="", foreground="#c60")
        self.alias_note.pack(side="left", padx=(14, 4))
        self.split_y = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text="separate Y axes", variable=self.split_y,
                        command=self.draw_preview).pack(side="right")
        self.split_t = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text="separate time axes", variable=self.split_t,
                        command=self.draw_preview).pack(side="right", padx=(0, 10))

    # -- helpers -----------------------------------------------------------

    def log(self, text):
        self.msgs.put(text)

    def pump(self):
        while not self.msgs.empty():
            self.logbox.insert("end", self.msgs.get() + "\n", "entry")
            self.logbox.see("end")
        self.root.after(100, self.pump)

    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get() or ".")
        if d:
            self.outdir.set(d)
            self.save_config()

    def on_edit(self, key, on_change):
        if on_change:
            on_change()
        # A rewrite the panel did to itself (moving the mode row's values to
        # where the new mode keeps them) is not an edit, and redrawing halfway
        # through one would show a row that is part old mode and part new.
        if self.quiet:
            return
        # A value the panel worked out stops being one the moment it is typed
        # over, so the tint comes off at the first keystroke in the box.
        self.computed.discard(key)
        if not self.loading:
            # The cell just typed in is the setting, and everything describing
            # the same thing another way becomes the arithmetic. Not while a
            # read is landing: the generator reporting every cell at once is
            # not a choice between them.
            for group in self.groups_for(key):
                self.driver[group] = key
            if key.startswith("C") and ":BSWV:" in key:
                self.recompute(int(key[1]))
        self.refresh_marks()
        self.draw_preview()

    def edited(self, key):
        """True if the panel value differs from what the generator last said."""
        return self.vars[key].get().strip() != self.inst_vals[key]

    def linked_groups(self, ch):
        """Cells on this channel that are one setting in different guises.

        A group is a tuple of sides, and every side says the same thing a
        different way - so typing into one side makes every cell on the others
        arithmetic. The first side leads until told otherwise. Sides rather
        than single cells because the levels take two of each: amplitude and
        offset together are high and low together.

        Computed rather than a constant because which relations are real
        depends on the wave type: a pulse has no sample clock to speak of, an
        arb has no duty cycle, and DC has no frequency to have a period.
        """
        wvtp = self.vars[f"C{ch}:BSWV:WVTP"].get().strip().upper()
        groups = []
        # TrueArb clocks the record out point by point, so freq = rate /
        # points, and the point count belongs to the waveform rather than to
        # the panel. Sa/s leads: it is what the record is actually clocked by,
        # and what the sequence builder fills in.
        if wvtp == "ARB" and \
                self.vars[f"C{ch}:SRATE:MODE"].get().strip() == "TARB":
            groups.append(((f"C{ch}:SRATE:VALUE",), (f"C{ch}:BSWV:FRQ",)))
        if wvtp in OSCILLATING:
            # Reciprocals. Frequency leads because that is what the rest of the
            # panel is written in - the modulation row, the sweep, the preview.
            groups.append(((f"C{ch}:BSWV:FRQ",), (f"C{ch}:BSWV:PERI",)))
            # The same span of volts from the middle or from the ends:
            # high = offset + ampl/2, low = offset - ampl/2. Amplitude and
            # offset lead, being what the output is specified in.
            groups.append(((f"C{ch}:BSWV:AMP", f"C{ch}:BSWV:OFST"),
                           (f"C{ch}:BSWV:HLEV", f"C{ch}:BSWV:LLEV")))
        # A pulse width is its duty against the period: width = duty / 100 /
        # freq. Width leads, being the absolute one - and the one the preview
        # already prefers when both are set.
        if wvtp == "PULSE":
            groups.append(((f"C{ch}:BSWV:WIDTH",), (f"C{ch}:BSWV:DUTY",)))
        return groups

    def groups_for(self, key):
        """Every linked group this cell belongs to as things stand.

        More than one is normal: under TrueArb the frequency is both the far
        side of the sample clock and the near side of the period.
        """
        for ch in CHANNELS:
            if key.startswith(f"C{ch}:"):
                return [group for group in self.linked_groups(ch)
                        if any(key in side for side in group)]
        return []

    def driving_side(self, group):
        """The side of a group that is currently the setting."""
        driver = self.driver.get(group)
        for side in group:
            if driver in side:
                return side
        return group[0]

    def derived(self, key):
        """True when some other cell is the setting and this one is its result.

        Never sent. The generator works it out, and putting both descriptions
        of one setting into a single command is two instructions with the last
        one winning.
        """
        return any(key not in self.driving_side(group)
                   for group in self.groups_for(key))

    def is_computed(self, key):
        """True when this cell shows arithmetic rather than a setting.

        Two ways a number lands in a box without being typed there. It is the
        far side of a pair the generator keeps in step - see `linked_groups`.
        Or this app worked it out, which is what the sequence builder does with
        the clock a record specified in seconds needs to be played at; that one
        is sent, so it is not `derived`.
        """
        return key in self.computed or self.derived(key)

    def recompute(self, ch):
        """Bring the derived side of every pair on this channel into step.

        Showing a period beside a frequency only earns its place if setting
        either fills in the other; two boxes left to disagree would be worse
        than one box. One pass over the channel rather than a reaction to the
        keystroke, so that a chain lands at once - a new frequency moves the
        period, and moves the duty that an unchanged width now implies.

        The frequency/sample-rate pair is left out on purpose. Working it out
        needs the record's point count, which lives on the generator rather
        than in the panel, so the panel can say that cell is arithmetic without
        being able to do the arithmetic itself.
        """
        def num(key):
            try:
                return float(self.vars[f"C{ch}:BSWV:{key}"].get().strip())
            except ValueError:
                return None

        drives = {key.rsplit(":", 1)[1] for group in self.linked_groups(ch)
                  for key in self.driving_side(group)}
        new = {}

        if "FRQ" in drives:
            freq = num("FRQ")
            new["PERI"] = 1.0 / freq if freq else None
        elif "PERI" in drives:
            peri = num("PERI")
            new["FRQ"] = 1.0 / peri if peri else None

        if "AMP" in drives:
            amp, ofst = num("AMP"), num("OFST")
            pair = (ofst + amp / 2, ofst - amp / 2) \
                if amp is not None and ofst is not None else (None, None)
            new["HLEV"], new["LLEV"] = pair
        elif "HLEV" in drives:
            high, low = num("HLEV"), num("LLEV")
            pair = (high - low, (high + low) / 2) \
                if high is not None and low is not None else (None, None)
            new["AMP"], new["OFST"] = pair

        # Whichever way the first pair went, the width and duty hang off the
        # frequency that came out of it.
        freq = new.get("FRQ") if isinstance(new.get("FRQ"), float) else num("FRQ")
        if "WIDTH" in drives:
            width = num("WIDTH")
            new["DUTY"] = width * freq * 100.0 \
                if freq and width is not None else None
        elif "DUTY" in drives:
            duty = num("DUTY")
            new["WIDTH"] = duty / 100.0 / freq \
                if freq and duty is not None else None

        self.quiet = True
        try:
            for key, value in new.items():
                var = self.vars[f"C{ch}:BSWV:{key}"]
                text = "" if value is None else fmt_value(value)
                if var.get().strip() != text:
                    var.set(text)
        finally:
            self.quiet = False

    def unapplied(self):
        """The cells holding an edit that has not been sent.

        The same rule the asterisks use: a greyed cell is not part of the
        current settings and a derived one moved because something else was
        typed, so neither is an edit waiting to go anywhere.
        """
        return [key for key in self.marks
                if str(self.widgets[key].cget("state")) != "disabled"
                and not self.derived(key) and self.edited(key)]

    def refresh_marks(self):
        if not hasattr(self, "sync"):
            return          # still building the panel
        pending = 0
        for key, mark in self.marks.items():
            live = str(self.widgets[key].cget("state")) != "disabled"
            halo = self.halos.get(key)
            if halo is not None:
                # A greyed cell is not part of the current settings, so it is
                # neither an edit nor a computed value - just what was left
                # behind by the previous wave type.
                halo.configure(bg=COMPUTED_BG if live and self.is_computed(key)
                               else self.halo_off)
            # A disabled cell is not part of the current wave type, so whatever
            # is left in it is stale rather than an edit waiting to be applied;
            # a derived one moved because something else was typed, and is not
            # going to be sent either.
            if not live or self.derived(key):
                mark.configure(text=" ")
                continue
            if self.edited(key):
                pending += 1
                mark.configure(text="*")
            else:
                mark.configure(text=" ")
        if not self.read_stamp:
            self.sync.configure(text="not read yet", foreground="#666")
        elif pending:
            self.sync.configure(
                text=f"{pending} edit(s) not applied - press Apply changes",
                foreground="#c60")
        else:
            self.sync.configure(text=f"in sync ({self.read_stamp})",
                                foreground="#060")

    def on_wave_type(self, ch):
        """Grey out every cell the current settings give no meaning to.

        Two levels: the arb cells only apply to WVTP,ARB, and within those the
        sample rate and interpolation only apply to the TrueArb clock - under
        DDS the generator derives the timing from the frequency instead, and
        leaving an empty Sa/s box editable invites filling in a number that
        goes nowhere.
        """
        wvtp = self.vars[f"C{ch}:BSWV:WVTP"].get().strip().upper()
        for key, _, applies in WAVE_PARAMS:
            on = wvtp in applies
            self.enable(f"C{ch}:BSWV:{key}", on)
            getattr(self, f"lab{ch}_{key}").configure(
                foreground="#000" if on else "#aaa")

        self.check_pwm(ch)
        if not self.loading:
            # A different wave type is a different set of pairs - a pulse gains
            # a duty, DC loses its period - so the derived cells are worked out
            # again rather than left holding the last type's answers.
            self.recompute(ch)

        arb = wvtp == "ARB"
        tarb = arb and self.vars[f"C{ch}:SRATE:MODE"].get().strip() == "TARB"
        for key, on in ((f"C{ch}:ARWV:NAME", arb), (f"C{ch}:SRATE:MODE", arb),
                        (f"C{ch}:SRATE:VALUE", tarb)):
            self.enable(key, on)
            label = self.arb_labels.get(key)
            if label is not None:
                label.configure(foreground="#000" if on else "#aaa")

    def on_mode(self, ch):
        """Relabel the mode parameter slots for the mode now selected, and move
        their values to wherever the new mode keeps them."""
        mode = self.vars[f"C{ch}:MODE"].get().strip() or "Off"
        spec = MODE_PARAMS.get(mode, [])
        # Not while the panel is being filled from the generator: there the
        # fresh values arrive straight after and carrying the old ones over
        # would fight with them.
        if not self.loading and self.mode_shown.get(ch) != mode:
            self.remap_mode_slots(ch, self.mode_shown.get(ch), mode)
        self.mode_shown[ch] = mode
        for slot in range(MODE_SLOTS):
            lab = getattr(self, f"modlab{ch}_{slot}")
            w = self.widgets[f"C{ch}:MODE:{slot}"]
            if slot < len(spec):
                label, _, choices = spec[slot]
                lab.configure(text=label + ":", foreground="#000")
                w.configure(values=list(choices) if choices else (),
                            state="readonly" if choices else "normal")
            else:
                lab.configure(text="")
                w.configure(values=(), state="disabled")
        self.check_pwm(ch)

    def remap_mode_slots(self, ch, old, new):
        """Carry the mode row across a change of mode, matched on meaning.

        The row is one set of boxes reused by every mode, and the modes do not
        agree on the order: ASK is (key freq, source) while FSK is (key freq,
        hop freq, source). Kept by position, a source of INT lands under the
        Hop freq label and reads as a hop frequency somebody chose. So a value
        survives only when the new mode has the same SCPI parameter, and every
        other box starts empty rather than holding a number typed for
        something else.
        """
        carried = {}
        for slot, (_, key, _) in enumerate(MODE_PARAMS.get(old or "Off", [])):
            value = self.vars[f"C{ch}:MODE:{slot}"].get().strip()
            if value:
                carried[key] = value
        spec = MODE_PARAMS.get(new, [])
        self.quiet = True
        try:
            for slot in range(MODE_SLOTS):
                key = spec[slot][1] if slot < len(spec) else None
                self.vars[f"C{ch}:MODE:{slot}"].set(
                    carried.get(key, "") if key else "")
        finally:
            self.quiet = False

    def pwm_ok(self, ch):
        """PWM is the one mode the carrier constrains: there is nothing for it
        to widen unless the wave type is PULSE."""
        return (self.vars[f"C{ch}:MODE"].get().strip() != "PWM"
                or self.vars[f"C{ch}:BSWV:WVTP"].get().strip().upper()
                == PWM_CARRIER)

    def check_pwm(self, ch):
        """Say so, once, each time the panel newly asks for PWM on a carrier
        that cannot carry it.

        Called from both sides of the pair, since either the mode or the wave
        type can be the half that just changed, and only on the way into the
        bad state - otherwise every later keystroke on the channel would raise
        the same box again.
        """
        bad = not self.pwm_ok(ch)
        if bad and not self.pwm_bad[ch] and not self.loading:
            wvtp = self.vars[f"C{ch}:BSWV:WVTP"].get().strip() or "(none)"
            messagebox.showerror("PWM carrier",
                                 "The carrier of PWM can only be pulse")
            self.log(f"CH{ch}: PWM needs a {PWM_CARRIER} carrier, "
                     f"and the wave type is {wvtp}.")
        self.pwm_bad[ch] = bad

    def mode_key(self, ch, slot):
        """SCPI key the given mode slot currently stands for, or None."""
        mode = self.vars[f"C{ch}:MODE"].get().strip() or "Off"
        spec = MODE_PARAMS.get(mode, [])
        return spec[slot][1] if slot < len(spec) else None

    def set_busy(self, busy):
        self.busy = busy
        live = bool(self.awg.inst) and not busy
        state = "normal" if live else "disabled"
        buttons = [self.read_btn, self.apply_btn, self.mem_btn,
                   self.save_btn, self.recall_btn]
        for btn in buttons:
            # Save and Recall live in a window that is usually not open.
            if btn is not None and btn.winfo_exists():
                btn.configure(state=state)
        for ch in CHANNELS:
            getattr(self, f"on_btn{ch}").configure(state=state)
            getattr(self, f"off_btn{ch}").configure(state=state)
        self.upload_btn.configure(
            state="normal" if live and self.arb_samples is not None else "disabled")

    def show_lamp(self, ch, state):
        self.out_state[ch] = state
        lamp = getattr(self, f"lamp{ch}")
        if state == "ON":
            lamp.configure(text="ON", foreground="#a00")
        elif state == "OFF":
            lamp.configure(text="OFF", foreground="#060")
        else:
            lamp.configure(text="?", foreground="#666")

    # -- panel <-> instrument ---------------------------------------------

    def flatten(self, ch, blocks):
        """One channel's raw replies -> the flat {panel key: value} the panel uses.

        Keys the current reply does not mention (DUTY on a sine, say) come back
        empty rather than missing, so a cell that does not apply reads blank
        instead of holding a stale number from a previous wave type.
        """
        out = {}
        outp = parse_reply(blocks.get("OUTP", ""))
        out[f"C{ch}:OUTP:LOAD"] = fmt_value(outp.get("LOAD", ""))
        out[f"C{ch}:OUTP:PLRT"] = str(outp.get("PLRT", "") or "")

        bswv = parse_reply(blocks.get("BSWV", ""))
        out[f"C{ch}:BSWV:WVTP"] = str(bswv.get("WVTP", "") or "")
        for key, _, _ in WAVE_PARAMS:
            raw = bswv.get(key, "")
            out[f"C{ch}:BSWV:{key}"] = fmt_value(raw) if raw != "" else ""

        arwv = parse_reply(blocks.get("ARWV", ""))
        name = str(arwv.get("NAME", "") or "")
        # The generator appends .bin on readback but will not take it back.
        out[f"C{ch}:ARWV:NAME"] = name[:-4] if name.endswith(".bin") else name

        srate = parse_reply(blocks.get("SRATE", ""))
        out[f"C{ch}:SRATE:MODE"] = str(srate.get("MODE", "") or "")
        val = srate.get("VALUE", "")
        out[f"C{ch}:SRATE:VALUE"] = fmt_value(val) if val != "" else ""

        # Whichever of the three mode blocks is on decides what the row shows.
        mode, params = "Off", {}
        for verb in MODE_VERBS:
            parsed = parse_reply(blocks.get(verb, ""))
            if parsed.get("STATE") != "ON":
                continue
            if verb == "MDWV":
                mode = str(parsed.get("TYPE", "AM"))
            else:
                mode = "Sweep" if verb == "SWWV" else "Burst"
            params = parsed
            break
        out[f"C{ch}:MODE"] = mode
        for slot in range(MODE_SLOTS):
            spec = MODE_PARAMS.get(mode, [])
            if slot < len(spec):
                raw = params.get(spec[slot][1], "")
                out[f"C{ch}:MODE:{slot}"] = fmt_value(raw) if raw != "" else ""
            else:
                out[f"C{ch}:MODE:{slot}"] = ""
        return out

    def show_channel(self, ch, blocks, overwrite=False):
        """Main thread only. Puts instrument values in the panel, keeping any
        edit not applied yet - unless overwrite is set, which is the case after
        an Apply, when the generator is the authority on what took effect."""
        values = self.flatten(ch, blocks)
        # What the generator reports is not an edit, so neither the mode row's
        # carry-over nor the PWM complaint should fire while it lands.
        kept = 0
        self.loading = True
        try:
            # The mode row is relabelled by the mode itself, so that has to be
            # set before its slots or the slots land under the wrong labels.
            for key in (f"C{ch}:BSWV:WVTP", f"C{ch}:MODE"):
                if overwrite or not self.edited(key):
                    self.vars[key].set(values[key])
                self.inst_vals[key] = values[key]
            self.on_wave_type(ch)
            self.on_mode(ch)

            done = (f"C{ch}:BSWV:WVTP", f"C{ch}:MODE")
            for key, value in values.items():
                # Exact keys, not a suffix test: "C1:SRATE:MODE" also ends in
                # ":MODE", and matching it here left the Clock cell permanently
                # blank because it was skipped by the loop that fills the panel.
                if key in done:
                    continue
                was_edited = self.edited(key)
                self.inst_vals[key] = value
                if overwrite or not was_edited:
                    self.vars[key].set(value)
                elif self.vars[key].get().strip() != value:
                    kept += 1
        finally:
            self.loading = False
        # Whatever the generator turned out to be set to is the new baseline for
        # the complaint, so a panel read back in a bad state says so once.
        self.pwm_bad[ch] = not self.pwm_ok(ch)

        self.show_lamp(ch, parse_reply(blocks.get("OUTP", "")).get("STATE"))
        if kept:
            self.log(f"  (CH{ch}: kept {kept} unapplied edit(s))")

    def collect(self, ch):
        """Everything edited on this channel, as {verb: {key: value}}.

        Only edited cells are sent. That keeps an Apply from rewriting settings
        nobody touched, which matters on a channel that is currently driving
        something.
        """
        blocks = {}

        def add(verb, key, panel_key):
            if self.edited(panel_key) and \
                    str(self.widgets[panel_key].cget("state")) != "disabled" \
                    and not self.derived(panel_key):
                blocks.setdefault(verb, {})[key] = self.vars[panel_key].get().strip()

        add("OUTP", "LOAD", f"C{ch}:OUTP:LOAD")
        add("OUTP", "PLRT", f"C{ch}:OUTP:PLRT")
        for key, _, _ in WAVE_PARAMS:
            add("BSWV", key, f"C{ch}:BSWV:{key}")
        add("ARWV", "NAME", f"C{ch}:ARWV:NAME")
        for key in ("MODE", "VALUE"):
            add("SRATE", key, f"C{ch}:SRATE:{key}")

        # The wave type always rides along with any BSWV write: sending
        # parameters without it can land them on the previous type.
        wvtp = self.vars[f"C{ch}:BSWV:WVTP"].get().strip().upper()
        if "BSWV" in blocks or self.edited(f"C{ch}:BSWV:WVTP"):
            blocks.setdefault("BSWV", {})
            blocks["BSWV"] = {"WVTP": wvtp, **blocks["BSWV"]}

        mode = self.vars[f"C{ch}:MODE"].get().strip() or "Off"
        slots = [f"C{ch}:MODE:{s}" for s in range(MODE_SLOTS)]
        if self.edited(f"C{ch}:MODE") or any(
                self.edited(k) and str(self.widgets[k].cget("state")) != "disabled"
                for k in slots):
            params = {}
            for slot in range(MODE_SLOTS):
                key = self.mode_key(ch, slot)
                value = self.vars[f"C{ch}:MODE:{slot}"].get().strip()
                if key and value != "":
                    params[key] = value
            blocks["MODE"] = (mode, params)
        return blocks

    # -- actions -----------------------------------------------------------

    def do_connect(self):
        def work():
            try:
                idn = self.awg.connect()
                self.root.after(0, lambda: self.status.configure(
                    text=idn[:70], foreground="#060"))
                self.log(f"Connected: {idn}")
                self.log(f"Address:   {self.awg.addr}")
                blocks = {ch: self.awg.read_channel(ch) for ch in CHANNELS}
                names = self.awg.user_waveforms()
                self.root.after(0, lambda: self.after_read(blocks, names))
            except Exception as exc:
                self.root.after(0, lambda: self.status.configure(
                    text="Not connected", foreground="#a00"))
                self.log(f"ERROR: {exc}")
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    def after_read(self, blocks, names=None, overwrite=False):
        """Main thread. Land a fresh read into the panel."""
        for ch, b in blocks.items():
            self.show_channel(ch, b, overwrite=overwrite)
        if names is not None:
            for ch in CHANNELS:
                getattr(self, f"arbcombo{ch}").configure(values=names)
            self.log(f"User waveforms: {', '.join(names) if names else '(none)'}")
            self.refresh_memory(names)
        self.read_stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.set_busy(False)
        self.refresh_marks()
        self.draw_preview()

    def do_discard(self):
        """Put every cell back to what the generator last reported.

        The counterpart to Apply, and the thing that was missing: an edit could
        only ever be sent or quietly carried, never taken back.
        """
        pending = self.unapplied()
        if not pending:
            self.log("Nothing to discard - the panel matches the generator.")
            return
        note = ("" if self.read_stamp else
                "\n\nNothing has been read from the generator yet, so this "
                "will leave the panel empty.")
        if not messagebox.askokcancel(
                "Discard changes",
                f"Put {len(pending)} unapplied change(s) back to what the "
                f"generator last reported?{note}"):
            return
        # Loading, because none of this is the user typing: no mode row
        # carry-over, no PWM complaint, no driver moving to whatever was
        # written last.
        self.loading = True
        try:
            for key, var in self.vars.items():
                value = self.inst_vals.get(key, "")
                if var.get().strip() != value:
                    var.set(value)
            self.computed.clear()
            for ch in CHANNELS:
                self.on_wave_type(ch)
                self.on_mode(ch)
        finally:
            self.loading = False
        self.refresh_marks()
        self.draw_preview()
        self.log(f"Discarded {len(pending)} unapplied change(s).")

    def do_read(self):
        if self.busy or not self.awg.inst:
            return
        pending = self.unapplied()
        # A read used to keep unapplied edits, which left the panel showing a
        # mixture of what the generator holds and what it does not, with only
        # the asterisks to tell them apart. Overwriting is the honest thing;
        # asking first is what makes it safe.
        if pending and not messagebox.askyesno(
                "Unapplied changes",
                f"{len(pending)} change(s) have not been applied.\n\n"
                "Reading will overwrite them with what the generator actually "
                "holds.\n\nRead anyway?"):
            self.log(f"Read cancelled - {len(pending)} unapplied change(s) kept.")
            return
        self.set_busy(True)

        def work():
            try:
                blocks = {ch: self.awg.read_channel(ch) for ch in CHANNELS}
                names = self.awg.user_waveforms()
                self.log("Read back from generator."
                         + (f" {len(pending)} unapplied change(s) overwritten."
                            if pending else ""))
                self.root.after(0, lambda: self.after_read(blocks, names,
                                                           overwrite=True))
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    def do_apply(self):
        if self.busy or not self.awg.inst:
            return
        plan = {ch: self.collect(ch) for ch in CHANNELS}
        plan = {ch: b for ch, b in plan.items() if b}
        if not plan:
            self.log("No changes to apply.")
            return
        # The generator takes PWM on a sine without complaint and then does
        # something else with it, so the refusal has to happen here. Only the
        # channels actually sending a mode block are in the way.
        bad = [ch for ch, blocks in plan.items()
               if "MODE" in blocks and not self.pwm_ok(ch)]
        if bad:
            messagebox.showerror("PWM carrier",
                                 "The carrier of PWM can only be pulse")
            self.log("Apply cancelled: "
                     + " and ".join(f"CH{c}" for c in bad)
                     + " asks for PWM on a carrier that is not "
                     + f"{PWM_CARRIER}.")
            return
        # Changing the waveform under a channel that is already driving
        # something is a real change to the experiment, not just to the panel.
        live = [ch for ch in plan if self.out_state.get(ch) == "ON"]
        if live and self.confirm_output.get():
            chans = " and ".join(f"CH{c}" for c in live)
            if not messagebox.askokcancel(
                    "Apply to a live output?",
                    f"{chans} {'is' if len(live) == 1 else 'are'} currently ON.\n\n"
                    "Applying will change what is being output straight away.\n"
                    "Continue?"):
                self.log("Apply cancelled.")
                return
        self.set_busy(True)

        def work():
            try:
                refused = []
                for ch, blocks in plan.items():
                    self.log(f"Applying to CH{ch}:")
                    refused += self.awg.apply_channel(ch, blocks, log=self.log)
                    if blocks.get("ARWV"):
                        self.report_arb_timing(ch)
                if refused:
                    # Worth a box rather than a log line: a refused command is
                    # exactly the case where the panel and the generator are
                    # about to disagree, and the read-back below is what makes
                    # the edit look like it was silently thrown away.
                    self.root.after(0, lambda r=list(refused): messagebox.showwarning(
                        "The generator refused a command",
                        "These went out and came back rejected:\n\n"
                        + "\n".join(r)
                        + "\n\nThe panel is about to show what the generator "
                          "actually holds, so anything in them is lost. The "
                          "log says what it objected to."))
                fresh = {ch: self.awg.read_channel(ch) for ch in CHANNELS}
                names = self.awg.user_waveforms()
                self.root.after(0, lambda: self.after_read(fresh, names,
                                                           overwrite=True))
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    def report_arb_timing(self, ch):
        """Say out loud what selecting an arb did to the frequency.

        In TrueArb the frequency is not an independent setting: the points come
        out at the sample clock, so freq = rate / points. Load a waveform of a
        different length and the frequency moves on its own, which looks like
        the generator changing a setting behind your back unless you know it is
        arithmetic. Instrument thread only.
        """
        try:
            wave = parse_reply(self.awg.query(f"C{ch}:BSWV?"))
            srate = parse_reply(self.awg.query(f"C{ch}:SRATE?"))
            freq = wave.get("FRQ")
            if srate.get("MODE") == "TARB":
                rate = srate.get("VALUE")
                if isinstance(rate, float) and isinstance(freq, float) and freq:
                    self.log(f"  CH{ch} TrueArb: {rate:,.0f} Sa/s over "
                             f"{round(rate / freq):,} points -> {freq:g} Hz")
            elif isinstance(freq, float):
                self.log(f"  CH{ch} DDS: whole record plays once per period, "
                         f"{freq:g} Hz")
        except Exception:
            pass                      # a log line is never worth failing over

    def toggle_output(self, ch, on):
        if self.busy or not self.awg.inst:
            return
        if on and self.confirm_output.get():
            wave = self.vars[f"C{ch}:BSWV:WVTP"].get().strip() or "?"
            amp = self.vars[f"C{ch}:BSWV:AMP"].get().strip() or "?"
            freq = self.vars[f"C{ch}:BSWV:FRQ"].get().strip() or "?"
            load = self.vars[f"C{ch}:OUTP:LOAD"].get().strip() or "?"
            if not messagebox.askokcancel(
                    f"Switch CH{ch} output ON?",
                    f"CH{ch} will start driving whatever is connected:\n\n"
                    f"    {wave}   {freq} Hz   {amp} Vpp\n"
                    f"    into {load} ohm\n\n"
                    "This is the generator's own last-read setting, not any "
                    "unapplied edit in the panel."):
                return
        self.set_busy(True)

        def work():
            try:
                self.awg.set_output(ch, on)
                blocks = {ch: self.awg.read_channel(ch)}
                self.log(f"CH{ch} output {'ON' if on else 'OFF'}")
                self.root.after(0, lambda: self.after_read(blocks))
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    # -- arbitrary waveform upload ----------------------------------------

    def pick_arb(self):
        path = filedialog.askopenfilename(
            title="Waveform samples",
            filetypes=[("Sample data", "*.csv *.txt *.dat *.npy"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            table, names = read_table(path)
            if table.shape[0] < 2:
                raise ValueError(f"only {table.shape[0]} sample(s) in the file")
        except Exception as exc:
            self.log(f"Could not read {os.path.basename(path)}: {exc}")
            messagebox.showerror("Cannot read file", str(exc))
            return

        self.log(f"Loaded {path}")
        self.take_table(table, names, path,
                        os.path.splitext(os.path.basename(path))[0])

    def take_table(self, table, names, source, suggested):
        """Adopt a set of samples, wherever they came from - a file, a built
        shape or pasted text. All three land here so they share the column
        picker, the preview and the upload button."""
        self.arb_table = np.asarray(table, dtype=np.float64)
        self.arb_source = source
        ncols = self.arb_table.shape[1]
        labels = [f"{i + 1}: {names[i]}" if names and i < len(names)
                  else f"column {i + 1}" for i in range(ncols)]
        self.arb_col_box.configure(values=labels,
                                   state="readonly" if ncols > 1 else "disabled")
        self.arb_col.set(labels[default_column(names, ncols)])
        self.arb_name.set(safe_name(suggested)[:16] or "wave2")
        if ncols > 1:
            self.log(f"  {self.arb_table.shape[0]} rows x {ncols} columns"
                     + (f": {', '.join(names)}" if names else ""))
        if self.canvas is not None:
            self.show_pending.set(True)        # show what is about to go up
        self.pick_column()

    def pick_column(self):
        """Take the selected column of the loaded file as the samples."""
        if self.arb_table is None:
            return
        try:
            index = self.arb_col_box.cget("values").index(self.arb_col.get())
        except ValueError:
            index = 0
        data = np.asarray(self.arb_table[:, index], dtype=np.float64).ravel()
        self.arb_samples = data
        self.arb_element = data

        note = f"{os.path.basename(self.arb_source)} - {data.size} pts"
        if self.arb_table.shape[1] > 1:
            note += f", col {index + 1}"
        self.arb_info.configure(text=note, foreground="#000")
        self.log(f"  using column {index + 1}: {data.size} pts, "
                 f"{data.min():g} to {data.max():g}")
        self.set_busy(self.busy)
        self.draw_preview()

    def do_clear_pending(self):
        """Drop the waveform waiting to be uploaded.

        Nothing on the generator is touched - this is the panel forgetting, not
        the instrument. Worth having because the pending record is otherwise
        only replaced, never let go: build something by mistake and it stays on
        the preview, and one wrong press of Upload puts it on a channel.
        """
        if self.arb_samples is None and self.arb_table is None:
            return
        was = self.arb_source or "pending waveform"
        self.arb_samples = self.arb_table = self.arb_element = None
        self.arb_source = ""
        self.arb_col_box.configure(values=(), state="disabled")
        self.arb_col.set("")
        self.arb_info.configure(text="no file loaded", foreground="#666")
        self.log(f"Pending waveform cleared ({was}). Nothing was sent.")
        self.set_busy(self.busy)
        self.draw_preview()

    def do_upload(self):
        if self.busy or not self.awg.inst or self.arb_samples is None:
            return
        ch = int(self.arb_ch.get())
        name = safe_name(self.arb_name.get())
        if self.out_state.get(ch) == "ON" and self.confirm_output.get():
            if not messagebox.askokcancel(
                    "Upload to a live output?",
                    f"CH{ch} is currently ON. Uploading selects the new "
                    "waveform immediately, changing what is being output.\n\n"
                    "Continue?"):
                return
        self.set_busy(True)
        samples, norm = self.arb_samples, self.norm.get()

        def work():
            try:
                n = self.awg.upload_arb(ch, name, samples, normalize=norm)
                self.log(f"Uploaded '{name}' ({n} pts) and selected it on CH{ch}")
                # Keep the samples so the channel preview can draw this
                # waveform whenever it is the one selected. The generator
                # cannot read a stored waveform back out, so if we do not
                # remember it here, nothing can ever show it.
                self.known_waves[name] = dac_samples(samples, normalize=norm)
                cache_save(name, self.known_waves[name])
                blocks = {c: self.awg.read_channel(c) for c in CHANNELS}
                names = self.awg.user_waveforms()
                self.root.after(0, lambda: self.after_read(blocks, names,
                                                           overwrite=True))
            except Exception as exc:
                self.log(f"ERROR uploading: {exc}")
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    # -- setups ------------------------------------------------------------

    def do_save_setup(self):
        if self.busy or not self.awg.inst:
            return
        self.set_busy(True)
        outdir = self.outdir.get().strip()
        prefix = safe_name(self.prefix.get())

        def work():
            try:
                snap = self.awg.snapshot()
                os.makedirs(outdir, exist_ok=True)
                stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                base = os.path.join(outdir, f"{prefix}_{stamp}")
                with open(base + ".json", "w", encoding="utf-8") as fh:
                    json.dump(snap, fh, indent=2)
                with open(base + ".txt", "w", encoding="utf-8") as fh:
                    fh.write(describe(snap))
                self.log(f"Saved setup: {base}.json (+ .txt)")
            except Exception as exc:
                self.log(f"ERROR saving setup: {exc}")
            finally:
                self.root.after(0, lambda: self.set_busy(False))
                self.root.after(0, self.save_config)
        threading.Thread(target=work, daemon=True).start()

    def do_recall_setup(self):
        if self.busy or not self.awg.inst:
            return
        path = filedialog.askopenfilename(
            title="Recall setup", initialdir=self.outdir.get() or ".",
            filetypes=[("Setup files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                snap = json.load(fh)
            chans = snap["channels"]
        except Exception as exc:
            messagebox.showerror("Cannot read setup", str(exc))
            self.log(f"Could not read {path}: {exc}")
            return

        was_on = [ch for ch in CHANNELS
                  if parse_reply(chans.get(str(ch), {}).get("OUTP", "")).get("STATE")
                  == "ON"]
        note = ""
        if was_on:
            note = ("\n\nThe file was saved with "
                    + " and ".join(f"CH{c}" for c in was_on)
                    + " ON. Outputs will be left as they are now - switch them "
                      "on yourself afterwards if that is what you want.")
        live = [ch for ch in CHANNELS if self.out_state.get(ch) == "ON"]
        if live:
            note += ("\n\n" + " and ".join(f"CH{c}" for c in live)
                     + " is ON right now, so this will change what is being "
                       "output straight away.")
        if not messagebox.askokcancel("Recall setup",
                                      f"Apply {os.path.basename(path)} to the "
                                      f"generator?{note}"):
            return
        self.set_busy(True)

        def work():
            try:
                for ch in CHANNELS:
                    blocks = chans.get(str(ch))
                    if not blocks:
                        continue
                    self.log(f"Recalling CH{ch}:")
                    self.awg.apply_channel(ch, self.plan_from_snapshot(blocks),
                                           log=self.log)
                fresh = {ch: self.awg.read_channel(ch) for ch in CHANNELS}
                names = self.awg.user_waveforms()
                self.log(f"Recalled {os.path.basename(path)} "
                         "(output switches left untouched)")
                self.root.after(0, lambda: self.after_read(fresh, names,
                                                           overwrite=True))
            except Exception as exc:
                self.log(f"ERROR recalling: {exc}")
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def plan_from_snapshot(blocks):
        """A saved channel's raw replies -> the write plan apply_channel wants."""
        plan = {}
        outp = parse_reply(blocks.get("OUTP", ""))
        plan["OUTP"] = {k: v for k, v in outp.items() if k in ("LOAD", "PLRT")}

        bswv = {k: v for k, v in parse_reply(blocks.get("BSWV", "")).items()
                if k not in READ_ONLY_KEYS}
        plan["BSWV"] = bswv

        arwv = parse_reply(blocks.get("ARWV", ""))
        if arwv.get("INDEX") is not None:
            # Built-in waveforms only reload by index; NAME silently no-ops.
            plan["ARWV"] = {"INDEX": arwv["INDEX"]}
        elif arwv.get("NAME"):
            name = str(arwv["NAME"])
            plan["ARWV"] = {"NAME": name[:-4] if name.endswith(".bin") else name}

        plan["SRATE"] = parse_reply(blocks.get("SRATE", ""))

        mode, params = "Off", {}
        for verb in MODE_VERBS:
            parsed = parse_reply(blocks.get(verb, ""))
            if parsed.get("STATE") != "ON":
                continue
            mode = (str(parsed.get("TYPE", "AM")) if verb == "MDWV"
                    else ("Sweep" if verb == "SWWV" else "Burst"))
            params = {k: v for k, v in parsed.items()
                      if k not in READ_ONLY_KEYS
                      and k not in ("STATE", "TYPE", "CARR")}
            if parsed.get("MARK_STATE") == "OFF":
                # An idle sweep marker reads back as 0 Hz but clamps to START on
                # the way in, which would make the recall inexact.
                params.pop("MARK_FREQ", None)
            break
        plan["MODE"] = (mode, params)
        return plan

    # -- preview -----------------------------------------------------------

    def arb_style(self, ch):
        """How the generator gets from one stored point to the next.

        TrueArb clocks each point out and holds it, so the output is a
        staircase. DDS resamples the record to land on the frequency you asked
        for and ramps between points instead. Same samples, different shape on
        a scope - so the preview has to follow the clock rather than assume.
        """
        tarb = self.vars[f"C{ch}:SRATE:MODE"].get().strip() == "TARB"
        return ("steps-post", "held") if tarb else ("default", "interpolated")

    def trace_for(self, ch, span):
        """One channel's curve over a shared time window, or None."""
        wvtp = self.vars[f"C{ch}:BSWV:WVTP"].get().strip().upper()
        vals = {key: self.vars[f"C{ch}:BSWV:{key}"].get().strip()
                for key, _, _ in WAVE_PARAMS}
        name = self.vars[f"C{ch}:ARWV:NAME"].get().strip()
        arb = self.known_waves.get(name) if wvtp == "ARB" else None
        hold = self.arb_style(ch)[0] == "steps-post"
        mode, mod = self.channel_mode(ch)
        curve = preview_curve(wvtp, vals, arb=arb, hold=hold, span=span,
                              period=self.channel_period(ch), mode=mode, mod=mod,
                              invert=self.vars[f"C{ch}:OUTP:PLRT"].get().strip()
                              == "INVT")
        if curve is None:
            return None, (f"CH{ch} {wvtp} '{name}' not held locally"
                          if wvtp == "ARB" else f"CH{ch} {wvtp}")
        label = f"CH{ch} {wvtp}"
        if mode != "Off":
            label += f" +{mode}"
        if arb is not None:
            # Say which way the clock joins the points up: it changes the shape
            # on screen, so the trace should carry it rather than leaving the
            # reader to check the Clock cell.
            label += f" '{name}' ({'held' if hold else 'interpolated'})"
        if curve[2]:
            label += f" [{curve[2]}]"
        return curve[:2], label

    def channel_mode(self, ch):
        """The modulation/sweep/burst row as (mode, {SCPI key: value})."""
        mode = self.vars[f"C{ch}:MODE"].get().strip() or "Off"
        params = {}
        for slot in range(MODE_SLOTS):
            key = self.mode_key(ch, slot)
            value = self.vars[f"C{ch}:MODE:{slot}"].get().strip()
            if key and value:
                params[key] = value
        return mode, params

    def channel_period(self, ch):
        """Repeat time of whatever the channel is set to, in seconds.

        Deferred to preview_period so a modulated channel is framed by its
        envelope rather than its carrier - otherwise slow modulation on a fast
        carrier is drawn as a plain unmodulated tone.
        """
        vals = {key: self.vars[f"C{ch}:BSWV:{key}"].get().strip()
                for key, _, _ in WAVE_PARAMS}
        mode, mod = self.channel_mode(ch)
        return preview_period(self.vars[f"C{ch}:BSWV:WVTP"].get().strip().upper(),
                              vals, mode, mod)

    def pending_arb_period(self, ch, n_points):
        """How often a record of `n_points` would repeat on this channel, or
        None when its length is not what sets that.

        Under TrueArb it is points / sample rate, which is knowable exactly and
        is *not* the channel's present frequency - loading a record of a
        different length is what changes that frequency, so FRQ is still
        describing the record being replaced. Under DDS the record is resampled
        into one period whatever its length, so its length says nothing and the
        channel's frequency stands: None, and the caller falls back to it.
        """
        if self.vars[f"C{ch}:SRATE:MODE"].get().strip() != "TARB":
            return None
        try:
            rate = float(self.vars[f"C{ch}:SRATE:VALUE"].get().strip() or 0)
        except ValueError:
            return None
        return n_points / rate if rate > 0 else None

    def pending_period(self, ch, n_points):
        """How long a window the pending record wants on the channel it is
        aimed at: its own repeat time where that is knowable, otherwise the
        channel's."""
        return self.pending_arb_period(ch, n_points) or self.channel_period(ch)

    def show_alias_note(self, spans, target, pending, pending_span):
        """Name the traces the preview cannot draw faithfully, beside the
        switches that turn them on.

        The window is set by the slowest thing sharing it, so a fast carrier
        under slow modulation - or a long arb, or a pulse with edges in
        nanoseconds - can come down to a couple of points per cycle, and what
        is drawn is then an alias rather than the shape the generator will put
        out. `spans` is per channel, since with separate time axes a channel is
        only framed by what shares its own panel.
        """
        if getattr(self, "alias_note", None) is None:
            return
        bad = []
        for ch, span in spans.items():
            wvtp = self.vars[f"C{ch}:BSWV:WVTP"].get().strip().upper()
            vals = {key: self.vars[f"C{ch}:BSWV:{key}"].get().strip()
                    for key, _, _ in WAVE_PARAMS}
            name = self.vars[f"C{ch}:ARWV:NAME"].get().strip()
            arb = self.known_waves.get(name) if wvtp == "ARB" else None
            mode, mod = self.channel_mode(ch)
            n = preview_points(span, wvtp, vals, mode, mod, arb)
            if preview_aliasing(span, n, wvtp, vals, mode, mod, arb):
                bad.append(f"CH{ch}")
        if pending is not None:
            # Drawn through the target channel's settings but from its own
            # record, so it can be the coarse one on a plot whose channels are
            # both fine.
            vals = {key: self.vars[f"C{target}:BSWV:{key}"].get().strip()
                    for key, _, _ in WAVE_PARAMS}
            mode, mod = self.channel_mode(target)
            arb_period = self.pending_arb_period(target, pending.size)
            n = preview_points(pending_span, "ARB", vals, mode, mod, pending,
                               arb_period)
            if preview_aliasing(pending_span, n, "ARB", vals, mode, mod,
                                pending, arb_period):
                bad.append("pending")

        text = ""
        if bad:
            named = (bad[0] if len(bad) == 1
                     else ", ".join(bad[:-1]) + " and " + bad[-1])
            text = f"{named} may be aliasing"
        self.alias_note.configure(text=text)

    def preview_axes(self, panels, twin):
        """The axes to draw on: `panels` stacked, the first optionally twinned.

        Rebuilt only when that shape changes. Clearing and re-adding on every
        redraw would be simpler, but a twinx made fresh each time stacks up
        axes for the life of the session - and a redraw happens on every
        keystroke.
        """
        key = (panels, twin)
        if key != self.axes_key:
            self.fig.clf()
            self.axes = list(self.fig.subplots(panels, 1, squeeze=False)[:, 0])
            self.ax = self.axes[0]
            self.ax2 = self.ax.twinx() if twin else None
            # Stacked panels need room between them for a whole x axis each,
            # so the plot has to grow or they end up two slivers.
            #
            # Growing the Figure alone does not do it, three times over. The
            # width has to be carried across or it snaps back to the 5.6 it
            # was built with, and the figure then renders small over whatever
            # the widget was showing before. The widget keeps its own
            # requested height, so the pane never makes room. And the canvas
            # syncs the figure *from* the widget on every <Configure>, so a
            # height set only on the figure is undone by the first window
            # resize. The widget's height is the thing to set; the figure is
            # set to match so that this draw is already the right size.
            height = 3.0 + 1.15 * (panels - 1)
            self.fig.set_size_inches(self.fig.get_figwidth(), height)
            self.canvas.get_tk_widget().configure(
                height=int(round(height * self.fig.dpi)))
            self.fig.subplots_adjust(left=0.14, right=0.86, top=0.90,
                                     bottom=0.18, hspace=0.75)
            self.axes_key = key
        else:
            for axis in self.axes:
                axis.clear()
            if self.ax2 is not None:
                self.ax2.clear()
        if self.ax2 is not None:
            # clear() puts a twinned axis's label back on the left, where it
            # lands on top of the first axis's. The ticks survive; the label
            # does not.
            self.ax2.yaxis.set_label_position("right")
            self.ax2.yaxis.set_ticks_position("right")
        return self.axes

    def draw_preview(self):
        """Every enabled trace, on one shared time axis or on one each.

        Shared is the default and the honest one: the channels are simultaneous
        on the bench, and a window each would draw them as if aligned when they
        are not. But a 10 Hz ramp beside a 1 MHz tone leaves the fast trace a
        solid band, and **separate time axes** is for that - each trace framed
        by its own period, at the cost of the two no longer sharing a clock.

        The pending waveform is drawn as it would come out of the channel it is
        aimed at, dashed and in a paler shade of that channel's colour.
        """
        if self.canvas is None:
            return
        wanted = [ch for ch in CHANNELS if self.show_ch[ch].get()]
        target = int(self.arb_ch.get())
        pending = None
        if self.show_pending.get() and self.arb_samples is not None:
            try:
                # Draw what will be stored, not what came out of the file: the
                # upload normalises, so raw values here would put the same
                # waveform at a different height before and after sending.
                pending = dac_samples(self.arb_samples, normalize=self.norm.get())
            except ValueError:
                pending = None

        if not wanted and pending is None:
            axis = self.preview_axes(1, False)[0]
            axis.text(0.5, 0.5,
                      "nothing selected" + chr(10) + "tick CH1, CH2 or pending",
                      ha="center", va="center", transform=axis.transAxes,
                      color="#666")
            axis.set_xticks([]); axis.set_yticks([])
            self.show_alias_note({}, target, None, 0.0)
            self.canvas.draw_idle()
            return

        # One panel per channel when times are split, otherwise all in one. The
        # pending trace joins the panel holding the channel it is aimed at, and
        # gets a panel of its own only when that channel is not being shown.
        if self.split_t.get():
            groups = [[ch] for ch in wanted]
            if pending is not None and target not in wanted:
                groups.append([])
        else:
            groups = [list(wanted)]
        pending_at = None
        if pending is not None:
            pending_at = next((i for i, g in enumerate(groups) if target in g),
                              len(groups) - 1)

        # Two y axes only earn their keep when two channels share one panel;
        # with a panel each they already have a scale apiece.
        twin = (not self.split_t.get() and self.split_y.get()
                and len(wanted) == 2)
        axes = self.preview_axes(len(groups), twin)

        spans, pending_span, notes = {}, 0.0, []
        for index, (axis, group) in enumerate(zip(axes, groups)):
            periods = [self.channel_period(ch) for ch in group]
            if index == pending_at:
                periods.append(self.pending_period(target, pending.size))
            span = 2.0 * max(periods)
            for ch in group:
                spans[ch] = span
            if index == pending_at:
                pending_span = span

            handles = []
            for ch in group:
                where = self.ax2 if (twin and ch == 2) else axis
                curve, label = self.trace_for(ch, span)
                if curve is None:
                    notes.append(label)
                    continue
                t, v = curve
                handles += where.plot(t * 1e3, v, lw=1.1, color=CH_COLOUR[ch],
                                      label=label)

            if index == pending_at:
                where = self.ax2 if (twin and target == 2) else axis
                vals = {key: self.vars[f"C{target}:BSWV:{key}"].get().strip()
                        for key, _, _ in WAVE_PARAMS}
                hold = self.arb_style(target)[0] == "steps-post"
                mode, mod = self.channel_mode(target)
                # arb_period rather than period: what times an arb is how often
                # the record repeats, and the window is already fixed by span.
                # A period passed here would be read by nothing, which is how
                # this trace came to be drawn at the channel's stale frequency.
                curve = preview_curve(
                    "ARB", vals, arb=pending, hold=hold, span=span, mode=mode,
                    mod=mod, invert=self.vars[f"C{target}:OUTP:PLRT"].get()
                    .strip() == "INVT",
                    arb_period=self.pending_arb_period(target, pending.size))
                if curve is not None:
                    t, v = curve[:2]
                    handles += where.plot(
                        t * 1e3, v, lw=1.4, ls="--",
                        color=PENDING_COLOUR[target],
                        label=f"pending -> CH{target} ({pending.size} pts, "
                              f"{'held' if hold else 'interpolated'})")

            axis.set_xlabel("time (ms)")
            axis.grid(alpha=0.3)
            if twin:
                axis.set_ylabel("CH1 volts", color=CH_COLOUR[1])
                self.ax2.set_ylabel("CH2 volts", color=CH_COLOUR[2])
                axis.tick_params(axis="y", colors=CH_COLOUR[1])
                self.ax2.tick_params(axis="y", colors=CH_COLOUR[2])
            elif len(groups) > 1 and group:
                axis.set_ylabel(f"CH{group[0]} volts", color=CH_COLOUR[group[0]])
                axis.tick_params(axis="y", colors=CH_COLOUR[group[0]])
            else:
                axis.set_ylabel("volts", color="black")
                axis.tick_params(axis="y", colors="black")
            if handles:
                axis.legend(handles, [h.get_label() for h in handles],
                            fontsize=7, loc="upper right", framealpha=0.85)

        self.show_alias_note(spans, target, pending, pending_span)
        self.axes[0].set_title(" | ".join(notes) if notes else "",
                               fontsize=8, color="#666")
        self.canvas.draw_idle()

    # -- config ------------------------------------------------------------

    def current_cfg(self):
        return {
            "outdir": self.outdir.get(),
            "prefix": self.prefix.get(),
            "arb_name": self.arb_name.get(),
            "arb_ch": self.arb_ch.get(),
            "normalise": bool(self.norm.get()),
            "confirm_output": bool(self.confirm_output.get()),
        }

    def load_config(self):
        """Restore what the last session was using. Anything missing, malformed
        or of the wrong type is ignored and leaves the default in place."""
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                cfg = json.load(fh)
            if not isinstance(cfg, dict):
                raise ValueError("not a JSON object")
        except FileNotFoundError:
            self.saved_cfg = self.current_cfg()
            return
        except Exception as exc:
            self.log(f"Ignoring unreadable {CONFIG_PATH}: {exc}")
            self.saved_cfg = self.current_cfg()
            return

        for key, var in (("outdir", self.outdir), ("prefix", self.prefix),
                         ("arb_name", self.arb_name), ("arb_ch", self.arb_ch)):
            value = cfg.get(key)
            if isinstance(value, str) and value.strip():
                var.set(value)
        for key, var in (("normalise", self.norm),
                         ("confirm_output", self.confirm_output)):
            value = cfg.get(key)
            if isinstance(value, (bool, int)):
                var.set(bool(value))

        self.saved_cfg = self.current_cfg()
        self.log(f"Restored last session from {CONFIG_PATH}")

    def save_config(self):
        """Called when the folder is picked, after a save, and on close. Writes
        only when something actually changed."""
        cfg = self.current_cfg()
        if cfg == self.saved_cfg:
            return
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            self.saved_cfg = cfg
        except Exception as exc:
            self.log(f"Could not save {CONFIG_PATH}: {exc}")

    def on_close(self):
        # Outputs are deliberately left exactly as they are: closing a control
        # panel should never interrupt something the bench is in the middle of.
        self.save_config()
        self.awg.close()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
