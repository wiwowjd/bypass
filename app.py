"""
Flask API for Cloudflare Turnstile solver.
Uses a persistent event loop to avoid asyncio.run() conflicts in threaded Flask.
"""

import os
import asyncio
import logging
import threading
from flask import Flask, request, jsonify
from solver import get_turnstile_token

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Persistent background event loop
# This avoids "no running event loop" and "loop already closed" issues that
# occur when asyncio.run() is called repeatedly inside a threaded Flask server.
# ---------------------------------------------------------------------------

_loop = asyncio.new_event_loop()
_loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
_loop_thread.start()


def run_async(coro):
    """Submit a coroutine to the background loop and block until done."""
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result()  # blocks the Flask worker thread


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    return jsonify({
        "status": "running",
        "service": "turnstile-solver",
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/solve", methods=["POST"])
def solve():
    data = request.get_json(silent=True)

    if not data:
        return jsonify({"status": "error", "message": "JSON body required"}), 400

    url     = data.get("url")
    sitekey = data.get("sitekey")
    timeout = float(data.get("timeout", 45))

    if not url or not sitekey:
        return jsonify({
            "status": "error",
            "message": "'url' and 'sitekey' are required fields",
        }), 400

    try:
        result = run_async(
            get_turnstile_token(
                url=url,
                sitekey=sitekey,
                headless=True,
                timeout=timeout,
            )
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
    # use_reloader=False is critical – reloader forks the process and breaks
    # the background event loop thread.
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)
