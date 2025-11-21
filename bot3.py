import os
import json
import asyncio
from typing import Optional

from pyrogram import Client, filters
from pyrogram.types import Message
import httpx

# --- Configuration ---
API_ID = int(os.getenv("API_ID", "123456"))    # your Telegram API ID (learn more from my.telegram.org)
API_HASH = os.getenv("API_HASH", "your_api_hash")
BOT_TOKEN = os.getenv("BOT_TOKEN", "your_bot_token_here")

DATA_FILE = "render_keys.json"   # simple storage; use a secure vault in production
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))  # optional: restrict commands to this chat/user (0 = disabled)

RENDER_BASE = "https://api.render.com/v1"

# --- Helpers for storing API keys (very simple) ---
def load_store() -> dict:
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_store(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def set_api_key_for_chat(chat_id: int, api_key: str):
    data = load_store()
    data[str(chat_id)] = api_key
    save_store(data)

def get_api_key_for_chat(chat_id: int) -> Optional[str]:
    return load_store().get(str(chat_id))

# --- HTTP helper using httpx (async) ---
async def render_request(method: str, path: str, api_key: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {api_key}"
    headers["Accept"] = "application/json"
    url = f"{RENDER_BASE}{path}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.request(method, url, headers=headers, **kwargs)
        # Basic error handling
        if r.status_code == 429:
            raise RuntimeError("Rate limited by Render API (429).")
        if r.status_code >= 400:
            raise RuntimeError(f"Render API error {r.status_code}: {r.text}")
        return r.json()

# --- Pyrogram bot ---
app = Client("render-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

def admin_only(func):
    async def wrapper(client, message: Message):
        if ADMIN_CHAT_ID != 0 and message.chat.id != ADMIN_CHAT_ID:
            await message.reply_text("Unauthorized.")
            return
        await func(client, message)
    return wrapper

@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message: Message):
    await message.reply_text(
        "Hi — I can help manage your Render account.\n"
        "Commands:\n"
        "/setkey <API_KEY> - store your Render API key for this chat\n"
        "/getkey - show whether a key is stored (not the key itself)\n"
        "/list_services - list first page of services\n"
        "/restart <service_id> - restart a service\n"
        "/scale <service_id> <count> - set manual instance count\n        /env_set <service_id> KEY=VALUE[,KEY2=VALUE2] - replace env vars\n    "
    )

@app.on_message(filters.command("setkey") & filters.private)
@admin_only
async def setkey(client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: /setkey <RENDER_API_KEY>")
        return
    api_key = message.command[1].strip()
    set_api_key_for_chat(message.chat.id, api_key)
    await message.reply_text("Render API key saved for this chat. (Keep it secret!)")

@app.on_message(filters.command("getkey") & filters.private)
@admin_only
async def getkey(client, message: Message):
    key = get_api_key_for_chat(message.chat.id)
    if key:
        await message.reply_text("A Render API key is stored for this chat.")
    else:
        await message.reply_text("No Render API key stored. Use /setkey to add one.")

@app.on_message(filters.command("list_services") & filters.private)
@admin_only
async def list_services(client, message: Message):
    api_key = get_api_key_for_chat(message.chat.id)
    if not api_key:
        await message.reply_text("No Render API key stored. Use /setkey <KEY>.")
        return
    try:
        data = await render_request("GET", "/services", api_key, params={"limit": 20})
    except Exception as e:
        await message.reply_text(f"Error: {e}")
        return

    services = data.get("data") or data  # some endpoints return data wrapper
    if not services:
        await message.reply_text("No services found.")
        return

    text_lines = []
    for svc in services:
        # show id, name, type, live state and owner/workspace id
        text_lines.append(f"{svc.get('id')}\n  • name: {svc.get('name')}\n  • type: {svc.get('type')}\n  • state: {svc.get('serviceDetails', {}).get('liveState') or svc.get('state')}\n")
    await message.reply_text("\n\n".join(text_lines))

@app.on_message(filters.command("restart") & filters.private)
@admin_only
async def restart(service_client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: /restart <service_id>")
        return
    service_id = message.command[1].strip()
    api_key = get_api_key_for_chat(message.chat.id)
    if not api_key:
        await message.reply_text("No Render API key stored.")
        return
    try:
        await render_request("POST", f"/services/{service_id}/restart", api_key)
        await message.reply_text(f"Restart requested for service `{service_id}`.")
    except Exception as e:
        await message.reply_text(f"Error: {e}")

@app.on_message(filters.command("scale") & filters.private)
@admin_only
async def scale(service_client, message: Message):
    if len(message.command) < 3:
        await message.reply_text("Usage: /scale <service_id> <instance_count>")
        return
    service_id = message.command[1].strip()
    try:
        count = int(message.command[2])
    except ValueError:
        await message.reply_text("instance_count must be an integer.")
        return
    api_key = get_api_key_for_chat(message.chat.id)
    if not api_key:
        await message.reply_text("No Render API key stored.")
        return
    try:
        payload = {"instances": count}
        await render_request("POST", f"/services/{service_id}/scale", api_key, json=payload)
        await message.reply_text(f"Scale request sent: `{service_id}` -> {count} instances.")
    except Exception as e:
        await message.reply_text(f"Error: {e}")

@app.on_message(filters.command("env_set") & filters.private)
@admin_only
async def env_set(client, message: Message):
    if len(message.command) < 3:
        await message.reply_text("Usage: /env_set <service_id> KEY=VALUE[,KEY2=VALUE2,...]\nThis will REPLACE all env vars for the service with the ones you provide.")
        return
    service_id = message.command[1].strip()
    kvs_raw = " ".join(message.command[2:])
    # simple parse KEY=VALUE,KEY2=VALUE2...
    pairs = []
    for part in kvs_raw.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        pairs.append({"name": k.strip(), "value": v.strip(), "type": "env"})
    if not pairs:
        await message.reply_text("No valid KEY=VALUE pairs found.")
        return
    api_key = get_api_key_for_chat(message.chat.id)
    if not api_key:
        await message.reply_text("No Render API key stored.")
        return
    try:
        # Render expects a list of env var objects. This endpoint *replaces* all env vars.
        await render_request("PUT", f"/services/{service_id}/env-vars", api_key, json=pairs)
        await message.reply_text("Environment variables updated (replacement).")
    except Exception as e:
        await message.reply_text(f"Error: {e}")

# Error handler (basic)
@app.on_message(filters.private & filters.command)
async def fallback(client, message):
    # any unknown command
    known = {"start","setkey","getkey","list_services","restart","scale","env_set"}
    cmd = message.command[0].lstrip("/")
    if cmd not in known:
        await message.reply_text("Unknown command. Use /start to see available commands.")

if __name__ == "__main__":
    print("Starting Render management bot...")
    app.run()
