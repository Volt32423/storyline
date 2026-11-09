# gunicorn reads this file automatically, so Render's start command stays
# `gunicorn app:app`.

# One process with several threads: requests that wait on Claude (a photo
# upload takes a few seconds) don't block everyone else, and in-memory caches
# are shared across requests.
workers = 1
threads = 8

# Photo uploads and the /test pages wait on Claude; give them time.
timeout = 120
