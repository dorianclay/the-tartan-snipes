import os
import re
import json
from collections import defaultdict
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv

load_dotenv()

app = App(token=os.environ["SLACK_BOT_TOKEN"])

TALLY_FILE = "tally.json"
CURSOR_FILE = "cursors.json"  # tracks how far back we've already backfilled

# ── Persistence helpers ───────────────────────────────────────────────────────

def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# { channel_id: { user_id: count } }
tally = defaultdict(lambda: defaultdict(int), {
    ch: defaultdict(int, counts)
    for ch, counts in load_json(TALLY_FILE, {}).items()
})

# { channel_id: oldest_ts_we_have_processed }
cursors = load_json(CURSOR_FILE, {})

def save_tally():
    save_json(TALLY_FILE, {ch: dict(counts) for ch, counts in tally.items()})

def save_cursors():
    save_json(CURSOR_FILE, cursors)

# ── Core logic ────────────────────────────────────────────────────────────────

def message_has_image(message):
    for f in message.get("files", []):
        if f.get("mimetype", "").startswith("image/"):
            return True
    for attachment in message.get("attachments", []):
        if attachment.get("image_url") or attachment.get("thumb_url"):
            return True
    return False

def process_message(message, channel):
    """Count mentions+images. Returns True if anything was counted."""
    if message.get("bot_id") or message.get("subtype"):
        return False
    if not message_has_image(message):
        return False
    mentioned_users = re.findall(r"<@([A-Z0-9]+)>", message.get("text", ""))
    if not mentioned_users:
        return False
    for user_id in set(mentioned_users):
        tally[channel][user_id] += 1
    return True

# ── Backfill on startup ───────────────────────────────────────────────────────

def backfill_channel(client, channel_id):
    """Page through history oldest-first, stopping at already-processed messages."""
    oldest = cursors.get(channel_id)  # None means we've never backfilled this channel
    count = 0
    cursor = None

    while True:
        kwargs = {"channel": channel_id, "limit": 200}
        if oldest:
            kwargs["oldest"] = oldest  # only fetch messages newer than last backfill
        if cursor:
            kwargs["cursor"] = cursor

        resp = client.conversations_history(**kwargs)
        messages = resp.get("messages", [])

        for msg in reversed(messages):  # process oldest first
            if process_message(msg, channel_id):
                count += 1

        # Update cursor to the newest message we've seen
        if messages:
            cursors[channel_id] = messages[0]["ts"]  # messages[0] is most recent
            save_cursors()

        if not resp.get("response_metadata", {}).get("next_cursor"):
            break
        cursor = resp["response_metadata"]["next_cursor"]

    save_tally()
    print(f"Backfilled #{channel_id}: {count} new image-tag messages processed.")

def backfill_all(client):
    """Backfill all public channels the bot is a member of."""
    resp = client.conversations_list(types="public_channel", exclude_archived=True)
    for channel in resp.get("channels", []):
        if channel.get("is_member"):
            backfill_channel(client, channel["id"])

SNIPER_CHANNEL_ID = None  # resolved once on first home-tab open

def get_sniper_channel_id(client):
    global SNIPER_CHANNEL_ID
    if SNIPER_CHANNEL_ID:
        return SNIPER_CHANNEL_ID
    cursor = None
    while True:
        kwargs = {"types": "public_channel", "exclude_archived": True, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        resp = client.conversations_list(**kwargs)
        for ch in resp.get("channels", []):
            if ch["name"] == "snipers":
                SNIPER_CHANNEL_ID = ch["id"]
                return SNIPER_CHANNEL_ID
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return None

def build_home_view(client):
    channel_id = get_sniper_channel_id(client)
    counts = tally.get(channel_id) if channel_id else None

    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "📸 Sniper Leaderboard"}}]

    if not counts:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "No tagged images recorded yet in #snipers."}})
    else:
        # Column headers
        blocks.append({
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": "*Victim*"},
                {"type": "mrkdwn", "text": "*Snipes*"},
            ]
        })
        blocks.append({"type": "divider"})

        rows = sorted(counts.items(), key=lambda x: -x[1])
        medals = ["🥇", "🥈", "🥉"]
        for i, (user_id, count) in enumerate(rows):
            info = client.users_info(user=user_id)
            name = info["user"]["profile"].get("display_name") or info["user"]["name"]
            prefix = medals[i] if i < len(medals) else f"{i + 1}."
            blocks.append({
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"{prefix} {name}"},
                    {"type": "mrkdwn", "text": str(count)},
                ]
            })

    return {"type": "home", "blocks": blocks}

# ── Event handlers ────────────────────────────────────────────────────────────

@app.event("app_home_opened")
def handle_home_opened(event, client):
    print(f"app_home_opened: tab={event.get('tab')} user={event.get('user')}")
    if event.get("tab") != "home":
        return
    client.views_publish(user_id=event["user"], view=build_home_view(client))

@app.command("/tally")
def handle_tally_command(ack, command, respond, client):
    ack()
    channel = command["channel_id"]
    if command.get("text", "").strip() == "reset":
        tally[channel].clear()
        save_tally()
        respond("Tally reset for this channel.")
        return
    counts = tally.get(channel)
    if not counts:
        respond("No tagged images recorded yet in this channel.")
        return
    lines = ["*📸 Image Tag Tally:*"]
    for user_id, count in sorted(counts.items(), key=lambda x: -x[1]):
        info = client.users_info(user=user_id)
        name = info["user"]["profile"].get("display_name") or info["user"]["name"]
        lines.append(f"{name}: {count}")
    respond("\n".join(lines))

@app.message()
def handle_message(message, client):
    if process_message(message, message.get("channel")):
        save_tally()
        client.reactions_add(
            channel=message["channel"],
            timestamp=message["ts"],
            name="white_check_mark"
        )
        cursors[message["channel"]] = message["ts"]
        save_cursors()
        client.views_publish(user_id=message["user"], view=build_home_view(client))

# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from slack_sdk import WebClient
    startup_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    print("Backfilling history...")
    backfill_all(startup_client)
    print("Starting bot...")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
