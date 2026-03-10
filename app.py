import os
import asyncio
from flask import Flask, request, jsonify
from solver import get_turnstile_token

app = Flask(__name__)


@app.route("/")
def home():
    return jsonify({
        "status": "running",
        "service": "turnstile-solver"
    })


@app.route("/solve", methods=["POST"])
def solve():

    try:

        data = request.get_json()

        url = data.get("url")
        sitekey = data.get("sitekey")

        if not url or not sitekey:
            return jsonify({
                "status": "error",
                "message": "url and sitekey required"
            }), 400

        result = asyncio.run(
            get_turnstile_token(
                url=url,
                sitekey=sitekey
            )
        )

        return jsonify(result)

    except Exception as e:

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


if __name__ == "__main__":

    port = int(os.environ.get("PORT", 8080))

    app.run(
        host="0.0.0.0",
        port=port
                      )
