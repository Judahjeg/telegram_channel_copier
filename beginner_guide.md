# Absolute Beginner's Guide: Testing & Hosting Your Telegram Copier Bot for Free

If you have no prior coding or Docker experience, don't worry! Follow this simple step-by-step guide to test the bot on your computer first, and then host it online 24/7 for free.

---

## 📌 Phase 1: Get Your Free Telegram Bot Token (2 Minutes)

1. Open Telegram on your phone or desktop.
2. Search for `@BotFather` (the official Telegram bot with a blue verification checkmark) and start a chat.
3. Send the command:
   ```text
   /newbot
   ```
4. Follow the prompts:
   - Enter a name for your bot (e.g. `My Channel Copier`).
   - Enter a username ending in `bot` (e.g. `my_cool_copier_bot`).
5. **BotFather will give you an HTTP API Token** that looks like this:
   `7123456789:AAFl...your_token_here...`
6. **Save this token securely!** You will use it as your `BOT_TOKEN`.

---

## 📌 Phase 2: Set Up Telegram Channels & Permissions (3 Minutes)

1. **Add Your Bot as an Admin**:
   - Go to your **Source Channel/Group** -> Edit -> Administrators -> Add Administrator -> Search for your bot username -> Save.
   - Go to your **Destination Channel/Group** -> Edit -> Administrators -> Add Administrator -> Search for your bot username -> Save (Make sure "Post Messages" permission is enabled).

2. **Get Chat IDs**:
   - Forward any message from your Source Channel to the Telegram bot `@RawDataBot`.
   - Look at the text response for `"chat": {"id": -100xxxxxxxxxx}`.
   - Repeat for your Destination Channel.
   - Note: Channel IDs always start with `-100`.

3. **Get a Message ID to Start From**:
   - Open your source channel.
   - Right-click on a message -> **Copy Link**.
   - The link looks like `https://t.me/c/1234567890/1500`. The number `1500` is your `start_message_id`.

---

## 📌 Phase 3: Test It on Your Computer Right Now (5 Minutes)

### On macOS / Linux:

1. Open the **Terminal** app.
2. Navigate to your bot folder:
   ```bash
   cd ~/.gemini/antigravity/scratch/telegram_channel_copier
   ```
3. Install the required library:
   ```bash
   pip3 install -r requirements.txt
   ```
4. Edit `config.json` with your real chat IDs and desired `activate_at` timestamp.
5. Set your bot token and start the bot:
   ```bash
   export BOT_TOKEN="your_token_from_botfather_here"
   python3 bot.py
   ```

You will see logs like:
```text
2026-07-28 00:10:00 - [INFO] - Loaded 2 channel pairs from configuration.
2026-07-28 00:10:00 - [INFO] - 🤖 Starting Telegram Channel Copier Bot (Polling mode)...
```

---

## 📌 Phase 4: Host It 24/7 For Free Online (Without Keeping Your PC On)

### Option A: Render.com (Easiest – No Linux Commands Required)

[Render.com](https://render.com) offers free hosting suitable for Python background workers.

1. **Create a Free Account** on [Render.com](https://render.com) and [GitHub.com](https://github.com).
2. Upload this bot folder (`telegram_channel_copier`) to a private GitHub repository.
3. On Render Dashboard:
   - Click **New +** -> **Background Worker**.
   - Connect your GitHub repository.
   - **Environment**: Select `Python 3`.
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
4. Under **Environment Variables**:
   - Key: `BOT_TOKEN`
   - Value: `your_token_from_botfather_here`
5. Click **Create Background Worker**. Done! Render will run your bot 24/7 for free.

---

### Option B: Oracle Cloud Free Tier (Powerful & Permanent)

Oracle provides a **100% Always Free** cloud virtual machine (VM) with 4 Arm cores and 24 GB RAM.

1. Sign up for [Oracle Cloud Free Tier](https://www.oracle.com/cloud/free/).
2. Create an `Always Free` Ubuntu Compute Instance.
3. Connect to your VM via SSH and run:
   ```bash
   git clone <your-repo-url>
   cd telegram_channel_copier
   docker-compose up -d --build
   ```
   *(Your bot will run silently in the background forever).*
