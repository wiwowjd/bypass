from flask import Flask, request, jsonify
import asyncio
from solver import get_turnstile_token

app = Flask(__name__)

@app.route("/solve", methods=["POST"])
def solve():
    data = request.json

    url = data.get("url")
    sitekey = data.get("sitekey")

    result = asyncio.run(
        get_turnstile_token(
            url=url,
            sitekey=sitekey,
            browser_type="chromium",
            headless=True
        )
    )

    return jsonify(result)

app.run(host="0.0.0.0", port=8080)
