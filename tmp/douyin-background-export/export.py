from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageOps

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
ORIGINALS = OUT / "originals_webp"
PNG = OUT / "png"
FRAMES = OUT / "frames_720x1280"
GIF_FRAMES = OUT / "gif_frames_480x854"
MUSIC_URL = "https://sf11-cdn-tos.douyinstatic.com/obj/ies-music/7431871242870819625.mp3"

for directory in (OUT, ORIGINALS, PNG, FRAMES, GIF_FRAMES):
    directory.mkdir(parents=True, exist_ok=True)

urls = [line.strip() for line in (ROOT / "urls.txt").read_text(encoding="utf-8").splitlines() if line.strip()]
if len(urls) != 26:
    raise RuntimeError(f"Expected 26 image URLs, found {len(urls)}")

session = requests.Session()
session.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Chrome/138 Mobile Safari/537.36",
        "Referer": "https://www.douyin.com/",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
)


def fetch(url: str, attempts: int = 5) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=45, allow_redirects=True)
            response.raise_for_status()
            if len(response.content) < 1024:
                raise RuntimeError(f"Response unexpectedly small: {len(response.content)} bytes")
            return response.content
        except Exception as exc:
            last_error = exc
            print(f"Attempt {attempt}/{attempts} failed: {exc}")
    raise RuntimeError(f"Download failed after {attempts} attempts: {url}") from last_error


def wallpaper_frame(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    image = image.convert("RGB")
    background = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
    background = background.filter(ImageFilter.GaussianBlur(radius=max(size) // 45))

    foreground = image.copy()
    foreground.thumbnail((size[0] - 32, size[1] - 32), Image.Resampling.LANCZOS)
    x = (size[0] - foreground.width) // 2
    y = (size[1] - foreground.height) // 2

    canvas = background.convert("RGBA")
    shadow = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(shadow)
    draw.rounded_rectangle(
        (x - 8, y - 8, x + foreground.width + 8, y + foreground.height + 8),
        radius=20,
        fill=(0, 0, 0, 100),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    canvas.alpha_composite(shadow)
    canvas.alpha_composite(foreground.convert("RGBA"), (x, y))
    return canvas.convert("RGB")


manifest: list[dict[str, object]] = []
for index, url in enumerate(urls, start=1):
    print(f"Downloading image {index:02d}/26")
    raw = fetch(url)
    original_path = ORIGINALS / f"{index:02d}.webp"
    original_path.write_bytes(raw)

    with Image.open(BytesIO(raw)) as source:
        source.load()
        rgb = source.convert("RGB")
        png_path = PNG / f"{index:02d}.png"
        rgb.save(png_path, "PNG", optimize=True)

        frame = wallpaper_frame(rgb, (720, 1280))
        frame_path = FRAMES / f"{index:02d}.jpg"
        frame.save(frame_path, "JPEG", quality=95, optimize=True)

        gif_frame = wallpaper_frame(rgb, (480, 854))
        gif_frame_path = GIF_FRAMES / f"{index:02d}.png"
        gif_frame.save(gif_frame_path, "PNG", optimize=True)

        manifest.append(
            {
                "index": index,
                "source_url": url,
                "width": rgb.width,
                "height": rgb.height,
                "original": str(original_path.relative_to(OUT)),
                "png": str(png_path.relative_to(OUT)),
            }
        )

(OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

# GIF: one second per image, endless loop.
gif_images = [Image.open(GIF_FRAMES / f"{index:02d}.png").convert("P", palette=Image.Palette.ADAPTIVE, colors=256) for index in range(1, 27)]
gif_path = OUT / "china_backgrounds_26_images.gif"
gif_images[0].save(
    gif_path,
    save_all=True,
    append_images=gif_images[1:],
    duration=1000,
    loop=0,
    optimize=True,
    disposal=2,
)
for image in gif_images:
    image.close()

# Video: 1.2 seconds per still image, 720x1280, H.264 for broad Android compatibility.
concat_file = OUT / "frames.txt"
with concat_file.open("w", encoding="utf-8") as handle:
    for index in range(1, 27):
        handle.write(f"file '{(FRAMES / f'{index:02d}.jpg').resolve()}'\n")
        handle.write("duration 1.2\n")
    handle.write(f"file '{(FRAMES / '26.jpg').resolve()}'\n")

silent_mp4 = OUT / "android_live_wallpaper_720x1280_silent.mp4"
subprocess.run(
    [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-movflags", "+faststart", str(silent_mp4),
    ],
    check=True,
)

# Optional version with the original Douyin music.
music_path = OUT / "original_music.mp3"
try:
    music_path.write_bytes(fetch(MUSIC_URL))
    music_mp4 = OUT / "android_dynamic_background_with_music_720x1280.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(silent_mp4), "-stream_loop", "-1", "-i", str(music_path),
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart", str(music_mp4),
        ],
        check=True,
    )
except Exception as exc:
    print(f"Music version skipped: {exc}")

# Separate original-image archive.
original_zip = OUT / "26_no_watermark_original_images.zip"
with zipfile.ZipFile(original_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(ORIGINALS.glob("*.webp")):
        archive.write(path, arcname=f"originals_webp/{path.name}")
    for path in sorted(PNG.glob("*.png")):
        archive.write(path, arcname=f"png/{path.name}")
    archive.write(OUT / "manifest.json", arcname="manifest.json")

# Complete delivery archive.
complete_zip = OUT / "douyin_china_background_complete_package.zip"
with zipfile.ZipFile(complete_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(OUT.rglob("*")):
        if not path.is_file() or path == complete_zip:
            continue
        archive.write(path, arcname=str(path.relative_to(OUT)))

print("Created:")
for path in sorted(OUT.iterdir()):
    if path.is_file():
        print(f"- {path.name}: {path.stat().st_size} bytes")
