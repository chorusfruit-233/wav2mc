from pathlib import Path

import numpy as np
import soundfile as sf

SAMPLE_RATE = 48_000
DURATION = 4.0

samples = int(SAMPLE_RATE * DURATION)
time = np.arange(samples) / SAMPLE_RATE
fade = np.minimum(np.clip(time / 0.05, 0, 1), np.clip((DURATION - time) / 0.05, 0, 1))

signal = (
    0.48 * np.sin(2 * np.pi * 440 * time)
    + 0.25 * np.sin(2 * np.pi * 660 * time + 0.4)
    + 0.12 * np.sin(2 * np.pi * 880 * time + 1.0)
)
signal *= fade

output = Path("test_tone.wav")
sf.write(output, signal.astype(np.float32), SAMPLE_RATE, subtype="PCM_16")
print(output.resolve())
