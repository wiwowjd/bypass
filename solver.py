"""
Production-ready async Cloudflare Turnstile solver.
Supports Managed Mode for real/production sitekeys.
Optimized for Railway / Docker headless environments.

Strategy:
  1. Navigate to the REAL target URL (correct origin for CF validation).
  2. Intercept only that one URL and serve our Turnstile HTML so CF sees
     the right Referer / Origin headers.
  3. Poll window.__TURNSTILE_TOKEN__ every 250ms.
  4. On CF interactive challenge, wait for the iframe to settle and re-poll.
"""

import asyncio
import os
import time
import logging
from typing import Optional
from dataclasses import dataclass, asdict

from patchright.async_api import async_playwright, Page, Browser, BrowserContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TurnstileResult:
    status: str
    turnstile_value: Optional[str]
    elapsed_time_seconds: float
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# HTML served at the target URL – CF sees the correct origin
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <title>Verify</title>
</head>
<body>
  <div id="ts-widget" style="width:300px;margin:40px auto;"></div>

  <script>
    window.__TURNSTILE_TOKEN__   = null;
    window.__TURNSTILE_ERROR__   = null;
    window.__TURNSTILE_EXPIRED__ = false;

    function onSuccess(token) {
      console.log('[TS] token received, length=' + token.length);
      window.__TURNSTILE_TOKEN__ = token;
    }
    function onError(code) {
      console.warn('[TS] error:', code);
      window.__TURNSTILE_ERROR__ = String(code || 'unknown');
    }
    function onExpire() {
      console.warn('[TS] expired');
      window.__TURNSTILE_TOKEN__   = null;
      window.__TURNSTILE_EXPIRED__ = true;
    }

    function renderWidget() {
      if (typeof turnstile === 'undefined') {
        setTimeout(renderWidget, 200);
        return;
      }
      console.log('[TS] rendering widget, sitekey=%SITEKEY%');
      turnstile.render('#ts-widget', {
        sitekey:             '%SITEKEY%',
        theme:               'light',
        callback:            onSuccess,
        'error-callback':    onError,
        'expired-callback':  onExpire,
        'refresh-expired':   'auto',
      });
    }
  </script>

  <!--
    Load api.js AFTER the callback functions are defined.
    onload=renderWidget is called by CF when the script is ready.
  -->
  <script
    src="https://challenges.cloudflare.com/turnstile/v0/api.js?onload=renderWidget"
    async defer
  ></script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Browser args – Railway / Docker hardened
# ---------------------------------------------------------------------------

_BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",          # use /tmp not tiny /dev/shm
    "--memory-pressure-off",
    "--disable-blink-features=AutomationControlled",
    "--disable-gpu",
    "--disable-gpu-sandbox",
    "--disable-software-rasterizer",
    "--no-zygote",
    "--single-process",
    "--no-first-run",
    "--no-service-autorun",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-hang-monitor",
    "--disable-client-side-phishing-detection",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-popup-blocking",
    "--disable-translate",
    "--disable-sync",
    "--disable-features=TranslateUI,BlinkGenPropertyTrees,AudioServiceOutOfProcess",
    "--enable-features=NetworkService,NetworkServiceInProcess",
    "--metrics-recording-only",
    "--mute-audio",
    "--hide-scrollbars",
    "--ignore-certificate-errors",
    "--ignore-ssl-errors",
    "--allow-running-insecure-content",
    "--window-size=1280,800",
    "--log-level=3",
    "--silent-debugger-extension-api",
]

# Headers that make the browser look like a real Chrome user
_EXTRA_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------

