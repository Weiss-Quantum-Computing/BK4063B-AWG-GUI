"""
Control for the B&K Precision 4063B arbitrary waveform generator over VISA.

The 4060B series speaks the Siglent SDG-style SCPI dialect: most commands are
prefixed with a channel selector ("C1:" / "C2:") and both commands and query
responses use comma-separated KEY,VALUE lists.

    from bk4063b import BK4063B

    with BK4063B() as awg:                 # auto-finds the 4063B on USB
        print(awg.idn)
        print(awg.get_basic_wave(1))
        awg.sine(1, freq=1e3, amp=2.0, offset=0.0)
        awg.output(1, True, load=50)

This is the project's only instrument layer: bk4063b_awg_gui.py imports it
rather than carrying its own copy, so a fix to the SCPI ordering, the reply
parser or the arb upload path lands in the panel and in scripts at once.

Requires: pyvisa + a VISA runtime (NI-VISA is installed on this machine).
          numpy is optional - it only speeds up dac_samples().
"""

from __future__ import annotations

import datetime
import struct

import pyvisa

try:                       # only the sample-prep fast path needs it
    import numpy as np
except ImportError:
    np = None

__all__ = ["BK4063B", "InstrumentError", "parse_reply", "dac_samples",
           "USB_ID", "CHANNELS", "CARR_KEYS", "TARB_BLOCKS", "MODE_VERB",
           "MODE_VERBS"]

# USB vendor/product for the B&K 4060B series.
USB_ID = "0xF4EC::0xEE38"

CHANNELS = (1, 2)

# Carrier settings a mode block will accept as CARR,<key>,<value>, so they can
# be changed without a BSWV write - which switches modulation, sweep and burst
# straight off. Measured on the instrument, because the difference is silent:
# AMP, FRQ, OFST, PHSE and DUTY all take and leave a burst running, while
# CARR,PERI, CARR,HLEV and CARR,LLEV are accepted and quietly ignored. The rest
# are from the programming guide's own CARR table.
CARR_KEYS = {"WVTP", "FRQ", "PHSE", "AMP", "OFST", "SYM", "DUTY",
             "RISE", "FALL", "DLY", "STDEV", "MEAN"}

# Sweep and burst are not available while a channel's sample clock is TrueArb.
# Measured, not inferred: BTWV STATE,ON and SWWV STATE,ON both read back OFF
# under TARB and ON under DDS, reproducibly, while MDWV is unaffected. The
# generator does not refuse the command - it accepts it and stays off, which is
# why a burst set up from the panel looked like it was losing its settings.
TARB_BLOCKS = ("Sweep", "Burst")

# Which SCPI block each mode lives in. All three are cleared before one is
# enabled, because the instrument allows only one at a time.
MODE_VERB = {"Sweep": "SWWV", "Burst": "BTWV"}
MODE_VERBS = ("MDWV", "SWWV", "BTWV")


class InstrumentError(RuntimeError):
    pass


def _fmt(value) -> str:
    """Render a parameter value the way the instrument expects.

    Numbers go out bare (the firmware assumes base units: Hz, V, s, degrees).
    Strings pass through untouched so callers can force units, e.g. "1.5MHz".
    """
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _strip_unit(text: str):
    """'1000HZ' -> 1000.0, '0.001S' -> 0.001, '1000000Sa/s' -> 1000000.0,
    'NOR' -> 'NOR'."""
    body = text.rstrip("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ%/")
    try:
        return float(body)
    except ValueError:
        return text


def dac_samples(samples, normalize=True):
    """The samples exactly as the DAC will receive them, in -1..+1.

    One definition on purpose. This used to be written out separately where the
    bytes were packed, where the local copy was kept, and not at all where the
    pending trace was drawn - so the preview showed raw file values while
    everything after the upload showed normalised ones, and the same waveform
    changed height the moment it was sent.

    Returns a numpy array when numpy is installed (which is what the panel
    plots), a list otherwise.
    """
    if np is not None:
        data = np.asarray(samples, dtype=np.float64).ravel()
        if normalize:
            peak = float(np.max(np.abs(data))) if data.size else 0.0
            if peak == 0:
                raise ValueError("all samples are zero; nothing to normalise")
            data = data / peak
        return np.clip(data, -1.0, 1.0)

    data = [float(s) for s in samples]
    if normalize:
        peak = max((abs(s) for s in data), default=0.0)
        if peak == 0:
            raise ValueError("all samples are zero; nothing to normalise")
        data = [s / peak for s in data]
    return [max(-1.0, min(1.0, s)) for s in data]


