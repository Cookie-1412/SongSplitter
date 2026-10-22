"""Stem separation engine built on Demucs.

Audio is decoded/encoded with a bundled ffmpeg (imageio-ffmpeg), so no system
ffmpeg install is needed and any common format (mp3, wav, flac, m4a, ogg...) works.
"""
import os
import subprocess
import threading
import warnings
from pathlib import Path
from types import SimpleNamespace

# AMD ROCm: silence MIOpen's noisy kernel-tuning warnings (errors still show).
os.environ.setdefault("MIOPEN_LOG_LEVEL", "2")
warnings.filterwarnings("ignore", message=".*attention on Current AMD GPU is still experimental.*")

import imageio_ffmpeg
import numpy as np
import torch
import demucs.apply
from demucs.apply import BagOfModels, apply_model
from demucs.pretrained import get_model

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# mode id -> (demucs model name, label, description)
MODES = {
    "2stems": ("htdemucs", "2 stems", "Vocals + Instrumental"),
    "4stems": ("htdemucs", "4 stems", "Vocals, Drums, Bass, Other"),
    "6stems": ("htdemucs_6s", "6 stems", "Vocals, Drums, Bass, Guitar, Piano, Other"),
    "4stems_hq": ("htdemucs_ft", "4 stems HQ", "Best quality, ~4x slower"),
}

_models = {}
_models_lock = threading.Lock()
_device = "cuda" if torch.cuda.is_available() else "cpu"
DEVICE_NAME = torch.cuda.get_device_name(0) if _device == "cuda" else "CPU"


def load_model(name):
    with _models_lock:
        if name not in _models:
            model = get_model(name)
            model.to(_device).eval()
            _models[name] = model
        return _models[name]


def warmup(name="htdemucs"):
    """Load the default model and run it once on silence.

    On a GPU the first run compiles/tunes kernels (tens of seconds), so doing it
    at startup makes the user's first song as fast as later ones.
    """
    model = load_model(name)
    with torch.no_grad():
        apply_model(model, torch.zeros(1, model.audio_channels, model.samplerate * 8),
                    device=_device, split=True, overlap=0.25)


# --- progress reporting -----------------------------------------------------
# demucs.apply wraps its chunk loop in tqdm.tqdm(...) when progress=True. We
# swap in a tracker that forwards progress to a callback. Jobs run one at a
# time on a single worker thread, so a module-level callback is safe.
_progress_cb = None


class _Tracker:
    def __init__(self, iterable, *args, **kwargs):
        self.iterable = list(iterable)

    def __iter__(self):
        total = len(self.iterable)
        for i, item in enumerate(self.iterable):
            yield item
            if _progress_cb:
                _progress_cb(i + 1, total)


demucs.apply.tqdm = SimpleNamespace(tqdm=_Tracker)


# --- audio I/O --------------------------------------------------------------
def decode(path, samplerate, channels=2):
    """Decode any audio file to a float32 array of shape (channels, samples)."""
    cmd = [FFMPEG, "-v", "error", "-i", str(path), "-vn",
           "-f", "f32le", "-ac", str(channels), "-ar", str(samplerate), "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("Could not read audio file: "
                           + proc.stderr.decode(errors="ignore").strip()[-300:])
    return np.frombuffer(proc.stdout, dtype=np.float32).reshape(-1, channels).T.copy()


def encode(audio, samplerate, out_path, fmt):
    """Write a (channels, samples) float array to mp3 or wav."""
    audio = np.clip(audio, -1.0, 1.0).astype(np.float32)
    channels = audio.shape[0]
    codec = ["-c:a", "libmp3lame", "-b:a", "320k"] if fmt == "mp3" else ["-c:a", "pcm_s16le"]
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "f32le", "-ac", str(channels),
           "-ar", str(samplerate), "-i", "-", *codec, str(out_path)]
    proc = subprocess.run(cmd, input=audio.T.tobytes(), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("Could not write audio: " + proc.stderr.decode(errors="ignore")[-300:])


def peaks(audio, n=800):
    """Downsampled peak envelope (0..1) for drawing waveforms."""
    mono = np.abs(audio).max(axis=0)
    if mono.size == 0:
        return []
    edges = np.linspace(0, mono.size, n + 1).astype(int)
    vals = np.array([mono[a:b].max() if b > a else 0.0 for a, b in zip(edges[:-1], edges[1:])])
    return np.round(np.clip(vals, 0, 1), 3).tolist()


# --- separation -------------------------------------------------------------
def separate(in_path, out_dir, mode="4stems", fmt="mp3", on_progress=None):
    """Split a song into stems. Returns a list of {name, file, peaks} dicts.

    on_progress(fraction, message) is called as work proceeds.
    """
    global _progress_cb
    report = on_progress or (lambda f, m: None)
    model_name = MODES[mode][0]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    report(0.01, "Loading AI model (first run downloads it)...")
    model = load_model(model_name)
    sr = model.samplerate

    report(0.04, "Decoding audio...")
    wav = torch.from_numpy(decode(in_path, sr, model.audio_channels))
    duration = wav.shape[1] / sr

    # Normalize like the demucs CLI does.
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    wav = (wav - mean) / std

    n_models = len(model.models) if isinstance(model, BagOfModels) else 1
    state = {"done": 0, "last": 0}

    def tick(i, total):
        frac = (state["done"] + i / total) / n_models
        if i == total:
            state["done"] += 1
        report(0.06 + 0.84 * frac, f"Separating stems... {int(frac * 100)}%")

    _progress_cb = tick
    try:
        with torch.no_grad():
            sources = apply_model(model, wav[None], device=_device, shifts=1,
                                  split=True, overlap=0.25, progress=True)[0]
    finally:
        _progress_cb = None

    sources = (sources * std + mean).cpu().numpy()
    stems = dict(zip(model.sources, sources))

    if mode == "2stems":
        vocals = stems.pop("vocals")
        stems = {"vocals": vocals, "instrumental": sum(stems.values())}

    order = ["vocals", "instrumental", "drums", "bass", "guitar", "piano", "other"]
    stems = dict(sorted(stems.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99))

    results = []
    for i, (name, audio) in enumerate(stems.items()):
        report(0.90 + 0.09 * i / len(stems), f"Saving {name}...")
        filename = f"{name}.{fmt}"
        encode(audio, sr, out_dir / filename, fmt)
        results.append({"name": name, "file": filename, "peaks": peaks(audio)})

    report(1.0, "Done")
    return {"stems": results, "duration": duration}
