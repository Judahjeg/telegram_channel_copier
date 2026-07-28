# Telegram Channel & Group Message Copier Bot

A production-ready Telegram bot written in Python using `python-telegram-bot` (v21+). It automatically copies (not forwards) messages between paired source and destination channels/groups with randomized delays, scheduled activation times, album grouping, and SQLite persistence.

---

## 🌟 Features

- **Clean Copying**: Uses `copy_message` / `copy_messages` so posts appear as original content without "Forwarded from" tags.
- **Media Support**: Copies text, photos, videos, audio, voice notes, documents, stickers, animations/GIFs, and recreate polls fresh.
- **Album / Media Group Preservation**: Keeps grouped media (albums) together without splitting into standalone posts.
- **Scheduled Pair Activation**: Pairs remain dormant until their configured `activate_at` ISO timestamp is reached.
- **Randomized Delay**: Waits a random delay between 120–240 seconds per message/album before posting.
- **Independent Queues**: Multiple channel pairs process independently; a delay in one pair does not block others.
- **SQLite Persistence**: Remembers the `last_processed_message_id` for every pair so resuming after a restart picks up seamlessly.
- **Docker-ready**: Includes `Dockerfile` and `docker-compose.yml` for effortless deployment on Oracle Cloud / Linux VMs.

---

## 📦 Bringing Over an Old Channel's Materials (Easiest Way)

New channel pairs only copy messages posted while the bot is running — they can't reach back
into a channel's history automatically. To bring over materials already sitting in an old
channel, no chat IDs, no setup, no terminal:

1. In the **OLD channel**, long-press the **first** message you want migrated → **Copy Message
   Link** → paste that link to the bot in a private chat (forwarding the message instead works
   identically).
2. Do the same for the **last** message you want migrated.
3. Send the bot one message/link from the **NEW channel**.
4. The bot replies with what it understood — reply **yes** and it runs the migration itself.

That's the whole thing. Repeat those four steps for each channel you need to migrate. Prefer
typing exact numbers instead? `/backfill source_id dest_id start_id end_id` does the same thing
directly, or `/backfill ClassName start_id end_id` if you've already registered the class via
`/addclass`.

---

## 🤖 Easy Telegram Commands (No Coding Required!)

Once your bot is running, open a chat with your bot in Telegram to use these commands:

- **`/start`** – Shows welcome message and quick menu overview.
- **`/backfill <Subject> start_id end_id`** – Migrates messages already sitting in an old source channel (posted before the bot was watching) into the new destination channel. Real-time copying only reacts to messages posted while the bot is running, so previously posted lesson materials need this one-time migration. Note: old captions are copied as-is since the Bot API can't read historical message content for AI cleanup.
- **`/pairs`** – Displays detailed status, channel IDs, message spacing, and last copied message ID.
- **`/schedule`** – Displays your weekly class schedule timetable for all 5 subjects.
- **`/setdelay <Subject> <seconds>`** – Dictate message spacing directly in Telegram (e.g. `/setdelay Biology 180` sets 3-minute spacing).
- **`/status`** – Dashboard showing bot health, runtime, and pending message queues.
- **`/activate <Subject>`** – Manually toggle a subject active/dormant.
- **`/help`** – Beginner's guide and usage tips directly inside Telegram.

---

## 🔍 How to Obtain Telegram IDs

### 1. Finding Channel / Group Chat IDs (`source_chat_id` and `destination_chat_id`)

- **Option A (Via Telegram Bot)**:
  1. Add `@RawDataBot` or `@userinfobot` to your channel or group temporarily.
  2. The bot will send a JSON message containing chat details.
  3. Look for `"chat": { "id": -100xxxxxxxxxx }`.
  4. Note that channel and supergroup IDs in Telegram always start with `-100`.

- **Option B (Via Telegram Web)**:
  1. Open Telegram Web (`web.telegram.org`) in your browser.
  2. Open the source or destination channel/group.
  3. Look at the URL in your browser address bar:
     `https://web.telegram.org/a/#-1001234567890`
  4. The number `-1001234567890` is your `chat_id`.

> ⚠️ **Important Permission Setup**:
> - Add your copier bot to **BOTH** the source and destination chats as an **Administrator**.
> - **Source Chat**: Bot requires privileges to read messages/channel posts.
> - **Destination Chat**: Bot requires privileges to post messages.

---

