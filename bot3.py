#!/usr/bin/env python3
"""
Render-management Telegram bot using Pyrofork.

Requires:
    pip install pyrofork aiohttp python-dotenv
Set env:
    BOT_TOKEN, API_ID, API_HASH
"""

import os
import json
import asyncio
import traceback
from typing import Dict, Optional

import aiohttp
from Pyrogram import Client, filters  # Pyrofork usage (Pyrogram fork)
from pyrofork.types import Message  # type hints (if available)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")  # required
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise SystemExit("Please set BOT_TOKEN, API_ID and API_HASH environment variables.")

# file to persist per-user render API keys (very simple)
SESSIONS_FILE = "render_sessions.json"
RENDER_API_BASE = "https://api.render.com/v1"

# in-memory cache loaded from file
sessions: Dict[str, Dict] = {}

def load_sessions():
    global sessions
    try:
        if os.path.exists(SESSIONS_FILE):
            with open(SESSIONS_FILE, "r") as f:
                sessions = json.load(f)
        else:
            sessions = {}
    except Exception:
        print("Failed loading sessions file:")
        traceback.print_exc()
        sessions = {}

def save_sessions():
    try:
        with open(SESSIONS_FILE, "w") as f:
            json.dump(sessions, f, indent=2)
    except Exception:
        print("Failed saving sessions file:")
        traceback.print_exc()

def mask_key(key: str) -> str:
    if not key:
        return "<none>"
    if len(key) <= 8:
        return key[:2] + "..." + key[-2:]
    return key[:4] + "..." + key[-4:]

# small helper for Render API requests using aiohttp
async def render_request(method: str, path: str, api_key: str, **kwargs):
    """
    Make an authenticated request to Render API. Raises on non-2xx.
    Returns JSON-decoded response body or None if empty.
    """
    url = f"{RENDER_API_BASE}{path}"
    headers = kwargs.pop("headers", {})
    headers.update({
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })

    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, headers=headers, **kwargs) as resp:
                text = await resp.text()
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = text
                if 200 <= resp.status < 300:
                    return data
                else:
                    raise RuntimeError(f"Render API error {resp.status}: {text}")
    except Exception:
        # bubble up with full traceback for logging at caller
        raise

# ---------------------------------------------------------
# Pyrofork bot setup
app = Client(
    name="render_mgmt_bot",
    bot_token=BOT_TOKEN,
    api_id=int(API_ID),
    api_hash=API_HASH,
)

# load persisted sessions at startup
load_sessions()

# helper to get user's stored API key
def get_user_key(user_id: int) -> Optional[str]:
    return sessions.get(str(user_id), {}).get("api_key")

def set_user_key(user_id: int, api_key: str):
    sessions[str(user_id)] = {"api_key": api_key}
    save_sessions()

def del_user_key(user_id: int):
    sessions.pop(str(user_id), None)
    save_sessions()

# ----------------- Command Handlers ---------------------

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    await message.reply_text(
        "Hi — I'm a Render-management bot.\n"
        "Commands:\n"
        "/login <api_key>\n"
        "/view\n"
        "/logout\n"
        "/acc_info\n"
        "/app\n"
        "/del <app_name>\n"
        "/restart <app_name>\n"
        "/env_vars <app_name>\n"
        "/env_set <app_name> KEY VALUE\n"
        "/env_del <app_name> KEY\n"
    )

@app.on_message(filters.command("login") & filters.private)
async def login_handler(client: Client, message: Message):
    try:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text("Usage: /login <api_key>")

        api_key = args[1].strip()
        # quick validation: try to get the authenticated user
        try:
            user_info = await render_request("GET", "/users", api_key)
        except Exception as e:
            await message.reply_text("Failed to validate API key: " + str(e))
            return

        set_user_key(message.from_user.id, api_key)
        # user_info often contains email/name in the response
        pretty = json.dumps(user_info, indent=2) if isinstance(user_info, dict) else str(user_info)
        await message.reply_text(f"Logged in. Associated user info:\n<pre>{pretty}</pre>", parse_mode="html")
    except Exception:
        tb = traceback.format_exc()
        await message.reply_text(f"Error in /login:\n<pre>{tb}</pre>", parse_mode="html")

