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
WAVE_TYPES = ("SINE", "SQUARE", "RAMP", "PULSE", "NOISE", "DC", "ARB")
LOADS = ("50", "75", "100", "600", "10000", "HZ")

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
WAVE_PARAMS = [
    ("FRQ",   "Freq (Hz)",    {"SINE", "SQUARE", "RAMP", "PULSE", "ARB"}),
    ("AMP",   "Ampl (Vpp)",   {"SINE", "SQUARE", "RAMP", "PULSE", "ARB"}),
    ("OFST",  "Offset (V)",   {"SINE", "SQUARE", "RAMP", "PULSE", "ARB", "DC"}),
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

# Modulation types appear as a bare tag with no value, mid-response.
_BARE_TAGS = {"AM", "DSBAM", "FM", "PM", "PWM", "ASK", "FSK", "PSK"}

# Readback-only keys: the instrument reports them but rejects them on the way
# back in, or derives them from something else we are already sending.
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

ENV_CHOICES = ("None", "Blackman", "Gaussian", "Hann", "Tukey")


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
    if name == "Blackman":
        return np.blackman(n)
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


def _build_blackman(n, p):
    return np.blackman(n)


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
    "Blackman":        (_build_blackman, list(_CARRIER)),
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
                         ("Envelope", "env", "Blackman", ENV_CHOICES)]),
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
    if n % 2:
        n += 1                              # the generator rejects odd counts
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


def preview_period(wvtp, vals, mode="Off", mod=None):
    """The repeat time worth drawing two of.

    With a mode running that is the envelope, not the carrier: two carrier
    cycles of a 1 kHz tone under 10 Hz AM is a flat sine with no modulation
    visible, which is how the preview managed to look plausible while ignoring
    the whole modulation row.
    """
    num, mnum = _numget(vals), _numget(mod or {})
    freq = num("FRQ", 1000.0)
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


