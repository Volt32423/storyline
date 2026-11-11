# gunicorn reads this file automatically, so Render's start command stays
# `gunicorn app:app`.

# One process with several threads: requests that wait on Claude (a photo
# upload takes a few seconds) don't block everyone else, and in-memory caches
# are shared across requests.
workers = 1
threads = 8

# Photo uploads and the /test pages wait on Claude; give them time.
timeout = 120



def post_worker_init(worker):
    """Start the app's background work (Sheet setup, keep-awake, reading the
    page's data) in the worker that serves pages, as soon as it boots. Render
    imports the app once in gunicorn's main process before forking this
    worker, so nothing may start at import time. See app.start_background."""
    from app import start_background
    start_background()
