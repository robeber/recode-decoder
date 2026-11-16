# RECODE Image Converter

A small FastAPI service that converts JPG/PNG images hosted on `i.postimg.cc`
into a compact colour-palette + RLE response for a Lua drawing script.

## Deploy on Render

1. Upload these files to a GitHub repository.
2. In Render, choose **New → Web Service** and connect that repository.
3. Choose the **Free** plan.
4. Render can read `render.yaml`; otherwise use:
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. After deploy, open `https://YOUR-SERVICE.onrender.com/health`.
   It should return `{"ok": true}`.

## API

`/convert?url=https%3A%2F%2Fi.postimg.cc%2F...jpg&width=48&colors=32`

- Accepts HTTPS URLs hosted on `i.postimg.cc` only.
- Accepts JPG and PNG.
- `width`: 8–96; aspect ratio is preserved.
- `colors`: 2–256.
- Maximum downloaded image size: 5 MB.
- Returns JSON with `width`, `height`, `palette` (RGB triplets), and `rle`.
- `rle` consists of 2-byte pairs encoded as hex: run length, palette index.
  Both bytes are represented by two hexadecimal characters.

The free Render service may sleep after 15 minutes without requests.
