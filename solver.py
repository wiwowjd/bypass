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
        <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async></script>
    </head>
    <body>
        <!-- cf turnstile -->
    </body>
    </html>
    """

    def __init__(self, headless=True):
        self.headless = headless

    async def _setup_page(self, browser, url, sitekey, action=None, cdata=None):

        page = await browser.new_page()

        url_with_slash = url + "/" if not url.endswith("/") else url

        turnstile_div = f'<div class="cf-turnstile" data-sitekey="{sitekey}"'

        if action:
            turnstile_div += f' data-action="{action}"'

        if cdata:
            turnstile_div += f' data-cdata="{cdata}"'

        turnstile_div += "></div>"

        html = self.HTML_TEMPLATE.replace(
            "<!-- cf turnstile -->",
            turnstile_div
        )

        await page.route(
            url_with_slash,
            lambda route: route.fulfill(
                body=html,
                status=200
            )
        )

        await page.goto(url_with_slash)

        return page

    async def _get_turnstile_response(self, page, attempts=10):

        for _ in range(attempts):

            try:

                value = await page.input_value(
                    "[name=cf-turnstile-response]"
                )

                if value:

                    el = await page.query_selector(
                        "[name=cf-turnstile-response]"
                    )

                    return await el.get_attribute("value")

                else:

                    await page.click(
                        "//div[@class='cf-turnstile']",
                        timeout=3000
                    )

                    await asyncio.sleep(1)

            except:

                await asyncio.sleep(1)

        return None

    async def solve(self, url, sitekey, action=None, cdata=None):

        start = time.time()

        playwright = await async_playwright().start()

        browser = await playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--no-zygote",
                "--single-process"
            ]
        )

        try:

            page = await self._setup_page(
                browser,
                url,
                sitekey,
                action,
                cdata
            )

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
    action=None,
    cdata=None,
    headless=True,
    browser_type="chromium"
):

    solver = AsyncTurnstileSolver(headless=headless)

    result = await solver.solve(
        url=url,
        sitekey=sitekey,
        action=action,
        cdata=cdata
    )

    return result.__dict__
