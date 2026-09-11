# Deploying ASP Exam Checker live (web UI)

`web.py` serves the same monitor as the desktop app (`app.py`) as a website:
settings form + live console in the browser, reusing `dist/scrapper.py`.
It reads `PORT` from the environment and switches Chromium to **headless**
automatically in the cloud, so it runs unchanged on Render / Railway.

Run locally first to confirm: `python web.py` → http://127.0.0.1:8766

---

## ⚠️ Read this before deploying (differences vs Trip-Scrapper)

1. **This is a 24/7 monitor, not a search page.** Free tiers *sleep after ~15 min
   without visitors* — and a sleeping service does NOT check for exam slots.
   Fix: create a free monitor at https://uptimerobot.com that pings your app URL
   every 10 minutes. That keeps it awake (and doubles as downtime alerts).
2. **No visible browser in the cloud** (headless). You can't "leave the browser
   open and pay manually" like on the PC. The flow becomes: Telegram notification
   → open the link from the message on your phone → book/pay there.
   **Auto-update rebooking still works fully** — it's all automated.
3. **Your personal data (IDNP, buletin, certificat) lives on the server.**
   Always set the `ASP_UI_PASSWORD` env var, otherwise anyone with the URL can
   read your settings and control the monitor.
4. **Ephemeral disk**: settings entered in the web form are lost on each
   redeploy/restart. Either re-enter them, or set the `CREDENTIALS_JSON` env var
   to the full contents of your local `credentials.json` (one line) — the app
   seeds the file from it on boot.
5. **eservicii.gov.md may block datacenter IPs.** Unknown until tried — if every
   check errors out from the cloud but works locally, that's the cause.
6. RAM: headless Chromium fits in Render's free 512 MB, but it's tight.

---

## ⛔ ASP blocks Render's IP — the scan goes through a Cloudflare Worker

**2026-08-04 incident**: every scan failed with `get-service a raspuns 402`
(*402 Payment Required*, an nginx page). Proven **not** to be our bug:

| Where the request came from | Result |
|---|---|
| Home (Moldova, residential) | ✅ 200 — even with the `Sec-Fetch-*` headers stripped |
| Another datacenter | ✅ 200 |
| **Render (Frankfurt)** | ❌ **402** |

Same code, same headers → the block is on the **IP's reputation**, not on the
request. The service was scanning every **2 minutes** (≈3,600 requests/day from
one IP), which is what earned the flag. ⚠️ **No header change can fix this** —
don't repeat the 2026-08-03 bisection, it's a dead end here.

Fix = leave via a different IP. `proxy/worker.js` is a Cloudflare Worker that
relays only the 3 public calendar routes (whitelisted, key-protected).

**Live since 2026-08-04: `https://asp-proxy.wancea.workers.dev`** (Worker
`asp-proxy`, workers.dev subdomain `wancea`, free plan = 100k req/day vs our
~480). Verified through the Worker: no key → 403, wrong key → 403, unlisted
path → 404, POST → 405, and a full scan → 3 locations / 81 days.

Set on Render:

- `ASP_API_PROXY` = `https://asp-proxy.wancea.workers.dev`
- `ASP_API_PROXY_SECRET` = same string as the Worker's `PROXY_KEY` secret

Redeploy the Worker with `cd proxy && npx wrangler deploy`; rotate the key with
`npx wrangler secret put PROXY_KEY` (then update Render to match).

⚠️ A brand-new workers.dev subdomain needs ~2 min before TLS works — `curl`
exiting 35 right after `wrangler deploy` is propagation, not a broken Worker.

Both unset → the app talks to ASP directly (local/GUI/.exe are unaffected, and
so are the browser paths — Chromium has its own fingerprint and its own luck).
If the Worker's IP ever gets flagged too, change `ASP_API_PROXY` only.

🔴 **Also raise `interval_minutes`** (10–15). At 2 minutes the next IP burns too.

---

## ⛔ One always-on service per Render workspace (750-hour ceiling)

**2026-07-22 incident**: both `asp-exam-checker` and `Trip-Scrapper` were suspended —
*"Your workspace has used all of its 750 free instance hours this month."*

Render grants **750 free instance hours per _workspace_ per calendar month** — not per
service. This monitor is deliberately kept awake 24/7 (self-ping + external watchdog), so on
its own it burns **744 h in a 31-day month**. That leaves **6 hours of headroom and room for
exactly zero other free services** in the same workspace.

**Fix: give this service a workspace of its own.** Services cannot be transferred between
workspaces — you recreate them. Two options, same steps:

1. **New workspace on the same Render account** (easiest — GitHub is already authorized):
   workspace switcher → *New Workspace*. Each workspace gets its own 750 h.
2. **New Render account** (different email) if Render won't give free instances to a second
   workspace.

### Recreate checklist

