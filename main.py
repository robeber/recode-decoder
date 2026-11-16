import ipaddress
import os
import re
import secrets
import socket
import subprocess
import tempfile
import threading

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from fastapi.responses import FileResponse
import imageio_ffmpeg

from io import BytesIO
from urllib.parse import urlparse
import ipaddress
import socket

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, ImageOps, UnidentifiedImageError

app = FastAPI(title="RECODE Image Converter", version="1.0.0")

# RECODE can call this API directly. The converter only fetches Postimages URLs.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

ALLOWED_HOSTS = {"i.postimg.cc, rule34. xxx, api.rule34.xxx, wimg.rule34.xxx"}
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
MAX_SOURCE_PIXELS = 16_000_000
MAX_OUTPUT_SIDE = 96

Image.MAX_IMAGE_PIXELS = MAX_SOURCE_PIXELS


def validate_source_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise HTTPException(
            status_code=400,
            detail="Only HTTPS image URLs from i.postimg.cc are accepted.",
        )
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise HTTPException(status_code=400, detail="Invalid image URL.")

    # Reject unusual DNS results as an extra SSRF safeguard.
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        for item in addresses:
            ip = ipaddress.ip_address(item[4][0])
            if not ip.is_global:
                raise HTTPException(status_code=400, detail="Image host did not resolve publicly.")
    except socket.gaierror:
        raise HTTPException(status_code=502, detail="Could not resolve the image host.")


def download_image(url: str) -> Image.Image:
    validate_source_url(url)
    try:
        with requests.get(
            url,
            headers={"User-Agent": "RECODE-Image-Converter/1.0"},
            timeout=(8, 15),
            stream=True,
            allow_redirects=False,
        ) as response:
            if response.is_redirect:
                raise HTTPException(status_code=400, detail="Image redirects are not accepted.")
            response.raise_for_status()

            content_type = response.headers.get("content-type", "").split(";")[0].lower()
            if content_type not in {"image/jpeg", "image/png"}:
                raise HTTPException(status_code=415, detail="Source must be a JPG or PNG.")

            length = response.headers.get("content-length")
            if length and int(length) > MAX_DOWNLOAD_BYTES:
                raise HTTPException(status_code=413, detail="Image is too large (5 MB maximum).")

            chunks = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    chunks.extend(chunk)
                    if len(chunks) > MAX_DOWNLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="Image is too large (5 MB maximum).")

        image = Image.open(BytesIO(chunks))
        if image.format not in {"JPEG", "PNG"}:
            raise HTTPException(status_code=415, detail="Source must be a JPG or PNG.")
        image.load()
        if image.width * image.height > MAX_SOURCE_PIXELS:
            raise HTTPException(status_code=413, detail="Source image dimensions are too large.")
        return image
    except HTTPException:
        raise
    except requests.Timeout:
        raise HTTPException(status_code=504, detail="Image host took too long to respond.")
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Could not download the image.")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(status_code=400, detail="The URL did not contain a valid JPG or PNG.")


@app.get("/")
def home():
    return {
        "service": "RECODE Image Converter",
        "usage": "/convert?url=POSTIMAGES_IMAGE_URL&width=48&colors=32",
        "formats": ["JPEG", "PNG"],
    }


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/convert")
def convert(
    url: str = Query(..., min_length=12, max_length=2048),
    width: int = Query(48, ge=8, le=MAX_OUTPUT_SIDE),
    colors: int = Query(32, ge=2, le=256),
):
    image = download_image(url)

    # Composite transparency onto white, then resize without changing aspect ratio.
    image = ImageOps.exif_transpose(image).convert("RGBA")
    background = Image.new("RGBA", image.size, (255, 255, 255, 255))
    image = Image.alpha_composite(background, image).convert("RGB")

    scale = min(width / image.width, MAX_OUTPUT_SIDE / image.height)
    out_w = max(1, round(image.width * scale))
    out_h = max(1, round(image.height * scale))
    image = image.resize((out_w, out_h), Image.Resampling.LANCZOS)

    quantized = image.quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
    palette_raw = quantized.getpalette()
    used_indices = sorted(set(quantized.getdata()))
    palette = [
        palette_raw[i * 3:i * 3 + 3]
        for i in used_indices
    ]
    remap = {old: new for new, old in enumerate(used_indices)}
    pixels = [remap[i] for i in quantized.getdata()]

    # Binary RLE pairs: one byte run length (1..255), one byte palette index.
    encoded = bytearray()
    pos = 0
    while pos < len(pixels):
        index = pixels[pos]
        end = pos + 1
        while end < len(pixels) and pixels[end] == index and end - pos < 255:
            end += 1
        encoded.extend((end - pos, index))
        pos = end

    return {
        "width": out_w,
        "height": out_h,
        "palette": palette,
        "rle": encoded.hex().upper(),
        "format": "RLE8",
    }

# ==========================================
# RECODE VIDEO VIEWER
# ==========================================

VIDEO_MAX_BYTES = 100 * 1024 * 1024
VIDEO_MAX_DURATION = 300
VIDEO_FPS = 15
VIDEO_WIDTH = 320
VIDEO_HEIGHT = 180

VIDEO_ROOT = Path(tempfile.gettempdir()) / "recode_videos"
VIDEO_ROOT.mkdir(parents=True, exist_ok=True)

VIDEO_JOBS = {}
VIDEO_LOCK = threading.Lock()
VIDEO_EXECUTOR = ThreadPoolExecutor(max_workers=2)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def video_update(job_id, **values):
    with VIDEO_LOCK:
        if job_id in VIDEO_JOBS:
            VIDEO_JOBS[job_id].update(values)


