from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageOps

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "live_output"
RAW = OUT / "raw_sources"
RAW_LIVE = RAW / "live_mp4"
RAW_STATIC = RAW / "static_images"
CANVAS = OUT / "static_canvas"
ANDROID = OUT / "android_mp4"
GIFS = OUT / "gif"
COLLECTION = OUT / "collection"
VENDOR = ROOT / "vendor" / "douyin_parse"
SHARE_URL = "https://v.douyin.com/-K9Bpgl1daM/"
AWEME_ID = "7666707482186192869"
MUSIC_URL = "https://sf11-cdn-tos.douyinstatic.com/obj/ies-music/7431871242870819625.mp3"

for directory in (OUT, RAW_LIVE, RAW_STATIC, CANVAS, ANDROID, GIFS, COLLECTION):
    directory.mkdir(parents=True, exist_ok=True)

session = requests.Session()
session.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": f"https://www.douyin.com/note/{AWEME_ID}",
        "Origin": "https://www.douyin.com",
    }
)


def run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command))
    return subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def fetch_bytes(url: str, attempts: int = 5, min_size: int = 256) -> tuple[bytes, str]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=60, allow_redirects=True)
            response.raise_for_status()
            content = response.content
            if len(content) < min_size:
                raise RuntimeError(f"response too small: {len(content)} bytes")
            return content, response.headers.get("content-type", "")
        except Exception as exc:
            last_error = exc
            print(f"download attempt {attempt}/{attempts} failed: {exc}")
            time.sleep(attempt)
    raise RuntimeError(f"Unable to download {url}") from last_error


def fetch_json(url: str, *, params: dict[str, str] | None = None) -> dict[str, Any] | None:
    try:
        response = session.get(url, params=params, timeout=60, allow_redirects=True)
        print("JSON request", response.status_code, response.url)
        if response.status_code != 200 or not response.content:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception as exc:
        print("JSON request failed:", exc)
        return None


def find_aweme_detail(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        detail = value.get("aweme_detail")
        if isinstance(detail, dict) and detail.get("images"):
            return detail
        if value.get("aweme_id") == AWEME_ID and value.get("images"):
            return value
        for child in value.values():
            found = find_aweme_detail(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_aweme_detail(child)
            if found:
                return found
    return None


def obtain_detail() -> tuple[dict[str, Any], dict[str, Any]]:
    errors: list[str] = []

    # First choice: current open-source parser with a_bogus/X-Bogus support.
    try:
        sys.path.insert(0, str(VENDOR))
        from douyin_video_parser import DouyinVideoParser  # type: ignore

        parser = DouyinVideoParser()
        raw = parser.get_aweme_detail(AWEME_ID, original_url=f"https://www.douyin.com/note/{AWEME_ID}")
        if isinstance(raw, dict):
            detail = find_aweme_detail(raw)
            if detail:
                return raw, detail
        errors.append("open-source parser returned no aweme_detail")
    except Exception as exc:
        errors.append(f"open-source parser failed: {exc!r}")

    # Second choice: public hybrid endpoint from the upstream project.
    for host in ("https://api.douyin.wtf", "https://douyin.wtf"):
        raw = fetch_json(
            f"{host}/api/hybrid/video_data",
            params={"url": SHARE_URL, "minimal": "false"},
        )
        if raw:
            detail = find_aweme_detail(raw)
            if detail:
                return raw, detail
            errors.append(f"{host} returned JSON but no aweme_detail")

    # Third choice: old mobile item-info endpoint, still useful on some public works.
    raw = fetch_json(
        "https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/",
        params={"item_ids": AWEME_ID},
    )
    if raw:
        detail = find_aweme_detail(raw)
        if detail:
            return raw, detail
        errors.append("iesdouyin iteminfo returned JSON but no aweme_detail")

    raise RuntimeError("; ".join(errors))


def first_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith("http"):
        return value
    if isinstance(value, list):
        for entry in value:
            url = first_url(entry)
            if url:
                return url
    if isinstance(value, dict):
        for key in ("url_list", "UrlList", "url", "uri"):
            if key in value:
                url = first_url(value[key])
                if url:
                    return url
    return None


def video_candidates(image: dict[str, Any]) -> list[str]:
    video = image.get("video")
    if not isinstance(video, dict):
        return []

    candidates: list[str] = []
    play_addr = video.get("play_addr")
    if isinstance(play_addr, dict):
        uri = play_addr.get("uri")
        if isinstance(uri, str) and uri:
            candidates.extend(
                [
                    f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=1080p&line=0",
                    f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=720p&line=0",
                ]
            )
        urls = play_addr.get("url_list")
        if isinstance(urls, list):
            candidates.extend(str(url) for url in urls if isinstance(url, str))

    for field in ("download_addr", "play_addr_h264", "play_addr_265", "play_addr_lowbr"):
        obj = video.get(field)
        if isinstance(obj, dict):
            urls = obj.get("url_list")
            if isinstance(urls, list):
                candidates.extend(str(url) for url in urls if isinstance(url, str))

    bit_rates = video.get("bit_rate")
    if isinstance(bit_rates, list):
        for rate in bit_rates:
            if not isinstance(rate, dict):
                continue
            addr = rate.get("play_addr")
            if isinstance(addr, dict) and isinstance(addr.get("url_list"), list):
                candidates.extend(str(url) for url in addr["url_list"] if isinstance(url, str))

    unique: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        clean = url.replace("playwm", "play").split("&watermark=")[0].split("&logo_name=")[0]
        if clean.startswith("http") and clean not in seen:
            seen.add(clean)
            unique.append(clean)
    return unique


def static_candidates(image: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for field in (
        "url_list",
        "download_url_list",
        "origin_url_list",
        "animated_url_list",
        "gif_url_list",
        "live_url_list",
        "motion_url_list",
    ):
        value = image.get(field)
        if isinstance(value, list):
            candidates.extend(str(url) for url in value if isinstance(url, str))
        elif isinstance(value, str):
            candidates.append(value)
    unique: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        if url.startswith("http") and url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def image_extension(content_type: str, raw: bytes) -> str:
    content_type = content_type.split(";", 1)[0].lower()
    mapped = mimetypes.guess_extension(content_type) if content_type else None
    if mapped in (".jpe", ".jpeg"):
        return ".jpg"
    if mapped in (".jpg", ".png", ".webp", ".gif"):
        return mapped
    try:
        with Image.open(BytesIO(raw)) as image:
            fmt = (image.format or "webp").lower()
        return ".jpg" if fmt in ("jpeg", "jpg") else f".{fmt}"
    except Exception:
        return ".bin"


def is_video_bytes(raw: bytes, content_type: str) -> bool:
    lowered = content_type.lower()
    if "video" in lowered or "octet-stream" in lowered:
        if raw[4:12] in (b"ftypisom", b"ftypmp42", b"ftypdash", b"ftypavc1") or b"ftyp" in raw[:32]:
            return True
    return b"ftyp" in raw[:32]


def ffprobe_duration(path: Path) -> float:
    try:
        result = run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture=True,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def create_static_canvas(source: Path, destination: Path) -> None:
    with Image.open(source) as original:
        image = original.convert("RGB")
        size = (720, 1280)
        background = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
        background = background.filter(ImageFilter.GaussianBlur(22))
        foreground = image.copy()
        foreground.thumbnail((688, 1248), Image.Resampling.LANCZOS)
        x = (720 - foreground.width) // 2
        y = (1280 - foreground.height) // 2
        canvas = background.convert("RGBA")
        shadow = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(shadow)
        draw.rounded_rectangle(
            (x - 8, y - 8, x + foreground.width + 8, y + foreground.height + 8),
            radius=18,
            fill=(0, 0, 0, 100),
        )
        canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)))
        canvas.alpha_composite(foreground.convert("RGBA"), (x, y))
        canvas.convert("RGB").save(destination, "JPEG", quality=96, optimize=True)


