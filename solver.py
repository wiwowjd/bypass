"""
Production-ready async Cloudflare Turnstile solver.
Uses page.set_content() to inject Turnstile widget without routing tricks.
Optimized for Railway / Docker headless environments.
"""

import asyncio
import os
import time
import logging
from typing import Optional
from dataclasses import dataclass, asdict
from urllib.parse import urlparse

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
# HTML – minimal, clean, no race conditions
# Turnstile JS is loaded synchronously via ?render=explicit
# so we control exactly when render() is called.
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <title>CF</title>
  <script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"></script>
</head>
<body>
  <div id="ts" style="position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);"></div>
  <script>
    window.__TURNSTILE_TOKEN__   = null;
    window.__TURNSTILE_ERROR__   = null;
    window.__TURNSTILE_EXPIRED__ = false;

    function waitAndRender() {
      if (typeof turnstile === 'undefined' || typeof turnstile.render !== 'function') {
        setTimeout(waitAndRender, 100);
        return;
      }
      turnstile.render('#ts', {
        sitekey:            '__SITEKEY__',
        callback:           function(t){ window.__TURNSTILE_TOKEN__ = t; },
        'error-callback':   function(c){ window.__TURNSTILE_ERROR__ = String(c || 'err'); },
        'expired-callback': function(){ window.__TURNSTILE_TOKEN__ = null; window.__TURNSTILE_EXPIRED__ = true; },
        'refresh-expired':  'auto',
      });
    }
    document.addEventListener('DOMContentLoaded', waitAndRender);
  </script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Browser args – Railway / Docker
# ---------------------------------------------------------------------------

_BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
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


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------

class AsyncTurnstileSolver:

    def __init__(self, headless: bool = True, timeout: float = 60.0):
        self.headless = headless
        self.timeout  = timeout

    async def _create_context(self, browser: Browser, url: str) -> BrowserContext:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

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
            # Spoof origin so Turnstile validation uses the correct domain
            extra_http_headers={
                "Referer": url,
                "Origin":  origin,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        return ctx

    async def _build_page(self, ctx: BrowserContext, url: str, sitekey: str) -> Page:
        page = await ctx.new_page()

        # Forward page console + errors to Railway logs
        page.on("console", lambda m: logger.debug("[PAGE] %s %s", m.type, m.text))
        page.on("pageerror", lambda e: logger.warning("[PAGE-ERR] %s", e))

        html = _HTML_TEMPLATE.replace("__SITEKEY__", sitekey)

        # Navigate to real URL first so cookies/origin are set correctly,
        # then overwrite with our Turnstile HTML in place.
        # This makes CF see the correct domain in the widget's origin check.
        try:
            await page.goto(url, wait_until="commit", timeout=20_000)
        except Exception as exc:
            logger.warning("Initial goto failed (ok): %s", exc)

        # Overwrite page content — keeps the URL/origin intact
        await page.set_content(html, wait_until="domcontentloaded")

        return page

    async def _poll(self, page: Page) -> tuple[Optional[str], Optional[str]]:
        deadline = time.monotonic() + self.timeout
        last_log = 0.0

        while time.monotonic() < deadline:
            try:
                s = await page.evaluate("""() => ({
                    token:   window.__TURNSTILE_TOKEN__   || null,
                    error:   window.__TURNSTILE_ERROR__   || null,
                    expired: window.__TURNSTILE_EXPIRED__ || false
                })""")
            except Exception as exc:
                logger.debug("evaluate error: %s", exc)
                await asyncio.sleep(0.5)
                continue

            if s.get("token"):
                return s["token"], None

            if s.get("error"):
                err = str(s["error"])
                logger.warning("CF error: %s", err)
                # Interactive challenge codes – keep waiting
                if err in ("300023", "300030", "300031", "600010"):
                    await asyncio.sleep(1)
                    continue
                return None, f"turnstile-error-{err}"

            if s.get("expired"):
                return None, "token-expired"

            # Progress log every 10s
            now = time.monotonic()
            if now - last_log >= 10:
                remaining = deadline - now
                logger.info("Waiting for token… %.0fs remaining", remaining)
                last_log = now

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

            browser = await playwright.chromium.launch(**launch_kwargs)
            context = await self._create_context(browser, url)

            try:
                page         = await self._build_page(context, url, sitekey)
                token, error = await self._poll(page)
            finally:
                await context.close()

            elapsed = round(time.monotonic() - start, 3)

            if token:
                logger.info("Solved in %.3fs  token[:20]=%s…", elapsed, token[:20])
                return TurnstileResult("success", token, elapsed)

            logger.warning("Failed: %s (%.3fs)", error, elapsed)
            return TurnstileResult("failure", None, elapsed, error or "unknown")

        except Exception as exc:
            elapsed = round(time.monotonic() - start, 3)
            logger.exception("Unexpected error")
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
    Returns: {status, turnstile_value, elapsed_time_seconds, reason}
    """
    solver = AsyncTurnstileSolver(headless=headless, timeout=timeout)
    return (await solver.solve(url, sitekey)).to_dict()


# ---------------------------------------------------------------------------
# Smoke-test: python solver.py
# ---------------------------------------------------------------------------