def validate_video_url(url):
    parsed = urlparse(url)

    if parsed.scheme != "https":
        raise HTTPException(400, "HTTPS video URLs only.")

    if not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(400, "Invalid video URL.")

    if parsed.port not in (None, 443):
        raise HTTPException(400, "Invalid video port.")

    host = parsed.hostname

    try:
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            addresses = [
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(
                    host, 443, type=socket.SOCK_STREAM
                )
            ]

        if not addresses or any(not ip.is_global for ip in addresses):
            raise HTTPException(400, "Video host must be publicly accessible.")

    except socket.gaierror:
        raise HTTPException(502, "Could not resolve video host.")


def get_video_duration(path):
    result = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=30
    )

    match = re.search(
        r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
        result.stderr
    )

    if not match:
        raise ValueError("Could not read video duration.")

    hours, minutes, seconds = match.groups()

    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def process_video(job_id, url):
    folder = VIDEO_ROOT / job_id
    folder.mkdir(parents=True, exist_ok=True)

    source = folder / "source_video"
    pattern = folder / "frame_%06d.jpg"

    try:
        video_update(
            job_id,
            status="downloading",
            progress=0,
            message="Downloading video"
        )

        validate_video_url(url)

        with requests.get(
            url,
            with requests.get(
    url,
    headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
    },
    stream=True,
    timeout=(10, 30),
    allow_redirects=False,
) as response:

            if response.is_redirect:
                raise ValueError("Video redirects are not supported.")

            response.raise_for_status()

            content_type = response.headers.get(
                "content-type", ""
            ).split(";")[0].lower()

            if content_type not in {
                "video/mp4",
                "video/webm",
                "video/quicktime"
            }:
                raise ValueError("URL must point directly to an MP4, WebM or MOV video.")

            length = response.headers.get("content-length")

            if length and int(length) > VIDEO_MAX_BYTES:
                raise ValueError("Video exceeds the 100 MB limit.")

            downloaded = 0

            with open(source, "wb") as file:
                for chunk in response.iter_content(65536):
                    if not chunk:
                        continue

                    downloaded += len(chunk)

                    if downloaded > VIDEO_MAX_BYTES:
                        raise ValueError("Video exceeds the 100 MB limit.")

                    file.write(chunk)

                    video_update(
                        job_id,
                        progress=min(20, int(downloaded / VIDEO_MAX_BYTES * 20))
                    )

        duration = get_video_duration(source)

        if duration <= 0 or duration > VIDEO_MAX_DURATION:
            raise ValueError("Video must be between 1 second and 5 minutes.")

        video_update(
            job_id,
            status="processing",
            progress=20,
            message="Processing video",
            duration=duration
        )

        command = [
            FFMPEG,
            "-y",
            "-i", str(source),
            "-t", str(VIDEO_MAX_DURATION),
            "-vf",
            (
                f"fps={VIDEO_FPS},"
                f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:"
                "force_original_aspect_ratio=decrease,"
                f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2"
            ),
            "-q:v", "6",
            "-start_number", "1",
            "-progress", "pipe:1",
            "-nostats",
            "-loglevel", "error",
            str(pattern)
        ]

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1
        )

        for line in process.stdout:
            if line.startswith("out_time_ms="):
                try:
                    elapsed = int(line.split("=")[1]) / 1_000_000
                    percent = 20 + int(
                        min(1, elapsed / duration) * 80
                    )

                    video_update(job_id, progress=percent)

                except (ValueError, ZeroDivisionError):
                    pass

        return_code = process.wait()

        if return_code != 0:
            raise ValueError("Video processing failed.")

        frames = sorted(folder.glob("frame_*.jpg"))

        if not frames:
            raise ValueError("No video frames were generated.")

        source.unlink(missing_ok=True)

        video_update(
            job_id,
            status="ready",
            progress=100,
            message="Ready",
            frame_count=len(frames),
            frame_interval=1 / VIDEO_FPS
        )

    except Exception as error:
        video_update(
            job_id,
            status="error",
            message=str(error),
            progress=0
        )


@app.get("/video/start")
def video_start(
    url: str = Query(..., min_length=12, max_length=2048)
):
    with VIDEO_LOCK:
        active = sum(
            job["status"] in {"queued", "downloading", "processing"}
            for job in VIDEO_JOBS.values()
        )

        if active >= 2:
            raise HTTPException(
                429,
                "The server is currently processing two videos."
            )

        job_id = secrets.token_urlsafe(16)

        VIDEO_JOBS[job_id] = {
            "status": "queued",
            "progress": 0,
            "message": "Waiting",
            "frame_count": 0
        }

    VIDEO_EXECUTOR.submit(process_video, job_id, url)

    return {
        "job_id": job_id,
        "status": "queued"
    }


@app.get("/video/status/{job_id}")
def video_status(job_id: str):
    with VIDEO_LOCK:
        job = VIDEO_JOBS.get(job_id)

        if not job:
            raise HTTPException(404, "Video job not found.")

        return {
            "job_id": job_id,
            **job
        }


@app.get("/video/frame/{job_id}/{frame_index}")
def video_frame(job_id: str, frame_index: int):
    with VIDEO_LOCK:
        job = VIDEO_JOBS.get(job_id)

        if not job:
            raise HTTPException(404, "Video job not found.")

        if job["status"] != "ready":
            raise HTTPException(409, "Video is not ready yet.")

        if frame_index < 0 or frame_index >= job["frame_count"]:
            raise HTTPException(404, "Frame does not exist.")

    frame_number = frame_index + 1

    path = (
        VIDEO_ROOT
        / job_id
        / f"frame_{frame_number:06d}.jpg"
    )

    if not path.is_file():
        raise HTTPException(404, "Frame file not found.")

    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"}
    )
