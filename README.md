# AI Brief

A personal morning reader for iPhone, with tabs for **AI · Markets · Tech · News · Long reads**. Every morning, a GitHub Action pulls your feeds, optionally writes a short Claude summary for each tab, and publishes a page you open like an app from your home screen.

```
GitHub Action (daily cron) → build.py → fetch feeds → [Claude summary] → site/index.html → GitHub Pages → iPhone home screen
                                                                       ↘ [ntfy push notification]
```

**Cost:** free. The AI summary is optional and costs a few cents a day in API usage.
**Dependencies:** none. It uses only Python's standard library.

---

## Files

| File | What it does |
|---|---|
| `feeds.json` | Your tabs, each with its sections, sources, quick links, and a `focus` line that steers its AI summary. **Edit this to customize.** |
| `build.py` | Fetches feeds, calls Claude once per tab (optional), renders the page, sends the push (optional). |
| `template.html` | The page design: tab bar with unread counts, dark/light mode, read tracking, "Hide read", "Mark read". |
| `site/` | App shell: manifest, icons, offline support. `index.html` here is a sample and gets rebuilt every run. |
| `.github/workflows/daily.yml` | Runs the build daily at 10:40 UTC (4:40 AM CST / 5:40 AM CDT), with a backup at 11:25 UTC that only runs if the first one was skipped by GitHub, and deploys it. |

---

## Setup (about 20 minutes)

### 1. Create the repo
1. Sign in to GitHub with a **personal** account. This is personal learning, so keep it off work accounts.
2. Create a new **public** repo named `ai-brief`. Free GitHub Pages requires a public repo; the page only shows public feed content.
3. Upload everything in this folder, including the hidden `.github` folder. On a Mac, press `Cmd+Shift+.` in Finder to show hidden folders, or use GitHub Desktop.

### 2. Turn on GitHub Pages
Go to repo **Settings → Pages → Source: GitHub Actions**.

### 3. (Optional) Turn on AI summaries
1. Get an API key at console.anthropic.com and add a few dollars of credit.
2. In the repo, go to **Settings → Secrets and variables → Actions → New repository secret**.
   - Name: `ANTHROPIC_API_KEY`, Value: your key
3. Optional: add a **variable** (not a secret) named `ANTHROPIC_MODEL` to change the model. The default is `claude-haiku-5-5`, which is the cheapest.

Never paste the key into code or a file. Keep it only in GitHub Secrets.

### 4. Run it once
Go to the **Actions tab → Build AI Brief → Run workflow**. When it turns green, your URL is
`https://<your-username>.github.io/ai-brief/`

### 5. (Optional) Morning push notification
1. Install the free **ntfy** app on your iPhone.
2. Pick a long random topic name, e.g. `aibrief-7f3k92xq`. Anyone who knows the name can read it, so make it unguessable.
3. In the ntfy app, subscribe to that topic.
4. Add the repo secret `NTFY_TOPIC` = your topic name.
5. Add the repo **variable** `SITE_URL` = your Pages URL, so tapping the notification opens the brief.

---

## iPhone setup

### Add it to the home screen as an app
1. Open your Pages URL in **Safari**.
2. Tap **Share → Add to Home Screen → Add**.

It now opens full-screen with its own icon, and shows the last brief when you're offline.

### Make a one-tap Shortcut
1. Open the **Shortcuts** app, tap **+**, and name it **AI Brief**.
2. Add the action **Set Focus → Do Not Disturb → Turn On Until Time** (15 min). This step is optional.
3. Add the action **Open URLs** with your Pages URL.

Trigger it any of these ways:
- **Siri:** "Hey Siri, AI Brief"
- **Back Tap:** Settings → Accessibility → Touch → Back Tap → Double Tap → **AI Brief**
- **Action Button** (iPhone 15 Pro and later): Settings → Action Button → Shortcut → **AI Brief**
- **Daily automation:** Shortcuts → Automation → **+** → Time of Day (e.g. 7:00 AM) → Run Immediately → Run Shortcut → **AI Brief**

---

## Customizing

- **Add a source:** add `{"name": "...", "url": "<rss feed url>"}` to a section in `feeds.json`. For most sites, `<site>/feed` or `<site>/rss` works. Substack sites always use `/feed`, but `*.substack.com` addresses may be blocked from GitHub's servers, so prefer a custom domain if the newsletter has one.
- **Add or reorder tabs:** add an object to `tabs` in `feeds.json` with `name`, `focus`, `sections`, and `links`. Order in the file is the order on screen.
- **Open a specific tab:** add `#` plus the tab name in lowercase with dashes, e.g. `…/aibrief/#news` or `…/aibrief/#long-reads`. This is handy in an iPhone Shortcut.
- **Email-only newsletters:** use kill-the-newsletter.com to turn them into a feed URL.
- **Change the time:** edit the two `cron` lines in `daily.yml` (main and backup). They are in UTC. Avoid minute `0`: GitHub often delays or drops runs at the top of the hour.
- **Too much or too little:** change `days` or `max_per_feed` for a section.
- **Run locally:** `python3 build.py`, then open `site/index.html`.

If a feed fails, the rest still build, and the failure is listed at the bottom of the page.

## Notes

- Read tracking and your last-opened tab are stored on your phone only (Safari local storage).
- AP News and Reuters have no public feeds, so they're quick links on the News tab.
- AI Snake Oil was renamed **AI as Normal Technology**. The feed in `feeds.json` already points to the new address.
- Epoch AI, GovAI, Anthropic, Lawfare, and the others under "Check-ins" had no verified feed, so they're quick links instead.
