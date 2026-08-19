#!/usr/bin/env python3
"""
Generate a Blackman-windowed 100 kHz burst for the 4063B.

10 ms of 100 kHz carrier under a Blackman envelope: 1000 carrier cycles, the
window tapering to zero at both ends so the record joins onto itself cleanly
when it repeats.

The sample rate is a choice about resolution, not about the output: the file
carries shape only. What sets the real timing is the Clock setting on the
channel - see the numbers this script prints.
"""

import numpy as np

CARRIER_HZ = 100e3        # tone inside the envelope
DURATION_S = 10e-3        # length of the whole windowed burst
SAMPLE_RATE = 5e6         # 50 samples per carrier cycle
NAME = "blackman_100kHz_10ms"

n_points = int(round(DURATION_S * SAMPLE_RATE))
if n_points % 2:                       # the generator rejects odd point counts
    n_points += 1

t = np.arange(n_points) / SAMPLE_RATE
carrier = np.sin(2 * np.pi * CARRIER_HZ * t)
envelope = np.blackman(n_points)
samples = envelope * carrier           # peak is ~1.0, so it needs no scaling

np.savetxt(f"{NAME}.csv", np.column_stack([t, samples]),
           delimiter=",", header="time_s,volts", comments="", fmt="%.9g")
np.savetxt(f"{NAME}.txt", samples, fmt="%.9g")
np.save(f"{NAME}.npy", samples)

print(f"points           : {n_points}")
print(f"duration         : {DURATION_S * 1e3:g} ms")
print(f"sample rate      : {SAMPLE_RATE / 1e6:g} MSa/s")
print(f"carrier          : {CARRIER_HZ / 1e3:g} kHz")
print(f"carrier cycles   : {CARRIER_HZ * DURATION_S:g}")
print(f"samples / cycle  : {SAMPLE_RATE / CARRIER_HZ:g}")
print(f"sample range     : {samples.min():+.6f} to {samples.max():+.6f}")
print()
print("To play it back at its true 10 ms length, either:")
print(f"  Clock = TrueArb, Sa/s = {SAMPLE_RATE:g}      (period = points / rate)")
print(f"  Clock = DDS,     Freq  = {1 / DURATION_S:g} Hz   (whole record = one period)")