def make_static_mp4(canvas: Path, output: Path) -> None:
    run(
        [
            "ffmpeg", "-y", "-loop", "1", "-i", str(canvas), "-t", "4",
            "-vf",
            "scale=768:1366,zoompan=z='min(zoom+0.00045,1.055)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=120:s=720x1280:fps=30,format=yuv420p",
            "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "19",
            "-movflags", "+faststart", str(output),
        ]
    )


def make_live_mp4(source: Path, output: Path) -> None:
    duration = ffprobe_duration(source)
    limit = min(duration, 8.0) if duration > 0 else 8.0
    filter_graph = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,boxblur=20:5[bg2];"
        "[fg]scale=720:1280:force_original_aspect_ratio=decrease[fg2];"
        "[bg2][fg2]overlay=(W-w)/2:(H-h)/2,fps=30,format=yuv420p"
    )
    run(
        [
            "ffmpeg", "-y", "-i", str(source), "-t", f"{limit:.3f}",
            "-filter_complex", filter_graph,
            "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "19",
            "-movflags", "+faststart", str(output),
        ]
    )


def make_gif(source_mp4: Path, output_gif: Path) -> None:
    palette_filter = (
        "fps=10,scale=360:-2:flags=lanczos,split[s0][s1];"
        "[s0]palettegen=max_colors=160:stats_mode=diff[p];"
        "[s1][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle"
    )
    run(["ffmpeg", "-y", "-i", str(source_mp4), "-t", "5", "-lavfi", palette_filter, "-loop", "0", str(output_gif)])


raw_response, aweme = obtain_detail()
(OUT / "aweme_response.json").write_text(json.dumps(raw_response, ensure_ascii=False, indent=2), encoding="utf-8")
(OUT / "aweme_detail.json").write_text(json.dumps(aweme, ensure_ascii=False, indent=2), encoding="utf-8")

images = aweme.get("images")
if not isinstance(images, list) or not images:
    raise RuntimeError("No ordered images array in aweme detail")

print(f"Ordered album items: {len(images)}")
manifest: list[dict[str, Any]] = []
processed_mp4s: list[Path] = []

