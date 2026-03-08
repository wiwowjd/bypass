import os
import subprocess
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

CLIENT_KEY = os.getenv("CLIENT_KEY", "hiroxen")
CLOUDFLYER_PORT = int(os.getenv("CLOUDFLYER_PORT", 3001))
CLOUDFLYER_URL = f"http://127.0.0.1:{CLOUDFLYER_PORT}"


def start_cloudflyer():
    cmd = [
        "cloudflyer",
        "-K", CLIENT_KEY,
        "-H", "0.0.0.0",
        "-P", str(CLOUDFLYER_PORT),
        "-M", "5"
    ]
    subprocess.Popen(cmd)


start_cloudflyer()


@app.route("/")
def home():
    return jsonify({
        "service": "cloudflyer api",
        "status": "running"
    })


@app.route("/bypass-turnstile", methods=["POST"])
def bypass_turnstile():

    data = request.json

    payload = {
        "clientKey": CLIENT_KEY,
        "type": "Turnstile",
        "url": data.get("url"),
        "siteKey": data.get("siteKey"),
        "userAgent": data.get("userAgent"),
        "proxy": data.get("proxy")
    }

    r = requests.post(f"{CLOUDFLYER_URL}/createTask", json=payload)

    return jsonify(r.json())


@app.route("/bypass-recaptcha", methods=["POST"])
def bypass_recaptcha():

    data = request.json

    payload = {
        "clientKey": CLIENT_KEY,
        "type": "RecaptchaInvisible",
        "url": data.get("url"),
        "siteKey": data.get("siteKey"),
        "action": data.get("action"),
        "userAgent": data.get("userAgent"),
        "proxy": data.get("proxy")
    }

    r = requests.post(f"{CLOUDFLYER_URL}/createTask", json=payload)

    return jsonify(r.json())


@app.route("/bypass-cloudflare", methods=["POST"])
def bypass_cloudflare():

    data = request.json

    payload = {
        "clientKey": CLIENT_KEY,
        "type": "CloudflareChallenge",
        "url": data.get("url"),
        "userAgent": data.get("userAgent"),
        "proxy": data.get("proxy"),
        "content": data.get("content", False)
    }

    r = requests.post(f"{CLOUDFLYER_URL}/createTask", json=payload)

    return jsonify(r.json())


@app.route("/task-result", methods=["POST"])
def task_result():

    data = request.json

    payload = {
        "clientKey": CLIENT_KEY,
        "taskId": data.get("taskId")
    }

    r = requests.post(f"{CLOUDFLYER_URL}/getTaskResult", json=payload)

    return jsonify(r.json())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
