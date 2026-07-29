from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "final_output"
COVERS = OUT / "01_no_watermark_covers"
RAW = OUT / "02_no_watermark_original_motion_mp4"
ANDROID = OUT / "03_android_live_wallpaper_mp4"
GIFS = OUT / "04_gif_animations"
COMBINED = OUT / "05_combined"
META = OUT / "metadata"
AWEME_ID = "7666707482186192869"
MOBILE_NOTE_URL = f"https://m.douyin.com/share/note/{AWEME_ID}"

for folder in (OUT, COVERS, RAW, ANDROID, GIFS, COMBINED, META):
    folder.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 16; 24129PN74C) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Mobile Safari/537.36"
)

session = requests.Session()
session.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Referer": MOBILE_NOTE_URL,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
)


def run(command: list[str], capture: bool = False) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_bytes(urls: list[str], min_size: int = 512, attempts: int = 4) -> tuple[bytes, str, str]:
    errors: list[str] = []
    for url in urls:
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        for attempt in range(1, attempts + 1):
            try:
                response = session.get(url, timeout=90, allow_redirects=True)
                response.raise_for_status()
                if len(response.content) < min_size:
                    raise RuntimeError(f"too small: {len(response.content)} bytes")
                return response.content, response.headers.get("content-type", ""), response.url
            except Exception as exc:
                errors.append(f"{url} attempt {attempt}: {exc}")
                time.sleep(min(attempt, 3))
    raise RuntimeError(" | ".join(errors[-20:]))


def image_extension(raw: bytes, content_type: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    ext = mimetypes.guess_extension(content_type) if content_type else None
    if ext in (".jpeg", ".jpe"):
        ext = ".jpg"
    if ext in (".jpg", ".png", ".webp", ".gif"):
        return ext
    with Image.open(BytesIO(raw)) as image:
        fmt = (image.format or "webp").lower()
    return ".jpg" if fmt == "jpeg" else f".{fmt}"


def ffprobe(path: Path) -> dict[str, Any]:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size,format_name:stream=index,codec_name,codec_type,width,height,r_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        capture=True,
    )
    return json.loads(result.stdout)


def get_slides_detail() -> tuple[dict[str, Any], str]:
    print("Opening the real Douyin mobile note page...", flush=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            viewport={"width": 430, "height": 932},
            user_agent=USER_AGENT,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            is_mobile=True,
            has_touch=True,
        )
        page = context.new_page()
        captured: dict[str, Any] = {}
        captured_url = ""

        def on_response(response):
            nonlocal captured, captured_url
            if "/web/api/v2/aweme/slidesinfo/" not in response.url:
                return
            try:
                data = response.json()
                details = data.get("aweme_details") if isinstance(data, dict) else None
                if isinstance(details, list) and details and str(details[0].get("aweme_id")) == AWEME_ID:
                    captured = data
                    captured_url = response.url
                    print("Captured signed slidesinfo response", flush=True)
            except Exception as exc:
                print("Could not decode slidesinfo response:", exc, flush=True)

        page.on("response", on_response)
        page.goto(MOBILE_NOTE_URL, wait_until="domcontentloaded", timeout=120_000)
        deadline = time.time() + 90
        while time.time() < deadline and not captured:
            page.wait_for_timeout(1000)
            page.mouse.wheel(0, 400)
        (META / "douyin_mobile_page.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(META / "douyin_mobile_page.png"), full_page=True)
        browser.close()

    if not captured:
        raise RuntimeError("The signed Douyin slidesinfo response was not captured")
    return captured, captured_url


def make_static_android(image_path: Path, output: Path) -> None:
    run(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(image_path),
            "-t",
            "4",
            "-vf",
            (
                "scale=768:1366:force_original_aspect_ratio=increase,"
                "crop=768:1366,"
                "zoompan=z='min(zoom+0.00045,1.055)':x='iw/2-(iw/zoom/2)':"
                "y='ih/2-(ih/zoom/2)':d=120:s=720x1280:fps=30,"
                "format=yuv420p"
            ),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "19",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )


def normalize_motion(source: Path, output: Path) -> None:
    filter_graph = (
        "[0:v]split=2[background][foreground];"
        "[background]scale=720:1280:force_original_aspect_ratio=increase,"
        "crop=720:1280,gblur=sigma=24[blurred];"
        "[foreground]scale=720:1280:force_original_aspect_ratio=decrease[front];"
        "[blurred][front]overlay=(W-w)/2:(H-h)/2,fps=30,format=yuv420p"
    )
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-filter_complex",
            filter_graph,
            "-map",
            "[v]" if False else "0:v:0",
        ]
    )


def normalize_motion(source: Path, output: Path) -> None:
    filter_graph = (
        "[0:v]split=2[background][foreground];"
        "[background]scale=720:1280:force_original_aspect_ratio=increase,"
        "crop=720:1280,gblur=sigma=24[blurred];"
        "[foreground]scale=720:1280:force_original_aspect_ratio=decrease[front];"
        "[blurred][front]overlay=(W-w)/2:(H-h)/2,fps=30,format=yuv420p[outv]"
    )
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-filter_complex",
            filter_graph,
            "-map",
            "[outv]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "19",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )


def make_gif(source: Path, output: Path) -> None:
    palette = (
        "fps=10,scale=360:640:flags=lanczos,split[s0][s1];"
        "[s0]palettegen=max_colors=192:stats_mode=diff[p];"
        "[s1][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle"
    )
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-lavfi",
            palette,
            "-loop",
            "0",
            str(output),
        ]
    )


def zip_paths(zip_path: Path, roots: list[Path], extra_files: list[Path] | None = None) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for root in roots:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, arcname=str(path.relative_to(OUT)))
        for path in extra_files or []:
            if path.is_file():
                archive.write(path, arcname=str(path.relative_to(OUT)))


slides_response, slides_url = get_slides_detail()
(META / "slidesinfo_response.json").write_text(
    json.dumps(slides_response, ensure_ascii=False, indent=2), encoding="utf-8"
)
(META / "slidesinfo_request_url.txt").write_text(slides_url, encoding="utf-8")

aweme_details = slides_response.get("aweme_details")
if not isinstance(aweme_details, list) or not aweme_details:
    raise RuntimeError("No aweme_details in the captured response")
aweme = aweme_details[0]
images = aweme.get("images")
if not isinstance(images, list) or len(images) != 26:
    raise RuntimeError(f"Expected 26 ordered album items; got {len(images) if isinstance(images, list) else 'none'}")

music_urls = ((aweme.get("music") or {}).get("play_url") or {}).get("url_list") or []
manifest: list[dict[str, Any]] = []
android_files: list[Path] = []
preview_files: list[Path] = []

for index, item in enumerate(images, start=1):
    if not isinstance(item, dict):
        raise RuntimeError(f"Album item {index} is malformed")
    number = f"{index:02d}"
    print(f"Processing album item {number}/26", flush=True)

    image_urls = item.get("url_list") or item.get("download_url_list") or []
    image_raw, image_type, image_final_url = fetch_bytes(list(image_urls), min_size=512)
    cover_ext = image_extension(image_raw, image_type)
    cover_path = COVERS / f"{number}_cover{cover_ext}"
    cover_path.write_bytes(image_raw)
    with Image.open(cover_path) as verification:
        verification.load()
        cover_size = [verification.width, verification.height]

    video = item.get("video") if isinstance(item.get("video"), dict) else None
    raw_motion_path: Path | None = None
    raw_motion_url: str | None = None
    raw_probe: dict[str, Any] | None = None

    android_path = ANDROID / f"{number}_android_720x1280.mp4"
    gif_path = GIFS / f"{number}_animation.gif"

    if video:
        play_addr = video.get("play_addr") if isinstance(video.get("play_addr"), dict) else {}
        video_urls = list(play_addr.get("url_list") or [])
        uri = play_addr.get("uri")
        if isinstance(uri, str) and uri:
            video_urls.extend(
                [
                    f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=1080p&line=0",
                    f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=720p&line=0",
                ]
            )
        motion_raw, motion_type, raw_motion_url = fetch_bytes(video_urls, min_size=20_000)
        if b"ftyp" not in motion_raw[:64] and "video" not in motion_type.lower():
            raise RuntimeError(f"Album item {number} did not return an MP4")
        raw_motion_path = RAW / f"{number}_original_no_watermark.mp4"
        raw_motion_path.write_bytes(motion_raw)
        raw_probe = ffprobe(raw_motion_path)
        normalize_motion(raw_motion_path, android_path)
    else:
        make_static_android(cover_path, android_path)

    make_gif(android_path, gif_path)
    android_probe = ffprobe(android_path)
    gif_probe = ffprobe(gif_path)
    android_files.append(android_path)
    preview_files.append(android_path)

    manifest.append(
        {
            "index": index,
            "clip_type": item.get("clip_type"),
            "cover_file": str(cover_path.relative_to(OUT)),
            "cover_size": cover_size,
            "cover_source_url": image_final_url,
            "original_motion_file": str(raw_motion_path.relative_to(OUT)) if raw_motion_path else None,
            "original_motion_source_url": raw_motion_url,
            "original_motion_probe": raw_probe,
            "android_file": str(android_path.relative_to(OUT)),
            "android_probe": android_probe,
            "gif_file": str(gif_path.relative_to(OUT)),
            "gif_probe": gif_probe,
            "sha256": {
                "cover": sha256(cover_path),
                "original_motion": sha256(raw_motion_path) if raw_motion_path else None,
                "android": sha256(android_path),
                "gif": sha256(gif_path),
            },
        }
    )

# Concatenate all normalized files in original order.
concat_path = COMBINED / "concat.txt"
with concat_path.open("w", encoding="utf-8") as handle:
    for path in android_files:
        handle.write(f"file '{path.resolve()}'\n")