def preview_curve(wvtp, vals, arb=None, periods=2.0, n=2000, hold=True,
                  period=None, span=None, mode="Off", mod=None, invert=False):
    """What the panel currently describes, in volts against seconds.

    Computed here rather than read back from the generator: the point is to show
    what Apply would produce, before it is applied. `hold` picks how an arb gets
    from one stored point to the next - held under TrueArb, ramped under DDS.

    `period` overrides the repeat time that would otherwise come from FRQ, and
    `span` sets how much time to draw. Both exist so several traces can be put
    on one shared time axis: two channels running at different frequencies are
    simultaneous in the real world, and drawing each over its own private window
    would imply they line up when they do not.

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

    if period is None:
        period = preview_period(wvtp, vals, mode, mod)
    if span is None:
        span = periods * period
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
            # The convention bench generators use: at 0% depth the output is
            # half amplitude, at 100% the envelope just reaches zero.
            scale = (1.0 + mnum("DEPTH", 100.0) / 100.0 * m) / 2.0
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
        """
        prefix = f"C{ch}"

        outp = blocks.get("OUTP") or {}
        parts = [f"{k},{v}" for k, v in outp.items() if k != "STATE" and v != ""]
        if parts:
            self.write(f"{prefix}:OUTP {','.join(parts)}")
            log(f"  {prefix}:OUTP {','.join(parts)}")

        arb = blocks.get("ARWV") or {}
        if arb.get("NAME"):
            self.write(f"{prefix}:ARWV NAME,{arb['NAME']}")
            log(f"  {prefix}:ARWV NAME,{arb['NAME']}")
        elif arb.get("INDEX") not in (None, ""):
            self.write(f"{prefix}:ARWV INDEX,{int(float(arb['INDEX']))}")
            log(f"  {prefix}:ARWV INDEX,{int(float(arb['INDEX']))}")

        srate = blocks.get("SRATE") or {}
        if srate:
            args = ",".join(f"{k},{v}" for k, v in srate.items() if v != "")
            if args:
                self.write(f"{prefix}:SRATE {args}")
                log(f"  {prefix}:SRATE {args}")

        bswv = blocks.get("BSWV") or {}
        if bswv:
            args = ",".join(f"{k},{v}" for k, v in bswv.items() if v != "")
            if args:
                self.write(f"{prefix}:BSWV {args}")
                log(f"  {prefix}:BSWV {args}")

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
                self.write(f"{prefix}:{verb} STATE,ON")
                args = ",".join(f"{k},{v}" for k, v in params.items() if v != "")
                lead = f"{mode}," if verb == "MDWV" else ""
                if lead or args:
                    self.write(f"{prefix}:{verb} {lead}{args}")
                log(f"  {prefix}:{verb} STATE,ON / {lead}{args}")
            else:
                log(f"  {prefix}: modulation, sweep and burst all off")

    def upload_arb(self, ch, name, samples, normalize=True):
        """Upload a waveform into user memory and select it on this channel.

        Sent as signed 16-bit little-endian, which is what the 16-bit DAC in the
        4063B expects. Point count must be even.
        """
        data = np.asarray(samples, dtype=np.float64).ravel()
        if data.size < 2:
            raise ValueError("need at least 2 samples")
        if data.size % 2:
            data = data[:-1]                     # odd counts are rejected outright
        if normalize:
            peak = float(np.max(np.abs(data)))
            if peak == 0:
                raise ValueError("all samples are zero; nothing to normalise")
            data = data / peak
        codes = np.clip(np.round(data * 32767), -32768, 32767).astype("<i2")

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
        self.out_state = {ch: None for ch in CHANNELS}

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
        self.sync = ttk.Label(bar, text="not read yet", foreground="#666")
        self.sync.pack(side="left", padx=6)

        self.build_setups(left, pad)
        self.build_memory(left, pad)
        # The waveform tools sit under the preview rather than in the left
        # column: they are what the preview is showing, and the channel panels
        # already fill the height on a laptop screen.
        self.build_preview(right, pad)
        self.build_builder(right, pad)
        self.build_arb(right, pad)

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
        # None -> plain entry. A list of choices -> fixed dropdown. An empty
        # tuple -> dropdown that is also typeable, for the arb name, whose
        # suggestions are filled in from the generator once it is read.
        if choices is not None:
            w = ttk.Combobox(holder, textvariable=var, values=list(choices),
                             width=max(4, width - 3),
                             state="readonly" if choices else "normal")
        else:
            w = ttk.Entry(holder, textvariable=var, width=width)
        w.pack(side="left")
        mark = ttk.Label(holder, text=" ", width=1, foreground="#c60")
        mark.pack(side="left")

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
        self.cell(self._grid(o), f"C{ch}:OUTP:LOAD", LOADS, 0, 0, 9)
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
        ttk.Label(r, text="Points:").pack(side="left", padx=(10, 2))
        self.build_pts = tk.StringVar(value="10000")
        ttk.Entry(r, textvariable=self.build_pts, width=9).pack(side="left")
        ttk.Button(r, text="Build", command=self.do_build).pack(side="left", padx=(10, 4))
        ttk.Button(r, text="Type/paste values...",
                   command=self.do_paste).pack(side="left")

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
            self.shape_labels.append(lab)
            self.shape_vars.append(var)
            self.shape_boxes.append(box)
        self.on_shape()

    def on_shape(self):
        """Relabel the parameter slots for the shape now selected."""
        spec = BUILD_SHAPES[self.shape.get()][1]
        for slot in range(BUILD_SLOTS):
            lab = self.shape_labels[slot]
            var = self.shape_vars[slot]
            box = self.shape_boxes[slot]
            if slot < len(spec):
                label, _key, default, choices = spec[slot]
                lab.configure(text=label + ":", foreground="#000")
                box.configure(values=list(choices) if choices else (),
                              state="readonly" if choices else "normal")
                var.set(default)
            else:
                lab.configure(text="")
                box.configure(values=(), state="disabled")
                var.set("")

    def do_build(self):
        shape = self.shape.get()
        spec = BUILD_SHAPES[shape][1]
        values = {spec[i][1]: self.shape_vars[i].get() for i in range(len(spec))}
        try:
            data = build_waveform(shape, int(float(self.build_pts.get())), values)
        except Exception as exc:
            self.log(f"Could not build {shape}: {exc}")
            messagebox.showerror("Cannot build waveform", str(exc))
            return
        detail = ", ".join(f"{spec[i][0]}={self.shape_vars[i].get()}"
                           for i in range(len(spec)) if self.shape_vars[i].get())
        self.log(f"Built {shape}: {data.size} pts" + (f" ({detail})" if detail else ""))
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

    def build_setups(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Setups")
        f.pack(fill="x", **pad)
        of = ttk.Frame(f)
        of.pack(fill="x", padx=6, pady=(6, 2))
        default_dir = os.path.join(os.path.expanduser("~"), "Desktop", "awg_setups")
        self.outdir = tk.StringVar(value=default_dir)
        ttk.Entry(of, textvariable=self.outdir).pack(side="left", fill="x",
                                                     expand=True)
        ttk.Button(of, text="...", width=3,
                   command=self.pick_dir).pack(side="left", padx=6)

        pf = ttk.Frame(f)
        pf.pack(fill="x", padx=6, pady=2)
        ttk.Label(pf, text="Prefix:").pack(side="left")
        self.prefix = tk.StringVar(value="awg")
        ttk.Entry(pf, textvariable=self.prefix, width=16).pack(side="left", padx=4)
        self.save_btn = ttk.Button(pf, text="Save setup", command=self.do_save_setup,
                                   state="disabled")
        self.save_btn.pack(side="left", padx=(8, 4))
        self.recall_btn = ttk.Button(pf, text="Recall setup...",
                                     command=self.do_recall_setup, state="disabled")
        self.recall_btn.pack(side="left")

        gf = ttk.Frame(f)
        gf.pack(fill="x", padx=6, pady=(2, 6))
        # Both default on. Turning an output on is the one thing here that can
        # put a voltage into something that was not expecting it.
        self.confirm_output = tk.BooleanVar(value=True)
        ttk.Checkbutton(gf, text="confirm before switching an output on",
                        variable=self.confirm_output).pack(side="left")

    def build_preview(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Preview (what Apply would produce)")
        f.pack(fill="x", **pad)
        if Figure is None:
            ttk.Label(f, text="matplotlib not installed - no preview",
                      foreground="#666").pack(padx=8, pady=8)
            self.canvas = None
            return
        self.fig = Figure(figsize=(5.6, 3.0), dpi=100)
        self.ax = self.fig.add_subplot(111)
        # A second y axis made once and hidden when not wanted: building a fresh
        # twinx on every redraw would stack up axes for the life of the session.
        self.ax2 = self.ax.twinx()
        self.ax2.set_visible(False)
        self.fig.subplots_adjust(left=0.14, right=0.86, top=0.90, bottom=0.18)
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
        self.split_y = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text="separate Y axes", variable=self.split_y,
                        command=self.draw_preview).pack(side="right")

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
        self.refresh_marks()
        self.draw_preview()

    def edited(self, key):
        """True if the panel value differs from what the generator last said."""
        return self.vars[key].get().strip() != self.inst_vals[key]

    def refresh_marks(self):
        if not hasattr(self, "sync"):
            return          # still building the panel
        pending = 0
        for key, mark in self.marks.items():
            # A disabled cell is not part of the current wave type, so whatever
            # is left in it is stale rather than an edit waiting to be applied.
            if str(self.widgets[key].cget("state")) == "disabled":
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

        arb = wvtp == "ARB"
        tarb = arb and self.vars[f"C{ch}:SRATE:MODE"].get().strip() == "TARB"
        for key, on in ((f"C{ch}:ARWV:NAME", arb), (f"C{ch}:SRATE:MODE", arb),
                        (f"C{ch}:SRATE:VALUE", tarb)):
            self.enable(key, on)
            label = self.arb_labels.get(key)
            if label is not None:
                label.configure(foreground="#000" if on else "#aaa")

    def on_mode(self, ch):
        """Relabel the mode parameter slots for the mode now selected."""
        mode = self.vars[f"C{ch}:MODE"].get().strip() or "Off"
        spec = MODE_PARAMS.get(mode, [])
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

    def mode_key(self, ch, slot):
        """SCPI key the given mode slot currently stands for, or None."""
        mode = self.vars[f"C{ch}:MODE"].get().strip() or "Off"
        spec = MODE_PARAMS.get(mode, [])
        return spec[slot][1] if slot < len(spec) else None

    def set_busy(self, busy):
        self.busy = busy
        live = bool(self.awg.inst) and not busy
        state = "normal" if live else "disabled"
        for btn in (self.read_btn, self.apply_btn, self.save_btn,
                    self.recall_btn, self.mem_btn):
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
        # The mode row is relabelled by the mode itself, so that has to be set
        # before its slots or the slots land under the wrong labels.
        for key in (f"C{ch}:BSWV:WVTP", f"C{ch}:MODE"):
            if overwrite or not self.edited(key):
                self.vars[key].set(values[key])
            self.inst_vals[key] = values[key]
        self.on_wave_type(ch)
        self.on_mode(ch)

        kept = 0
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
                    str(self.widgets[panel_key].cget("state")) != "disabled":
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

    def do_read(self):
        if self.busy or not self.awg.inst:
            return
        self.set_busy(True)

        def work():
            try:
                blocks = {ch: self.awg.read_channel(ch) for ch in CHANNELS}
                names = self.awg.user_waveforms()
                self.log("Read back from generator.")
                self.root.after(0, lambda: self.after_read(blocks, names))
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
                for ch, blocks in plan.items():
                    self.log(f"Applying to CH{ch}:")
                    self.awg.apply_channel(ch, blocks, log=self.log)
                    if blocks.get("ARWV"):
                        self.report_arb_timing(ch)
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

        note = f"{os.path.basename(self.arb_source)} - {data.size} pts"
        if self.arb_table.shape[1] > 1:
            note += f", col {index + 1}"
        if data.size % 2:
            note += " (odd count, last dropped)"
        self.arb_info.configure(text=note, foreground="#000")
        self.log(f"  using column {index + 1}: {data.size} pts, "
                 f"{data.min():g} to {data.max():g}")
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
                kept = np.asarray(samples, dtype=np.float64).ravel()[:n]
                if norm:
                    kept = kept / (float(np.max(np.abs(kept))) or 1.0)
                self.known_waves[name] = np.clip(kept, -1.0, 1.0)
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

    def pending_period(self, ch, n_points):
        """How long the pending record would last on the channel it is aimed at.

        Under TrueArb that is points / sample rate, which is knowable exactly
        and is *not* the channel's present frequency - loading a record of a
        different length changes the frequency. Under DDS the record is one
        period whatever its length, so the channel's frequency stands.
        """
        if self.vars[f"C{ch}:SRATE:MODE"].get().strip() == "TARB":
            try:
                rate = float(self.vars[f"C{ch}:SRATE:VALUE"].get().strip() or 0)
            except ValueError:
                rate = 0.0
            if rate > 0:
                return n_points / rate
        return self.channel_period(ch)

    def draw_preview(self):
        """Every enabled trace on one shared time axis.

        Channels are drawn over a common window because they are simultaneous on
        the bench; giving each its own window would imply an alignment that does
        not exist. The pending waveform is drawn as it would come out of the
        channel it is aimed at, in that channel's colour but dashed.
        """
        if self.canvas is None:
            return
        self.ax.clear()
        self.ax2.clear()
        # clear() puts a twinned axis's label back on the left, where it lands on
        # top of the first axis's label. The ticks survive; the label does not.
        self.ax2.yaxis.set_label_position("right")
        self.ax2.yaxis.set_ticks_position("right")

        wanted = [ch for ch in CHANNELS if self.show_ch[ch].get()]
        target = int(self.arb_ch.get())
        pending = (self.arb_samples if self.show_pending.get()
                   and self.arb_samples is not None else None)

        periods = [self.channel_period(ch) for ch in wanted]
        if pending is not None:
            periods.append(self.pending_period(target, pending.size))
        if not periods:
            self.ax.text(0.5, 0.5,
                         "nothing selected" + chr(10) +
                         "tick CH1, CH2 or pending",
                         ha="center", va="center", transform=self.ax.transAxes,
                         color="#666")
            self.ax.set_xticks([]); self.ax.set_yticks([])
            self.ax2.set_visible(False)
            self.canvas.draw_idle()
            return

        span = 2.0 * max(periods)
        # Two y axes only earn their keep when there are two channels to
        # separate; with one trace it is just a duplicated scale.
        split = self.split_y.get() and len(wanted) == 2
        self.ax2.set_visible(split)

        handles, notes = [], []
        for ch in wanted:
            axis = self.ax2 if (split and ch == 2) else self.ax
            curve, label = self.trace_for(ch, span)
            if curve is None:
                notes.append(label)
                continue
            t, v = curve
            handles += axis.plot(t * 1e3, v, lw=1.1, color=CH_COLOUR[ch],
                                 label=label)

        if pending is not None:
            axis = self.ax2 if (split and target == 2) else self.ax
            vals = {key: self.vars[f"C{target}:BSWV:{key}"].get().strip()
                    for key, _, _ in WAVE_PARAMS}
            hold = self.arb_style(target)[0] == "steps-post"
            curve = preview_curve("ARB", vals, arb=pending, hold=hold, span=span,
                                  period=self.pending_period(target, pending.size))
            if curve is not None:
                t, v = curve[:2]
                handles += axis.plot(
                    t * 1e3, v, lw=1.2, ls="--", color=CH_COLOUR[target],
                    label=f"pending -> CH{target} ({pending.size} pts, "
                          f"{'held' if hold else 'interpolated'})")

        self.ax.set_xlabel("time (ms)")
        self.ax.grid(alpha=0.3)
        if split:
            self.ax.set_ylabel("CH1 volts", color=CH_COLOUR[1])
            self.ax2.set_ylabel("CH2 volts", color=CH_COLOUR[2])
            self.ax.tick_params(axis="y", colors=CH_COLOUR[1])
            self.ax2.tick_params(axis="y", colors=CH_COLOUR[2])
        else:
            self.ax.set_ylabel("volts", color="black")
            self.ax.tick_params(axis="y", colors="black")
        if handles:
            self.ax.legend(handles, [h.get_label() for h in handles],
                           fontsize=7, loc="upper right", framealpha=0.85)
        if notes:
            self.ax.set_title(" | ".join(notes), fontsize=8, color="#666")
        else:
            self.ax.set_title("")
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
