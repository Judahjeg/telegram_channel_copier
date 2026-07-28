from __future__ import annotations

import argparse
import asyncio
import os

import db
import userbot_client as ub

from telegram import Bot
from telegram.error import RetryAfter, BadRequest, TimedOut, NetworkError

LOGIN_STATE_FILE = ".login_state"


async def _call_with_retry(coro_func, max_attempts: int = 4, **kwargs):
    for attempt in range(max_attempts):
        try:
            return await coro_func(**kwargs)
        except RetryAfter as e:
            wait_s = float(e.retry_after) + 1.0
            print(f"  [flood control] waiting {wait_s:.0f}s...")
            await asyncio.sleep(wait_s)
        except (TimedOut, NetworkError):
            if attempt == max_attempts - 1:
                raise
            await asyncio.sleep(3 * (attempt + 1))
    return None


def _fmt_dt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


async def cmd_login_request(args) -> None:
    phone_code_hash = await ub.request_login_code(args.phone)
    with open(LOGIN_STATE_FILE, "w") as f:
        f.write(f"{args.phone}\n{phone_code_hash}\n")
    print(f"Code sent to {args.phone}. Check Telegram/SMS, then run:")
    print("  python migrate_cli.py login-verify <the_code> [--password YOUR_2FA_PASSWORD]")


async def cmd_login_verify(args) -> None:
    if not os.path.exists(LOGIN_STATE_FILE):
        print("No pending login request found. Run 'login-request <phone>' first.")
        return
    with open(LOGIN_STATE_FILE) as f:
        phone, phone_code_hash = f.read().splitlines()
    result = await ub.complete_login(phone, args.code, phone_code_hash, password=args.password)
    os.remove(LOGIN_STATE_FILE)
    print(result)


async def cmd_list_channels(args) -> None:
    channels = await ub.list_channels()
    print(f"\n{'ID':<16}{'Type':<12}{'Username':<20}Title")
    print("-" * 80)
    for c in channels:
        uname = f"@{c.username}" if c.username else "-"
        print(f"{c.id:<16}{c.kind:<12}{uname:<20}{c.title}")
    print(f"\n{len(channels)} chats found.")


async def cmd_analyze(args) -> None:
    print(f"Fetching history for {args.channel_id} (this can take a while for large channels)...")
    messages = await ub.fetch_channel_history(args.channel_id, limit=args.limit)
    stats = ub.analyze_channel(messages, gap_minutes=args.gap_minutes)

    if stats["message_count"] == 0:
        print("No messages found (check the ID and that this account is a member of that chat).")
        return

    print(f"\nMessages: {stats['message_count']}")
    print(f"Date range: {stats['date_range'][0]} -> {stats['date_range'][1]}")
    print(f"Media messages: {stats['media_count']}")
    print(f"Approx text volume: {stats['total_text_chars']} chars (~{stats['estimated_tokens']} tokens)")
    print(f"Detected sessions (gap > {args.gap_minutes}min): {stats['session_count']}\n")
    for i, s in enumerate(stats["sessions"]):
        print(
            f"  [{i}] {s['start']}  ->  {s['end']}  "
            f"({s['message_count']} msgs, {s['media_count']} media, {s['duration_minutes']}min)"
        )


async def cmd_preview_timing(args) -> None:
    messages = await ub.fetch_channel_history(args.channel_id, limit=args.limit)
    sessions = ub.group_into_sessions(messages, gap_minutes=args.gap_minutes)
    if not sessions:
        print("No messages found in this channel.")
        return
    if args.session_index >= len(sessions):
        print(f"Only {len(sessions)} session(s) detected (valid index range: 0-{len(sessions) - 1}).")
        return

    session = sessions[args.session_index]
    deltas = session.deltas_seconds
    print(f"\nSession [{args.session_index}]: {_fmt_dt(session.start_date)} -> {_fmt_dt(session.end_date)}")
    print(f"{len(session.messages)} messages, {session.duration_seconds / 60:.1f} min total\n")
    for msg, delta in zip(session.messages, deltas):
        capped = min(delta, args.max_gap_seconds)
        marker = " (capped)" if delta > args.max_gap_seconds else ""
        kind = "[media]" if msg.has_media else "[text]"
        print(f"  +{capped:>6.0f}s{marker}  msg #{msg.id}  {kind}")


