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

1. Open the live site. Everything you need is on that one page, with no login.
2. Under **Try it**, enter your US phone number and the demo code from our submission, then press **Call me as Grandpa Frank**. You'll play Frank, a made-up grandfather in his 80s. The page shows the exact line the call will open with and which memory it will bring up, plus a short cheat sheet about Frank.
3. Talk for a minute or two, then hang up. Your story appears in **Frank's story book** within a minute or two (refresh the page).
4. Optional: under **Photos Storyline has seen**, text an old photo to the number shown, or upload one. You'll see what Claude saw in it, and the next call asks Frank about it.
5. Optional: open **the printable story book** (`/book`, linked from the story book section). It holds every story Frank has told, oldest first, laid out like a keepsake the family can print or save as a PDF.

Calls are capped at 3 minutes, and at 3 per number per day.

## The six superpowers

The page's **What's working right now** panel shows live proof for each one.

| Superpower | What it does here | Where to see it |
|---|---|---|
| Voice | Vapi calls you and has a warm, patient conversation | Your phone rings; the panel shows the last call |
| Texting | Linq: text an old photo in, and the family gets each new story by text | Photos Storyline has seen; the family's phone |
| Vision | Claude describes each photo and writes a gentle question about it. Only the description is kept, never the photo | "What Claude saw" on each photo; the next call says it looked at the photo and asks about it |
| Knowledge | Before every call, Storyline reads Frank's life profile (`knowledge/profile.md`, a knowledge file) plus every story he has told, so it can retell them and fill in what he forgets | The call retells a past story and reminds Frank of details; the panel names the next story |
| Real-world data | OpenWeather: today's weather in Pittsburgh | The call's first line, shown on the page and said on the call |
| Actions | Claude acts on its own: after each call it saves the story with a `save_story` tool call and flags anything the family should check on, and during the call it hangs up kindly with the `endCall` tool. Every story, photo and call goes into a Google Sheet | The panel shows Claude's latest action; the view-only Sheet link |

## Status

- [x] Step 0: skeleton (`/health`, placeholder page, Sheet setup script)
- [x] Step 1: the outbound call (`POST /call`; preview the next call at `/call/preview?code=DEMO_CODE`)
- [x] Step 2: saving the story after the call. Test without a phone call: open `/test/story?code=DEMO_CODE` (shows Claude's story, saves nothing) or `/test/story?code=DEMO_CODE&save=1` (also saves it to the Sheet as source "test", which calls never bring up). Or run `python scripts/replay_vapi.py` to test the webhook itself.
- [x] Step 3: photos by text (Linq) and upload. Test without texting: the upload form on the page, or `python scripts/replay_linq.py URL --photo IMAGE_URL`. One-time Linq webhook setup: `/setup/linq?code=DEMO_CODE`.
- [x] Step 4: the judge-ready page: the call card with the call's live opening line, the superpowers panel with live proof, the photos Claude has seen, and Frank's story book. Page data is cached for 60 seconds, and the page still loads if the Sheet or the weather is down.
- [x] The printable story book (`/book`): a cover, contents, and every story oldest first. "Print or save as PDF" gives clean pages. Concern notes stay out of the keepsake.

## Running it yourself

### Accounts you need

Twilio (one voice number), Vapi, Linq, the Anthropic API, OpenWeather, a Google account for the Sheet, and Render. No Google Cloud project is needed.

### Local run

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill in the values
.venv/bin/gunicorn app:app  # or: .venv/bin/python app.py
```

Open http://localhost:8000 (gunicorn) or http://localhost:5000 (`python app.py`).

### Google Sheet setup

The app talks to the Sheet through a small Apps Script that lives inside the Sheet (`apps_script/Code.gs`), so there's no Google Cloud project or service account key.

1. Create an empty Google Sheet. Click **Share** and set "Anyone with the link" to **Viewer**, so judges can see it.
2. In the Sheet, open **Extensions > Apps Script**. Replace everything in `Code.gs` with this repo's `apps_script/Code.gs`.
3. Change `SECRET` near the top to a long random string (30+ letters and numbers), and click Save.
4. Click **Deploy > New deployment**. Next to "Select type", click the gear and choose **Web app**. Set **Execute as: Me** and **Who has access: Anyone**, then click **Deploy**.
5. Google asks you to authorize. Choose your account. If it says "Google hasn't verified this app", click **Advanced**, then **Go to (your project) (unsafe)**, then **Allow**. It's your own script, so this is expected.
6. Copy the **Web app URL** (it ends in `/exec`). Opening it in a browser should show `Storyline sheet bridge is running`.
7. Set `SHEETS_WEBAPP_URL` to that URL, `SHEETS_WEBAPP_SECRET` to the same secret, and `GOOGLE_SHEET_ID` to the long string between `/d/` and `/edit` in the Sheet's URL.

That's it. Every time the app starts, it creates any missing `stories`, `photos` and `calls` tabs and adds Frank's seed stories if they aren't there yet. Render's logs show `Sheet ready` when it worked. To do the same by hand, run `.venv/bin/python scripts/seed_sheet.py` (add `--dry-run` to preview the seed stories without touching the Sheet).

If you change `Code.gs` later, the change only goes live after **Deploy > Manage deployments >** pencil icon **> Version: New version > Deploy**. The URL stays the same.

### Deploy on Render

1. Create a **Web Service** from this GitHub repo and choose the **Free** instance type.
2. Build command: `pip install -r requirements.txt`. Start command: `gunicorn app:app`. Health check path: `/health`.
3. Add the variables from `.env.example` in the Environment section. Python 3.12 comes from `.python-version`.

Render's free plan puts an app to sleep after 15 minutes without visitors, and waking it takes about a minute. Storyline keeps itself awake: once `PUBLIC_BASE_URL` is set, it visits its own `/health` every 10 minutes. Render's logs show `Keep-awake on` after each deploy. One always-on free service uses about 720 of Render's 750 free hours a month.

## Limits

- Texting runs on Linq's free plan, which caps the number of contacts. If texting isn't available, the upload form does the same job for photos.
- Calls are capped at 3 minutes, 3 per number per day, and `MAX_CALLS_PER_DAY` in total.
- Storyline only replies to people who text it first, and never answers STOP, UNSUBSCRIBE and similar opt-out words.
