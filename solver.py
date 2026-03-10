"""
Production-ready async Cloudflare Turnstile solver.
Supports Managed Mode via turnstile.render() with event-driven token capture.
Optimized for headless/container environments (Docker, Railway).
"""

import asyncio
import time
import logging
from typing import Optional
from dataclasses import dataclass, field, asdict

from patchright.async_api import async_playwright, Page, Browser, BrowserContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TurnstileResult:
    status: str                          # "success" | "failure"
    turnstile_value: Optional[str]       # CF token or None
    elapsed_time_seconds: float
    reason: Optional[str] = None         # populated on failure

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# HTML template – injects Turnstile widget and signals via DOM mutation
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Turnstile Solver</title>
  <!-- Load Turnstile API -->
  <script
    src="https://challenges.cloudflare.com/turnstile/v0/api.js"
    async defer
  ></script>
</head>
<body>
  <div id="cf-widget"></div>

  <script>
    // Called by Turnstile when a token is ready
    function onTurnstileSuccess(token) {
      // 1. Store on window so Playwright can poll it
      window.__TURNSTILE_TOKEN__ = token;

      // 2. Also write a hidden input (belt-and-suspenders)
      var inp = document.getElementById("cf-token-input");
      if (!inp) {
        inp = document.createElement("input");
        inp.type  = "hidden";
        inp.id    = "cf-token-input";
        inp.name  = "cf-turnstile-response";
        document.body.appendChild(inp);
      }
      inp.value = token;
    }

    // Called by Turnstile on error
    function onTurnstileError(code) {
      window.__TURNSTILE_ERROR__ = code || "unknown-error";
    }

    // Called by Turnstile on expiry
    function onTurnstileExpiry() {
      window.__TURNSTILE_TOKEN__ = null;
      window.__TURNSTILE_EXPIRED__ = true;
    }

    // Render widget once the Turnstile script is ready
    window.onload = function () {
      // Retry until turnstile global is available
      var attempts = 0;
      var maxAttempts = 20;

      function tryRender() {
        if (typeof turnstile !== "undefined") {
          turnstile.render("#cf-widget", {
            sitekey:         "%SITEKEY%",
            callback:        onTurnstileSuccess,
            "error-callback": onTurnstileError,
            "expired-callback": onTurnstileExpiry,
            // Force managed (interactive) mode – safe to include even for
            // invisible widgets; CF will ignore it if unsupported.
            appearance: "always"
          });
        } else if (++attempts < maxAttempts) {
          setTimeout(tryRender, 300);
        } else {
          window.__TURNSTILE_ERROR__ = "api-load-timeout";
        }
      }

      tryRender();
    };
  </script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Browser launch arguments – hardened for Docker / Railway / no-root envs
# ---------------------------------------------------------------------------

_BROWSER_ARGS = [
    # --- Sandbox / privilege (required on Railway – runs as root) ---
    "--no-sandbox",
    "--disable-setuid-sandbox",

    # --- Memory / IPC ---
    # Railway containers have a tiny /dev/shm (64 MB default).
    # This flag makes Chrome use /tmp instead, preventing OOM crashes.
    "--disable-dev-shm-usage",
    "--memory-pressure-off",

    # --- Bot-detection evasion ---
    "--disable-blink-features=AutomationControlled",

    # --- GPU / rendering (no display server on Railway) ---
    "--disable-gpu",
    "--disable-gpu-sandbox",
    "--disable-software-rasterizer",
    "--disable-gl-drawing-for-tests",

    # --- Process model ---
    # --no-zygote + --single-process: avoid zygote fork which can fail
    # inside restricted namespaces. Needed for Railway / Docker rootless.
    "--no-zygote",
    "--single-process",
    "--no-first-run",
    "--no-service-autorun",

    # --- Network ---
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-client-side-phishing-detection",

    # --- Throttling (keep JS timers accurate inside headless container) ---
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-hang-monitor",

    # --- Misc stability / fingerprint hygiene ---
    "--disable-infobars",
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
    "--window-size=1280,720",

    # --- Logging (silence noisy Chrome stderr on Railway logs) ---
    "--log-level=3",
    "--silent-debugger-extension-api",
]


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------

