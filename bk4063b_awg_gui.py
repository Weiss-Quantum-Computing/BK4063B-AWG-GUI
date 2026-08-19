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

CHANNELS = (1, 2)
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


def preview_curve(wvtp, vals, arb=None, periods=2.0, n=2000):
    """One or two cycles of what the panel currently describes, in volts.

    Computed here rather than read back from the generator: the point is to show
    what Apply would produce, before it is applied. Returns (t, v) or None when
    there is nothing meaningful to draw.
    """
    def num(key, default=0.0):
        try:
            return float(vals.get(key, "") or default)
        except ValueError:
            return default

    amp, ofst = num("AMP", 1.0), num("OFST")
    freq = num("FRQ", 1000.0)
    if freq <= 0:
        freq = 1000.0
    period = 1.0 / freq
    t = np.linspace(0.0, periods * period, n)
    ph = num("PHSE") / 360.0
    x = (t / period + ph) % 1.0                 # phase within a cycle, 0..1

    if wvtp == "DC":
        return t, np.full_like(t, ofst)
    if wvtp == "NOISE":
        rng = np.random.default_rng(0)          # fixed seed: a still picture
        return t, num("MEAN") + num("STDEV", 0.5) * rng.standard_normal(n)
    if wvtp == "SINE":
        y = np.sin(2 * np.pi * x)
    elif wvtp == "SQUARE":
        y = np.where(x < num("DUTY", 50.0) / 100.0, 1.0, -1.0)
    elif wvtp == "RAMP":
        sym = np.clip(num("SYM", 50.0) / 100.0, 1e-6, 1 - 1e-6)
        y = np.where(x < sym, 2 * x / sym - 1, 1 - 2 * (x - sym) / (1 - sym))
    elif wvtp == "PULSE":
        width = num("WIDTH")
        duty = width / period if width > 0 else num("DUTY", 20.0) / 100.0
        y = np.where(x < np.clip(duty, 1e-6, 1 - 1e-6), 1.0, -1.0)
    elif wvtp == "ARB":
        if arb is None or len(arb) < 2:
            return None                          # shape lives on the instrument
        idx = (x * len(arb)).astype(int) % len(arb)
        y = arb[idx]
    else:
        return None
    return t, ofst + (amp / 2.0) * y


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
        self.arb_source = ""
        self.read_stamp = ""

        # panel key -> widgets/state, exactly one entry per editable cell
        self.vars = {}                # key -> StringVar shown in the panel
        self.marks = {}               # key -> "edited" marker label
        self.inst_vals = {}           # key -> value the generator last reported
        self.widgets = {}             # key -> the entry/combobox itself
        self.natural = {}             # key -> state to restore when re-enabled
        self.out_state = {ch: None for ch in CHANNELS}

        root.title("BK4063B AWG GUI")
        win_w = min(1220, root.winfo_screenwidth() - 80)
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

        self.build_arb(left, pad)
        self.build_setups(left, pad)
        self.build_preview(right, pad)

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
        ttk.Label(a, text="Arb wave:").grid(row=0, column=0, sticky="e", padx=(0, 4))
        combo = self.cell(a, f"C{ch}:ARWV:NAME", (), 0, 1, 16)
        setattr(self, f"arbcombo{ch}", combo)
        ttk.Label(a, text="Clock:").grid(row=0, column=2, sticky="e", padx=(8, 4))
        self.cell(a, f"C{ch}:SRATE:MODE", ("DDS", "TARB"), 0, 3, 8)
        ttk.Label(a, text="Sa/s:").grid(row=0, column=4, sticky="e", padx=(8, 4))
        self.cell(a, f"C{ch}:SRATE:VALUE", None, 0, 5, 11)
        ttk.Label(a, text="Interp:").grid(row=0, column=6, sticky="e", padx=(8, 4))
        self.cell(a, f"C{ch}:SRATE:INTER", ("LINE", "HOLD"), 0, 7, 8)

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
        ttk.Combobox(r2, textvariable=self.arb_ch, values=[str(c) for c in CHANNELS],
                     width=3, state="readonly").pack(side="left", padx=4)
        self.norm = tk.BooleanVar(value=True)
        ttk.Checkbutton(r2, text="normalise to full scale",
                        variable=self.norm).pack(side="left", padx=8)
        self.upload_btn = ttk.Button(r2, text="Upload", command=self.do_upload,
                                     state="disabled")
        self.upload_btn.pack(side="left", padx=4)

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
        self.fig.subplots_adjust(left=0.15, right=0.97, top=0.90, bottom=0.18)
        self.canvas = FigureCanvasTkAgg(self.fig, master=f)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=4, pady=4)

        r = ttk.Frame(f)
        r.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(r, text="Show:").pack(side="left")
        self.preview_ch = tk.StringVar(value="1")
        cb = ttk.Combobox(r, textvariable=self.preview_ch,
                          values=[f"{c}" for c in CHANNELS], width=4,
                          state="readonly")
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.draw_preview())

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
        """Grey out the parameters the selected wave type has no use for."""
        wvtp = self.vars[f"C{ch}:BSWV:WVTP"].get().strip().upper()
        for key, _, applies in WAVE_PARAMS:
            on = wvtp in applies
            self.enable(f"C{ch}:BSWV:{key}", on)
            getattr(self, f"lab{ch}_{key}").configure(
                foreground="#000" if on else "#aaa")
        for key in (f"C{ch}:ARWV:NAME", f"C{ch}:SRATE:MODE",
                    f"C{ch}:SRATE:VALUE", f"C{ch}:SRATE:INTER"):
            self.enable(key, wvtp == "ARB")

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
                    self.recall_btn):
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
        out[f"C{ch}:SRATE:INTER"] = str(srate.get("INTER", "") or "")

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
        for key, value in values.items():
            if key.endswith(":WVTP") or key.endswith(":MODE"):
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
        for key in ("MODE", "VALUE", "INTER"):
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
                fresh = {ch: self.awg.read_channel(ch) for ch in CHANNELS}
                names = self.awg.user_waveforms()
                self.root.after(0, lambda: self.after_read(fresh, names,
                                                           overwrite=True))
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

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

        self.arb_table = table
        self.arb_source = path
        ncols = table.shape[1]
        labels = [f"{i + 1}: {names[i]}" if names and i < len(names)
                  else f"column {i + 1}" for i in range(ncols)]
        self.arb_col_box.configure(values=labels,
                                   state="readonly" if ncols > 1 else "disabled")
        self.arb_col.set(labels[default_column(names, ncols)])

        self.log(f"Loaded {path}")
        self.log(f"  {table.shape[0]} rows x {ncols} column(s)"
                 + (f": {', '.join(names)}" if names else ""))
        self.arb_name.set(safe_name(os.path.splitext(os.path.basename(path))[0])[:16]
                          or "wave2")
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

    def draw_preview(self):
        if self.canvas is None:
            return
        ch = int(self.preview_ch.get())
        wvtp = self.vars[f"C{ch}:BSWV:WVTP"].get().strip().upper()
        vals = {key: self.vars[f"C{ch}:BSWV:{key}"].get().strip()
                for key, _, _ in WAVE_PARAMS}
        arb = self.arb_samples if (wvtp == "ARB" and self.arb_samples is not None
                                   and int(self.arb_ch.get()) == ch) else None
        curve = preview_curve(wvtp, vals, arb=arb)

        self.ax.clear()
        if curve is None:
            msg = ("select a wave type" if not wvtp else
                   f"CH{ch}: '{self.vars[f'C{ch}:ARWV:NAME'].get()}' lives on the "
                   "generator\nload the file here to preview it")
            self.ax.text(0.5, 0.5, msg, ha="center", va="center",
                         transform=self.ax.transAxes, color="#666")
            self.ax.set_xticks([])
            self.ax.set_yticks([])
        else:
            t, v = curve
            self.ax.plot(t * 1e3, v, lw=1.2)
            self.ax.set_xlabel("time (ms)")
            self.ax.set_ylabel("volts")
            self.ax.grid(alpha=0.3)
            src = " (from file)" if arb is not None else ""
            self.ax.set_title(f"CH{ch}  {wvtp}{src}", fontsize=9)
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
