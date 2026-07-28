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

## 🕰️ Migrating Old Channels With Their Real Class Timing (`migrate_cli.py`)

The bot itself (via the Telegram **Bot API**) can only react to messages posted while it's
running — it cannot read the *content* or *original timestamps* of messages already sitting in
an old channel from a previous year. `migrate_cli.py` fills that gap using your own Telegram
account (via [Telethon](https://docs.telethon.dev/)), which *can* read full channel history.

It lets you replay an old class's real message-by-message rhythm (the natural bursts and pauses
from when the tutor actually taught it) into this year's channel, instead of dumping everything
at once or on a fake uniform delay — while the actual posting still goes through your bot's
`copy_message` call, so media is copied cleanly with no re-upload and no "Forwarded from" tag.

### One-time setup

1. Get a free `api_id` / `api_hash` from <https://my.telegram.org> → **API Development Tools**.
2. Set them as environment variables alongside your existing `BOT_TOKEN`:
   ```bash
   export TELEGRAM_API_ID="12345678"
   export TELEGRAM_API_HASH="your_api_hash_here"
   ```
3. Log in once (two steps, since it needs your live Telegram code):
   ```bash
   python migrate_cli.py login-request +2348012345678
   # check Telegram/SMS for the code, then:
   python migrate_cli.py login-verify 12345
   ```
   This creates a local `migration_userbot.session` file that persists the login — **treat it
   like a password**, it grants full access to your Telegram account. Don't commit it or share it.

### Using it

Run `python migrate_cli.py` with no arguments for an interactive menu (list channels, analyze a
channel's message/media volume and detected class sessions, preview the real timing of a
session, dry-run a migration, or run it for real). Every action is also available as a direct
subcommand (`python migrate_cli.py --help`) for scripting, e.g.:

```bash
python migrate_cli.py list-channels
python migrate_cli.py analyze -1001234567890
python migrate_cli.py preview-timing -1001234567890 --session-index 0
python migrate_cli.py dry-run -1001234567890 -1009876543210 --session-index 0
python migrate_cli.py run-backfill -1001234567890 -1009876543210 --session-index 0
```

Real gaps between messages are replayed as-is (capped at `--max-gap-seconds`, default 20
minutes, so one long real-life pause doesn't stall the whole replay). Runs are resume-safe: if
interrupted, re-running the same `run-backfill` command skips messages already migrated for
that pair name.