@app.on_message(filters.command("view") & filters.private)
async def view_handler(client: Client, message: Message):
    try:
        key = get_user_key(message.from_user.id)
        await message.reply_text(f"API key: {mask_key(key)}")
    except Exception:
        tb = traceback.format_exc()
        await message.reply_text(f"Error in /view:\n<pre>{tb}</pre>", parse_mode="html")

@app.on_message(filters.command("logout") & filters.private)
async def logout_handler(client: Client, message: Message):
    try:
        del_user_key(message.from_user.id)
        await message.reply_text("Logged out and removed stored API key.")
    except Exception:
        tb = traceback.format_exc()
        await message.reply_text(f"Error in /logout:\n<pre>{tb}</pre>", parse_mode="html")

@app.on_message(filters.command("acc_info") & filters.private)
async def acc_info_handler(client: Client, message: Message):
    try:
        api_key = get_user_key(message.from_user.id)
        if not api_key:
            return await message.reply_text("No Render API key stored. Use /login <api_key> first.")
        user = await render_request("GET", "/users", api_key)
        # Render returns user object with name/email fields (if available)
        pretty = json.dumps(user, indent=2)
        await message.reply_text(f"Authenticated user info:\n<pre>{pretty}</pre>", parse_mode="html")
    except Exception:
        tb = traceback.format_exc()
        await message.reply_text(f"Error in /acc_info:\n<pre>{tb}</pre>", parse_mode="html")

@app.on_message(filters.command("app") & filters.private)
async def list_apps_handler(client: Client, message: Message):
    try:
        api_key = get_user_key(message.from_user.id)
        if not api_key:
            return await message.reply_text("No Render API key stored. Use /login <api_key> first.")
        services = await render_request("GET", "/services?limit=200", api_key)
        # services is usually a list of service objects
        if not services:
            return await message.reply_text("No services found.")
        lines = []
        for s in services:
            # pick id and name and type and state
            sid = s.get("id") or s.get("serviceId") or s.get("service_id")
            name = s.get("name") or s.get("serviceName") or "<unnamed>"
            s_type = s.get("serviceType") or s.get("type") or ""
            state = s.get("state") or s.get("status") or ""
            lines.append(f"{name}  —  id: {sid}  ({s_type})  [{state}]")
        text = "Services:\n" + "\n".join(lines)
        # if very long, send as file
        if len(text) > 4000:
            await message.reply_document(document=bytes(text, "utf-8"), file_name="services.txt")
        else:
            await message.reply_text(text)
    except Exception:
        tb = traceback.format_exc()
        await message.reply_text(f"Error in /app:\n<pre>{tb}</pre>", parse_mode="html")

# helper: find service by name (exact or case-insensitive)
async def find_service_by_name(api_key: str, name: str):
    services = await render_request("GET", "/services?limit=500", api_key)
    if not isinstance(services, list):
        raise RuntimeError("Unexpected services response")
    name_lower = name.lower()
    # exact match first, else startswith, else contains
    for s in services:
        if (s.get("name") or "").lower() == name_lower:
            return s
    for s in services:
        if (s.get("name") or "").lower().startswith(name_lower):
            return s
    for s in services:
        if name_lower in (s.get("name") or "").lower():
            return s
    return None

@app.on_message(filters.command("del") & filters.private)
async def delete_app_handler(client: Client, message: Message):
    try:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text("Usage: /del <app_name>")
        app_name = args[1].strip()
        api_key = get_user_key(message.from_user.id)
        if not api_key:
            return await message.reply_text("No Render API key stored. Use /login <api_key> first.")
        svc = await find_service_by_name(api_key, app_name)
        if not svc:
            return await message.reply_text("Service not found.")
        sid = svc["id"]
        await render_request("DELETE", f"/services/{sid}", api_key)
        await message.reply_text(f"Deleted service {svc.get('name')} (id: {sid}).")
    except Exception:
        tb = traceback.format_exc()
        await message.reply_text(f"Error in /del:\n<pre>{tb}</pre>", parse_mode="html")

