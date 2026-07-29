from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "douyin_page_probe_output"
OUT.mkdir(parents=True, exist_ok=True)
AWEME_ID = "7666707482186192869"
URLS = [
    "https://v.douyin.com/-K9Bpgl1daM/",
    f"https://www.douyin.com/note/{AWEME_ID}",
    f"https://www.douyin.com/aweme/detail/{AWEME_ID}",
]


def find_interesting(value: Any, path: str = "$") -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        keys = {str(k).lower() for k in value}
        if (
            "images" in keys
            or "aweme_detail" in keys
            or "live_photo_type" in keys
            or "clip_type" in keys
            or "play_addr" in keys
            or "image_post_info" in keys
        ):
            found.append({"path": path, "value": value})
        for key, child in value.items():
            found.extend(find_interesting(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(find_interesting(child, f"{path}[{index}]"))
    return found


with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--autoplay-policy=no-user-gesture-required",
        ],
    )

    profiles = [
        {
            "name": "desktop",
            "viewport": {"width": 1440, "height": 1200},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "is_mobile": False,
        },
        {
            "name": "android",
            "viewport": {"width": 430, "height": 932},
            "user_agent": "Mozilla/5.0 (Linux; Android 16; 24129PN74C) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36",
            "is_mobile": True,
        },
    ]

    for profile in profiles:
        profile_dir = OUT / profile["name"]
        profile_dir.mkdir(parents=True, exist_ok=True)
        context = browser.new_context(
            viewport=profile["viewport"],
            user_agent=profile["user_agent"],
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            is_mobile=profile["is_mobile"],
            has_touch=profile["is_mobile"],
            device_scale_factor=1,
            java_script_enabled=True,
        )
        page = context.new_page()
        responses: list[dict[str, Any]] = []
        network: list[dict[str, Any]] = []
        consoles: list[str] = []

        page.on("console", lambda message: consoles.append(f"{message.type}: {message.text}"))
        page.on("pageerror", lambda exc: consoles.append(f"pageerror: {exc}"))

        def on_response(response):
            url = response.url
            content_type = response.headers.get("content-type", "")
            network.append(
                {
                    "url": url,
                    "status": response.status,
                    "content_type": content_type,
                    "method": response.request.method,
                    "post_data": response.request.post_data,
                }
            )
            lowered = url.lower()
            relevant = (
                AWEME_ID in url
                or "aweme/detail" in lowered
                or "aweme/v1/web/aweme/detail" in lowered
                or "iteminfo" in lowered
                or "play_addr" in lowered
                or "live_photo" in lowered
                or "aweme/v1/play" in lowered
                or "note" in lowered and "douyin" in lowered
                or "json" in content_type.lower()
            )
            if not relevant:
                return
            entry: dict[str, Any] = {
                "url": url,
                "status": response.status,
                "content_type": content_type,
            }
            try:
                body = response.body()
                entry["bytes"] = len(body)
                if len(body) <= 20_000_000:
                    text = body.decode("utf-8", errors="replace")
                    try:
                        parsed = json.loads(text)
                        entry["json"] = parsed
                        entry["interesting"] = find_interesting(parsed)
                    except Exception:
                        entry["text"] = text[:1_000_000]
            except Exception as exc:
                entry["error"] = repr(exc)
            responses.append(entry)

        page.on("response", on_response)

        visits: list[dict[str, Any]] = []
        for index, url in enumerate(URLS, start=1):
            visit: dict[str, Any] = {"requested": url}
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                visit["status"] = response.status if response else None
                visit["final_url"] = page.url
                page.wait_for_timeout(12_000)
                # Scroll and interact to encourage lazy media and data requests.
                for _ in range(4):
                    page.mouse.wheel(0, 800)
                    page.wait_for_timeout(1200)
                try:
                    videos = page.locator("video")
                    visit["videos"] = [
                        {
                            "src": videos.nth(i).get_attribute("src"),
                            "poster": videos.nth(i).get_attribute("poster"),
                            "currentSrc": videos.nth(i).evaluate("e => e.currentSrc"),
                        }
                        for i in range(min(videos.count(), 100))
                    ]
                except Exception as exc:
                    visit["videos_error"] = repr(exc)
                visit["body_text"] = page.locator("body").inner_text(timeout=10_000)[:200_000]
                visit["html_bytes"] = len(page.content().encode("utf-8"))
                (profile_dir / f"visit_{index}.html").write_text(page.content(), encoding="utf-8")
                page.screenshot(path=str(profile_dir / f"visit_{index}.png"), full_page=True)

                # Inspect script JSON and well-known application state globals.
                scripts = page.locator("script")
                script_data: list[dict[str, Any]] = []
                for i in range(min(scripts.count(), 300)):
                    script = scripts.nth(i)
                    text = script.text_content() or ""
                    if AWEME_ID in text or any(token in text for token in ("aweme_detail", "live_photo_type", "play_addr", "image_post_info")):
                        script_data.append(
                            {
                                "index": i,
                                "id": script.get_attribute("id"),
                                "type": script.get_attribute("type"),
                                "text": text[:5_000_000],
                            }
                        )
                visit["interesting_scripts"] = script_data

                globals_result = page.evaluate(
                    """
                    () => {
                      const names = ['_ROUTER_DATA','RENDER_DATA','__INITIAL_STATE__','__NEXT_DATA__','SIGI_STATE','__SSR_DATA__'];
                      const out = {};
                      for (const name of names) {
                        try {
                          if (window[name] !== undefined) out[name] = window[name];
                        } catch (e) { out[name] = {error:String(e)}; }
                      }
                      return out;
                    }
                    """
                )
                visit["globals"] = globals_result
                visit["globals_interesting"] = find_interesting(globals_result)
            except Exception as exc:
                visit["error"] = repr(exc)
                try:
                    visit["final_url"] = page.url
                    (profile_dir / f"visit_{index}_error.html").write_text(page.content(), encoding="utf-8")
                    page.screenshot(path=str(profile_dir / f"visit_{index}_error.png"), full_page=True)
                except Exception:
                    pass
            visits.append(visit)

        (profile_dir / "visits.json").write_text(json.dumps(visits, ensure_ascii=False, indent=2), encoding="utf-8")
        (profile_dir / "responses.json").write_text(json.dumps(responses, ensure_ascii=False, indent=2), encoding="utf-8")
        (profile_dir / "network.json").write_text(json.dumps(network, ensure_ascii=False, indent=2), encoding="utf-8")
        (profile_dir / "console.txt").write_text("\n".join(consoles), encoding="utf-8")
        context.close()

    browser.close()

print("Douyin page probe complete")