### 2. Finding a Starting Message ID (`start_message_id`)

1. Open the source channel/group in Telegram Desktop or Web.
2. Right-click (or long press) on the specific message from which you want copying to start.
3. Select **Copy Post Link** (or **Copy Message Link**).
4. The link will look like:
   `https://t.me/c/1234567890/1500` or `https://t.me/mychannel/1500`
5. The number at the end (`1500`) is your `start_message_id`.

---

## ⚙️ Configuration (`config.json`)

Create or edit `config.json` in the root folder. You can configure as many pairs as you need:

```json
[
  {
    "name": "Biology Channel Pair",
    "source_chat_id": -1001234567890,
    "destination_chat_id": -1009876543210,
    "start_message_id": 1500,
    "activate_at": "2026-08-03T08:00:00"
  },
  {
    "name": "Chemistry Channel Pair",
    "source_chat_id": -1001112223334,
    "destination_chat_id": -1005556667778,
    "start_message_id": 800,
    "activate_at": "2026-08-17T08:00:00"
  }
]
```

### Configuration Fields

| Field | Type | Description |
|---|---|---|
| `name` | string | Unique name for the pair (used in logs and SQLite database). |
| `source_chat_id` | integer | Channel/Group ID to copy messages **from** (must start with `-100`). |
| `destination_chat_id` | integer | Channel/Group ID to copy messages **to** (must start with `-100`). |
| `start_message_id` | integer | Only messages with `ID >= start_message_id` will be processed. |
| `activate_at` | string | Activation timestamp in ISO format (`YYYY-MM-DDTHH:MM:SS`). Pair stays dormant until this time. |

---

## 🚀 Running Locally

### Prerequisites
- Python 3.10 or higher.
- A Telegram Bot Token from `@BotFather`.

### Steps

1. **Clone or navigate to project directory**:
   ```bash
   cd telegram_channel_copier
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Set Bot Token & Run**:
   - On **Linux / macOS**:
     ```bash
     export BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrsTUVwxyZ"
     python bot.py
     ```
   - On **Windows (PowerShell)**:
     ```powershell
     $env:BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrsTUVwxyZ"
     python bot.py
     ```

---

## 🐳 Deploying via Docker (Linux VM / Oracle Cloud)

### Option 1: Docker Compose (Recommended)

1. Set your `BOT_TOKEN` environment variable on your server:
   ```bash
   export BOT_TOKEN="your_telegram_bot_token_here"
   ```

2. Launch container stack in background:
   ```bash
   docker-compose up -d --build
   ```

3. View live logs:
   ```bash
   docker-compose logs -f
   ```

### Option 2: Docker CLI

1. Build the Docker image:
   ```bash
   docker build -t telegram-copier .
   ```

2. Run container with persistent SQLite storage and config binding:
   ```bash
   docker run -d \
     --name telegram_copier \
     --restart unless-stopped \
     -e BOT_TOKEN="your_telegram_bot_token_here" \
     -v $(pwd)/config.json:/app/config.json \
     -v $(pwd)/copier.db:/app/copier.db \
     telegram-copier
   ```

3. Monitor container logs:
   ```bash
   docker logs -f telegram_copier
   ```

---

## 📊 Database & Resuming State

The bot uses an SQLite database `copier.db` stored in the working directory. It automatically records progress:

- When restarted, the bot reads `last_processed_id` from `copier.db`.
- It ignores any incoming update with `message_id <= last_processed_id`.
- This guarantees zero duplicate posts and zero skipped posts upon restart.

---

## 🗓️ Weekly Auto-Delivery (No Manual Triggering)

For a range of old messages that should show up automatically every week during your actual
class time slot, instead of running `/backfill` yourself each session:

```
/autodeliver source_id dest_id start_id end_id days start_time duration_minutes [Name]
/autodeliver -1001111111111 -1002222222222 1 850 Mon,Wed,Fri 15:00 120 Physics
```

This delivers messages #1–850 automatically every Mon/Wed/Fri, starting at 15:00 **UTC**, spread
naturally 2–5 minutes apart across a 120-minute window — picking up exactly where it left off
each session until the whole range has been delivered over however many weeks that takes. You
get a message when each session starts and finishes; nothing to trigger yourself. Use
`/stopautodeliver` to see active schedules and remaining message counts, or
`/stopautodeliver Name` to cancel one.

**Times are UTC.** If you're in Nigeria (WAT, UTC+1), subtract 1 hour from your local class time
— a 4pm WAT class is `15:00` here.

---

## 🧠 Recovering Quiz Polls During Migration

Telegram refuses to let this bot `copy_message` a quiz poll unless the bot itself created that
quiz — it's how Telegram protects a quiz's correct answer from being read by anyone else, and no
code change gets around it. If your old channel's quizzes were posted by a different bot/account
(almost certainly true), every migration path (`/backfill`, `/autodeliver`, `/timedbackfill`)
would otherwise just skip them silently.

If you've connected your account with `/userbotlogin` (see below), the bot automatically
recovers them instead: it reads the quiz's real question, options, and correct answer directly
from the channel via your account (closed quizzes already reveal this; open ones get a single
throwaway vote cast to force Telegram to reveal it), then recreates it as a fresh quiz poll on
the destination with the same content. Without a connected account, quiz polls are skipped and
reported clearly in the completion message, with a suggestion to use `/quiz ClassName` to
generate a new AI practice quiz instead (not the original, but a working substitute).

---

## 🕰️ Migrating Old Channels With Their Real Class Timing

The bot itself (via the Telegram **Bot API**) can only react to messages posted while it's
running — it cannot read the *content* or *original timestamps* of messages already sitting in
an old channel from a previous year. Reading that requires a real Telegram user account (via
[Telethon](https://docs.telethon.dev/)), which *can* read full channel history — something
Bot API bots fundamentally cannot do. Two ways to use this:

### One-time setup (needed either way)

1. Get a free `api_id` / `api_hash` from <https://my.telegram.org> → **API Development Tools**
   (if the form errors on submit, make sure the "Short name" field has no symbols/spaces and the
   URL field isn't empty — filling it with `https://example.com` is enough).
