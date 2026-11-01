# AppX Downloader Bot

A Telegram bot that logs into AppX / Parmar Academy, downloads full course content
(videos, PDFs, images) and uploads it **topic-wise** as forum topics into a Telegram
supergroup — with MongoDB-backed resume-on-restart and aria2c 16-connection downloads.

---

## Architecture

```
You (Telegram) ──→ Bot (Pyrogram MTProto) ──→ AppX API
                         │
                         ↓  aria2c / yt-dlp download
                    /tmp/ (Heroku dyno)
                         │
                         ↓  MTProto upload (up to 2 GB, no 50 MB limit)
              Telegram Supergroup (forum topics)
                         │
                    MongoDB Atlas
               (job state, topic IDs, auth users)
```

**Key design decisions:**
- **Pyrogram** (not python-telegram-bot): bot uses `api_id + api_hash + bot_token` → MTProto protocol → uploads up to 2 GB without a local Bot API server
- **No StringSession / no user account**: the bot itself is made admin in the supergroup
- **aria2c** with 16 parallel connections (`-x 16 -j 1 -k 1M`) for fast downloads
- **pyromod** `listen()` for interactive conversation flow (no messy ConversationHandler)
- **MongoDB** persists job progress → if Heroku restarts mid-job, progress is saved

---

## One-Click Heroku Deploy

[![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/yourusername/appx-bot)

> Replace `yourusername/appx-bot` with your actual GitHub repo URL after pushing.

---

## Setup

### 1 — Prerequisites

| What | Where |
|---|---|
| Bot Token | [@BotFather](https://t.me/BotFather) → `/newbot` |
| API ID + API Hash | [my.telegram.org](https://my.telegram.org) → App |
| Your Telegram ID | [@userinfobot](https://t.me/userinfobot) |
| Supergroup | Create group → Settings → **Topics** → enable → make bot **admin** with "Manage Topics" permission |
| Supergroup ID | Forward any group message to [@userinfobot](https://t.me/userinfobot) |
| MongoDB Atlas | [cloud.mongodb.com](https://cloud.mongodb.com) → Free M0 cluster → copy connection string |

### 2 — Deploy to Heroku

1. Push this repo to GitHub.
2. Click the **Deploy to Heroku** button.
3. Fill in all env vars in the form.
4. Click **Deploy app**.
5. Go to **Resources** tab → enable the `worker` dyno (disable `web` if present).

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `API_ID` | ✅ | Telegram app API ID |
| `API_HASH` | ✅ | Telegram app API Hash |
| `BOT_TOKEN` | ✅ | BotFather token |
| `OWNER_ID` | ✅ | Your Telegram user ID |
| `SUPERGROUP_ID` | ✅ | Target supergroup (negative int) |
| `MONGO_URL` | ✅ | MongoDB connection string |
| `CREDIT` | ❌ | Caption credit text (default: `AppX Bot`) |
| `DOWNLOAD_DIR` | ❌ | Temp dir on dyno (default: `/tmp/appx_downloads`) |

---

## Commands

| Command | Who | Description |
|---|---|---|
| `/start` | All | Welcome message |
| `/appx` | Auth users | Start a course download session |
| `/stop` | Auth users | Cancel current session |
| `/status` | Auth users | Show active jobs |
| `/id` | All | Show your Telegram ID |
| `/addauth <id>` | Owner | Authorize a user |
| `/rmauth <id>` | Owner | Remove a user |
| `/users` | Owner | List all authorized users |

---

## How `/appx` works

1. **Domain** — Enter your AppX API domain (e.g. `parmaracademyapi.teachx.in`)
2. **Login** — JWT token or email+password
3. **Course** — Pick from your enrolled courses
4. **Subjects** — Pick subjects (or `all`)
5. **Batch name** — Label for captions
6. **Quality + topic-wise** — Resolution + whether to create forum topics

The bot then fetches all lessons, downloads each one with aria2c/yt-dlp, and uploads it
to your supergroup. Progress is saved to MongoDB every item — if the dyno restarts, no
re-downloads from scratch.

---

## Notes

- The **supergroup must have Topics enabled** (forum mode).
- The bot must be **admin** with **"Manage Topics"** permission.
- Use a **Standard-2X** dyno (or larger) — downloads can be CPU/memory intensive.
- Files are deleted from the dyno after upload.
- The pyromod session file (`appx_bot.session`) is stored in the dyno's working directory. It persists across restarts as long as the dyno isn't destroyed.