@app.on_message(filters.command("restart") & filters.private)
async def restart_handler(client: Client, message: Message):
    try:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text("Usage: /restart <app_name>")
        app_name = args[1].strip()
        api_key = get_user_key(message.from_user.id)
        if not api_key:
            return await message.reply_text("No Render API key stored. Use /login <api_key> first.")
        svc = await find_service_by_name(api_key, app_name)
        if not svc:
            return await message.reply_text("Service not found.")
        sid = svc["id"]
        await render_request("POST", f"/services/{sid}/restart", api_key)
        await message.reply_text(f"Restart triggered for {svc.get('name')} (id: {sid}).")
    except Exception:
        tb = traceback.format_exc()
        await message.reply_text(f"Error in /restart:\n<pre>{tb}</pre>", parse_mode="html")

@app.on_message(filters.command("env_vars") & filters.private)
async def env_vars_handler(client: Client, message: Message):
    try:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text("Usage: /env_vars <app_name>")
        app_name = args[1].strip()
        api_key = get_user_key(message.from_user.id)
        if not api_key:
            return await message.reply_text("No Render API key stored. Use /login <api_key> first.")
        svc = await find_service_by_name(api_key, app_name)
        if not svc:
            return await message.reply_text("Service not found.")
        sid = svc["id"]
        envs = await render_request("GET", f"/services/{sid}/env-vars", api_key)
        pretty = json.dumps(envs, indent=2)
        await message.reply_text(f"Env vars for {svc.get('name')}:\n<pre>{pretty}</pre>", parse_mode="html")
    except Exception:
        tb = traceback.format_exc()
        await message.reply_text(f"Error in /env_vars:\n<pre>{tb}</pre>", parse_mode="html")

@app.on_message(filters.command("env_set") & filters.private)
async def env_set_handler(client: Client, message: Message):
    try:
        parts = message.text.split(maxsplit=3)
        if len(parts) < 4:
            return await message.reply_text("Usage: /env_set <app_name> KEY VALUE")
        _, app_name, key, value = parts
        api_key = get_user_key(message.from_user.id)
        if not api_key:
            return await message.reply_text("No Render API key stored. Use /login <api_key> first.")
        svc = await find_service_by_name(api_key, app_name)
        if not svc:
            return await message.reply_text("Service not found.")
        sid = svc["id"]
        # PUT to /services/{serviceId}/env-vars/{envVarKey}
        body = {"value": value}
        await render_request("PUT", f"/services/{sid}/env-vars/{key}", api_key, json=body)
        await message.reply_text(f"Set env var {key} for {svc.get('name')}. Note: changes won't auto-deploy. Call /restart or trigger a deploy.")
    except Exception:
        tb = traceback.format_exc()
        await message.reply_text(f"Error in /env_set:\n<pre>{tb}</pre>", parse_mode="html")

@app.on_message(filters.command("env_del") & filters.private)
async def env_del_handler(client: Client, message: Message):
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            return await message.reply_text("Usage: /env_del <app_name> KEY")
        _, app_name, key = parts
        api_key = get_user_key(message.from_user.id)
        if not api_key:
            return await message.reply_text("No Render API key stored. Use /login <api_key> first.")
        svc = await find_service_by_name(api_key, app_name)
        if not svc:
            return await message.reply_text("Service not found.")
        sid = svc["id"]
        await render_request("DELETE", f"/services/{sid}/env-vars/{key}", api_key)
        await message.reply_text(f"Deleted env var {key} for {svc.get('name')}.")
    except Exception:
        tb = traceback.format_exc()
        await message.reply_text(f"Error in /env_del:\n<pre>{tb}</pre>", parse_mode="html")

# global exception handler (for safety)
@app.on_message(filters.command(["help", "commands"]) & filters.private)
async def help_handler(client: Client, message: Message):
    await start_handler(client, message)

# run bot
if __name__ == "__main__":
    try:
        print("Starting Pyrofork Render-management bot...")
        app.run()
    except Exception:
        traceback.print_exc()
