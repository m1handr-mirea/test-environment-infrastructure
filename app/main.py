"""Small HTTP service that exposes a marker stored in the preview's Redis."""
import os

from flask import Flask, jsonify, request
from redis import Redis
from redis.exceptions import RedisError

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4096
preview = os.environ["PREVIEW_ID"]
store = Redis(
    host="store", password=os.environ["REDIS_PASSWORD"],
    socket_timeout=2, socket_connect_timeout=2, decode_responses=True,
)


@app.errorhandler(RedisError)
def store_error(_error):
    return jsonify(status="unavailable"), 503


@app.get("/api/health")
def health():
    store.ping()
    return jsonify(status="ok")


@app.route("/api/marker", methods=["GET", "PUT"])
def marker():
    if request.method == "PUT":
        payload = request.get_json(silent=True)
        label = payload.get("label") if isinstance(payload, dict) else None
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
            return jsonify(error="Expected a label of 1–80 characters"), 400
        store.set("preview:marker", label.strip())
    return jsonify(preview=preview, label=store.get("preview:marker") or "")
