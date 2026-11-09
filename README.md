# Storyline

**Helping a grandfather with a fading memory revisit and save his life stories.**

Storyline is an AI companion that phones Grandpa Frank. Each call gently brings back one memory: it retells a story he shared before and invites him to add to it, asks about an old family photo, or starts a new story. It never quizzes him. Every story is saved to a family Google Sheet so none of them are lost.

- **Live site:** _coming soon_
- **Demo video:** _coming soon_

## Why this matters to me

My grandfather is losing his memory. Talking about his life is a good way to exercise his brain, and it lets him revisit memories he may not fully remember anymore.

That's what Storyline does. Each call asks him about his life. When a memory is slipping, it gently reminds him of what he shared before, so he can pick the story back up instead of losing it. And every story is saved, so our family keeps them too.

*For privacy, "Frank" in this demo is a made-up grandfather with made-up stories. The reason I built it is real.*

## Try it (for judges)

_Coming soon._ You'll enter your phone number and the demo code from our submission on the live site, then play Grandpa Frank when Storyline calls.

## The six superpowers

| Superpower | What it does here | Where to see it |
|---|---|---|
| Voice | Vapi calls you and has a warm, patient conversation | Your phone rings |
| Texting | Linq: text an old photo in, and the family gets each new story by text | The photo appears on the site |
| Vision | Claude describes each photo and writes a gentle question about it | The next call asks about the photo |
| Knowledge | Frank's profile plus every saved story go into each call | The AI retells a past story |
| Real-world data | OpenWeather: today's weather in Pittsburgh | The call opens with it |
| Actions | Every story, photo and call is written to a Google Sheet | The Sheet link on the site |

## Status

- [x] Step 0: skeleton (`/health`, placeholder page, Sheet setup script)
- [ ] Step 1: the outbound call
- [ ] Step 2: saving the story after the call
- [ ] Step 3: photos by text and upload
- [ ] Step 4: the judge-ready page

## Running it yourself

### Accounts you need

Twilio (one voice number), Vapi, Linq, the Anthropic API, OpenWeather, Google Cloud (a service account for Sheets), and Render.

### Local run

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill in the values
.venv/bin/gunicorn app:app  # or: .venv/bin/python app.py
```

Open http://localhost:8000 (gunicorn) or http://localhost:5000 (`python app.py`).

### Google Sheet setup

1. In Google Cloud, create a project, enable the **Google Sheets API** and the **Google Drive API**, then create a service account and download its JSON key.
2. Create an empty Google Sheet. Share it with the service account's email (`client_email` in the key) as **Editor**, and set "Anyone with the link" to **Viewer**.
3. Put the key file's contents in `GOOGLE_SERVICE_ACCOUNT_JSON` (on Render, paste the whole file; in a local `.env`, put it on one line), and the Sheet's id (the long string between `/d/` and `/edit` in its URL) in `GOOGLE_SHEET_ID`.
4. That's it. Every time the app starts, it creates any missing `stories`, `photos` and `calls` tabs and adds Frank's seed stories if they aren't there yet. Render's logs show `Sheet ready` when it worked. To do the same by hand, run `.venv/bin/python scripts/seed_sheet.py` (add `--dry-run` to preview the seed stories without touching the Sheet).

### Deploy on Render

1. Create a **Web Service** from this GitHub repo and choose the **Free** instance type.
2. Build command: `pip install -r requirements.txt`. Start command: `gunicorn app:app`. Health check path: `/health`.
3. Add the variables from `.env.example` in the Environment section. Python 3.12 comes from `.python-version`.

## Limits

- Texting runs on Linq's free plan, which caps the number of contacts.
- Calls are capped at 3 minutes.
