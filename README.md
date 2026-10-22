# SongSplitter

A local, private version of splitmysong.com: upload a song and AI splits it into
separate stems (vocals, drums, bass, guitar, piano, other) that you can mix,
solo, mute and download.

Powered by [Demucs](https://github.com/facebookresearch/demucs) (Hybrid Transformer Demucs by Meta AI).
Everything runs on your machine, and nothing is uploaded anywhere.

## Run it

Double-click **`run.bat`**. On first run it creates a Python virtual environment
and installs dependencies (a few minutes). Then it opens http://127.0.0.1:8765
in your browser.

The first split with each mode also downloads that AI model (~80 MB, or ~320 MB for HQ).

## Modes

| Mode        | Stems                                     | Model          |
|-------------|-------------------------------------------|----------------|
| 2 stems     | vocals, instrumental                      | `htdemucs`     |
| 4 stems     | vocals, drums, bass, other                | `htdemucs`     |
| 6 stems     | vocals, drums, bass, guitar, piano, other | `htdemucs_6s`  |
| 4 stems HQ  | vocals, drums, bass, other (best quality) | `htdemucs_ft`  |

## GPU acceleration (AMD Radeon RX 7800 XT)

This machine runs on the GPU through AMD ROCm. `run.bat` uses the `.venv-gpu`
environment when it exists and falls back to the CPU-only `.venv` otherwise.
The page header shows which device is in use.

Measured on a 2-minute song:

| Mode       | CPU      | RX 7800 XT |
|------------|----------|------------|
| 4 stems    | ~50–75 s | ~11 s      |
| 6 stems    | ~48 s    | ~12 s      |
| 4 stems HQ | (slow)   | ~34 s      |

When the server starts, it warms up the GPU (tens of seconds) so the first song is fast too.

- Force CPU mode: `run.bat cpu`
- Rebuild the GPU environment:
  ```
  python -m venv .venv-gpu
  .venv-gpu\Scripts\python.exe -m pip install --index-url https://stable.repo.amd.com/rocm/whl-next/ "torch[device-gfx1101]" torchaudio
  .venv-gpu\Scripts\python.exe -m pip install demucs fastapi "uvicorn[standard]" python-multipart soundfile imageio-ffmpeg
  ```
  `gfx1101` is the RX 7800 XT / 7700 XT. Use `gfx1100` for a 7900-series card or `gfx1201` for a 9070-series card.
- NVIDIA cards use the CUDA build instead: `--index-url https://download.pytorch.org/whl/cu128 torch torchaudio`.
- Keep the AMD Adrenalin driver up to date. ROCm on Windows is still fairly new.

## Files

- `app.py`: web server (FastAPI) and job queue
- `separator.py`: Demucs separation plus ffmpeg decode/encode
- `static/index.html`: UI (upload, progress, multitrack mixer)
- `jobs/`: uploaded songs and generated stems (delete from the UI or remove the folder)