1. New workspace/account → **New** → **Web Service** → same private repo → Docker → **Free**.
2. Environment variables — copy from the old service's Environment page, **except**:
   - ➕ add `ASP_TG_TOKEN` (the shared bot token)
   - ❌ **do NOT copy `RENDER_SERVICE_ID`** — Render injects it automatically, and a copied
     one points at the *old* service, so settings-persistence would silently write there and
     the new service would lose everything on every restart. `web.py` now detects this at
     boot (compares against `RENDER_SERVICE_NAME`), disables syncing and alerts on Telegram —
     but just don't set it.
   - 🔁 `RENDER_API_KEY` must be a key that can reach the **new** workspace (a new account
     needs a brand-new key).
   - ❌ `MAX_MONITORS` is obsolete (one shared scan now) — drop it.
   - ✅ `ACCOUNTS_JSON` — copy it across, otherwise all accounts and settings are gone.
3. Deploy, open the new URL, log in, confirm the accounts are there.
4. **Delete the old `asp-exam-checker` service** from the old workspace — otherwise it keeps
   eating the hours that Trip-Scrapper needs.
5. Update the new URL in `wancea23/uptime-watchdog` → Settings → Secrets → `ASP_URL`.
6. Tell the users the site address changed.

> The new workspace's hour counter starts at **0 immediately**, so the monitor can run again
> right away instead of waiting for the 1st of next month.

---

## Sharing it with other people (multi-user model)

Since 2026-07-22 the web app is built for **several users on one server**:

* **One shared scan.** The site is read **once per cycle for all three DECA
  locations**, no matter how many people are running. Whoever starts first
  starts the scan; everyone who starts afterwards just begins receiving
  notifications from that same scan. Stopping only unsubscribes *you* — the scan
  keeps running for the others and shuts down only when the last user stops.
  The scan interval is the smallest one requested by the active users.
* **Per-user filtering.** All three locations are always scanned, but each
  account is notified **only for the locations it ticked** (and its months).
* **One shared Telegram bot.** Nobody enters a bot token or chat id any more.

### Creating the shared bot (once, by you)

1. Telegram → **@BotFather** → `/newbot` → pick a name and a username.
2. Copy the token and set it as `ASP_TG_TOKEN` on Render (or locally:
   `set ASP_TG_TOKEN=123456:ABC...` before `python web.py`).
3. Restart. The log prints `Bot Telegram comun activ: @yourbot`.

### What your friends do

1. Open the site, create an account (invite code if you set `ASP_REGISTER_CODE`).
2. Press **Conecteaza Telegram** → the bot opens → **START**. That's it — the
   page detects the link within a few seconds.
3. Tick months + locations, press **PORNESTE MONITORUL**.

Bot commands: `/status` (their monitor state), `/stop` (unlink this chat),
`/start <code>` (link, done automatically by the button).

> Without `ASP_TG_TOKEN` the monitor refuses to start — there would be nowhere
> to send notifications.

---

## Step 1 — standalone repo (same trick as Trip-Scrapper)

This folder lives inside the big `Code` git repo, so copy it out first.
Note: `.gitignore` excludes `dist/`, so **copy `dist/scrapper.py` to the root**
of the standalone folder (`web.py` looks in both places).

```bash
# from a terminal, anywhere outside the Code repo:
cp -r "/path/to/ASP Scrapper" ./asp-checker
cd asp-checker
cp dist/scrapper.py ./scrapper.py
rm -rf dist build venv __pycache__ debug_out credentials.json "ASP Exam Checker.spec"
git init && git add . && git commit -m "ASP Exam Checker web"
# create an EMPTY repo on github.com (PRIVATE recommended!), then:
git remote add origin https://github.com/<your-username>/asp-checker.git
git branch -M main && git push -u origin main
```

> 🔐 `credentials.json` is gitignored AND deleted above — never push it.

## Step 2 — Render (via GitHub)

1. https://dashboard.render.com → **New** → **Web Service** → pick the repo.
2. Render detects the **Dockerfile** (needed — Chromium won't install on the
   plain Python runtime). Instance type: **Free**.
3. Environment variables:
   - `ASP_TG_TOKEN` = the shared Telegram bot token (**required** — see below)
   - `ASP_UI_PASSWORD` = a password you choose (required, see above)
   - `ASP_REGISTER_CODE` = invite code for new accounts (optional; empty = anyone
     with the URL can create an account)
   - `CREDENTIALS_JSON` = contents of your credentials.json (optional but handy)
4. Create. First build takes a few minutes (downloads Chromium).
5. Open `https://asp-checker-XXXX.onrender.com`, enter the password, check the
   settings, press **PORNESTE MONITORUL**, watch the console.
6. **Don't skip:** add the UptimeRobot ping (see warning #1).

(`render.yaml` is included, so the **Blueprint** flow also works.)

## Alternative — Railway (no GitHub)

```bash
npm i -g @railway/cli
railway login
railway init
railway up                    # uploads this folder, builds the Dockerfile
railway variables set ASP_UI_PASSWORD=<parola>
railway domain                # public https URL
```

Railway's paid-credit instances don't sleep, which suits a monitor better —
while trial credit lasts.

## Updating later

Push to GitHub → Render auto-redeploys. If you changed the scrapper logic,
remember the deployed repo uses the root-level `scrapper.py` copy:

```bash
cp dist/scrapper.py ./scrapper.py
git add . && git commit -m "update" && git push
```
