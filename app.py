import os
from flask import Flask, request, jsonify
from cloudflyer import Cloudflyer

app = Flask(__name__)

CLIENT_KEY = os.getenv("CLIENT_KEY", "hiroxen")

solver = Cloudflyer(
    client_key=CLIENT_KEY,
    max_tasks=5
)

def check_key(req):
    key = req.headers.get("x-api-key") or req.json.get("clientKey")
    return key == CLIENT_KEY


@app.route("/")
def home():
    return jsonify({
        "service": "cloudflyer bypass api",
        "endpoints": [
            "/bypass-turnstile",
            "/bypass-recaptcha",
            "/bypass-cloudflare"
        ]
    })


# -----------------------------
# TURNSTILE
# -----------------------------
@app.route("/bypass-turnstile", methods=["POST"])
def bypass_turnstile():

    if not check_key(request):
        return jsonify({"error": "invalid api key"}), 403

    data = request.json
    url = data.get("url")
    sitekey = data.get("sitekey")
    proxy = data.get("proxy")
    userAgent = data.get("userAgent")

    try:

        result = solver.solve_turnstile(
            url=url,
            sitekey=sitekey,
            proxy=proxy,
            user_agent=userAgent
        )

        return jsonify({
            "status": "success",
            "token": result
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        })


# -----------------------------
# RECAPTCHA
# -----------------------------
@app.route("/bypass-recaptcha", methods=["POST"])
def bypass_recaptcha():

    if not check_key(request):
        return jsonify({"error": "invalid api key"}), 403

    data = request.json
    url = data.get("url")
    sitekey = data.get("sitekey")
    proxy = data.get("proxy")
    userAgent = data.get("userAgent")

    try:

        result = solver.solve_recaptcha(
            url=url,
            sitekey=sitekey,
            proxy=proxy,
            user_agent=userAgent
        )

        return jsonify({
            "status": "success",
            "token": result
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        })


# -----------------------------
# CLOUDFLARE
# -----------------------------
@app.route("/bypass-cloudflare", methods=["POST"])
def bypass_cloudflare():

    if not check_key(request):
        return jsonify({"error": "invalid api key"}), 403

    data = request.json
    url = data.get("url")
    proxy = data.get("proxy")
    userAgent = data.get("userAgent")

    try:

        result = solver.solve_cloudflare(
            url=url,
            proxy=proxy,
            user_agent=userAgent
        )

        return jsonify({
            "status": "success",
            "solution": result
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
