"""
Flask API for Cloudflare Turnstile solver.
"""

import os
import asyncio
import logging
import threading
import socket
from flask import Flask, request, jsonify
from solver import get_turnstile_token

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

_loop = asyncio.new_event_loop()
_loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
_loop_thread.start()


def run_async(coro):
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result()


@app.route("/")
def home():
    return jsonify({"status": "running", "service": "turnstile-solver"})


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/debug")
def debug():
    """
    Full environment + Turnstile diagnostic.
    Uses the same blank-page / spoof-origin strategy as solver.py.
    """
    results = {}

    # DNS
    for host in ["challenges.cloudflare.com", "unlimitedclaude.com"]:
        try:
            results[f"dns_{host}"] = socket.gethostbyname(host)
        except Exception as e:
            results[f"dns_{host}"] = f"FAILED: {e}"

    # Chromium version
    try:
        from patchright.async_api import async_playwright

        async def _version():
            async with async_playwright() as p:
                b = await p.chromium.launch(headless=True, args=[
                    "--no-sandbox", "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage", "--no-zygote",
                    "--single-process", "--disable-gpu",
                ])
                v = b.version
                await b.close()
                return v

        results["chromium_version"] = run_async(_version())
    except Exception as e:
        results["chromium_version"] = f"FAILED: {e}"

    # Browser test — same strategy as solver (blank page, spoofed origin)
    try:
        from patchright.async_api import async_playwright
        from urllib.parse import urlparse

        TARGET_URL  = "https://unlimitedclaude.com/signup"
        TARGET_KEY  = "0x4AAAAAACneiSOWK6AiWeJr"
        parsed      = urlparse(TARGET_URL)
        origin      = f"{parsed.scheme}://{parsed.netloc}"

        async def _browser_test():
            logs, errors, net_fail = [], [], []

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=[
                    "--no-sandbox", "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage", "--no-zygote",
                    "--single-process", "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                    "--ignore-certificate-errors",
                ])

                ctx = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    extra_http_headers={
                        "Referer":         TARGET_URL,
                        "Origin":          origin,
                        "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Sec-Fetch-Site":  "same-origin",
                    },
                )

                page = await ctx.new_page()
                page.on("console",      lambda m: logs.append(f"[{m.type}] {m.text}"))
                page.on("pageerror",    lambda e: errors.append(str(e)))
                page.on("requestfailed",lambda r: net_fail.append(f"{r.url} — {r.failure}"))

                html = """<!DOCTYPE html><html><head>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"></script>
</head><body><div id="ts"></div><script>
window.__T__=null; window.__E__=null;
function go(){
  if(typeof turnstile==='undefined'||typeof turnstile.render!=='function'){setTimeout(go,100);return;}
  console.log('TURNSTILE_READY');
  turnstile.render('#ts',{
    sitekey:'""" + TARGET_KEY + """',
    callback:function(t){console.log('TOKEN_OK:'+t.substring(0,30));window.__T__=t;},
    'error-callback':function(c){console.warn('TOKEN_ERR:'+String(c));window.__E__=String(c||'err');},
  });
}
document.addEventListener('DOMContentLoaded',go);
</script></body></html>"""

                await page.set_content(html, wait_until="domcontentloaded")

                import time
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    s = await page.evaluate("()=>({t:window.__T__||null,e:window.__E__||null})")
                    if s["t"] or s["e"]:
                        break
                    await asyncio.sleep(0.5)

                final = await page.evaluate("""()=>({
                    token: window.__T__ || null,
                    err:   window.__E__ || null,
                    tsLoaded: typeof turnstile !== 'undefined'
                })""")

                await ctx.close()
                await browser.close()

            return {
                "console_logs":     logs,
                "page_errors":      errors,
                "network_failures": net_fail,
                "turnstile_loaded": final.get("tsLoaded"),
                "token_received":   bool(final.get("token")),
                "token_preview":    (final.get("token") or "")[:40] or None,
                "cf_error_code":    final.get("err"),
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
        return jsonify({"status": "error", "message": "'url' and 'sitekey' required"}), 400

    try:
        result = run_async(get_turnstile_token(url=url, sitekey=sitekey, headless=True, timeout=timeout))
        return jsonify(result), 200 if result.get("status") == "success" else 422
    except Exception as exc:
        logger.exception("Unhandled error in /solve")
        return jsonify({"status": "error", "message": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info("Starting on port %d", port)
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)
