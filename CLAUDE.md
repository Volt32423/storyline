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
| Texting | Through Sendblue (iMessage; Linq's code stays as a fallback): family members text old photos to Storyline's number and the app reads them, and after each call the app texts the family the new story | The photo shows up on the site, and the family's phone gets the story |
| Vision | Claude describes each photo and writes one gentle question to ask about it | The description on the site; the next call asks about it |
| Knowledge | Before each call, the app loads `knowledge/profile.md` plus every saved story, so the AI can remind Frank of what he told it before | The AI retells a past story on the call |
| Real-world data | OpenWeather current weather in Frank's city, mentioned when the call opens | The weather on the site and in the call |
| Actions | Every story, photo and call is written to a Google Sheet | A view-only Sheet link on the site |

## Stack (don't add anything without asking)

- Python 3.12, Flask, gunicorn
- `anthropic` (Claude) and `requests` (for the Vapi, Sendblue, Linq and OpenWeather REST APIs, and the Sheet bridge)
- `pillow` and `pillow-heif` to convert and shrink photos before Claude sees them (added in step 3, because iPhone photos are often HEIC)
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
                 └──────── Sendblue receive webhook (texts + photos; or Linq's message.received)
Flask ──► Claude (vision + summaries), Google Sheet, OpenWeather, Sendblue or Linq (send texts)
```

### Routes

- `GET /`: the page (see "The page" below)
- `POST /call`: a form with a phone number and a demo code. Validates the code and the limits, builds the call context and starts the Vapi call.
- `POST /upload`: a photo upload form. This is the backup path for Vision if incoming texts misbehave.
- `POST /webhooks/sendblue`: inbound texts and photos from Sendblue (its `receive` webhook)
- `GET /setup/sendblue?code=DEMO_CODE`: points Sendblue's receive webhook at the app. The app also does this on every startup.
- `POST /webhooks/linq`: inbound texts and photos from Linq (`message.received`), kept in case Linq comes back
- `POST /webhooks/vapi`: Vapi server messages. Only `end-of-call-report` matters.
- `GET /live`: a 12-character fingerprint of the cached Sheet rows (`page.live_version()`). It never reads the Sheet; every save here updates the cache, so it changes the moment a photo, story or call is saved or a photo is asked about. The page polls it every 5 s (only while visible) and, when it changes, fetches `/` and swaps the elements marked `data-live-region` (the next-call line, the proof panel, the photos, the story book). No region holds a form, so nothing a visitor is typing is lost. New or changed items (`data-key`) get a brief gold outline.
- `POST /text-number`: a demo-code form that shows the texting number. A right code here, or on the call or upload form, sets `code_ok` in Flask's signed session cookie (key derived from `SHEETS_WEBAPP_SECRET`), and the page then shows the number with a note that only family phones get through on the free plan. Without it, the number never appears in the HTML.
- `GET /health`: returns 200 "ok". The app's own keep-awake thread (`_keep_awake` in `app.py`) visits `PUBLIC_BASE_URL/health` every 10 minutes (`KEEP_AWAKE_MINUTES`, 0 turns it off), so Render's free plan never sleeps. The request goes out and back in through Render, so it counts as a visitor. One always-on free service uses about 720 of the 750 free instance hours a month, so don't run a second always-on free service in the same Render workspace.
- `GET /book`: the printable story book (`templates/book.html`, `page.book_data()`). It shows a gold cover with arches, a contents list, every story oldest first, then "Photos the family sent" (each photo's description, question, date and the story it led to), with no `test` stories and no concerns. A story told about a photo shows what Claude saw in it. Print CSS puts the cover, the contents and the photos on their own pages and keeps each story and photo on one page. It's read-only, so it can't affect calls. Linked from the story book section and the footer.

### Google Sheet tabs (the app creates them on startup)

- `stories`: timestamp, title, summary, people, places, concern, source (seed or call), call_id
- `photos`: timestamp, source (text or upload), description, question, asked (TRUE/FALSE)
- `calls`: timestamp, phone_last4, status, duration_seconds, ended_reason, call_id (Vapi's call id, so the webhook can find the row), topic (what the call was about, e.g. `Asked about a photo: Two young men...` or `Retold "Rowboats at Pine Lake"`). `ensure_tabs` adds a new column's header on the next startup without redeploying the Apps Script.

On every startup, `app.start_background()` runs `knowledge.seed_sheet()` in a background thread: it creates any missing tabs and loads the stories under "Stories Frank has already told" in `knowledge/profile.md` into `stories`, with source = seed, skipping titles already there. A file lock stops two gunicorn workers from seeding at once. `scripts/seed_sheet.py` does the same by hand.

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

**One memory per call**, in this order (`calls.choose_plan`, shared with the page so its "Then it..." line always matches the call):
1. If a photo is waiting, say the family sent a photo and Storyline had a look at it, describe what it saw, and ask its question. This makes Vision audible on the call.
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
6. If `SMS_SENDING_ENABLED` is true, send a text through the texting service (Sendblue, or Linq) to `FAMILY_PHONE_NUMBER`: "New story from Grandpa Frank: {title}: {summary}", plus a concern line if there is one.
7. Save one real payload, with phone numbers scrubbed, to `tests/fixtures/` for `scripts/replay_vapi.py`.

### Incoming photo texts (`POST /webhooks/sendblue`, or `POST /webhooks/linq`)

**Switched to Sendblue (Nov 2026)**, because Linq never answered the sandbox request. `services/texting.py` picks the service: Sendblue when `SENDBLUE_API_KEY`, `SENDBLUE_API_SECRET` and `SENDBLUE_PHONE_NUMBER` are set, otherwise Linq. Sendblue (`services/sendblue.py`, checked against docs.sendblue.com): base `https://api.sendblue.com`, headers `sb-api-key-id` and `sb-api-secret-key`; send `POST /api/send-message` with `{number, from_number, content}` (`from_number` is required on the free shared-number plan; a 200 can still carry `status: "ERROR"`, so both are checked). Incoming texts arrive as flat JSON on the `receive` webhook: `number`, `content`, `media_url` (one attachment per message), `message_handle` (used to skip duplicates), `is_outbound`, `group_id` (group texts are ignored), `opted_out`, `sendblue_number`. The webhook is checked with a secret derived from `SHEETS_WEBAPP_SECRET` (`SENDBLUE_WEBHOOK_SECRET` overrides it). Sendblue sends it back in the `sb-signing-secret` header, and it's also in the webhook URL's `?key=`, so either one passes. On startup the app lists the account's webhooks (`GET /api/account/webhooks`) and adds its own (`POST`, `{"webhooks": [{url, secret}], "type": "receive"}`), removing an older Storyline one with `DELETE` if the secret changed, and leaving other webhooks alone. Never answer a Sendblue webhook with 410, which deletes it. **Free plan:** one number shared with other free users, up to 10 verified contacts, and only verified contacts' texts reach the app. So the page doesn't ask judges to text the number; it points them at the upload form, which runs the same pipeline. A photo and its caption may arrive as two messages, so the "text me a photo" tip waits 8 s and is skipped if a photo came. `scripts/replay_sendblue.py` posts `tests/fixtures/sendblue_receive.json` (synthetic, documented shape).

Linq (`services/linq.py`), built earlier (Nov 2026): Linq API: base `https://api.linqapp.com/api/partner`, Bearer token; new message `POST /v3/messages` with `{"to": [...], "message": {"parts": [...]}}`; reply `POST /v3/chats/{chatId}/messages` with `{"message": {"parts": [...]}}`; Standard Webhooks signature check: `webhook-id`, `webhook-timestamp`, `webhook-signature` = `v1,base64(HMAC-SHA256(key, "{id}.{ts}.{raw body}"))`, key = base64-decoded secret after `whsec_`, 5-minute window). `services/photos.py` runs the shared pipeline for texted and uploaded photos. **Vision uses `claude-haiku-5-5`, not Sonnet**: Sonnet 5.5 rejects a forced tool while its default adaptive thinking is on, and Haiku 5.5 allows it. Photos go through Pillow + pillow-heif first (HEIC converted, EXIF rotation applied, long edge ≤ 1600 px, JPEG), because Claude doesn't accept HEIC. The tool also returns `suitable`; unsuitable images aren't saved. CDN downloads try without the token, then with it. `GET /setup/linq?code=DEMO_CODE` creates the webhook subscription (pinned to version 2026-02-03) and shows the signing secret; it switches itself off once `LINQ_WEBHOOK_SECRET` is set. `gunicorn.conf.py` runs 1 worker with 8 threads and a 120 s timeout, so uploads waiting on Claude don't block other requests.

1. Verify the webhook signature the way Linq's Webhooks guide describes.
2. Return 200 right away and do the work in a background thread. Use `event_id` to skip duplicate deliveries.
3. Ignore everything except inbound `message.received` events. Pin the webhook version when creating the subscription, and check docs.linqapp.com for the exact payload.
4. For each `media` part in the message's `parts`, download the image from its `url` (on `cdn.linqapp.com`). Send it to Claude (`claude-haiku-5-5`, see the note above) and ask for JSON:
   - description: 2 sentences covering who, what, where and clues to the era
   - question: one warm, open question for Frank that offers details rather than testing him. Good: "This looks like a lake in summer. What did you love about days like that?" Bad: "Do you remember who this is?"
5. Append to `photos`.
6. If `SMS_SENDING_ENABLED` is true, reply in the same chat: "Got it! Storyline will ask Grandpa Frank about this photo on the next call."
7. Never reply to an opt-out text (STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT, OPT OUT, REVOKE), or to anyone Sendblue marks `opted_out`. Storyline only ever replies to people who texted it first.

Photos are text only: Storyline never stores or shows the image, just Claude's description and question. Everywhere a photo comes up, the page and the call make it clear that Claude looked at it ("What Claude saw").

`POST /upload` runs the same photo pipeline.

**Which story a photo led to** (`page.photo_links`): a call that asked about a photo has the topic `Asked about a photo: <start of the description>` (`calls.PHOTO_TOPIC`), and the story saved after that call has the call's id. The page's print says "Frank's story about it" with a link, the story's line says "about a photo the family sent", and the book links both ways. No extra Sheet column is needed, and it works for calls made before this was built.

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

**The page must answer fast, always.** Render's deploy check loads the page and wants an HTTP answer. A page that hangs on a slow Apps Script makes Render report "No open HTTP ports detected" and restart the app every few minutes, and visitors get a 502 each time. This happened once (Nov 2026). So `page.py` never waits more than `PAGE_WAIT_SECONDS` (4 s), and usually doesn't wait at all:
- Sheet and weather reads run in the background, one at a time per source.
- If the cached rows are older than 60 s, the page shows them right away and refreshes them in the background for the next visitor. Apps Script reads normally take 3–4 s, and nobody should wait for that.
- The page waits for a read, up to 4 s, only when nothing has been read yet (just after a restart).
- With nothing read yet (just after a restart), the page says "Loading from the family Sheet…", not "No stories yet". It then reloads itself after 5 s, at most 3 times and never while a visitor is filling in a form.
- Writes update the cached rows directly (`sheets.append` and `update_cell`), so a new photo or story shows at once.
- `page.warm()` starts reading as soon as the worker boots (from `start_background()`).
- Starting a call uses cached rows (`use_cache=True` in `check_limits` and `build_context`), so a slow Sheet doesn't hold up the call.
- Slow reads log `Sheet read for X took N s`, and failed reads log `Sheet read for X failed after N s: …`. If reads keep taking 10 s or more, check the Apps Script Executions page.
- Don't add anything to `GET /` or `GET /book` that waits on a network call.

Built (Nov 2026): `services/page.py` gathers everything in parallel and never raises. If the Sheet is down, it shows the last rows it read, and otherwise says plainly what's missing. Rows show a ✓ only when there's real, current evidence; otherwise they show a hollow circle and a sentence. The call card shows the call's real first message (live weather included) and what the next call will bring up, linked to that story. Photos show as "What Claude saw" prints: the six newest, with their question and an "Asked on a call" or "Waiting for the next call" chip. The story book leaves out `test` stories and flags concerns as "Worth checking on". A small script disables the submit button and shows a busy label, so a double click can't start two calls.

**Look** (the Storyline design system: https://claude.ai/artifact/UaANqeiqiRdzg3bCa7xCBD, private to the builder):
- **Color:** Pittsburgh's black and gold, light theme only (`color-scheme: light`), so every judge and the demo video see the same page.
- **Two big blocks:** the gold (`#f2b01e`, bridge gold) call card, in the hero's right column next to the wordmark and the call's spoken opening line, and the ink "What's working right now" panel with gold checks.
- **Bridge arches:** cut into the foot of both blocks, 3 on the card and 5 on the panel (3 on phones), plus 3 on the story book's gold cover. Use them nowhere else.
- **Small blocks:** gold-light and brick bleed off the top of the page, as on the design system's cover.
- **Layout:** photos and the story book are split sections, with the heading on the left and the content on the right.
- **Brick:** used only for concerns and errors, apart from the small top block.
- **Type:** Literata for Frank's world (stories, headings, spoken lines) and Public Sans for the machinery (forms, evidence, times). Both come from Google Fonts, with Georgia and system-ui fallbacks.
- **Source of truth:** the design tokens live at the top of `static/style.css`, and the design system's `tokens.json` and `bundle.css` are generated from that file. Change the CSS first, then regenerate the system.
- **Caching:** the stylesheet link carries a content hash (`?v=`), so a deploy never pairs the new page with an old stylesheet.

**Nothing starts at import time.** Render imports the app once in gunicorn's main process, then forks the worker that serves pages. Your logs show this: the app's first log line comes before `Starting gunicorn`. Threads don't survive a fork, and a thread caught mid-request at that moment can leave the worker stuck on a lock nobody releases. Anything a main-process thread reads also lands in the wrong process. In Nov 2026 this made the page say "Loading" forever while the reads succeeded.
- Background work (Sheet setup, keep-awake, `page.warm()`) starts in `app.start_background()`. It's called from gunicorn's `post_worker_init` hook in `gunicorn.conf.py`, or on the first request as a fallback, and runs once.
- `page.py` also gives the worker a fresh thread pool after a fork (`os.register_at_fork`).
- Tests call `storyline_app.start_background()` themselves.
- To reproduce Render locally: `gunicorn app:app --preload`.

## Environment variables

Commit `.env.example` with every name and no secret values. Keep `.env` out of git.

```
ANTHROPIC_API_KEY=
VAPI_API_KEY=
VAPI_PHONE_NUMBER_ID=          # the imported Twilio number's id in Vapi
SENDBLUE_API_KEY=              # texting: Sendblue OR Linq; Sendblue wins if both are set
SENDBLUE_API_SECRET=
SENDBLUE_PHONE_NUMBER=         # E.164; the Sendblue number (shared on the free plan)
LINQ_API_TOKEN=
LINQ_PHONE_NUMBER=             # E.164; the number people text photos to
LINQ_WEBHOOK_SECRET=           # for verifying Linq webhook signatures
FAMILY_PHONE_NUMBER=           # E.164; gets a text after each call. Must have texted Storyline's number once (and be a Sendblue verified contact).
SMS_SENDING_ENABLED=true       # turn off if sending texts misbehaves; reading photos still works
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
  texting.py            # which texting service is on, and the family's story text
  sendblue.py           # Sendblue send, download, webhook secret and setup
  linq.py               # Linq send + webhook signature check (the fallback)
  photos.py             # photo pipeline shared by texts and uploads
  page.py               # everything the home page shows (cached, never fails)
knowledge/profile.md    # Frank's fictional profile + seed stories (already written)
apps_script/Code.gs     # the Sheet bridge; pasted into the Sheet's Apps Script editor
templates/index.html
static/style.css
scripts/
  seed_sheet.py         # same setup as startup, by hand (--dry-run to preview)
  replay_vapi.py        # POST a saved end-of-call-report to a running app
  replay_sendblue.py    # POST a sample Sendblue incoming text to a running app
  replay_linq.py        # POST a saved message.received payload to a running app
tests/fixtures/         # saved payloads, phone numbers scrubbed
requirements.txt
gunicorn.conf.py        # 1 worker, 8 threads, 120 s timeout (read automatically by gunicorn)
.env.example
README.md
```

## Manual setup the builder does (Claude Code can't)

- **Twilio (voice only):** upgrade the account ($20) and buy a local US number with Voice. A 412 (Pittsburgh) number matches Frank. No texting setup is needed here.
- **Vapi:** create an account and import the Twilio number under Phone Numbers. SMS Enabled can stay off, since texting runs through Sendblue. Copy the number's id.
- **Sendblue:** sign up for the free plan at sendblue.com (no card). Copy the API key, API secret and the shared number into `SENDBLUE_API_KEY`, `SENDBLUE_API_SECRET` and `SENDBLUE_PHONE_NUMBER`. Add the family phone as a verified contact and text the number once from it. Render's logs then show `Sendblue webhook added` after the next deploy (or open `/setup/sendblue?code=DEMO_CODE`).
- **Linq (fallback, not in use):** sign up for the free sandbox and get an API token and a number. Subscribe `PUBLIC_BASE_URL/webhooks/linq` to `message.received` (in Linq's dashboard, or by opening `/setup/linq?code=DEMO_CODE` once `LINQ_API_TOKEN` is set), and copy the signing secret into `LINQ_WEBHOOK_SECRET`. Text the Linq number once from the family phone so the chat exists. Confirm the plan lasts through judging week and allows enough contacts (the free Hobby plan allows 20).
- **Anthropic Console:** create an API key.
- **OpenWeather:** create a key. New keys can take up to 2 hours to activate.
- **Google Sheet:** create the Sheet and set "anyone with the link" to Viewer. Paste `apps_script/Code.gs` into Extensions > Apps Script, set its `SECRET`, and deploy it as a web app (Execute as: Me, Who has access: Anyone). Copy the `/exec` URL. After any later edit to the script, deploy a new version. No Google Cloud project is needed.
- **Render:** create a Free web service from the GitHub repo. Build command `pip install -r requirements.txt`, start command `gunicorn app:app`, health check path `/health`. Add the environment variables.
- **Uptime alerts (optional):** the app keeps itself awake. A free UptimeRobot check on `/health` adds an email if the site ever goes down during judging.

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
- Give every external call (Claude, Sheets, Vapi, Sendblue, Linq, OpenWeather) a timeout, and wrap it in try/except so a failure gets logged and the app keeps working. A failed weather lookup must never stop a call.
- Log one clear line per event (call started, story saved, photo described) so problems are easy to find in Render's logs.
- Test with the replay scripts instead of real phone calls whenever you can, because every real call costs Vapi credits.
- Update README.md as features land.

## The README must include

- The live URL and a link to the demo video
- How judges try it. The demo code goes in the submission form, not the README.
- The six superpowers and where to see each one
- Setup: the accounts needed, the environment variables, deploying on Render, and creating the Sheet and its Apps Script bridge
- Limits: texting runs on Sendblue's free plan: a shared number and up to 10 verified contacts, so only the family's phones can text photos in. Calls are capped at 3 minutes.
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

- Render's logs show `Keep-awake on: visiting .../health` after each deploy, and no run of `Keep-awake visit failed` lines.
- Don't push to main during judging unless you're fixing a real bug, because every push redeploys.
- Check the Vapi credit and Twilio balances, and Sendblue's verified contacts, daily. Keep auto-recharge off everywhere.
- After judging, release the Twilio number so it stops billing monthly.
- **Reminder: close the Google Cloud billing account** from the abandoned service-account attempt, and check the $30 comes back. The app doesn't use Google Cloud (the Apps Script bridge doesn't need it), so this can be done any time.
