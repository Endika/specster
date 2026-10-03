import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from specster import screenshot_exec
from specster.evidence import PNG_MAGIC

ORIGIN = "http://127.0.0.1:8000"
HEIGHTS = {"/": 500, "/long": 90_000, "/missing": 500, "/blank": 500}


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class FakePage:
    def __init__(self, context: "FakeContext") -> None:
        self.context = context
        self.url = ""

    def goto(self, url: str, wait_until: str, timeout: float) -> FakeResponse | None:
        self.context.calls.append(("goto", url, wait_until))
        assert 0 < timeout <= 5_000
        if url.endswith("/broken"):
            raise TimeoutError("Timeout 5000ms exceeded.\n  navigating to /broken")
        self.url = url
        # Playwright answers None for a navigation with no HTTP response, such as about:blank.
        return {"/missing": FakeResponse(404), "/blank": None}.get(
            url.removeprefix(ORIGIN), FakeResponse(200)
        )

    def evaluate(self, script: str, arg: float | None = None) -> object:
        self.context.calls.append(("evaluate", script, arg))
        if "scrollHeight" in script:
            return HEIGHTS[self.url.removeprefix(ORIGIN)]
        return True

    def screenshot(self, path: str, **kwargs: Any) -> None:
        self.context.calls.append(("screenshot", kwargs))
        Path(path).write_bytes(PNG_MAGIC + self.url.encode())


class FakeClock:
    def __init__(self, context: "FakeContext") -> None:
        self.context = context

    def set_fixed_time(self, when: object) -> None:
        self.context.calls.append(("clock", when))


class FakeContext:
    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        self.calls: list[tuple[object, ...]] = []
        self.clock = FakeClock(self)
        self.closed = False

    def new_page(self) -> FakePage:
        return FakePage(self)

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, args: list[str]) -> None:
        self.args = args
        self.contexts: list[FakeContext] = []
        self.closed = False

    def new_context(self, **options: Any) -> FakeContext:
        self.contexts.append(FakeContext(options))
        return self.contexts[-1]

    def close(self) -> None:
        self.closed = True


class FakePlaywright:
    def __init__(self) -> None:
        self.browsers: list[FakeBrowser] = []
        self.chromium = self

    def launch(self, args: list[str], timeout: float) -> FakeBrowser:
        self.browsers.append(FakeBrowser(args))
        return self.browsers[-1]

    def __enter__(self) -> "FakePlaywright":
        return self

    def __exit__(self, *_: object) -> None:
        pass


@pytest.fixture
def playwright(monkeypatch: pytest.MonkeyPatch) -> FakePlaywright:
    fake = FakePlaywright()
    module = types.ModuleType("playwright.sync_api")
    module.__dict__["sync_playwright"] = lambda: fake
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return fake


def run(out: Path, pages: list[dict[str, str]], capsys: pytest.CaptureFixture[str]) -> list[str]:
    code = screenshot_exec.main([ORIGIN, str(out), json.dumps(pages), "6000", "5"])
    assert code == (1 if any(p["path"] == "/broken" for p in pages) else 0)
    return capsys.readouterr().out.splitlines()


def test_each_page_is_shot_on_desktop_and_mobile_with_a_steady_browser(
    tmp_path: Path, playwright: FakePlaywright, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "shots"
    log = run(out, [{"name": "home", "path": "/"}], capsys)
    (browser,) = playwright.browsers
    assert browser.args == ["--no-sandbox", "--disable-dev-shm-usage"] and browser.closed
    desktop, mobile = browser.contexts
    assert desktop.options == {
        "viewport": {"width": 1280, "height": 800},
        "locale": "en-US",
        "timezone_id": "UTC",
        "reduced_motion": "reduce",
    }
    assert mobile.options["viewport"] == {"width": 390, "height": 844}
    assert desktop.closed and mobile.closed
    assert desktop.calls[0] == ("clock", screenshot_exec.FIXED_TIME)
    assert desktop.calls[1] == ("goto", ORIGIN + "/", "networkidle")
    (fonts,) = [c for c in desktop.calls if c[:2] == ("evaluate", screenshot_exec.FONTS_READY)]
    assert isinstance(fonts[2], float) and 0 < fonts[2] <= 5_000
    kwargs = desktop.calls[-1][1]
    assert isinstance(kwargs, dict)
    assert kwargs["full_page"] and kwargs["animations"] == "disabled"
    assert kwargs["clip"] == {"x": 0, "y": 0, "width": 1280, "height": 800}
    assert sorted(p.name for p in out.iterdir()) == [
        "home.desktop.png",
        "home.desktop.status",
        "home.mobile.png",
        "home.mobile.status",
    ]
    assert (out / "home.mobile.png").read_bytes() == PNG_MAGIC + (ORIGIN + "/").encode()
    assert (out / "home.mobile.status").read_text() == "200"
    assert [line.split(" in ")[0] for line in log] == ["home desktop: ok", "home mobile: ok"]
    assert all(line.endswith(" s (HTTP 200)") for line in log)


@pytest.mark.usefixtures("playwright")
def test_the_status_each_page_answered_with_is_kept_and_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pages = [{"name": "missing", "path": "/missing"}, {"name": "blank", "path": "/blank"}]
    log = run(tmp_path, pages, capsys)
    assert (tmp_path / "missing.desktop.status").read_text() == "404"
    assert (tmp_path / "blank.mobile.status").read_text() == "none"
    assert (tmp_path / "missing.desktop.png").exists() and (tmp_path / "blank.mobile.png").exists()
    assert log[0].startswith("missing desktop: ok in ") and log[0].endswith(" (HTTP 404)")
    assert log[3].startswith("blank mobile: ok in ") and log[3].endswith(" (no response)")


def test_a_long_page_is_cut_at_the_height_limit(
    tmp_path: Path, playwright: FakePlaywright, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, [{"name": "long", "path": "/long"}], capsys)
    for context in playwright.browsers[0].contexts:
        kwargs = context.calls[-1][1]
        assert isinstance(kwargs, dict) and kwargs["clip"]["height"] == 6000


def test_a_page_that_fails_is_logged_and_the_rest_still_shot(
    tmp_path: Path, playwright: FakePlaywright, capsys: pytest.CaptureFixture[str]
) -> None:
    pages = [{"name": "broken", "path": "/broken"}, {"name": "home", "path": "/"}]
    log = run(tmp_path, pages, capsys)
    assert log[0] == "broken desktop: failed: TimeoutError: Timeout 5000ms exceeded."
    assert log[1] == "  navigating to /broken"
    assert sorted(p.name for p in tmp_path.glob("*.png")) == [
        "home.desktop.png",
        "home.mobile.png",
    ]
    assert not (tmp_path / "broken.desktop.status").exists()
    assert all(c.closed for c in playwright.browsers[0].contexts)
