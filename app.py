"""SongSplitter web server.

Run with:  .venv\\Scripts\\python.exe app.py   (or just double-click run.bat)
"""
import io
import json
import os
import queue
import re
import shutil
import threading
import time
import traceback
import uuid
import webbrowser
import zipfile
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import downloader
import separator

ROOT = Path(__file__).parent
JOBS_DIR = Path(os.environ.get("SONGSPLITTER_JOBS_DIR", ROOT / "jobs"))
JOBS_DIR.mkdir(parents=True, exist_ok=True)
HOST, PORT = "127.0.0.1", int(os.environ.get("SONGSPLITTER_PORT", 8765))

app = FastAPI(title="SongSplitter")
jobs = {}
jobs_lock = threading.Lock()
work_queue = queue.Queue()


def save_job(job):
    (JOBS_DIR / job["id"] / "job.json").write_text(json.dumps(job, indent=1))


def load_jobs():
    for leftover in JOBS_DIR.glob(".deleting-*"):
        shutil.rmtree(leftover, ignore_errors=True)
    for meta in JOBS_DIR.glob("[!.]*/job.json"):
        try:
            job = json.loads(meta.read_text())
        except (OSError, ValueError):
            continue
        if job.get("status") in ("queued", "running"):
            job.update(status="error", error="Interrupted (server was stopped)")
        jobs[job["id"]] = job


def update(job, **fields):
    with jobs_lock:
        job.update(fields)
    if fields.get("status") in ("done", "error"):
        save_job(job)


engine = {"ready": False}


def worker():
    try:
        separator.warmup()
    except Exception:
        traceback.print_exc()
    engine["ready"] = True
    while True:
        job_id = work_queue.get()
        job = jobs.get(job_id)
        if not job:
            continue
        job_dir = JOBS_DIR / job_id
        update(job, status="running", progress=0.0, message="Starting...")
        started = time.time()
        try:
            # Link jobs: download first (0-10% of the bar), then separate (10-100%).
            span = (0.0, 1.0)
            if not job["input"]:
                got = downloader.download(
                    job["source"], job_dir,
                    on_progress=lambda f, m: update(job, progress=round(0.1 * f, 3), message=m))
                update(job, input=got["file"], name=got["title"])
                save_job(job)
                span = (0.1, 0.9)
            result = separator.separate(
                job_dir / job["input"], job_dir, job["mode"], job["format"],
                on_progress=lambda f, m: update(job, progress=round(span[0] + span[1] * f, 3), message=m),
            )
            update(job, status="done", progress=1.0, message="Done",
                   stems=result["stems"], duration=result["duration"],
                   elapsed=round(time.time() - started, 1))
        except Exception as exc:  # report any failure to the UI
            traceback.print_exc()
            update(job, status="error", error=str(exc))


def public(job):
    with jobs_lock:
        data = dict(job)
    if data["status"] == "queued":
        data["queue_position"] = list(work_queue.queue).index(job["id"]) + 1 \
            if job["id"] in work_queue.queue else 1
    return data


def get_job(job_id):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/api/info")
def info():
    return {"device": separator.DEVICE_NAME, "gpu": separator._device != "cpu",
            "ready": engine["ready"]}


@app.get("/api/modes")
def modes():
    return [{"id": k, "label": v[1], "description": v[2]} for k, v in separator.MODES.items()]


def check_options(mode, format):
    if mode not in separator.MODES:
        raise HTTPException(400, "Unknown mode")
    if format not in ("mp3", "wav"):
        raise HTTPException(400, "Format must be mp3 or wav")


def new_job(name, mode, format, input_name=None, source=None):
    job_id = uuid.uuid4().hex[:12]
    (JOBS_DIR / job_id).mkdir()
    job = {
        "id": job_id, "name": name, "input": input_name, "source": source,
        "mode": mode, "format": format, "status": "queued", "progress": 0.0,
        "message": "Waiting in queue...", "created": time.time(), "stems": [],
    }
    return job


def enqueue(job):
    jobs[job["id"]] = job
    save_job(job)
    work_queue.put(job["id"])
    return public(job)


@app.post("/api/split")
async def split(file: UploadFile = File(...), mode: str = Form("4stems"),
                format: str = Form("mp3")):
    check_options(mode, format)
    ext = Path(file.filename or "song").suffix.lower()[:8] or ".audio"
    input_name = "input" + re.sub(r"[^.a-z0-9]", "", ext)
    job = new_job(Path(file.filename or "song").stem, mode, format, input_name=input_name)
    with open(JOBS_DIR / job["id"] / input_name, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return enqueue(job)


@app.post("/api/split_url")
def split_url(url: str = Form(...), mode: str = Form("4stems"), format: str = Form("mp3")):
    check_options(mode, format)
    url = url.strip()
    if not downloader.is_url(url):
        raise HTTPException(400, "Please paste a full link starting with http:// or https://")
    return enqueue(new_job(url, mode, format, source=url))


@app.get("/api/jobs")
def list_jobs():
    return sorted((public(j) for j in jobs.values()), key=lambda j: -j["created"])


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    return public(get_job(job_id))


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    job = get_job(job_id)
    if job["status"] in ("queued", "running"):
        raise HTTPException(409, "Job is still processing")
    # Rename first: on Windows this fails while any file inside is open, so a
    # delete either removes the whole job or nothing (never a half-deleted job).
    trash = JOBS_DIR / f".deleting-{job_id}"
    try:
        (JOBS_DIR / job_id).rename(trash)
    except FileNotFoundError:
        pass
    except OSError:
        raise HTTPException(409, "Some files of this song are in use. Close any player using them and try again.")
    jobs.pop(job_id, None)
    shutil.rmtree(trash, ignore_errors=True)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/stems/{filename}")
def stem_file(job_id: str, filename: str, download: bool = False):
    job = get_job(job_id)
    if filename not in {s["file"] for s in job.get("stems", [])}:
        raise HTTPException(404, "Stem not found")
    name = f"{job['name']} - {filename}" if download else None
    return FileResponse(JOBS_DIR / job_id / filename, filename=name)


@app.get("/api/jobs/{job_id}/zip")
def stems_zip(job_id: str):
    job = get_job(job_id)
    if job["status"] != "done":
        raise HTTPException(409, "Job is not finished")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for stem in job["stems"]:
            zf.write(JOBS_DIR / job_id / stem["file"], f"{job['name']} - {stem['file']}")
    buf.seek(0)
    name = quote(f"{job['name']} - stems.zip")
    return StreamingResponse(buf, media_type="application/zip", headers={
        "Content-Disposition": f"attachment; filename=\"stems.zip\"; filename*=UTF-8''{name}"})


@app.middleware("http")
async def no_stale_ui(request, call_next):
    # Make the browser re-check the page on every load so app updates show up.
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")


def port_in_use():
    import socket
    with socket.socket() as s:
        return s.connect_ex((HOST, PORT)) == 0


if __name__ == "__main__":
    if port_in_use():
        print(f"\n  SongSplitter is already running at http://{HOST}:{PORT}")
        print("  Close the other SongSplitter window first, then start this again,")
        print("  otherwise you keep using the old version.\n")
        raise SystemExit(1)
    load_jobs()
    threading.Thread(target=worker, daemon=True).start()
    url = f"http://{HOST}:{PORT}"
    print(f"\n  SongSplitter running at {url}  (Ctrl+C to stop)")
    print(f"  Processing on: {separator.DEVICE_NAME}\n")
    if not os.environ.get("SONGSPLITTER_NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
