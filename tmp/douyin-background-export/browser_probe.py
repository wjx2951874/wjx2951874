from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "browser_probe_output"
OUT.mkdir(parents=True, exist_ok=True)
SHARE_URL = "https://v.douyin.com/-K9Bpgl1daM/"
TARGETS = [
    "https://parse.ideaflow.top/",
    "https://www.tisho.cn/",
    "https://qsy.jyblog.com/",
]


def safe_name(url: str) -> str:
    host = urlparse(url).netloc.replace(".", "_")
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", host)


def serialize_element(el: Any) -> dict[str, Any]:
    return {
        "tag": el.evaluate("e => e.tagName"),
        "text": (el.inner_text(timeout=1000) if el.is_visible() else "")[:500],
        "href": el.get_attribute("href"),
        "src": el.get_attribute("src"),
        "download": el.get_attribute("download"),
        "class": el.get_attribute("class"),
        "type": el.get_attribute("type"),
        "placeholder": el.get_attribute("placeholder"),
    }


with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        viewport={"width": 1280, "height": 1800},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        accept_downloads=True,
    )

    overall: dict[str, Any] = {}

    for target in TARGETS:
        name = safe_name(target)
        target_dir = OUT / name
        target_dir.mkdir(parents=True, exist_ok=True)
        page = context.new_page()
        console_logs: list[str] = []
        network: list[dict[str, Any]] = []
        json_bodies: list[dict[str, Any]] = []
        downloads: list[dict[str, Any]] = []

        page.on("console", lambda msg, logs=console_logs: logs.append(f"{msg.type}: {msg.text}"))
        page.on("pageerror", lambda exc, logs=console_logs: logs.append(f"pageerror: {exc}"))

        def on_response(response):
            entry = {
                "url": response.url,
                "status": response.status,
                "content_type": response.headers.get("content-type", ""),
                "request_method": response.request.method,
                "request_post_data": response.request.post_data,
            }
            network.append(entry)
            ct = entry["content_type"].lower()
            interesting = (
                "json" in ct
                or any(token in response.url.lower() for token in ("api", "parse", "douyin", "live", "media"))
            )
            if interesting:
                try:
                    body = response.text()
                    if len(body) <= 5_000_000:
                        try:
                            parsed = json.loads(body)
                            json_bodies.append({"url": response.url, "status": response.status, "json": parsed})
                        except Exception:
                            json_bodies.append({"url": response.url, "status": response.status, "text": body[:200_000]})
                except Exception as exc:
                    json_bodies.append({"url": response.url, "status": response.status, "error": repr(exc)})

        page.on("response", on_response)

        def on_download(download):
            try:
                suggested = download.suggested_filename
                path = target_dir / f"download_{len(downloads)+1:02d}_{suggested}"
                download.save_as(str(path))
                downloads.append({"url": download.url, "suggested_filename": suggested, "path": str(path)})
            except Exception as exc:
                downloads.append({"url": download.url, "error": repr(exc)})

        page.on("download", on_download)

        result: dict[str, Any] = {"target": target}
        try:
            response = page.goto(target, wait_until="domcontentloaded", timeout=60_000)
            result["navigation_status"] = response.status if response else None
            page.wait_for_timeout(4000)

            # Dismiss common introduction modal.
            for text in ("我知道了", "知道了", "关闭"):
                try:
                    locator = page.get_by_text(text, exact=True)
                    if locator.count() and locator.first.is_visible():
                        locator.first.click(timeout=3000)
                        page.wait_for_timeout(500)
                        break
                except Exception:
                    pass

            inputs = page.locator("input, textarea")
            result["input_count"] = inputs.count()
            input_locator = None
            for i in range(inputs.count()):
                candidate = inputs.nth(i)
                try:
                    if candidate.is_visible() and candidate.is_enabled():
                        input_locator = candidate
                        break
                except Exception:
                    continue
            if input_locator is None:
                raise RuntimeError("No visible input or textarea")

            input_locator.fill(SHARE_URL)
            page.wait_for_timeout(500)

            clicked = False
            for selector_text in ("解析", "开始解析", "去除水印", "搜索"):
                try:
                    button = page.get_by_role("button", name=re.compile(selector_text)).first
                    if button.is_visible() and button.is_enabled():
                        button.click(timeout=5000)
                        clicked = True
                        result["clicked_button"] = selector_text
                        break
                except Exception:
                    pass
            if not clicked:
                # Fallback: click the first visible button adjacent to the input.
                buttons = page.locator("button")
                for i in range(buttons.count()):
                    button = buttons.nth(i)
                    try:
                        if button.is_visible() and button.is_enabled():
                            text = button.inner_text().strip()
                            if text and text not in ("我知道了", "关闭"):
                                button.click(timeout=5000)
                                clicked = True
                                result["clicked_button"] = text
                                break
                    except Exception:
                        continue
            if not clicked:
                raise RuntimeError("Could not find parse button")

            # Wait until response cards, Live Photo labels, or download controls appear.
            deadline = time.time() + 75
            while time.time() < deadline:
                body_text = page.locator("body").inner_text(timeout=5000)
                if any(token in body_text for token in ("实况图集", "下载实况", "批量下载", "解析成功", "LIVE")):
                    if "输入链接开始解析" not in body_text or len(json_bodies) > 0:
                        break
                page.wait_for_timeout(1500)

            page.wait_for_timeout(5000)
            result["body_text"] = page.locator("body").inner_text(timeout=5000)[:100_000]
            result["url_after"] = page.url

            elements = page.locator("a, button, img, video, source")
            serialized: list[dict[str, Any]] = []
            for i in range(min(elements.count(), 500)):
                try:
                    serialized.append(serialize_element(elements.nth(i)))
                except Exception:
                    pass
            result["elements"] = serialized

            # Capture useful global JS state when available.
            result["window_keys"] = page.evaluate(
                "Object.keys(window).filter(k => /data|app|live|media|parse/i.test(k)).slice(0,200)"
            )
        except Exception as exc:
            result["error"] = repr(exc)

        try:
            page.screenshot(path=str(target_dir / "page.png"), full_page=True)
        except Exception as exc:
            result["screenshot_error"] = repr(exc)
        try:
            (target_dir / "page.html").write_text(page.content(), encoding="utf-8")
        except Exception as exc:
            result["html_error"] = repr(exc)

        (target_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (target_dir / "network.json").write_text(json.dumps(network, ensure_ascii=False, indent=2), encoding="utf-8")
        (target_dir / "captured_responses.json").write_text(json.dumps(json_bodies, ensure_ascii=False, indent=2), encoding="utf-8")
        (target_dir / "console.txt").write_text("\n".join(console_logs), encoding="utf-8")
        (target_dir / "downloads.json").write_text(json.dumps(downloads, ensure_ascii=False, indent=2), encoding="utf-8")
        overall[name] = result
        page.close()

    (OUT / "overall.json").write_text(json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8")
    browser.close()

print("Browser probe completed")
