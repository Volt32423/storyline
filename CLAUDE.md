# CLAUDE.md — Storyline

## What this is

Storyline is an AI companion that phones **Grandpa Frank**, a grandfather whose memory is fading. Each call gently brings back one memory. It might retell a story he shared before and invite him to add to it, ask about an old family photo, or start a new story. Talking through his life gives his memory a gentle workout, and every story is saved to a family Google Sheet so none of them are lost.

**Frank is a made-up character with made-up stories** (see `knowledge/profile.md`). No real family information goes anywhere in this project.

This is a final project for an Applied AI class. **The deadline is Friday.** Judging happens online at any time during the following week. Judges will open the live site and try it themselves, so it must keep working unattended for that whole week.

## Scoring rules that shape every decision

- Judges score Creativity, Usefulness and Personal, 10 points each.
- Each "superpower" that works in the demo earns +5. We are going for all six. **A superpower that doesn't actually work earns zero, so reliability matters more than features.**
- Most of this project is glue between existing services. Don't build anything a library or service already does.

## The six superpowers and where a judge sees each one

| Superpower | How Storyline uses it | Where a judge sees it |
|---|---|---|
| Voice | Vapi places an outbound call, and the AI has a warm, patient conversation | Their phone rings |
| Texting | Through Linq: family members text old photos to Storyline's number and the app reads them, and after each call the app texts the family the new story | The photo shows up on the site, and the family's phone gets the story |
| Vision | Claude describes each photo and writes one gentle question to ask about it | The description on the site; the next call asks about it |
| Knowledge | Before each call, the app loads `knowledge/profile.md` plus every saved story, so the AI can remind Frank of what he told it before | The AI retells a past story on the call |
| Real-world data | OpenWeather current weather in Frank's city, mentioned when the call opens | The weather on the site and in the call |
| Actions | Every story, photo and call is written to a Google Sheet | A view-only Sheet link on the site |

## Stack (don't add anything without asking)

- Python 3.12, Flask, gunicorn
- `anthropic` (Claude) and `requests` (for the Vapi, Linq and OpenWeather REST APIs, and the Sheet bridge)
- Google Sheets is reached through a small Apps Script web app inside the Sheet (`apps_script/Code.gs`), called with a shared secret. No Google Cloud project, no service account key, no `gspread`. The builder's Google project blocks key creation, so don't switch back.
- Twilio is only the phone line behind Vapi. Our code never calls Twilio.
- One HTML page: a Jinja template with plain CSS and a little JS. No frontend framework.
- Hosting: a Render free web service, auto-deployed from the GitHub repo
- **No database. The Google Sheet is the database.** Render's free tier wipes the filesystem on every restart, redeploy or spin-down, so never store anything on local disk.

## Architecture

```
Browser ──► Flask app (Render) ──► Vapi create-call ──► imported Twilio number ──► phone rings
                 ▲   ▲                                         │
                 │   └──── end-of-call-report webhook ◄────────┘
                 └──────── Linq message.received webhook (texts + photos)
Flask ──► Claude (vision + summaries), Google Sheet, OpenWeather, Linq (send texts)
```

### Routes

- `GET /`: the page (see "The page" below)
- `POST /call`: a form with a phone number and a demo code. Validates the code and the limits, builds the call context and starts the Vapi call.
- `POST /upload`: a photo upload form. This is the backup path for Vision if incoming texts misbehave.
- `POST /webhooks/linq`: inbound texts and photos from Linq (`message.received`)
- `POST /webhooks/vapi`: Vapi server messages. Only `end-of-call-report` matters.
- `GET /health`: returns 200 "ok". An uptime pinger hits it every 10 minutes so Render never puts the app to sleep.

### Google Sheet tabs (the app creates them on startup)

