"""Run in the sandbox by the browser venv's python, never by Specster's own:
`python -I screenshot_exec.py <origin> <outdir> <pages json> <max height px> <timeout s>`

Writes `{name}.{desktop|mobile}.png` per page and one log line per shot; a page that fails is
logged and the next one still shot.
"""

import importlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

VIEWPORTS = (("desktop", 1280, 800), ("mobile", 390, 844))
LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
# A page that shows the date or time shows the same one on both sides.
FIXED_TIME = datetime(2026, 1, 1, 12, tzinfo=UTC)
# Both waits live in the page, so a stuck one is cut at the shot's deadline.
FONTS_READY = (
    "ms => Promise.race([document.fonts.ready.then(() => true),"
    " new Promise(resolve => setTimeout(() => resolve(false), ms))])"
)
PAGE_HEIGHT = "document.documentElement.scrollHeight"


def _shoot(
    browser: Any,
    url: str,
    path: Path,
    size: tuple[int, int],
    max_height: int,
    timeout_s: float,
) -> None:
    deadline = time.monotonic() + timeout_s

    def ms() -> float:
        return max(1.0, (deadline - time.monotonic()) * 1000)

    width, height = size
    context = browser.new_context(
        viewport={"width": width, "height": height},
        locale="en-US",
        timezone_id="UTC",
        reduced_motion="reduce",
    )
    try:
        context.clock.set_fixed_time(FIXED_TIME)
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=ms())
        page.evaluate(FONTS_READY, ms())
        full = int(page.evaluate(PAGE_HEIGHT))
        page.screenshot(
            path=str(path),
            full_page=True,
            clip={"x": 0, "y": 0, "width": width, "height": min(max(full, height), max_height)},
            animations="disabled",
            timeout=ms(),
        )
    finally:
        context.close()


def main(args: list[str]) -> int:
    origin, outdir, pages_json, max_height, timeout_s = args
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    pages = json.loads(pages_json)
    # Imported here: only the browser venv has Playwright.
    sync_api: Any = importlib.import_module("playwright.sync_api")
    failed = 0
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=LAUNCH_ARGS, timeout=float(timeout_s) * 1000)
        try:
            for page in pages:
                for viewport, width, height in VIEWPORTS:
                    shot = f"{page['name']}.{viewport}.png"
                    started = time.monotonic()
                    try:
                        _shoot(
                            browser,
                            origin + page["path"],
                            out / shot,
                            (width, height),
                            int(max_height),
                            float(timeout_s),
                        )
                    except Exception as e:
                        failed += 1
                        print(f"{page['name']} {viewport}: failed: {type(e).__name__}: {e}")
                    else:
                        took = time.monotonic() - started
                        print(f"{page['name']} {viewport}: ok in {took:.1f} s")
                    sys.stdout.flush()
        finally:
            browser.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
