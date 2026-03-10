"""
Flask API for Cloudflare Turnstile solver.
Uses a persistent event loop to avoid asyncio.run() conflicts in threaded Flask.
"""

import os
import asyncio
import logging
import threading
import subprocess
import socket
from flask import Flask, request, jsonify
from solver import get_turnstile_token

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Persistent background event loop
# ---------------------------------------------------------------------------

_loop = asyncio.new_event_loop()
_loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
_loop_thread.start()


def run_async(coro):
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    return jsonify({"status": "running", "service": "turnstile-solver"})


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/debug")
def debug():
    """
    Diagnose the Railway environment:
    - Can Chromium launch?
    - Can it reach challenges.cloudflare.com?
    - What does the page console say when Turnstile loads?
    Hit this endpoint first when /solve times out.
    """
    results = {}

    # 1. DNS resolution
    for host in ["challenges.cloudflare.com", "unlimitedclaude.com"]:
        try:
            ip = socket.gethostbyname(host)
            results[f"dns_{host}"] = ip
        except Exception as e:
            results[f"dns_{host}"] = f"FAILED: {e}"

    # 2. Chromium version
    try:
        from patchright.async_api import async_playwright

        async def _chromium_version():
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox",
                          "--disable-dev-shm-usage", "--no-zygote",
                          "--single-process", "--disable-gpu"],
                )
                version = browser.version
                await browser.close()
                return version

        version = run_async(_chromium_version())
        results["chromium_version"] = version
    except Exception as e:
        results["chromium_version"] = f"FAILED: {e}"

    # 3. Full browser test – load Turnstile page and capture ALL console output
    try:
        from patchright.async_api import async_playwright

        async def _browser_test():
            logs = []
            errors = []
            network_failures = []

            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox", "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage", "--no-zygote",
                        "--single-process", "--disable-gpu",
                        "--disable-blink-features=AutomationControlled",
                        "--ignore-certificate-errors",
                    ],
                )
                ctx = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    )
                )
                page = await ctx.new_page()

                page.on("console",  lambda m: logs.append(f"[{m.type}] {m.text}"))
                page.on("pageerror", lambda e: errors.append(str(e)))

                # Track failed network requests
                page.on("requestfailed", lambda r: network_failures.append(
                    f"{r.url} — {r.failure}"
                ))

                html = """<!DOCTYPE html><html><head>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"></script>
</head><body>
<div id="ts"></div>
<script>
window.__TOKEN__ = null;
window.__ERR__   = null;
function waitAndRender(){
  if(typeof turnstile==='undefined'||typeof turnstile.render!=='function'){
    setTimeout(waitAndRender,200); return;
  }
  console.log('TURNSTILE_READY');
  turnstile.render('#ts',{
    sitekey:'0x4AAAAAACneiSOWK6AiWeJr',
    callback:function(t){console.log('TOKEN_OK:'+t.substring(0,30));window.__TOKEN__=t;},
    'error-callback':function(c){console.warn('TOKEN_ERR:'+c);window.__ERR__=String(c);},
  });
}
document.addEventListener('DOMContentLoaded', waitAndRender);
</script></body></html>"""

                # goto real domain first for correct origin
                try:
                    await page.goto(
                        "https://unlimitedclaude.com/signup",
                        wait_until="commit", timeout=15_000
                    )
                except Exception as e:
                    logs.append(f"[goto-warn] {e}")

                await page.set_content(html, wait_until="domcontentloaded")

                # Wait up to 20s for any result
                import time
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    state = await page.evaluate("""() => ({
                        token: window.__TOKEN__ || null,
                        err:   window.__ERR__   || null
                    })""")
                    if state["token"] or state["err"]:
                        break
                    await asyncio.sleep(0.5)

                final = await page.evaluate("""() => ({
                    token: window.__TOKEN__ || null,
                    err:   window.__ERR__   || null,
                    turnstileLoaded: typeof turnstile !== 'undefined'
                })""")

                await ctx.close()
                await browser.close()

            return {
                "console_logs":       logs,
                "page_errors":        errors,
                "network_failures":   network_failures,
                "turnstile_loaded":   final.get("turnstileLoaded"),
                "token_received":     bool(final.get("token")),
                "token_preview":      (final.get("token") or "")[:40] or None,
                "cf_error_code":      final.get("err"),
            }

        results["browser_test"] = run_async(_browser_test())

    except Exception as e:
        results["browser_test"] = f"FAILED: {e}"

    return jsonify(results), 200


@app.route("/solve", methods=["POST"])
def solve():
    data = request.get_json(silent=True)

    if not data:
        return jsonify({"status": "error", "message": "JSON body required"}), 400

    url     = data.get("url")
    sitekey = data.get("sitekey")
    timeout = float(data.get("timeout", 60))

    if not url or not sitekey:
        return jsonify({
            "status": "error",
            "message": "'url' and 'sitekey' are required fields",
        }), 400

    try:
        result = run_async(
            get_turnstile_token(url=url, sitekey=sitekey, headless=True, timeout=timeout)
        )
        http_status = 200 if result.get("status") == "success" else 422
        return jsonify(result), http_status

    except Exception as exc:
        logger.exception("Unhandled error in /solve")
        return jsonify({"status": "error", "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info("Starting turnstile-solver on port %d", port)
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)