combined_silent = COMBINED / "all_26_full_silent.mp4"
run(
    [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(combined_silent),
    ]
)

combined_music: Path | None = None
music_path: Path | None = None
if isinstance(music_urls, list) and music_urls:
    music_raw, _, _ = fetch_bytes(list(music_urls), min_size=1024)
    music_path = COMBINED / "original_douyin_music.mp3"
    music_path.write_bytes(music_raw)
    combined_music = COMBINED / "all_26_full_with_original_music.mp4"
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(combined_silent),
            "-stream_loop",
            "-1",
            "-i",
            str(music_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(combined_music),
        ]
    )

# A shorter one-second-per-item preview montage and GIF.
preview_segments = COMBINED / "preview_segments"
preview_segments.mkdir(exist_ok=True)
preview_list = COMBINED / "preview_concat.txt"
with preview_list.open("w", encoding="utf-8") as handle:
    for index, source in enumerate(preview_files, start=1):
        segment = preview_segments / f"{index:02d}.mp4"
        run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(source),
                "-t",
                "1.2",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                str(segment),
            ]
        )
        handle.write(f"file '{segment.resolve()}'\n")

preview_mp4 = COMBINED / "all_26_quick_preview.mp4"
run(
    [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(preview_list),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(preview_mp4),
    ]
)
preview_gif = COMBINED / "all_26_quick_preview.gif"
make_gif(preview_mp4, preview_gif)
shutil.rmtree(preview_segments)
preview_list.unlink(missing_ok=True)
concat_path.unlink(missing_ok=True)

(META / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
summary = {
    "aweme_id": AWEME_ID,
    "title": aweme.get("desc"),
    "album_item_count": len(images),
    "static_item_count": sum(1 for item in images if not isinstance(item.get("video"), dict)),
    "motion_item_count": sum(1 for item in images if isinstance(item.get("video"), dict)),
    "cover_count": len(list(COVERS.glob("*"))),
    "raw_motion_count": len(list(RAW.glob("*.mp4"))),
    "android_mp4_count": len(list(ANDROID.glob("*.mp4"))),
    "gif_count": len(list(GIFS.glob("*.gif"))),
    "combined_silent": str(combined_silent.relative_to(OUT)),
    "combined_with_music": str(combined_music.relative_to(OUT)) if combined_music else None,
    "quick_preview_mp4": str(preview_mp4.relative_to(OUT)),
    "quick_preview_gif": str(preview_gif.relative_to(OUT)),
}
(META / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

readme = f"""抖音中国主题动态背景完整提取包

作品 ID：{AWEME_ID}
总项目数：26
静态项目：1（第 1 项说明图）
动态项目：25（第 2–26 项无水印原始 MP4）

目录说明：
01_no_watermark_covers：26 张无水印封面/静态图片
02_no_watermark_original_motion_mp4：25 个抖音原始无水印动态 MP4
03_android_live_wallpaper_mp4：26 个统一为 720×1280、H.264 的安卓兼容动态背景 MP4
04_gif_animations：26 个逐项 GIF 动图
05_combined：完整串联视频、原配乐版本及快速预览
metadata：解析原始数据、清单、分辨率、时长与 SHA-256 校验值

说明：横屏原素材在安卓版本中采用模糊铺底并居中显示，不裁掉主体；原始无水印 MP4 保持原样另行保存。
"""
(OUT / "README_中文说明.txt").write_text(readme, encoding="utf-8")

# Separate archives for easier phone downloads, plus a complete archive.
zip_paths(OUT / "A_26张无水印封面图片.zip", [COVERS], [META / "summary.json"])
zip_paths(OUT / "B_25个原始无水印动态MP4.zip", [RAW], [META / "summary.json"])
zip_paths(OUT / "C_26个安卓动态壁纸MP4.zip", [ANDROID], [META / "summary.json"])
zip_paths(OUT / "D_26个GIF动图.zip", [GIFS], [META / "summary.json"])
zip_paths(
    OUT / "E_完整提取包_图片_原始MP4_安卓MP4_GIF.zip",
    [COVERS, RAW, ANDROID, GIFS, COMBINED, META],
    [OUT / "README_中文说明.txt"],
)

# Final integrity assertions.
assert len(list(COVERS.glob("*"))) == 26
assert len(list(RAW.glob("*.mp4"))) == 25
assert len(list(ANDROID.glob("*.mp4"))) == 26
assert len(list(GIFS.glob("*.gif"))) == 26
for path in list(RAW.glob("*.mp4")) + list(ANDROID.glob("*.mp4")):
    probe = ffprobe(path)
    if not probe.get("streams"):
        raise RuntimeError(f"No media stream in {path}")

print("FINAL SUMMARY")
print(json.dumps(summary, ensure_ascii=False, indent=2))
for path in sorted(OUT.iterdir()):
    if path.is_file():
        print(path.name, path.stat().st_size)
