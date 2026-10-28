"""Fetch the audio track from a link (YouTube, SoundCloud, Bandcamp, ...) via yt-dlp."""
import re
import shutil
from pathlib import Path

import imageio_ffmpeg
import yt_dlp
from yt_dlp.utils import DownloadError

MAX_DURATION = 30 * 60  # seconds; guards against hour-long mixes/streams by accident

# YouTube needs a JavaScript runtime to unlock all formats. yt-dlp only looks for
# deno by default, so point it at whichever runtime is installed.
_JS_RUNTIMES = {name: {} for name in ("deno", "node", "bun") if shutil.which(name)}


def is_url(text):
    return bool(re.match(r"^https?://\S+$", text.strip()))


def _clean_error(exc):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
    msg = re.sub(r"^ERROR:\s*", "", msg).strip()
    if "Sign in to confirm" in msg or "age" in msg.lower() and "restricted" in msg.lower():
        return "This video requires signing in (age-restricted or private), so it can't be downloaded."
    if "Unsupported URL" in msg:
        return "This link isn't supported. Try a direct video/track page (YouTube, SoundCloud, Bandcamp, ...)."
    if "Video unavailable" in msg or "Private video" in msg:
        return "That video is unavailable (removed, private or blocked in your region)."
    status = re.search(r"HTTP Error (\d+)", msg)
    if status:
        return f"Couldn't open that link (the site answered HTTP {status.group(1)}). Check the link is correct."
    msg = re.sub(r"^\[[\w:]+\]\s*[\w-]*:\s*", "", msg)   # "[youtube] abc123: " prefix
    msg = re.sub(r"\s*\(caused by .*\)$", "", msg)       # Python exception repr suffix
    return msg[:400]


def download(url, out_dir, on_progress=None):
    """Download the best audio stream of `url` into out_dir/input.<ext>.

    Returns {"file": filename, "title": str}. Raises RuntimeError with a
    user-friendly message on failure.
    """
    report = on_progress or (lambda f, m: None)
    out_dir = Path(out_dir)

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                frac = min(d.get("downloaded_bytes", 0) / total, 1.0)
                report(frac, f"Downloading audio... {int(frac * 100)}%")
            else:
                report(0.5, "Downloading audio...")

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / "input.%(ext)s"),
        "noplaylist": True,
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
    }
    if _JS_RUNTIMES:
        opts["js_runtimes"] = _JS_RUNTIMES

    report(0.0, "Looking up link...")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("_type") == "playlist":
                entries = [e for e in info.get("entries") or [] if e]
                if not entries:
                    raise RuntimeError("That playlist is empty.")
                info = entries[0]
            if info.get("is_live"):
                raise RuntimeError("Live streams can't be split. Try again once the stream has ended.")
            duration = info.get("duration") or 0
            if duration > MAX_DURATION:
                raise RuntimeError(f"That's {duration // 60} minutes long. The limit is "
                                   f"{MAX_DURATION // 60} minutes per song.")
            info = ydl.process_ie_result(info, download=True)
    except DownloadError as exc:
        raise RuntimeError(_clean_error(exc)) from None

    downloads = info.get("requested_downloads") or []
    path = Path(downloads[0]["filepath"]) if downloads else None
    if not path or not path.exists():
        matches = list(out_dir.glob("input.*"))
        if not matches:
            raise RuntimeError("Download finished but no audio file was found.")
        path = matches[0]
    return {"file": path.name, "title": info.get("title") or "Untitled"}