- `stories`: timestamp, title, summary, people, places, concern, source (seed or call), call_id
- `photos`: timestamp, source (text or upload), description, question, asked (TRUE/FALSE)
- `calls`: timestamp, phone_last4, status, duration_seconds, ended_reason, call_id (Vapi's call id, so the webhook can find the row)

On every startup, `app.py` runs `knowledge.seed_sheet()` in a background thread: it creates any missing tabs and loads the stories under "Stories Frank has already told" in `knowledge/profile.md` into `stories`, with source = seed, skipping titles already there. A file lock stops two gunicorn workers from seeding at once. `scripts/seed_sheet.py` does the same by hand.

Only ever store the **last 4 digits** of a phone number. The Sheet is shared view-only with anyone who has the link, so judges can see it.

### Starting a call (`POST /call`)

1. Check `DEMO_CODE`. Enforce `MAX_CALLS_PER_DAY` by counting today's rows in `calls`, and allow at most 3 calls per number per day. Accept US numbers only and normalize them to E.164.
2. Get the weather from OpenWeather's current-weather endpoint for `GRANDPA_CITY`, in imperial units. If it fails, carry on without it.
3. Load `knowledge/profile.md`, every row in `stories`, and the newest photo with asked = FALSE.
4. Start an outbound call with Vapi's create-call API. Pass:
   - `VAPI_PHONE_NUMBER_ID` and the customer's number
   - a **transient assistant defined in code**, so the prompt lives in git
   - `maxDurationSeconds` = 180
   - server URL `PUBLIC_BASE_URL/webhooks/vapi`, set to send only `end-of-call-report` messages
5. Write a `calls` row with Vapi's call id, mark the photo as asked, and redirect to the page with "Calling you now…".

**Check every Vapi field name against docs.vapi.ai before using it. Don't guess.** Built and verified (Nov 2026): `assistant` = name, firstMessage, model {provider `anthropic`, model `claude-haiku-5-5`, messages [system], tools [{type `endCall`, messages [{type `request-start`, content}]}]}, voice {provider `vapi`, voiceId `Leah`}, maxDurationSeconds, server {url}, serverMessages. Model and voice can be changed with the optional `VAPI_*` env vars. Plan rotation: a pending photo first, otherwise the stories in turn (call count modulo story count). `GET /call/preview?code=DEMO_CODE` returns the next call's first message and prompt as JSON without dialing.

### The call prompt

**Persona.** "Storyline" is a warm, patient AI that Frank's grandson `BUILDER_NAME` set up to help Frank revisit and save his life stories. Whoever answers is treated as Frank; judges play him.

**First message.** Build it in code: greet him by name, say it's Storyline, the AI his grandson set up, say the call is saved for the family story book, and mention today's weather. For example: "Hi Frank, it's Storyline, the AI helper your grandson Sam set up to save your stories. This call goes into the family story book. I hear it's 48 and rainy in Pittsburgh today. Staying warm?"

**One memory per call**, in this order:
1. If a photo is waiting, describe what's in it and ask its question.
2. Otherwise, retell a past story in a sentence or two and invite him to add to it.
3. Otherwise, start a topic from "Topics he hasn't talked about yet" in the profile.

**Memory care rules.** These matter most.
- **Never quiz him.** Don't ask "Do you remember…?" Offer the memory instead: "You told me once about renting rowboats at Pine Lake for 50 cents an hour. What was that summer like?"
- If he can't recall something, warmly retell what he shared before and move on. Never make him feel he's failed.
- Never correct him or argue about details. If his version differs, go with it.
- Use short sentences and one question at a time. Leave room for pauses and let him talk.
- If he seems confused or upset, slow down, reassure him, and switch to a happier topic or wrap up kindly.
- Speak about his late wife June warmly, and follow his lead.
- Give no medical advice. If he mentions pain, a fall or feeling unwell, respond kindly. The summary flags it for the family.
- Wrap up warmly at around 2.5 minutes, and thank him for the story.

**What goes in the prompt:** the profile, past stories (title and summary), the weather, and any pending photo's description and question.

### After the call (`POST /webhooks/vapi`)

Built (Nov 2026): the webhook only accepts requests carrying `X-Storyline-Secret`, which the app sends to Vapi in `assistant.server.headers` (Vapi doesn't attach org credentials to transient assistants). The secret is derived from `SHEETS_WEBAPP_SECRET`; `VAPI_WEBHOOK_SECRET` overrides it. The summary uses a forced `save_story` tool call so the fields always come back, plus a `has_story` flag for calls with no memory. Duplicate reports for the same call id are saved once. `tests/fixtures/end_of_call_report.json` is a synthetic, documented-shape payload until a real one is saved. The summary runs at `output_config.effort = "high"` (Haiku 5.5's default is medium; adaptive thinking stays on, which works with the forced tool) with `max_tokens` 4096. `GET /test/story?code=DEMO_CODE` runs the sample transcript through Claude without saving; `&save=1` saves it with source `test`, and `build_context()` never puts `test` stories in a call.

1. Return 200 right away and do the work in a background thread.
2. Ignore every message type except `end-of-call-report`. The body is `{"message": {"type": ..., "call": ..., "artifact": ...}}`. Find the transcript in `message.artifact`, and check the docs for the exact field.
3. Skip calls shorter than about 20 seconds, since there's no story to save.
4. Send the transcript to Claude (`claude-haiku-5-5`) and ask for JSON:
   - title
   - summary: 3–5 sentences, in Frank's voice
   - people
   - places
   - concern: anything the family may want to check on, such as pain, a fall, feeling unwell or seeming upset. Empty if none. Don't make guesses about his memory or health.
5. Append to `stories` and update the `calls` row.
6. If `SMS_SENDING_ENABLED` is true, send a text through Linq to `FAMILY_PHONE_NUMBER`: "New story from Grandpa Frank: {title}: {summary}", plus a concern line if there is one.
7. Save one real payload, with phone numbers scrubbed, to `tests/fixtures/` for `scripts/replay_vapi.py`.

### Incoming photo texts (`POST /webhooks/linq`)

1. Verify the webhook signature the way Linq's Webhooks guide describes.
2. Return 200 right away and do the work in a background thread. Use `event_id` to skip duplicate deliveries.
3. Ignore everything except inbound `message.received` events. Pin the webhook version when creating the subscription, and check docs.linqapp.com for the exact payload.
4. For each `media` part in the message's `parts`, download the image from its `url` (on `cdn.linqapp.com`). Send it to Claude (`claude-sonnet-5-5`) and ask for JSON:
   - description: 2 sentences covering who, what, where and clues to the era
   - question: one warm, open question for Frank that offers details rather than testing him. Good: "This looks like a lake in summer. What did you love about days like that?" Bad: "Do you remember who this is?"
5. Append to `photos`.
6. If `SMS_SENDING_ENABLED` is true, reply in the same chat: "Got it! Storyline will ask Grandpa Frank about this photo on the next call."

`POST /upload` runs the same photo pipeline.

### The page (`GET /`)

One clean, mobile-friendly page that a judge who knows nothing about the project can use without help. Use large text and warm colors, with no login.

- Header: "Storyline" and a one-line pitch: "Helping a grandfather with a fading memory revisit and save his life stories."
- **Try it:** "You'll play Grandpa Frank, a made-up grandfather in his 80s whose memory is fading. Enter your number and the demo code from our submission, and you'll get a call within seconds. Calls are capped at 3 minutes."
- A short cheat sheet so judges can play along: "Frank grew up near Pittsburgh, worked in a steel mill, then delivered mail. His late wife was June. Make up the rest!"
- **Text an old photo to [LINQ_PHONE_NUMBER]**, plus the upload form
- **Superpowers panel:** six rows, each with a ✓ and the latest evidence: the current weather, the last photo description, the last story title, the Sheet link, and the last call time
- Frank's story book: every story, newest first
- Links to the view-only Google Sheet and the GitHub repo

Cache Sheet reads for 60 seconds so page loads don't hit Google's rate limits.

## Environment variables

Commit `.env.example` with every name and no secret values. Keep `.env` out of git.

```
ANTHROPIC_API_KEY=
VAPI_API_KEY=
VAPI_PHONE_NUMBER_ID=          # the imported Twilio number's id in Vapi
LINQ_API_TOKEN=
LINQ_PHONE_NUMBER=             # E.164; the number people text photos to
LINQ_WEBHOOK_SECRET=           # for verifying Linq webhook signatures
FAMILY_PHONE_NUMBER=           # E.164; gets a text after each call. Text the Linq number from it once first.
SMS_SENDING_ENABLED=true       # turn off if Linq sending misbehaves; reading photos still works
OPENWEATHER_API_KEY=
GRANDPA_NAME=Frank             # fictional
GRANDPA_CITY=Pittsburgh,US     # real city, used for the weather
BUILDER_NAME=Sam               # Frank's fictional grandson, who "set up" Storyline
SHEETS_WEBAPP_URL=             # the Apps Script web app URL, ending in /exec
SHEETS_WEBAPP_SECRET=          # same string as SECRET in apps_script/Code.gs
GOOGLE_SHEET_ID=               # only used for the view-only link
DEMO_CODE=
MAX_CALLS_PER_DAY=15
PUBLIC_BASE_URL=               # the Render URL, no trailing slash
```

## Repo layout

```
app.py                  # Flask routes only
services/
  config.py             # settings from env vars (and a local .env)
  sheets.py             # every Google Sheet read and write, via the Apps Script bridge
  knowledge.py          # load profile.md, parse and seed Frank's stories
  claude_ai.py          # photo description + story summary
  calls.py              # build the prompt, start the Vapi call
  weather.py
  texting.py            # Linq send + webhook signature check
knowledge/profile.md    # Frank's fictional profile + seed stories (already written)
apps_script/Code.gs     # the Sheet bridge; pasted into the Sheet's Apps Script editor
templates/index.html
static/style.css
scripts/
  seed_sheet.py         # same setup as startup, by hand (--dry-run to preview)
  replay_vapi.py        # POST a saved end-of-call-report to a running app
  replay_linq.py        # POST a saved message.received payload to a running app
tests/fixtures/         # saved payloads, phone numbers scrubbed
requirements.txt
.env.example
README.md
```

## Manual setup the builder does (Claude Code can't)

- **Twilio (voice only):** upgrade the account ($20) and buy a local US number with Voice. A 412 (Pittsburgh) number matches Frank. No texting setup is needed here.
- **Vapi:** create an account and import the Twilio number under Phone Numbers. SMS Enabled can stay off, since texting runs through Linq. Copy the number's id.
- **Linq:** sign up for the free sandbox and get an API token and a number. Subscribe `PUBLIC_BASE_URL/webhooks/linq` to `message.received`, and copy the signing secret. Text the Linq number once from the family phone so the chat exists. Confirm the plan lasts through judging week and allows enough contacts (the free Hobby plan allows 20).
- **Anthropic Console:** create an API key.
- **OpenWeather:** create a key. New keys can take up to 2 hours to activate.
- **Google Sheet:** create the Sheet and set "anyone with the link" to Viewer. Paste `apps_script/Code.gs` into Extensions > Apps Script, set its `SECRET`, and deploy it as a web app (Execute as: Me, Who has access: Anyone). Copy the `/exec` URL. After any later edit to the script, deploy a new version. No Google Cloud project is needed.
- **Render:** create a Free web service from the GitHub repo. Build command `pip install -r requirements.txt`, start command `gunicorn app:app`, health check path `/health`. Add the environment variables.
- **Uptime pinger** (e.g. UptimeRobot, free): check `/health` every 10 minutes.

## Build plan

Do one step at a time. Finish each step by passing its "done when" check, then commit to git.

0. **Sunday: skeleton.** Scaffold the repo with `/health` and a placeholder page, and deploy to Render. The app creates the Sheet's tabs and seed stories on startup.
   *Done when the Render URL loads and the Sheet has its tabs and Frank's seed stories.*
1. **Monday: the call.** Weather, profile and stories go into the prompt, then the Vapi outbound call starts.
   *Done when the builder's phone rings and the AI mentions the weather and retells a seed story without quizzing.*
2. **Tuesday: after the call.** Vapi webhook, then the Claude summary, then a `stories` row, plus the text if sending is enabled. Get `replay_vapi.py` working.
   *Done when hanging up adds a story row within a minute.*
3. **Wednesday: photos.** The Linq webhook and the upload form, then Claude vision, then a `photos` row, then the next call asks about the photo.
   *Done when a texted photo comes up on the next call.*
4. **Thursday: judge-ready.** The page, the superpowers panel, the demo code and call caps, caching, the README, and the uptime pinger. **No new features.**
   *Done when a friend who has never seen it can get a call from the live site using only the page.*

## Rules for working on this repo

- Keep it boring. Write the simplest code that works: no database, no auth system, no frontend framework, no async framework.
- Check the provider's current docs before using any third-party API field. If you're unsure, say so and ask.
- **Never commit secrets.** `.env`, any `*.json` key file and unscrubbed fixtures stay out of git. Check `git status` before every commit.
- Give every external call (Claude, Sheets, Vapi, Linq, OpenWeather) a timeout, and wrap it in try/except so a failure gets logged and the app keeps working. A failed weather lookup must never stop a call.
- Log one clear line per event (call started, story saved, photo described) so problems are easy to find in Render's logs.
- Test with the replay scripts instead of real phone calls whenever you can, because every real call costs Vapi credits.
- Update README.md as features land.

## The README must include

- The live URL and a link to the demo video
- How judges try it. The demo code goes in the submission form, not the README.
- The six superpowers and where to see each one
- Setup: the accounts needed, the environment variables, deploying on Render, and creating the Sheet and its Apps Script bridge
- Limits: texting runs on Linq's free plan, which caps the number of contacts. Calls are capped at 3 minutes.
- This section, word for word (the builder wrote it):

```markdown
## Why this matters to me

My grandfather is losing his memory. Talking about his life is a good way to exercise his brain, and it lets him revisit memories he may not fully remember anymore.

That's what Storyline does. Each call asks him about his life. When a memory is slipping, it gently reminds him of what he shared before, so he can pick the story back up instead of losing it. And every story is saved, so our family keeps them too.

*For privacy, "Frank" in this demo is a made-up grandfather with made-up stories. The reason I built it is real.*
```

## Privacy

Frank, his family and his stories are fictional. Never add real names, places or stories from the builder's family to the code, the Sheet, the prompts or the README, apart from the "Why this matters to me" section above. The AI says it's an AI and that the call is saved, in the very first message.

## Judging-week checklist

- The uptime pinger is running.
- Don't push to main during judging unless you're fixing a real bug, because every push redeploys.
- Check the Vapi credit and Twilio balances, and Linq's contact count, daily. Keep auto-recharge off everywhere.
- After judging, release the Twilio number so it stops billing monthly.
- **Reminder: close the Google Cloud billing account** from the abandoned service-account attempt, and check the $30 comes back. The app doesn't use Google Cloud (the Apps Script bridge doesn't need it), so this can be done any time.
