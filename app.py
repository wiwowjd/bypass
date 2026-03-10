from flask import Flask, request, jsonify
import asyncio
from solver import get_turnstile_token

app = Flask(__name__)


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "running",
        "service": "turnstile-solver"
    })


@app.route("/solve", methods=["POST"])
def solve():

    try:

        data = request.json

        url = data.get("url")
        sitekey = data.get("sitekey")
        action = data.get("action")
        cdata = data.get("cdata")

        if not url or not sitekey:
            return jsonify({
                "status": "error",
                "message": "url and sitekey required"
            }), 400

        result = asyncio.run(
            get_turnstile_token(
                url=url,
                sitekey=sitekey,
                action=action,
                cdata=cdata,
                browser_type="chromium",
                headless=True
            )
        )

        return jsonify(result)

    except Exception as e:

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8080
    )
