import asyncio
import time
from typing import Optional
from dataclasses import dataclass
from patchright.async_api import async_playwright


@dataclass
class TurnstileResult:
    turnstile_value: Optional[str]
    elapsed_time_seconds: float
    status: str
    reason: Optional[str] = None


class AsyncTurnstileSolver:

    HTML_TEMPLATE = """
    <!DOCTYPE html>
    <html>
    <head>
        <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
    </head>
    <body>
        <div id="cf"></div>

        <script>
        window.onload = function() {
            turnstile.render('#cf', {
                sitekey: '%SITEKEY%',
                callback: function(token) {
                    let input = document.createElement("input");
                    input.type = "hidden";
                    input.name = "cf-turnstile-response";
                    input.value = token;
                    document.body.appendChild(input);
                }
            });
        };
        </script>
    </body>
    </html>
    """

    def __init__(self, headless=True):
        self.headless = headless

    async def _setup_page(self, browser, url, sitekey):

        page = await browser.new_page()

        url_with_slash = url if url.endswith("/") else url + "/"

        html = self.HTML_TEMPLATE.replace("%SITEKEY%", sitekey)

        await page.route(
            url_with_slash,
            lambda route: route.fulfill(
                body=html,
                status=200,
                content_type="text/html"
            )
        )

        await page.goto(url_with_slash)

        return page

    async def _get_turnstile_response(self, page, timeout=30):

        start = time.time()

        while time.time() - start < timeout:

            try:

                token = await page.evaluate("""
                () => {
                    let el = document.querySelector('[name="cf-turnstile-response"]');
                    return el ? el.value : null;
                }
                """)

                if token:
                    return token

            except:
                pass

            await asyncio.sleep(1)

        return None

    async def solve(self, url, sitekey):

        start = time.time()

        playwright = await async_playwright().start()

        browser = await playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--no-first-run",
                "--no-zygote",
                "--single-process",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding"
            ]
        )

        try:

            page = await self._setup_page(browser, url, sitekey)

            token = await self._get_turnstile_response(page)

            elapsed = round(time.time() - start, 3)

            if not token:

                return TurnstileResult(
                    turnstile_value=None,
                    elapsed_time_seconds=elapsed,
                    status="failure",
                    reason="token not found"
                )

            return TurnstileResult(
                turnstile_value=token,
                elapsed_time_seconds=elapsed,
                status="success"
            )

        finally:

            await browser.close()
            await playwright.stop()


async def get_turnstile_token(
    url,
    sitekey,
    headless=True
):

    solver = AsyncTurnstileSolver(headless=headless)

    result = await solver.solve(
        url=url,
        sitekey=sitekey
    )

    return result.__dict__