# Modulation types appear as a bare tag with no value, mid-response.
_BARE_TAGS = {"AM", "DSBAM", "FM", "PM", "PWM", "ASK", "FSK", "PSK"}


def parse_reply(response: str) -> dict:
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

    out: dict = {}
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


class BK4063B:
    """A connected 4063B. Use as a context manager, or call close() yourself."""

    def __init__(self, resource: str | None = None, timeout: int = 20000,
                 resource_manager: pyvisa.ResourceManager | None = None,
                 connect: bool = True):
        """connect=False builds the object without touching the bus, for a caller
        that opens the instrument later from a worker thread."""
        self._given_rm = resource_manager       # if supplied, never closed by us
        self._rm = None
        self._timeout = timeout
        self.dev = None
        self.resource_name = ""
        self.idn = ""
        if connect:
            self.connect(resource)

    # ---------------------------------------------------------------- plumbing

    @property
    def connected(self) -> bool:
        return self.dev is not None

    @staticmethod
    def find(rm: pyvisa.ResourceManager | None = None) -> str:
        """Return the VISA resource string for the first 4060B-series unit."""
        rm = rm or pyvisa.ResourceManager()
        for name in rm.list_resources():
            if USB_ID in name:
                return name
        raise InstrumentError(
            "No B&K 4060B-series generator found. Resources seen: "
            + ", ".join(rm.list_resources())
        )

    def connect(self, resource: str | None = None) -> str:
        """Open the generator, check it answers as a 4063B, return its *IDN?.

        With no resource string: the generator's own VID/PID first, then anything
        else on USB, so an unexpected address still connects if what is behind it
        identifies correctly.
        """
        self.close()
        self._rm = self._given_rm or pyvisa.ResourceManager()
        if resource:
            candidates = [resource]
        else:
            seen = list(self._rm.list_resources())
            candidates = [r for r in seen if USB_ID in r]
            candidates += [r for r in seen
                           if r.startswith("USB") and r not in candidates]

        wrong = None
        for res in candidates:
            try:
                dev = self._rm.open_resource(res)
                dev.timeout = 5000                  # short while probing
                dev.read_termination = "\n"
                dev.write_termination = "\n"
                idn = dev.query("*IDN?").strip()
            except Exception:
                continue
            if "4063B" in idn:
                # Roomy once it is the right instrument: an arb upload is one
                # multi-megabyte transfer and the probe timeout will not carry it.
                dev.timeout = self._timeout
                dev.chunk_size = 1024 * 1024
                self.dev, self.idn, self.resource_name = dev, idn, res
                return idn
            wrong = wrong or (res, idn)
            try:
                dev.close()
            except Exception:
                pass

        self.close()
        if resource and wrong:
            raise InstrumentError(f"{wrong[0]} is not a 4063B: {wrong[1]}")
        raise InstrumentError(
            "No B&K 4063B found on USB. Check the rear-panel USB-B cable and that "
            "NI MAX still sees the generator. Resources seen: "
            + (", ".join(candidates) if candidates else "none"))

    def write(self, command: str) -> None:
        if self.dev is None:
            raise InstrumentError("not connected; call connect() first")
        self.dev.write(command)

    def query(self, command: str) -> str:
        if self.dev is None:
            raise InstrumentError("not connected; call connect() first")
        return self.dev.query(command).strip()

    def query_dict(self, command: str) -> dict:
        return parse_reply(self.query(command))

    def wait(self) -> None:
        """Block until the instrument finishes the preceding commands."""
        self.query("*OPC?")

    def reset(self) -> None:
        """*RST. Drops both outputs and returns to the power-on preset."""
        self.write("*RST")
        self.wait()

    def close(self) -> None:
        """Idempotent, and never raises: callers close on the way out of a failed
        connect as well as a good one."""
        dev, rm = self.dev, self._rm
        self.dev = self._rm = None
        for obj in (dev, rm if self._given_rm is None else None):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    @staticmethod
    def _ch(channel: int) -> str:
        if channel not in (1, 2):
            raise ValueError(f"channel must be 1 or 2, got {channel!r}")
        return f"C{channel}"

    def _set(self, channel: int, verb: str, params: dict) -> None:
        args = ",".join(f"{k},{_fmt(v)}" for k, v in params.items() if v is not None)
        if args:
            self.write(f"{self._ch(channel)}:{verb} {args}")

    # ------------------------------------------------------------------ output

    def output(self, channel: int, on: bool | None = None, load=None,
               polarity: str | None = None) -> dict:
        """Set and/or read the output switch, termination and polarity.

        load: 50 (or any 50..100000 ohm value) or "HZ" for high impedance.
              The generator scales its amplitude to the load you declare, so a
              mismatch here shows up as a 2x voltage error at the DUT.
        polarity: "NOR" or "INVT".
        """
        parts = []
        if on is not None:
            parts.append("ON" if on else "OFF")
        if load is not None:
            parts.append(f"LOAD,{_fmt(load)}")
        if polarity is not None:
            parts.append(f"PLRT,{polarity}")
        if parts:
            self.write(f"{self._ch(channel)}:OUTP {','.join(parts)}")
        return self.get_output(channel)

    def get_output(self, channel: int) -> dict:
        return self.query_dict(f"{self._ch(channel)}:OUTP?")

    def is_on(self, channel: int) -> bool:
        return self.get_output(channel).get("STATE") == "ON"

    def set_output(self, channel: int, on: bool) -> None:
        """Just the switch, with no readback - what the panel's ON/OFF buttons send."""
        self.write(f"{self._ch(channel)}:OUTP {'ON' if on else 'OFF'}")

    # -------------------------------------------------------------- basic wave

    def basic_wave(self, channel: int, **params) -> None:
        """Raw BSWV setter. Keys are the SCPI names (WVTP, FRQ, AMP, OFST, ...).

        Prefer the named helpers below; use this for parameters they do not cover.
        """
        self._set(channel, "BSWV", params)

    def get_basic_wave(self, channel: int) -> dict:
        return self.query_dict(f"{self._ch(channel)}:BSWV?")

    def sine(self, channel: int, freq=None, amp=None, offset=None, phase=None,
             high=None, low=None) -> None:
        self._set(channel, "BSWV", {"WVTP": "SINE", "FRQ": freq, "AMP": amp,
                                    "OFST": offset, "PHSE": phase,
                                    "HLEV": high, "LLEV": low})

    def square(self, channel: int, freq=None, amp=None, offset=None, phase=None,
               duty=None) -> None:
        self._set(channel, "BSWV", {"WVTP": "SQUARE", "FRQ": freq, "AMP": amp,
                                    "OFST": offset, "PHSE": phase, "DUTY": duty})

    def ramp(self, channel: int, freq=None, amp=None, offset=None, phase=None,
             symmetry=None) -> None:
        """symmetry: 0 = falling sawtooth, 50 = triangle, 100 = rising sawtooth."""
        self._set(channel, "BSWV", {"WVTP": "RAMP", "FRQ": freq, "AMP": amp,
                                    "OFST": offset, "PHSE": phase, "SYM": symmetry})

    def pulse(self, channel: int, freq=None, amp=None, offset=None, width=None,
              duty=None, rise=None, fall=None, delay=None) -> None:
        """width is the high time in seconds; rise/fall are edge times in seconds."""
        self._set(channel, "BSWV", {"WVTP": "PULSE", "FRQ": freq, "AMP": amp,
                                    "OFST": offset, "WIDTH": width, "DUTY": duty,
                                    "RISE": rise, "FALL": fall, "DLY": delay})

    def noise(self, channel: int, stdev=None, mean=None) -> None:
        self._set(channel, "BSWV", {"WVTP": "NOISE", "STDEV": stdev, "MEAN": mean})

    def dc(self, channel: int, offset=0.0) -> None:
        self._set(channel, "BSWV", {"WVTP": "DC", "OFST": offset})

    # --------------------------------------------------------------- arbitrary

    def arb(self, channel: int, name: str | None = None, index: int | None = None,
            freq=None, amp=None, offset=None, phase=None) -> None:
        """Select a stored arb by name (from list_waveforms) or built-in index,
        then optionally set its playback frequency/amplitude."""
        if name is not None:
            self.write(f"{self._ch(channel)}:ARWV NAME,{name}")
        elif index is not None:
            self.write(f"{self._ch(channel)}:ARWV INDEX,{int(index)}")
        self._set(channel, "BSWV", {"WVTP": "ARB", "FRQ": freq, "AMP": amp,
                                    "OFST": offset, "PHSE": phase})

    def get_arb(self, channel: int) -> dict:
        return self.query_dict(f"{self._ch(channel)}:ARWV?")

    def list_waveforms(self, user_only: bool = False) -> list[str]:
        """Names of stored waveforms: user-uploaded only, or the full built-in set."""
        raw = self.query("STL? USER" if user_only else "STL?")
        _, _, payload = raw.partition(" ")
        fields = [f.strip() for f in payload.split(",")]
        if user_only:
            # 'STL WVNM,wave1,wave2' -- drop the WVNM tag, keep the names.
            return [f for f in fields if f and f != "WVNM"]
        # 'STL M10, ExpFal, M100, ECG14' -- index/name pairs; keep the names.
        return fields[1::2]

    def sample_rate(self, channel: int, mode: str | None = None, value=None,
                    interpolation: str | None = None) -> dict:
        """DDS mode resamples the arb to hit an exact frequency; TARB (TrueArb)
        clocks the points out at a fixed sample rate instead.

        interpolation applies to TARB only: "LINE" or "HOLD".
        """
        self._set(channel, "SRATE", {"MODE": mode, "VALUE": value,
                                     "INTER": interpolation})
        return self.query_dict(f"{self._ch(channel)}:SRATE?")

    def upload_arb(self, channel: int, name: str, samples, freq=None, amp=None,
                   offset=None, phase=None, normalize: bool = True) -> int:
        """Upload a waveform to the generator user memory and select it.

        samples: an iterable of floats. With normalize=True they are scaled so
        the largest magnitude maps to full scale; pass normalize=False if the
        values are already in -1.0..+1.0 and you want the DAC headroom preserved
        exactly (values outside that range are clipped).

        Sent as signed 16-bit little-endian, which is what the 16-bit DAC in the
        4063B expects. Any count from 2 upwards works, odd included - measured,
        not assumed: 3, 7 and 101 points all store and read back at exactly
        their length.

        Returns the number of points stored.
        """
        if self.dev is None:
            raise InstrumentError("not connected; call connect() first")
        data = (np.asarray(samples, dtype=np.float64).ravel() if np is not None
                else [float(s) for s in samples])
        if len(data) < 2:
            raise ValueError("need at least 2 samples")
        data = dac_samples(data, normalize=normalize)

        if np is not None:
            codes = np.round(data * 32767).astype("<i2").tobytes()
        else:
            codes = b"".join(struct.pack("<h", int(round(v * 32767)))
                             for v in data)

        header = f"{self._ch(channel)}:WVDT WVNM,{name}"
        for key, value in (("FREQ", freq), ("AMPL", amp),
                           ("OFST", offset), ("PHASE", phase)):
            if value is not None:
                header += f",{key},{_fmt(value)}"
        header += ",WAVEDATA,"

        # One write_raw so the USBTMC END bit lands after the last data byte;
        # a trailing terminator here would be swallowed as waveform data.
        self.dev.write_raw(header.encode("ascii") + codes)
        self.wait()
        self.write(f"{self._ch(channel)}:ARWV NAME,{name}")
        return len(data)

    # -------------------------------------------------------------- modulation

    def modulation(self, channel: int, state: bool | None = None,
                   mod_type: str | None = None, **params) -> dict:
        """Amplitude/frequency/phase/pulse-width/shift-keying modulation.

        mod_type: AM, DSBAM, FM, PM, PWM, ASK, FSK or PSK.
        params are the SCPI keys for that type, e.g. for AM:
            SRC="INT", MDSP="SINE", FRQ=100, DEPTH=80

        Note the carrier constrains what is legal: PWM only applies to a PULSE
        carrier, and the rest need SINE/SQUARE/RAMP/ARB. Set the carrier with
        sine()/pulse()/etc. first, or the instrument quietly keeps its old type.
        """
        prefix = self._ch(channel)
        # STATE and the type tag must go in separate commands -- combining them
        # applies the state and silently discards the type switch.
        if state is not None:
            self.write(f"{prefix}:MDWV STATE,{'ON' if state else 'OFF'}")
        if mod_type is not None:
            parts = [mod_type]                 # bare type tag, no value
            for key, value in params.items():
                if value is not None:
                    parts.append(f"{key},{_fmt(value)}")
            self.write(f"{prefix}:MDWV {','.join(parts)}")
        elif params:
            raise ValueError("modulation params need mod_type so the instrument "
                             "knows which modulation they belong to")
        return self.query_dict(f"{prefix}:MDWV?")

    def modulation_off(self, channel: int) -> None:
        self.write(f"{self._ch(channel)}:MDWV STATE,OFF")

    # ------------------------------------------------------------------- sweep

    def sweep(self, channel: int, state: bool | None = None, time_s=None,
              start=None, stop=None, mode: str | None = None,
              direction: str | None = None, trigger: str | None = None,
              **params) -> dict:
        """mode: LINE or LOG. direction: UP or DOWN. trigger: INT, EXT or MAN."""
        fields = {"STATE": None if state is None else ("ON" if state else "OFF"),
                  "TIME": time_s, "START": start, "STOP": stop,
                  "SWMD": mode, "DIR": direction, "TRSR": trigger}
        fields.update(params)
        self._set(channel, "SWWV", fields)
        return self.query_dict(f"{self._ch(channel)}:SWWV?")

    def sweep_off(self, channel: int) -> None:
        self.write(f"{self._ch(channel)}:SWWV STATE,OFF")

    # ------------------------------------------------------------------- burst

    def burst(self, channel: int, state: bool | None = None, period=None,
              cycles=None, delay=None, mode: str | None = None,
              trigger: str | None = None, start_phase=None, **params) -> dict:
        """mode: "NCYC" (N-cycle) or "GATE". trigger: INT, EXT or MAN.

        cycles is the burst length in carrier cycles (NCYC mode).
        """
        fields = {"STATE": None if state is None else ("ON" if state else "OFF"),
                  "PRD": period, "TIME": cycles, "DLAY": delay,
                  "GATE_NCYC": mode, "TRSR": trigger, "STPS": start_phase}
        fields.update(params)
        self._set(channel, "BTWV", fields)
        return self.query_dict(f"{self._ch(channel)}:BTWV?")

    def burst_off(self, channel: int) -> None:
        self.write(f"{self._ch(channel)}:BTWV STATE,OFF")

    def trigger(self, channel: int) -> None:
        """Fire one manual burst trigger (needs TRSR,MAN)."""
        self.write(f"{self._ch(channel)}:BTWV MTRIG")

    # ------------------------------------------------------------------- misc

    def invert(self, channel: int, on: bool) -> None:
        self.write(f"{self._ch(channel)}:INVT {'ON' if on else 'OFF'}")

    def sync(self, channel: int, on: bool, source: str | None = None) -> dict:
        cmd = f"{self._ch(channel)}:SYNC {'ON' if on else 'OFF'}"
        if source:
            cmd += f",TYPE,{source}"
        self.write(cmd)
        return self.query_dict(f"{self._ch(channel)}:SYNC?")

    def align_phase(self) -> None:
        """Re-align the CH1/CH2 phase accumulators."""
        self.write("EQPHASE")

    def buzzer(self, on: bool) -> None:
        self.write(f"BUZZ {'ON' if on else 'OFF'}")

    def reference_clock(self, source: str | None = None) -> dict:
        """source: "INT" or "EXT" (10 MHz reference)."""
        if source:
            self.write(f"ROSC {source}")
        return self.query_dict("ROSC?")

    # --------------------------------------------------------- whole-channel
    def read_channel(self, channel: int) -> dict:
        """Every block the panel shows, as the instrument's own raw strings."""
        prefix = self._ch(channel)
        return {verb: self.query(f"{prefix}:{verb}?")
                for verb in ("OUTP", "BSWV", "ARWV", "SRATE",
                             "MDWV", "SWWV", "BTWV")}


    def apply_channel(self, ch: int, blocks: dict, log=lambda s: None) -> list:
        """Push one channel's settings, in the only order the 4063B accepts.

        `blocks` is {verb: {key: value}} holding just what should be written.
        The ordering is not cosmetic - the instrument silently overrides
        earlier writes with later ones:

          The declared load rescales amplitude, so it must precede BSWV -- set
            it after and a 1.5 Vpp request typed against HiZ silently becomes
            0.75 Vpp when the load is then changed to 50 ohm.
          ARWV and SRATE both force WVTP,ARB, so both must precede BSWV.
          BSWV clears any active modulation, so it must precede the mode block.

        Get this wrong and the commands are all accepted, with the caller and the
        generator quietly disagreeing about what is being output.

        Returns what did not take, which is nothing on a good day.
        """
        prefix = self._ch(ch)
        missed = []

        def send(command):
            self.write(command)
            log(f"  {command}")

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
        carrier_type = bswv.get("WVTP")
        # BSWV switches modulation, sweep and burst off - which is why setting
        # an amplitude used to take a burst with it. The mode blocks carry the
        # carrier's own settings for exactly this reason, so when everything
        # being changed can go that way, it does, and the mode never stops.
        # Anything CARR will not take still needs BSWV, and the mode block
        # below then puts the mode back.
        carried = {}
        active = blocks.get("MODE") and blocks["MODE"][0] != "Off"
        if bswv and active and set(bswv) <= CARR_KEYS:
            carried, bswv = bswv, {}
        if bswv:
            args = ",".join(f"{k},{v}" for k, v in bswv.items() if v != "")
            if args:
                send(f"{prefix}:BSWV {args}")

        if carrier_type == "ARB" and srate.get("MODE") == "TARB":
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
                args = ",".join(f"{k},{v}" for k, v in params.items() if v != "")
                carr = ",".join(f"{k},{v}" for k, v in carried.items() if v != "")
                if carr:
                    args = f"{args},CARR,{carr}" if args else f"CARR,{carr}"
                lead = f"{mode}," if verb == "MDWV" else ""
                if lead or args:
                    send(f"{prefix}:{verb} {lead}{args}")
                # Read it back rather than trusting the write. There is no
                # usable error channel on this generator: SYST:ERR? answers
                # every query with the same complaint about the query itself,
                # and *ESR? sets its command-error bit on every Cn: command
                # whether it worked or not - a plain BSWV AMP,1 sets it. The
                # state is the only thing that tells the truth.
                missed += self.mode_took(ch, verb, mode)
            else:
                log(f"  {prefix}: modulation, sweep and burst all off")
        return missed

    def mode_took(self, ch: int, verb: str, mode: str) -> list:
        """Empty if the mode really is on, or one line saying it is not."""
        try:
            state = parse_reply(self.query(f"{self._ch(ch)}:{verb}?")).get("STATE")
            if state == "ON":
                return []
            clock = parse_reply(self.query(f"{self._ch(ch)}:SRATE?")).get("MODE")
        except Exception:
            return []                 # a check that cannot run is not a failure
        why = (" - the sample clock is TrueArb, and this generator allows only "
               "modulation there. Set Clock to DDS on this channel."
               if clock == "TARB" and mode in TARB_BLOCKS else "")
        return [f"CH{ch} {mode} did not switch on{why}"]

    # ------------------------------------------------------- state save/restore

    def snapshot(self, channels=CHANNELS) -> dict:
        """Capture enough state to put the generator back the way you found it.

        Pass channels=(2,) to scope the snapshot -- restore() only rewrites what
        the snapshot holds, so scoping is how you guarantee a live channel is
        left alone.

        This is the shape the panel writes into awg_setups/*.json, string channel
        keys and all, so a setup saved by either one recalls in the other.
        """
        return {
            "captured": datetime.datetime.now().isoformat(timespec="seconds"),
            "instrument": self.idn,
            "address": self.resource_name,
            "channels": {str(ch): self.read_channel(ch) for ch in channels},
        }

    def restore(self, state: dict) -> None:
        """Replay a snapshot(). Outputs are switched last so nothing goes live
        mid-reconfiguration."""
        for ch, groups in state["channels"].items():
            ch = int(ch)
            prefix = self._ch(ch)
            outp = parse_reply(groups["OUTP"])

            # The declared load rescales amplitude, so it has to land before
            # BSWV - the same rule apply_channel spells out, and for the same
            # reason. Left out of the restore entirely, as it was, a 1.5 Vpp
            # snapshot taken into 50 ohm came back as 0.75 Vpp on a generator
            # sitting at HiZ, and the switch at the end of this loop then put
            # that on the output.
            terms = [f"{key},{_fmt(outp[key])}" for key in ("LOAD", "PLRT")
                     if outp.get(key) not in (None, "")]
            if terms:
                self.write(f"{prefix}:OUTP {','.join(terms)}")

            # Order is load-bearing. Selecting an arb forces WVTP,ARB, so ARWV
            # has to precede BSWV; writing BSWV clears any active modulation, so
            # it in turn has to precede the mode blocks.
            arb = parse_reply(groups["ARWV"])
            if arb.get("INDEX") is not None:
                # Built-in waveforms only reload by index; NAME silently no-ops.
                self.write(f"{prefix}:ARWV INDEX,{int(arb['INDEX'])}")
            elif arb.get("NAME"):
                self.write(f"{prefix}:ARWV NAME,{arb['NAME']}")

            srate = parse_reply(groups["SRATE"])
            self._set(ch, "SRATE", srate)

            carrier = parse_reply(groups["BSWV"])
            for key in ("PERI", "MAX_OUTPUT_AMP", "HLEV", "LLEV", "AMPVRMS"):
                carrier.pop(key, None)          # readback-only, rejected on write
            self._set(ch, "BSWV", carrier)

            if carrier.get("WVTP") == "ARB" and srate.get("MODE") == "TARB":
                # TrueArb quantizes its clock against the loaded waveform, so the
                # pre-BSWV write lands a few ppm off. Re-asserting it afterwards
                # fixes that, and is only safe here because SRATE forces WVTP,ARB
                # -- which is what this channel is already set to.
                self._set(ch, "SRATE", srate)

            # Modulation, sweep and burst are mutually exclusive; clear all three,
            # then re-enable whichever one was actually running.
            modes = {v: parse_reply(groups[v]) for v in ("MDWV", "SWWV", "BTWV")}
            for verb in modes:
                self.write(f"{prefix}:{verb} STATE,OFF")
            for verb, params in modes.items():
                if params.get("STATE") != "ON":
                    continue
                self.write(f"{prefix}:{verb} STATE,ON")
                params.pop("STATE", None)
                params.pop("CARR", None)        # restored by the BSWV pass above
                params.pop("TRMD", None)        # readback-only
                if params.get("MARK_STATE") == "OFF":
                    # An idle marker reads back as 0 Hz but clamps to START on
                    # the way in, which would make the restore inexact.
                    params.pop("MARK_FREQ", None)
                mod_type = params.pop("TYPE", None)
                args = ",".join(f"{k},{_fmt(v)}" for k, v in params.items()
                                if v is not None)
                lead = f"{mod_type}," if mod_type else ""
                if args:
                    self.write(f"{prefix}:{verb} {lead}{args}")

            self.write(f"{prefix}:OUTP {outp.get('STATE', 'OFF')}")


if __name__ == "__main__":
    with BK4063B() as awg:
        print("Connected:", awg.idn)
        print("Resource: ", awg.resource_name)
        for ch in (1, 2):
            out = awg.get_output(ch)
            wave = awg.get_basic_wave(ch)
            print(f"  CH{ch}: output={out.get('STATE')} load={out.get('LOAD')} "
                  f"| {wave.get('WVTP')} {wave.get('FRQ')} Hz "
                  f"{wave.get('AMP')} Vpp offset {wave.get('OFST')} V")
        print("User waveforms:", awg.list_waveforms(user_only=True))
