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

ALLOWED_HOSTS = {"i.postimg.cc"}
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