2. Set them as environment variables on your server, alongside your existing `BOT_TOKEN`:
   ```bash
   export TELEGRAM_API_ID="12345678"
   export TELEGRAM_API_HASH="your_api_hash_here"
   ```
   This must run somewhere with normal internet access (your VPS/Render host, or your own
   machine) — Telegram's user-account protocol (MTProto) needs raw connectivity that sandboxed
   CI-style environments often block, even when regular HTTPS works fine there.

### Option A: From your phone, via the bot itself (recommended)

Once `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` are set on the server running `bot.py`, just chat with
your own bot:

1. `/userbotlogin +2348012345678` → Telegram sends you a real login code, and the bot replies with
   a web link (`https://your-app.onrender.com/verify?token=...`).
2. Open that link **in your phone's browser, not Telegram** — and enter the code there. This step
   can't happen inside a Telegram message at all: Telegram automatically invalidates a login code
   the instant it detects that code being typed into *any* Telegram chat, even one sent to your own
   bot, so `/userbotverify <code>` as a chat command is a dead end by design on Telegram's part.
   The web page is what makes this actually work.
3. `/mychannels` — lists every channel/group you're in with its exact chat ID.
4. `/analyzechannel <chat_id>` — message/media volume and how many class "sessions" (contiguous
   bursts separated by big gaps, like overnight) got detected.
5. `/previewtiming <chat_id> <session_index>` — preview the real message-by-message rhythm of one
   session before committing to it.
6. `/timedbackfill <source_id> <dest_id> <session_index>` — replays that session into the new
   channel using its *real* original pacing (bursts and pauses, not a uniform fake delay), capped
   so no single real-life pause stalls the whole replay. Posting still goes through the bot's own
   `copy_message`, so media stays intact with no re-upload. Interrupted runs are resume-safe.

This creates a `migration_userbot.session` file on the server the first time you log in — **treat
it like a password**, it grants full access to your Telegram account. Never commit it.

### Option B: `migrate_cli.py` from a terminal

Same capabilities, scriptable from a terminal instead of Telegram:

```bash
python migrate_cli.py login-request +2348012345678   # then login-verify <code>
python migrate_cli.py list-channels
python migrate_cli.py analyze -1001234567890
python migrate_cli.py preview-timing -1001234567890 --session-index 0
python migrate_cli.py dry-run -1001234567890 -1009876543210 --session-index 0
python migrate_cli.py run-backfill -1001234567890 -1009876543210 --session-index 0
```

Run `python migrate_cli.py` with no arguments for an interactive menu version of the same thing.
