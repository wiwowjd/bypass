import os
import subprocess
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

CLIENT_KEY = os.getenv("CLIENT_KEY", "hiroxen")
CLOUDFLYER_PORT = int(os.getenv("CLOUDFLYER_PORT", 3001))
CLOUDFLYER_URL = f"http://127.0.0.1:{CLOUDFLYER_PORT}"


# start cloudflyer solver
def start_cloudflyer():
    try:
        cmd = [
            "cloudflyer",
            "-K", CLIENT_KEY,
            "-H", "0.0.0.0",
            "-P", str(CLOUDFLYER_PORT),
            "-M", "5"
        ]

        subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        print("cloudflyer started")

    except Exception as e:
        print("cloudflyer failed to start:", str(e))


start_cloudflyer()


# check solver status
def solver_alive():
    try:
        r = requests.get(CLOUDFLYER_URL, timeout=3)
        return True
    except:
        return False


@app.route("/")
def home():
    return jsonify({
        "service": "cloudflyer api",
        "status": "running"
    })


@app.route("/solver-status")
def solver_status():
    if solver_alive():
        return jsonify({"solver": "running"})
    else:
        return jsonify({"solver": "offline"})


@app.route("/bypass-turnstile", methods=["POST"])
def bypass_turnstile():

    if not solver_alive():
        return jsonify({
            "error": "solver_not_running"
        }), 500

    try:
        data = request.get_json(force=True)

        payload = {
            "clientKey": CLIENT_KEY,
            "type": "Turnstile",
            "url": data.get("url"),
            "siteKey": data.get("siteKey"),
            "userAgent": data.get("userAgent"),
            "proxy": data.get("proxy")
        }

        r = requests.post(
            f"{CLOUDFLYER_URL}/createTask",
            json=payload,
            timeout=30
        )

        return jsonify(r.json())

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/bypass-recaptcha", methods=["POST"])
def bypass_recaptcha():

    if not solver_alive():
        return jsonify({
            "error": "solver_not_running"
        }), 500

    try:
        data = request.get_json(force=True)

        payload = {
            "clientKey": CLIENT_KEY,
            "type": "RecaptchaInvisible",
            "url": data.get("url"),
            "siteKey": data.get("siteKey"),
            "action": data.get("action"),
            "userAgent": data.get("userAgent"),
            "proxy": data.get("proxy")
        }

        r = requests.post(
            f"{CLOUDFLYER_URL}/createTask",
            json=payload,
            timeout=30
        )

        return jsonify(r.json())

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/bypass-cloudflare", methods=["POST"])
def bypass_cloudflare():

    if not solver_alive():
        return jsonify({
            "error": "solver_not_running"
        }), 500

    try:
        data = request.get_json(force=True)

        payload = {
            "clientKey": CLIENT_KEY,
            "type": "CloudflareChallenge",
            "url": data.get("url"),
            "userAgent": data.get("userAgent"),
            "proxy": data.get("proxy"),
            "content": data.get("content", False)
        }

        r = requests.post(
            f"{CLOUDFLYER_URL}/createTask",
            json=payload,
            timeout=30
        )

        return jsonify(r.json())

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/task-result", methods=["POST"])
def task_result():

    if not solver_alive():
        return jsonify({
            "error": "solver_not_running"
        }), 500

    try:
        data = request.get_json(force=True)

        payload = {
            "clientKey": CLIENT_KEY,
            "taskId": data.get("taskId")
        }

        r = requests.post(
            f"{CLOUDFLYER_URL}/getTaskResult",
            json=payload,
            timeout=30
        )

        return jsonify(r.json())

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