async def cmd_dry_run(args) -> None:
    await _run_timed_backfill(args, live=False)


async def cmd_run_backfill(args) -> None:
    await _run_timed_backfill(args, live=True)


async def _run_timed_backfill(args, live: bool) -> None:
    bot_token = os.getenv("BOT_TOKEN")
    if live and not bot_token:
        print("BOT_TOKEN environment variable is required to actually post messages "
              "(same token your bot.py already uses).")
        return

    messages = await ub.fetch_channel_history(args.channel_id, limit=args.limit)
    sessions = ub.group_into_sessions(messages, gap_minutes=args.gap_minutes)
    if not sessions:
        print("No messages found in this channel.")
        return
    if args.session_index >= len(sessions):
        print(f"Only {len(sessions)} session(s) detected (valid index range: 0-{len(sessions) - 1}).")
        return

    session = sessions[args.session_index]
    deltas = session.deltas_seconds
    pair_name = args.pair_name or f"userbot_{args.channel_id}_to_{args.dest_id}_s{args.session_index}"

    bot = Bot(token=bot_token) if live else None
    copied = 0
    skipped = 0

    label = "" if live else "[DRY RUN] "
    print(
        f"{label}Replaying session [{args.session_index}] ({len(session.messages)} messages) "
        f"from {args.channel_id} -> {args.dest_id}"
    )
    print(f"Pair name for resume/tracking: '{pair_name}'\n")

    for msg, delta in zip(session.messages, deltas):
        wait_s = min(delta, args.max_gap_seconds)
        if wait_s > 0:
            marker = " (capped)" if delta > args.max_gap_seconds else ""
            print(f"  waiting {wait_s:.0f}s{marker} (original gap was {delta:.0f}s)...")
            if live:
                await asyncio.sleep(wait_s)

        already = db.get_dest_msg_id(pair_name, msg.id) if live else None
        if already:
            print(f"  msg #{msg.id}: already migrated -> dest #{already}, skipping (resume-safe)")
            skipped += 1
            continue

        if not live:
            print(f"  {label}would copy msg #{msg.id} ({'media' if msg.has_media else 'text'})")
            copied += 1
            continue

        try:
            result = await _call_with_retry(
                bot.copy_message,
                chat_id=args.dest_id,
                from_chat_id=args.channel_id,
                message_id=msg.id,
            )
            if result:
                db.save_message_mapping(pair_name, msg.id, result.message_id)
                copied += 1
                print(f"  msg #{msg.id}: copied -> dest #{result.message_id}")
        except BadRequest as e:
            skipped += 1
            print(f"  msg #{msg.id}: skipped ({e})")

    print(f"\nDone. Copied: {copied}, Skipped: {skipped}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Interactive migration tool: reads real history/timing from old channels "
                    "(via your own account, since the Bot API can't) and replays it into new "
                    "channels using the bot's copy API with the original class rhythm."
    )
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("login-request", help="Step 1: send yourself a Telegram login code")
    sp.add_argument("phone", help="Your phone number in international format, e.g. +2348012345678")
    sp.set_defaults(func=cmd_login_request)

    sp = sub.add_parser("login-verify", help="Step 2: verify the code you received")
    sp.add_argument("code")
    sp.add_argument("--password", default=None, help="Your 2FA password, if enabled on your account")
    sp.set_defaults(func=cmd_login_verify)

    sp = sub.add_parser("list-channels", help="List every channel/group your account can see, with chat IDs")
    sp.set_defaults(func=cmd_list_channels)

    sp = sub.add_parser("analyze", help="Test/inspect a channel: message count, media, detected sessions")
    sp.add_argument("channel_id", type=int)
    sp.add_argument("--gap-minutes", type=float, default=90.0)
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("preview-timing", help="Show the real message-by-message timing for one session")
    sp.add_argument("channel_id", type=int)
    sp.add_argument("--session-index", type=int, default=0)
    sp.add_argument("--gap-minutes", type=float, default=90.0)
    sp.add_argument("--max-gap-seconds", type=float, default=1200.0)
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_preview_timing)

    sp = sub.add_parser("dry-run", help="Simulate a timed backfill without posting anything")
    sp.add_argument("channel_id", type=int)
    sp.add_argument("dest_id", type=int)
    sp.add_argument("--session-index", type=int, default=0)
    sp.add_argument("--gap-minutes", type=float, default=90.0)
    sp.add_argument("--max-gap-seconds", type=float, default=1200.0)
    sp.add_argument("--pair-name", default=None)
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_dry_run)

    sp = sub.add_parser("run-backfill", help="Actually replay a session's real timing into the destination channel")
    sp.add_argument("channel_id", type=int)
    sp.add_argument("dest_id", type=int)
    sp.add_argument("--session-index", type=int, default=0)
    sp.add_argument("--gap-minutes", type=float, default=90.0)
    sp.add_argument("--max-gap-seconds", type=float, default=1200.0)
    sp.add_argument("--pair-name", default=None)
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_run_backfill)

    return p