for index, item in enumerate(images, start=1):
    if not isinstance(item, dict):
        raise RuntimeError(f"Image item {index} is not an object")

    prefix = f"{index:02d}"
    live_source: Path | None = None
    static_source: Path | None = None
    chosen_url: str | None = None
    download_errors: list[str] = []

    for candidate in video_candidates(item):
        try:
            raw, content_type = fetch_bytes(candidate, min_size=1024)
            if not is_video_bytes(raw, content_type):
                raise RuntimeError(f"not MP4; content-type={content_type}, size={len(raw)}")
            live_source = RAW_LIVE / f"{prefix}.mp4"
            live_source.write_bytes(raw)
            chosen_url = candidate
            break
        except Exception as exc:
            download_errors.append(f"video {candidate}: {exc}")

    if live_source is None:
        for candidate in static_candidates(item):
            try:
                raw, content_type = fetch_bytes(candidate, min_size=512)
                extension = image_extension(content_type, raw)
                if extension == ".bin":
                    raise RuntimeError(f"unrecognized image; content-type={content_type}, size={len(raw)}")
                static_source = RAW_STATIC / f"{prefix}{extension}"
                static_source.write_bytes(raw)
                with Image.open(static_source) as verification:
                    verification.load()
                chosen_url = candidate
                break
            except Exception as exc:
                download_errors.append(f"image {candidate}: {exc}")

    if live_source is None and static_source is None:
        raise RuntimeError(f"Unable to download album item {index}: {' | '.join(download_errors)}")

    final_mp4 = ANDROID / f"{prefix}.mp4"
    final_gif = GIFS / f"{prefix}.gif"
    source_type: str
    source_path: Path

    if live_source is not None:
        source_type = "live_photo_video"
        source_path = live_source
        make_live_mp4(live_source, final_mp4)
    else:
        source_type = "static_image_animated"
        source_path = static_source  # type: ignore[assignment]
        canvas = CANVAS / f"{prefix}.jpg"
        create_static_canvas(source_path, canvas)
        make_static_mp4(canvas, final_mp4)

    make_gif(final_mp4, final_gif)
    processed_mp4s.append(final_mp4)

    manifest.append(
        {
            "index": index,
            "source_type": source_type,
            "source_file": str(source_path.relative_to(OUT)),
            "android_mp4": str(final_mp4.relative_to(OUT)),
            "gif": str(final_gif.relative_to(OUT)),
            "source_url": chosen_url,
            "source_duration_seconds": ffprobe_duration(source_path) if live_source else None,
            "final_duration_seconds": ffprobe_duration(final_mp4),
            "live_photo_type": item.get("live_photo_type"),
            "clip_type": item.get("clip_type"),
        }
    )

(OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

# Concatenate all individually normalized Android videos in original album order.
concat_list = COLLECTION / "concat.txt"
with concat_list.open("w", encoding="utf-8") as handle:
    for path in processed_mp4s:
        handle.write(f"file '{path.resolve()}'\n")

collection_silent = COLLECTION / "all_26_android_dynamic_backgrounds_silent.mp4"
run(
    [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", "-movflags", "+faststart", str(collection_silent),
    ]
)

music_path = COLLECTION / "original_music.mp3"
try:
    music_raw, _ = fetch_bytes(MUSIC_URL, min_size=1024)
    music_path.write_bytes(music_raw)
    collection_music = COLLECTION / "all_26_android_dynamic_backgrounds_with_music.mp4"
    run(
        [
            "ffmpeg", "-y", "-i", str(collection_silent), "-stream_loop", "-1", "-i", str(music_path),
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart", str(collection_music),
        ]
    )
except Exception as exc:
    print("Original music download/mux skipped:", exc)

collection_gif = COLLECTION / "all_26_preview.gif"
collection_palette = (
    "fps=8,scale=320:-2:flags=lanczos,split[s0][s1];"
    "[s0]palettegen=max_colors=128:stats_mode=diff[p];"
    "[s1][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle"
)
run(["ffmpeg", "-y", "-i", str(collection_silent), "-lavfi", collection_palette, "-loop", "0", str(collection_gif)])

# Raw source package and complete package.
raw_zip = OUT / "ordered_raw_live_photos_and_images.zip"
with zipfile.ZipFile(raw_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(RAW.rglob("*")):
        if path.is_file():
            archive.write(path, arcname=str(path.relative_to(OUT)))
    archive.write(OUT / "manifest.json", arcname="manifest.json")

complete_zip = OUT / "complete_26_dynamic_backgrounds_android_and_gif.zip"
with zipfile.ZipFile(complete_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for directory in (RAW, ANDROID, GIFS, COLLECTION):
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.name != "concat.txt":
                archive.write(path, arcname=str(path.relative_to(OUT)))
    for metadata in (OUT / "manifest.json", OUT / "aweme_detail.json"):
        archive.write(metadata, arcname=metadata.name)

print("FINAL OUTPUT")
for path in sorted(OUT.rglob("*")):
    if path.is_file():
        print(path.relative_to(OUT), path.stat().st_size)
