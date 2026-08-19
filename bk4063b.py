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

Requires: pyvisa + a VISA runtime (NI-VISA is installed on this machine).
"""

from __future__ import annotations

import struct

import pyvisa

__all__ = ["BK4063B", "InstrumentError"]

# USB vendor/product for the B&K 4060B series.
_USB_ID = "0xF4EC::0xEE38"


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


# Modulation types appear as a bare tag with no value, mid-response.
_BARE_TAGS = {"AM", "DSBAM", "FM", "PM", "PWM", "ASK", "FSK", "PSK"}


def _parse(response: str) -> dict:
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
            out["CARR"] = _parse("CARR " + ",".join(fields[i + 1:]))
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

    def __init__(self, resource: str | None = None, timeout: int = 5000,
                 resource_manager: pyvisa.ResourceManager | None = None):
        self._rm = resource_manager or pyvisa.ResourceManager()
        self._owns_rm = resource_manager is None
        self.resource_name = resource or self.find(self._rm)
        self.dev = self._rm.open_resource(self.resource_name)
        self.dev.timeout = timeout
        self.dev.read_termination = "\n"
        self.dev.write_termination = "\n"
        self.idn = self.query("*IDN?")
        if "4063B" not in self.idn:
            raise InstrumentError(f"{self.resource_name} is not a 4063B: {self.idn}")

    # ---------------------------------------------------------------- plumbing

    @staticmethod
    def find(rm: pyvisa.ResourceManager | None = None) -> str:
        """Return the VISA resource string for the first 4060B-series unit."""
        rm = rm or pyvisa.ResourceManager()
        for name in rm.list_resources():
            if _USB_ID in name:
                return name
        raise InstrumentError(
            "No B&K 4060B-series generator found. Resources seen: "
            + ", ".join(rm.list_resources())
        )

    def write(self, command: str) -> None:
        self.dev.write(command)

    def query(self, command: str) -> str:
        return self.dev.query(command).strip()

    def query_dict(self, command: str) -> dict:
        return _parse(self.query(command))

    def wait(self) -> None:
        """Block until the instrument finishes the preceding commands."""
        self.query("*OPC?")

    def reset(self) -> None:
        """*RST. Drops both outputs and returns to the power-on preset."""
        self.write("*RST")
        self.wait()

    def close(self) -> None:
        try:
            self.dev.close()
        finally:
            if self._owns_rm:
                self._rm.close()

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
                   offset=None, phase=None, normalize: bool = True) -> None:
        """Upload a waveform to the generator user memory and select it.

        samples: an iterable of floats. With normalize=True they are scaled so
        the largest magnitude maps to full scale; pass normalize=False if the
        values are already in -1.0..+1.0 and you want the DAC headroom preserved
        exactly (values outside that range are clipped).

        Sent as signed 16-bit little-endian, which is what the 16-bit DAC in the
        4063B expects. Point count must be even.
        """
        data = [float(s) for s in samples]
        if len(data) < 2:
            raise ValueError("need at least 2 samples")
        if len(data) % 2:
            raise ValueError(f"point count must be even, got {len(data)}")

        if normalize:
            peak = max(abs(min(data)), abs(max(data)))
            if peak == 0:
                raise ValueError("all samples are zero; nothing to normalize")
            data = [s / peak for s in data]

        codes = bytearray()
        for s in data:
            code = int(round(max(-1.0, min(1.0, s)) * 32767))
            codes += struct.pack("<h", code)

        header = f"{self._ch(channel)}:WVDT WVNM,{name}"
        for key, value in (("FREQ", freq), ("AMPL", amp),
                           ("OFST", offset), ("PHASE", phase)):
            if value is not None:
                header += f",{key},{_fmt(value)}"
        header += ",WAVEDATA,"

        # One write_raw so the USBTMC END bit lands after the last data byte;
        # a trailing terminator here would be swallowed as waveform data.
        self.dev.write_raw(header.encode("ascii") + bytes(codes))
        self.wait()
        self.write(f"{self._ch(channel)}:ARWV NAME,{name}")

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

    # ------------------------------------------------------- state save/restore

    def snapshot(self, channels=(1, 2)) -> dict:
        """Capture enough state to put the generator back the way you found it.

        Pass channels=(2,) to scope the snapshot -- restore() only rewrites what
        the snapshot holds, so scoping is how you guarantee a live channel is
        left alone.
        """
        state = {"idn": self.idn, "channels": {}}
        for ch in channels:
            prefix = self._ch(ch)
            state["channels"][ch] = {
                "OUTP": self.query(f"{prefix}:OUTP?"),
                "BSWV": self.query(f"{prefix}:BSWV?"),
                "ARWV": self.query(f"{prefix}:ARWV?"),
                "SRATE": self.query(f"{prefix}:SRATE?"),
                "MDWV": self.query(f"{prefix}:MDWV?"),
                "SWWV": self.query(f"{prefix}:SWWV?"),
                "BTWV": self.query(f"{prefix}:BTWV?"),
            }
        return state

    def restore(self, state: dict) -> None:
        """Replay a snapshot(). Outputs are switched last so nothing goes live
        mid-reconfiguration."""
        for ch, groups in state["channels"].items():
            ch = int(ch)
            prefix = self._ch(ch)

            # Order is load-bearing. Selecting an arb forces WVTP,ARB, so ARWV
            # has to precede BSWV; writing BSWV clears any active modulation, so
            # it in turn has to precede the mode blocks.
            arb = _parse(groups["ARWV"])
            if arb.get("INDEX") is not None:
                # Built-in waveforms only reload by index; NAME silently no-ops.
                self.write(f"{prefix}:ARWV INDEX,{int(arb['INDEX'])}")
            elif arb.get("NAME"):
                self.write(f"{prefix}:ARWV NAME,{arb['NAME']}")

            srate = _parse(groups["SRATE"])
            self._set(ch, "SRATE", srate)

            carrier = _parse(groups["BSWV"])
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
            modes = {v: _parse(groups[v]) for v in ("MDWV", "SWWV", "BTWV")}
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

            self.write(f"{prefix}:OUTP {_parse(groups['OUTP']).get('STATE', 'OFF')}")


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