MENU = """
=== Telegram Migration Control Panel ===
1. List my channels (find chat IDs)
2. Analyze / test a channel
3. Preview real session timing
4. Dry-run a timed backfill (no posting)
5. Run a timed backfill for real
6. Exit
"""


async def interactive_loop() -> None:
    if not await ub.is_logged_in():
        print(
            "Not logged in yet. Run:\n"
            "  python migrate_cli.py login-request +yourphone\n"
            "  python migrate_cli.py login-verify <code>"
        )
        return

    while True:
        print(MENU)
        choice = input("Choose an option: ").strip()

        if choice == "1":
            await cmd_list_channels(argparse.Namespace())
        elif choice == "2":
            cid = int(input("Channel ID: ").strip())
            gap = float(input("Session gap in minutes [90]: ").strip() or 90)
            await cmd_analyze(argparse.Namespace(channel_id=cid, gap_minutes=gap, limit=None))
        elif choice == "3":
            cid = int(input("Channel ID: ").strip())
            idx = int(input("Session index [0]: ").strip() or 0)
            gap = float(input("Session gap in minutes [90]: ").strip() or 90)
            maxgap = float(input("Max gap cap in seconds [1200]: ").strip() or 1200)
            await cmd_preview_timing(
                argparse.Namespace(channel_id=cid, session_index=idx, gap_minutes=gap,
                                    max_gap_seconds=maxgap, limit=None)
            )
        elif choice in ("4", "5"):
            cid = int(input("Source channel ID: ").strip())
            did = int(input("Destination channel ID: ").strip())
            idx = int(input("Session index [0]: ").strip() or 0)
            gap = float(input("Session gap in minutes [90]: ").strip() or 90)
            maxgap = float(input("Max gap cap in seconds [1200]: ").strip() or 1200)
            pname = input("Pair name for tracking (blank = auto): ").strip() or None
            ns = argparse.Namespace(
                channel_id=cid, dest_id=did, session_index=idx, gap_minutes=gap,
                max_gap_seconds=maxgap, pair_name=pname, limit=None,
            )
            if choice == "4":
                await cmd_dry_run(ns)
            else:
                confirm = input(f"This will really post to chat {did}. Type 'yes' to continue: ").strip()
                if confirm.lower() == "yes":
                    await cmd_run_backfill(ns)
        elif choice == "6":
            break
        else:
            print("Unknown option.")


def main() -> None:
    db.init_db()
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        asyncio.run(interactive_loop())
        return

    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
