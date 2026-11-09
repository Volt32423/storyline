"""Storyline: Flask routes only. The real work lives in services/."""

import logging

from flask import Flask, render_template

from services import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("storyline")

app = Flask(__name__)


@app.get("/")
def index():
    return render_template(
        "index.html",
        grandpa_name=config.GRANDPA_NAME,
        linq_number=config.get("LINQ_PHONE_NUMBER"),
    )


@app.get("/health")
def health():
    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}


if __name__ == "__main__":
    app.run(debug=True, port=5000)