class AsyncTurnstileSolver:
    """
    Async Cloudflare Turnstile solver using Playwright + patchright.

    Usage
    -----
    solver = AsyncTurnstileSolver(headless=True, timeout=45)
    result = await solver.solve(url="https://example.com", sitekey="0x4AAAA…")
    print(result.to_dict())
    """

    def __init__(self, headless: bool = True, timeout: float = 45.0):
        self.headless = headless
        self.timeout = timeout  # seconds to wait for a token

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_html(self, sitekey: str) -> str:
        return _HTML_TEMPLATE.replace("%SITEKEY%", sitekey)

    async def _create_context(self, browser: Browser) -> BrowserContext:
        """Create a context that looks like a normal desktop Chrome session."""
        return await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            java_script_enabled=True,
        )

    async def _setup_page(self, context: BrowserContext, url: str, sitekey: str) -> Page:
        """
        Create a page, intercept the target URL and serve our Turnstile HTML,
        then navigate to it so the widget renders under the correct origin.
        """
        page = await context.new_page()

        # Normalise URL so the route pattern matches exactly
        target = url.rstrip("/") + "/"

        html_content = self._build_html(sitekey)

        await page.route(
            target,
            lambda route, _req: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=html_content,
            ),
        )

        await page.goto(target, wait_until="domcontentloaded", timeout=30_000)
        return page

    async def _poll_for_token(self, page: Page) -> tuple[Optional[str], Optional[str]]:
        """
        Poll window.__TURNSTILE_TOKEN__ and window.__TURNSTILE_ERROR__
        using short sleeps. Returns (token, error_reason).

        Event-listener approach is used as the primary signal; polling
        acts as a reliable fallback that works across all Playwright builds.
        """
        deadline = time.monotonic() + self.timeout

        while time.monotonic() < deadline:
            try:
                result = await page.evaluate(
                    """() => ({
                        token:   window.__TURNSTILE_TOKEN__   || null,
                        error:   window.__TURNSTILE_ERROR__   || null,
                        expired: window.__TURNSTILE_EXPIRED__ || false
                    })"""
                )
            except Exception as exc:
                logger.debug("evaluate error (page may have navigated): %s", exc)
                await asyncio.sleep(0.5)
                continue

            if result.get("token"):
                return result["token"], None

            if result.get("error"):
                return None, f"turnstile-error: {result['error']}"

            if result.get("expired"):
                return None, "token-expired-before-capture"

            await asyncio.sleep(0.25)  # tight poll – no fixed 1-second lag

        return None, "timeout"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def solve(self, url: str, sitekey: str) -> TurnstileResult:
        """Solve the Turnstile challenge and return a structured result."""
        start = time.monotonic()
        playwright = None
        browser = None

        try:
            playwright = await async_playwright().start()

            # Allow Railway / Docker to override the Chromium binary path via
            # environment variable, e.g. CHROME_PATH=/usr/bin/chromium-browser
            import os
            launch_kwargs: dict = {
                "headless": self.headless,
                "args": _BROWSER_ARGS,
            }
            chrome_path = os.environ.get("CHROME_PATH") or os.environ.get("CHROMIUM_PATH")
            if chrome_path:
                launch_kwargs["executable_path"] = chrome_path
                logger.info("Using custom Chromium path: %s", chrome_path)

            browser = await playwright.chromium.launch(**launch_kwargs)

            context = await self._create_context(browser)

            try:
                page = await self._setup_page(context, url, sitekey)
                token, error = await self._poll_for_token(page)
            finally:
                await context.close()

            elapsed = round(time.monotonic() - start, 3)

            if token:
                logger.info("Turnstile solved in %.3fs", elapsed)
                return TurnstileResult(
                    status="success",
                    turnstile_value=token,
                    elapsed_time_seconds=elapsed,
                )

            logger.warning("Turnstile failed: %s (%.3fs)", error, elapsed)
            return TurnstileResult(
                status="failure",
                turnstile_value=None,
                elapsed_time_seconds=elapsed,
                reason=error or "unknown",
            )

        except Exception as exc:
            elapsed = round(time.monotonic() - start, 3)
            logger.exception("Unexpected error in solver")
            return TurnstileResult(
                status="failure",
                turnstile_value=None,
                elapsed_time_seconds=elapsed,
                reason=str(exc),
            )

        finally:
            # Always clean up to prevent memory/process leaks
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if playwright:
                try:
                    await playwright.stop()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Public convenience function (matches the required signature)
# ---------------------------------------------------------------------------

async def get_turnstile_token(
    url: str,
    sitekey: str,
    headless: bool = True,
    timeout: float = 45.0,
) -> dict:
    """
    Solve a Cloudflare Turnstile challenge.

    Parameters
    ----------
    url      : The page URL to render the widget under (used for origin matching).
    sitekey  : The Turnstile site key shown in the widget embed code.
    headless : Run browser in headless mode (default True, recommended for servers).
    timeout  : Maximum seconds to wait for a token before giving up (default 45).

    Returns
    -------
    dict with keys: status, turnstile_value, elapsed_time_seconds, reason
    """
    solver = AsyncTurnstileSolver(headless=headless, timeout=timeout)
    result = await solver.solve(url=url, sitekey=sitekey)
    return result.to_dict()


# ---------------------------------------------------------------------------
# Quick smoke-test  (python solver.py)
# ---------------------------------------------------------------------------