class AsyncTurnstileSolver:

    def __init__(self, headless: bool = True, timeout: float = 60.0):
        self.headless = headless
        self.timeout  = timeout

    def _html(self, sitekey: str) -> str:
        return _HTML_TEMPLATE.replace("%SITEKEY%", sitekey)

    async def _create_context(self, browser: Browser) -> BrowserContext:
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            java_script_enabled=True,
            # Accept all cookies so CF can set its own
            accept_downloads=False,
        )
        await ctx.set_extra_http_headers(_EXTRA_HEADERS)
        return ctx

    async def _setup_page(self, ctx: BrowserContext, url: str, sitekey: str) -> Page:
        page = await ctx.new_page()

        # Intercept the target URL and serve our Turnstile HTML.
        # CF sees the correct origin/referer because the request goes to the real host.
        html_body = self._html(sitekey)
        target    = url.rstrip("/") + "/"

        async def handle_route(route):
            # Only intercept the main document – let CF challenge iframes pass through
            if route.request.resource_type == "document" and route.request.url.rstrip("/") + "/" == target:
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=html_body,
                )
            else:
                await route.continue_()

        await page.route("**/*", handle_route)

        # Forward console logs from the page so we can debug CF errors in Railway logs
        page.on("console", lambda msg: logger.debug("[PAGE] %s %s", msg.type, msg.text))

        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            logger.warning("goto raised (continuing): %s", exc)

        return page

    async def _poll_for_token(self, page: Page) -> tuple[Optional[str], Optional[str]]:
        deadline = time.monotonic() + self.timeout

        while time.monotonic() < deadline:
            try:
                state = await page.evaluate("""() => ({
                    token:   window.__TURNSTILE_TOKEN__   || null,
                    error:   window.__TURNSTILE_ERROR__   || null,
                    expired: window.__TURNSTILE_EXPIRED__ || false
                })""")
            except Exception as exc:
                logger.debug("evaluate error: %s", exc)
                await asyncio.sleep(0.5)
                continue

            if state.get("token"):
                return state["token"], None

            if state.get("error"):
                err = state["error"]
                logger.warning("CF error code: %s", err)
                # 300023 / 300030 = interactive challenge – keep waiting
                if err in ("300023", "300030", "300031"):
                    await asyncio.sleep(1)
                    continue
                return None, f"turnstile-error-{err}"

            if state.get("expired"):
                return None, "token-expired-before-capture"

            await asyncio.sleep(0.25)

        return None, "timeout"

    async def solve(self, url: str, sitekey: str) -> TurnstileResult:
        start      = time.monotonic()
        playwright = None
        browser    = None

        try:
            playwright = await async_playwright().start()

            launch_kwargs: dict = {"headless": self.headless, "args": _BROWSER_ARGS}
            chrome_path = os.environ.get("CHROME_PATH") or os.environ.get("CHROMIUM_PATH")
            if chrome_path:
                launch_kwargs["executable_path"] = chrome_path
                logger.info("Using custom Chromium: %s", chrome_path)

            browser = await playwright.chromium.launch(**launch_kwargs)
            context = await self._create_context(browser)

            try:
                page         = await self._setup_page(context, url, sitekey)
                token, error = await self._poll_for_token(page)
            finally:
                await context.close()

            elapsed = round(time.monotonic() - start, 3)

            if token:
                logger.info("Solved in %.3fs", elapsed)
                return TurnstileResult("success", token, elapsed)

            logger.warning("Failed: %s (%.3fs)", error, elapsed)
            return TurnstileResult("failure", None, elapsed, error or "unknown")

        except Exception as exc:
            elapsed = round(time.monotonic() - start, 3)
            logger.exception("Unexpected solver error")
            return TurnstileResult("failure", None, elapsed, str(exc))

        finally:
            if browser:
                try: await browser.close()
                except Exception: pass
            if playwright:
                try: await playwright.stop()
                except Exception: pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_turnstile_token(
    url: str,
    sitekey: str,
    headless: bool = True,
    timeout: float = 60.0,
) -> dict:
    """
    Solve a Cloudflare Turnstile challenge.

    Returns dict: {status, turnstile_value, elapsed_time_seconds, reason}
    """
    solver = AsyncTurnstileSolver(headless=headless, timeout=timeout)
    result = await solver.solve(url=url, sitekey=sitekey)
    return result.to_dict()


# ---------------------------------------------------------------------------
# Smoke-test: python solver.py
# ---------------------------------------------------------------------------

