#!/usr/bin/env python3
# bot.py — cleaned, safer, with traceback
import os
import json
import sqlite3
import textwrap
import traceback
from typing import Dict, Any, Optional, List, Tuple

import requests
from pyrogram import Client, filters, enums
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, Message, ForceReply
)

# ---------- Config ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # required
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
DB_PATH = os.getenv("DB_PATH", "render_manager.db")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")  # optional (Fernet key)
BASE = os.getenv("RENDER_API_BASE", "https://api.render.com/v1")

# ---------- Optional encryption for API keys ----------
try:
    from cryptography.fernet import Fernet, InvalidToken

    FERNET = Fernet(ENCRYPTION_KEY) if ENCRYPTION_KEY else None
except Exception:
    FERNET = None

# ---------- DB ----------
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()
cur.execute(
    """
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  api_key TEXT NOT NULL,
  workspace_id TEXT DEFAULT NULL
)
"""
)
cur.execute(
    """
CREATE TABLE IF NOT EXISTS states (
  user_id INTEGER PRIMARY KEY,
  action TEXT,
  data TEXT
)
"""
)
conn.commit()


def enc(s: str) -> str:
    if FERNET:
        return FERNET.encrypt(s.encode()).decode()
    return s


def dec(s: str) -> str:
    if FERNET:
        try:
            return FERNET.decrypt(s.encode()).decode()
        except InvalidToken:
            return ""
    return s


def set_api_key(user_id: int, key: str):
    e = enc(key)
    cur.execute(
        "INSERT INTO users(user_id, api_key) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET api_key=excluded.api_key",
        (user_id, e),
    )
    conn.commit()


def get_api_key(user_id: int) -> Optional[str]:
    row = cur.execute("SELECT api_key FROM users WHERE user_id=?", (user_id,)).fetchone()
    return dec(row[0]) if row else None


def set_workspace(user_id: int, ws_id: Optional[str]):
    cur.execute("UPDATE users SET workspace_id=? WHERE user_id=?", (ws_id, user_id))
    conn.commit()


def get_workspace(user_id: int) -> Optional[str]:
    row = cur.execute("SELECT workspace_id FROM users WHERE user_id=?", (user_id,)).fetchone()
    return row[0] if row else None


def set_state(user_id: int, action: Optional[str], data: Dict[str, Any]):
    cur.execute(
        "INSERT INTO states(user_id, action, data) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET action=excluded.action, data=excluded.data",
        (user_id, action, json.dumps(data)),
    )
    conn.commit()


def get_state(user_id: int) -> Tuple[Optional[str], Dict[str, Any]]:
    row = cur.execute("SELECT action, data FROM states WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return None, {}
    return row[0], (json.loads(row[1]) if row[1] else {})


def clear_state(user_id: int):
    cur.execute("DELETE FROM states WHERE user_id=?", (user_id,))
    conn.commit()


# ---------- Render API helper ----------
class RenderError(RuntimeError):
    def __init__(self, status: int, body: Any, text: str = ""):
        super().__init__(f"{status} {body or text}")
        self.status = status
        self.body = body
        self.text = text


class Render:
    def __init__(self, key: str):
        self._key = key
        self.h = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _r(self, method: str, path: str, **kw):
        url = f"{BASE}{path}"
        try:
            r = requests.request(method, url, headers=self.h, timeout=30, **kw)
        except requests.RequestException as re:
            raise RenderError(-1, {"message": str(re)}, text=str(re))
        content = r.content or b""
        text = ""
        try:
            text = content.decode("utf-8", errors="ignore")
        except Exception:
            text = str(content)

        if r.status_code >= 400:
            # Try to parse JSON error payload safely
            parsed = None
            try:
                parsed = r.json()
            except Exception:
                parsed = {"message": text or r.reason}
            raise RenderError(r.status_code, parsed, text=text)
        if content:
            try:
                return r.json()
            except Exception:
                # not JSON — return raw text
                return text
        return {}

    # Identity & Workspaces
    def me(self):
        return self._r("GET", "/users/me")

    def workspaces(self):
        return self._r("GET", "/workspaces")

    # Services
    def list_services(self, limit=20, cursor=None):
        q = f"?limit={limit}" + (f"&cursor={cursor}" if cursor else "")
        return self._r("GET", f"/services{q}")

    def get_service(self, service_id: str):
        return self._r("GET", f"/services/{service_id}")

    def create_service(self, payload: Dict[str, Any]):
        return self._r("POST", "/services", json=payload)

    def delete_service(self, service_id: str):
        return self._r("DELETE", f"/services/{service_id}")

    # Actions
    def trigger_deploy(self, service_id: str):
        return self._r("POST", f"/services/{service_id}/deploys")

    def restart(self, service_id: str):
        return self._r("POST", f"/services/{service_id}/restart")

    def suspend(self, service_id: str):
        return self._r("POST", f"/services/{service_id}/suspend")

    def resume(self, service_id: str):
        return self._r("POST", f"/services/{service_id}/resume")

    # Env vars
    def list_env_vars(self, service_id: str):
        return self._r("GET", f"/services/{service_id}/env-vars")

    def put_env_vars(self, service_id: str, envs: List[Dict[str, str]]):
        # replaces or inserts keys passed
        return self._r("PUT", f"/services/{service_id}/env-vars", json=envs)

    # Logs
    def recent_logs(self, service_id: str, limit=100):
        return self._r("GET", f"/logs?serviceId={service_id}&limit={limit}")


# ---------- Utilities ----------
def sanitize_text(text: str, secret: Optional[str]) -> str:
    """Sanitize any occurrence of secret in text."""
    if not secret:
        return text
    return text.replace(secret, "<REDACTED_API_KEY>")


def format_render_error(err: RenderError) -> str:
    """Create a friendly short message for RenderError."""
    msg = f"HTTP {err.status}"
    body = err.body or {}
    # try common keys
    if isinstance(body, dict):
        reason = body.get("message") or body.get("error") or body.get("detail") or str(body)
    else:
        reason = str(body)
    return f"{msg}: {reason}"


# ---------- UI helpers ----------
def main_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👤 Account", callback_data="acct"),
                InlineKeyboardButton("🧰 Workspaces", callback_data="workspaces"),
            ],
            [
                InlineKeyboardButton("📋 List Services", callback_data="svc:list"),
                InlineKeyboardButton("🚀 Deploy from Git", callback_data="create"),
            ],
        ]
    )


def service_actions(svc: Dict[str, Any]):
    sid = svc.get("id") or svc.get("serviceId") or svc.get("service_id")
    rows = [
        [
            InlineKeyboardButton("🔁 Trigger Deploy", callback_data=f"svc:deploy:{sid}"),
            InlineKeyboardButton("♻️ Restart", callback_data=f"svc:restart:{sid}"),
        ],
        [
            InlineKeyboardButton("⏸ Suspend", callback_data=f"svc:suspend:{sid}"),
            InlineKeyboardButton("▶️ Resume", callback_data=f"svc:resume:{sid}"),
        ],
        [
            InlineKeyboardButton("🧪 Logs", callback_data=f"svc:logs:{sid}"),
            InlineKeyboardButton("🌐 Env Vars", callback_data=f"svc:env:{sid}"),
        ],
        [InlineKeyboardButton("🗑 Delete", callback_data=f"svc:delete:{sid}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="svc:list")],
    ]
    return InlineKeyboardMarkup(rows)


def type_picker():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🌐 Web", callback_data="new:type:web_service"),
                InlineKeyboardButton("🛡 Private", callback_data="new:type:private_service"),
            ],
            [
                InlineKeyboardButton("⚙️ Worker", callback_data="new:type:background_worker"),
                InlineKeyboardButton("⏰ Cron", callback_data="new:type:cron_job"),
            ],
            [InlineKeyboardButton("📄 Static Site", callback_data="new:type:static_site")],
            [InlineKeyboardButton("⬅️ Cancel", callback_data="cancel")],
        ]
    )


def workspace_kb(workspaces: List[Dict[str, Any]], back_to: str):
    rows = []
    for w in workspaces:
        # support different payload shapes
        wid = w.get("id") or w.get("workspaceId") or w.get("ownerId") or str(w)
        name = w.get("name") or w.get("slug") or wid
        rows.append([InlineKeyboardButton(f"{name} ({wid})", callback_data=f"ws:set:{wid}|{back_to}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=back_to)])
    return InlineKeyboardMarkup(rows)


def ensure_key(user_id: int) -> Optional[str]:
    return get_api_key(user_id)


# ---------- Bot ----------
app = Client("render-manager-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

WELCOME = (
    "Welcome to *Render Manager*.\n\n"
    "• First, save your Render API key:\n"
    "`/login <RENDER_API_KEY>`\n\n"
    "Your key is stored per-user in a local DB (optionally Fernet-encrypted)."
)


@app.on_message(filters.command("start"))
async def start(_, m: Message):
    await m.reply_text(WELCOME, reply_markup=main_menu(), parse_mode=enums.ParseMode.MARKDOWN)


@app.on_message(filters.command("login") & filters.private)
async def login_cmd(_, m: Message):
    parts = m.text.strip().split(maxsplit=1)
    if len(parts) != 2:
        await m.reply_text("Send: `/login <RENDER_API_KEY>`", parse_mode=enums.ParseMode.MARKDOWN)
        return
    key = parts[1].strip()
    set_api_key(m.from_user.id, key)
    await m.reply_text("✅ API key saved.\nTap *Account* to verify.", parse_mode=enums.ParseMode.MARKDOWN, reply_markup=main_menu())


# ---------- Callbacks ----------
@app.on_callback_query()
async def on_cb(_, cq: CallbackQuery):
    uid = cq.from_user.id
    key = ensure_key(uid)
    if not key:
        try:
            # prefer editing message if possible
            if cq.message:
                await cq.message.edit_text("❗️No API key yet.\nSend: `/login <RENDER_API_KEY>`", parse_mode=enums.ParseMode.MARKDOWN)
            else:
                await cq.answer("No API key. Send /login <KEY>")
        finally:
            await cq.answer()
        return

    api = Render(key)
    data = cq.data or ""

    # helper to produce sanitized traceback for the user
    def make_tb_exc(e: Exception) -> str:
        tb = traceback.format_exc()
        return sanitize_text(tb, key)

    # Main items
    if data == "acct":
        try:
            me = api.me()
            ws = get_workspace(uid)
            txt = textwrap.dedent(
                f"""
            *Account*
            • Name: `{me.get('name')}`
            • Email: `{me.get('email')}`
            • ID: `{me.get('id')}`
            • Selected Workspace (ownerId for new services): `{ws or 'not set'}`
            """
            ).strip()
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🧰 Choose Workspace", callback_data="workspaces")],
                    [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
                ]
            )
            await cq.message.edit_text(txt, parse_mode=enums.ParseMode.MARKDOWN, reply_markup=kb)
        except RenderError as re:
            tb = make_tb_exc(re)
            short = format_render_error(re)
            # show a helpful short message plus the sanitized traceback for debugging
            await cq.message.edit_text(
                f"⚠️ {short}\n\nDetailed (sanitized) traceback:\n```\n{tb[-3000:]}\n```",
                parse_mode=enums.ParseMode.MARKDOWN,
                reply_markup=main_menu(),
            )
        except Exception as e:
            tb = make_tb_exc(e)
            await cq.message.edit_text(
                f"⚠️ Unexpected error: {sanitize_text(str(e), key)}\n\nTraceback:\n```\n{tb[-3000:]}\n```",
                reply_markup=main_menu(),
            )
        await cq.answer()
        return

    if data == "workspaces":
        try:
            w = api.workspaces()
            # accept different shapes: list, dict with items, dict with workspaces
            if isinstance(w, dict):
                workspaces = w.get("items") or w.get("workspaces") or w.get("data") or []
            elif isinstance(w, list):
                workspaces = w
            else:
                workspaces = []
            if not workspaces:
                await cq.message.edit_text("*No workspaces returned.*", parse_mode=enums.ParseMode.MARKDOWN, reply_markup=main_menu())
            else:
                await cq.message.edit_text(
                    "*Pick a workspace (ownerId)*", parse_mode=enums.ParseMode.MARKDOWN, reply_markup=workspace_kb(workspaces, "menu")
                )
        except RenderError as re:
            tb = sanitize_text(traceback.format_exc(), key)
            await cq.message.edit_text(
                f"⚠️ {format_render_error(re)}\n\nTraceback (sanitized):\n```\n{tb[-3000:]}\n```",
                reply_markup=main_menu(),
            )
        except Exception as e:
            tb = sanitize_text(traceback.format_exc(), key)
            await cq.message.edit_text(f"⚠️ {sanitize_text(str(e), key)}\n```\n{tb[-3000:]}\n```", reply_markup=main_menu())
        await cq.answer()
        return

    if data == "menu":
        await cq.message.edit_text("Main menu:", reply_markup=main_menu())
        await cq.answer()
        return

    # Workspace set
    if data.startswith("ws:set:"):
        try:
            _, _, rest = data.partition("ws:set:")
            ws_id, back_to = rest.split("|", 1)
            set_workspace(uid, ws_id)
            await cq.message.edit_text(
                f"✅ Workspace set to `{ws_id}`.",
                parse_mode=enums.ParseMode.MARKDOWN,
                reply_markup=main_menu() if back_to == "menu" else type_picker(),
            )
        except Exception as e:
            tb = sanitize_text(traceback.format_exc(), key)
            await cq.message.edit_text(f"⚠️ {sanitize_text(str(e), key)}\n```\n{tb[-2000:]}\n```", reply_markup=main_menu())
        await cq.answer()
        return

    # List services
    if data == "svc:list":
        try:
            lst = api.list_services(limit=50)
            # Accept list or dict shapes
            services = []
            if isinstance(lst, dict):
                # common keys
                services = lst.get("items") or lst.get("services") or lst.get("data") or []
            elif isinstance(lst, list):
                services = lst
            else:
                services = []

            rows = []
            if not services:
                rows.append([InlineKeyboardButton("⬅️ Menu", callback_data="menu")])
                await cq.message.edit_text("*No services found.*", parse_mode=enums.ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))
            else:
                for s in services:
                    name = s.get("name") or s.get("serviceName") or s.get("id")
                    stype = s.get("type") or s.get("serviceType") or ""
                    region = s.get("region") or s.get("regionId") or ""
                    sid = s.get("id") or s.get("serviceId") or s.get("service_id")
                    label = f"{name} · {stype} · {region}"
                    rows.append([InlineKeyboardButton(label, callback_data=f"svc:open:{sid}")])
                rows.append([InlineKeyboardButton("⬅️ Menu", callback_data="menu")])
                await cq.message.edit_text("*Your services:*", parse_mode=enums.ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))
        except RenderError as re:
            tb = sanitize_text(traceback.format_exc(), key)
            await cq.message.edit_text(
                f"⚠️ {format_render_error(re)}\n\nTraceback (sanitized):\n```\n{tb[-3000:]}\n```",
                reply_markup=main_menu(),
            )
        except Exception as e:
            tb = sanitize_text(traceback.format_exc(), key)
            await cq.message.edit_text(f"⚠️ {sanitize_text(str(e), key)}\n```\n{tb[-3000:]}\n```", reply_markup=main_menu())
        await cq.answer()
        return

    # Open a service
    if data.startswith("svc:open:"):
        sid = data.split(":", 2)[2]
        try:
            s = api.get_service(sid)
            txt = textwrap.dedent(
                f"""
            *{s.get('name')}*
            • id: `{s.get('id')}`
            • type: `{s.get('type')}`
            • region: `{s.get('region')}`
            • repo/branch: `{s.get('repo')}` @ `{s.get('branch')}`
            • plan: `{s.get('plan')}`
            • autoDeploy: `{s.get('autoDeploy')}`
            • url: {s.get('url','—')}
            """
            ).strip()
            await cq.message.edit_text(txt, parse_mode=enums.ParseMode.MARKDOWN, reply_markup=service_actions(s))
        except RenderError as re:
            tb = sanitize_text(traceback.format_exc(), key)
            await cq.message.edit_text(
                f"⚠️ {format_render_error(re)}\n\nTraceback:\n```\n{tb[-3000:]}\n```", reply_markup=main_menu()
            )
        except Exception as e:
            tb = sanitize_text(traceback.format_exc(), key)
            await cq.message.edit_text(f"⚠️ {sanitize_text(str(e), key)}\n```\n{tb[-3000:]}\n```", reply_markup=main_menu())
        await cq.answer()
        return

  # Service actions (deploy/restart/suspend/resume)
    if data.startswith("svc:deploy:"):
        sid = data.split(":", 2)[2]
        try:
            api.trigger_deploy(sid)
            await cq.answer("Deploy triggered ✅", show_alert=False)
        except RenderError as re:
            await cq.answer(format_render_error(re), show_alert=True)
        except Exception as e:
            await cq.answer(sanitize_text(str(e), key), show_alert=True)
        return

    if data.startswith("svc:restart:"):
        sid = data.split(":", 2)[2]
        try:
            api.restart(sid)
            await cq.answer("Restart requested ♻️", show_alert=False)
        except RenderError as re:
            await cq.answer(format_render_error(re), show_alert=True)
        except Exception as e:
            await cq.answer(sanitize_text(str(e), key), show_alert=True)
        return

    if data.startswith("svc:suspend:"):
        sid = data.split(":", 2)[2]
        try:
            api.suspend(sid)
            await cq.answer("Service suspended ⏸", show_alert=False)
        except RenderError as re:
            await cq.answer(format_render_error(re), show_alert=True)
        except Exception as e:
            await cq.answer(sanitize_text(str(e), key), show_alert=True)
        return

    if data.startswith("svc:resume:"):
        sid = data.split(":", 2)[2]
        try:
            api.resume(sid)
            await cq.answer("Service resumed ▶️", show_alert=False)
        except RenderError as re:
            await cq.answer(format_render_error(re), show_alert=True)
        except Exception as e:
            await cq.answer(sanitize_text(str(e), key), show_alert=True)
        return

    if data.startswith("svc:logs:"):
        sid = data.split(":", 2)[2]
        try:
            logs = api.recent_logs(sid, limit=200)
            lines = []
            if isinstance(logs, list):
                lines = [l.get("message", "") if isinstance(l, dict) else str(l) for l in logs]
            elif isinstance(logs, dict):
                items = logs.get("items") or logs.get("data") or []
                lines = [l.get("message", "") if isinstance(l, dict) else str(l) for l in items]

            chunk = "\n".join(lines[-50:]) or "No recent logs."

            await cq.message.edit_text(
                f"```\n{chunk[-3500:]}\n```",
                parse_mode=enums.ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Service", callback_data=f"svc:open:{sid}")]]
                )
            )
        except RenderError as re:
            await cq.answer(format_render_error(re), show_alert=True)
        except Exception as e:
            await cq.answer(sanitize_text(str(e), key), show_alert=True)
        return

    if data.startswith("svc:env:"):
        sid = data.split(":", 2)[2]
        try:
            envs = api.list_env_vars(sid)
            env_list = []
            if isinstance(envs, list):
                env_list = envs
            elif isinstance(envs, dict):
                env_list = envs.get("items") or envs.get("data") or []

            preview = "\n".join([f"{i['key']}={i.get('value','<secret>')}" for i in env_list][:15]) or "No env vars."
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("➕ Upsert (send K=V lines)", callback_data=f"env:put:{sid}")],
                    [InlineKeyboardButton("⬅️ Service", callback_data=f"svc:open:{sid}")],
                ]
            )
            await cq.message.edit_text(
                f"*Env Vars (first 15):*\n```\n{preview}\n```",
                parse_mode=enums.ParseMode.MARKDOWN,
                reply_markup=kb
            )
        except RenderError as re:
            await cq.answer(format_render_error(re), show_alert=True)
        except Exception as e:
            await cq.answer(sanitize_text(str(e), key), show_alert=True)
        return

    if data.startswith("env:put:"):
        sid = data.split(":", 2)[2]
        set_state(uid, "env-put", {"sid": sid})
        await cq.message.reply_text(
            "Send env lines like:\n```\nKEY1=value1\nKEY2=value2\n```",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=ForceReply(selective=True)
        )
        await cq.answer()
        return

    if data.startswith("svc:delete:"):
        sid = data.split(":", 2)[2]
        set_state(uid, "confirm-delete", {"sid": sid})
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("❗️Confirm Delete", callback_data=f"svc:confirmdelete:{sid}")],
                [InlineKeyboardButton("Cancel", callback_data=f"svc:open:{sid}")],
            ]
        )
        await cq.message.edit_text("Are you sure?", reply_markup=kb)
        await cq.answer()
        return

    if data.startswith("svc:confirmdelete:"):
        sid = data.split(":", 2)[2]
        try:
            api.delete_service(sid)
            await cq.message.edit_text("🗑 Deleted.", reply_markup=main_menu())
            await cq.answer("Deleted")
        except RenderError as re:
            await cq.answer(format_render_error(re), show_alert=True)
        except Exception as e:
            await cq.answer(sanitize_text(str(e), key), show_alert=True)
        return

    # Create service flow
    if data == "create":
        await cq.message.edit_text(
            "*Choose service type*:",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=type_picker()
        )
        await cq.answer()
        return

    if data.startswith("new:type:"):
        svc_type = data.split(":", 2)[2]
        payload = {"type": svc_type}
        ws = get_workspace(uid)

        if not ws:
            try:
                w = api.workspaces()
                if isinstance(w, dict):
                    workspaces = w.get("items") or w.get("workspaces") or w.get("data") or []
                elif isinstance(w, list):
                    workspaces = w
                else:
                    workspaces = []

                await cq.message.edit_text(
                    "*Pick a workspace first (ownerId)*",
                    parse_mode=enums.ParseMode.MARKDOWN,
                    reply_markup=workspace_kb(workspaces, "create")
                )
            except Exception as e:
                tb = sanitize_text(traceback.format_exc(), key)
                await cq.message.edit_text(
                    f"⚠️ {sanitize_text(str(e), key)}\n```\n{tb[-3000:]}\n```",
                    reply_markup=main_menu()
                )
            await cq.answer()
            return

        payload["ownerId"] = ws
        set_state(uid, "new-name", payload)

        await cq.message.reply_text(
            "Enter *service name*:",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=ForceReply(selective=True)
        )
        await cq.answer()
        return

    if data == "cancel":
        clear_state(uid)
        await cq.message.edit_text("Cancelled.", reply_markup=main_menu())
        await cq.answer()
        return

    await cq.answer()


# ---------- Text replies used in multi-step flows ----------
@app.on_message(filters.private & ~filters.command(["start", "login"]))
async def on_text(_, m: Message):
    uid = m.from_user.id
    key = get_api_key(uid)
    if not key:
        return

    api = Render(key)
    action, data = get_state(uid)

    def sanitize_exc_msg(e: Exception) -> str:
        tb = sanitize_text(traceback.format_exc(), key)
        return f"{sanitize_text(str(e), key)}\n\nTraceback (sanitized):\n```\n{tb[-3000:]}\n```"

    if action == "env-put":
        sid = data["sid"]
        upserts = []

        for line in (m.text or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                upserts.append({"key": k.strip(), "value": v.strip()})

        try:
            if upserts:
                api.put_env_vars(sid, upserts)
                await m.reply_text(f"✅ Upserted {len(upserts)} vars.")
            else:
                await m.reply_text("No KEY=VALUE pairs found.")
        except RenderError as re:
            await m.reply_text(
                f"⚠️ {format_render_error(re)}\n\nTraceback:\n```\n{sanitize_text(traceback.format_exc(), key)[-3000:]}\n```"
            )
        except Exception as e:
            await m.reply_text(sanitize_exc_msg(e))

        clear_state(uid)
        return

    if action == "new-name":
        data["name"] = m.text.strip()
        set_state(uid, "new-repo", data)

        await m.reply_text(
            "Send *Git repo URL* (e.g. `https://github.com/owner/repo`):",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=ForceReply(selective=True)
        )
        return

    if action == "new-repo":
        data["repo"] = m.text.strip()
        set_state(uid, "new-branch", data)

        await m.reply_text(
            "Branch name (e.g. `main`):",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=ForceReply(selective=True)
        )
        return

    if action == "new-branch":
        data["branch"] = m.text.strip()
        t = data["type"]

        if t == "static_site":
            set_state(uid, "new-static-build", data)
            await m.reply_text("Build command:", reply_markup=ForceReply(selective=True))
            return
        else:
            set_state(uid, "new-runtime", data)
            await m.reply_text(
                "Runtime env (`docker`, `node`, `python`, etc.):",
                reply_markup=ForceReply(selective=True)
            )
            return

    if action == "new-static-build":
        data["buildCommand"] = m.text.strip()
        set_state(uid, "new-publish", data)

        await m.reply_text("Publish path (e.g. `dist`):", reply_markup=ForceReply(selective=True))
        return

    if action == "new-publish":
        data["publishPath"] = m.text.strip()
        set_state(uid, "new-plan", data)

        await m.reply_text("Plan (send `starter` if unsure):", reply_markup=ForceReply(selective=True))
        return

    if action == "new-runtime":
        env = m.text.strip()
        data["env"] = env

        if env == "docker":
            set_state(uid, "new-docker-cmd", data)
            await m.reply_text("Docker command (or `-`):", reply_markup=ForceReply(selective=True))
        else:
            set_state(uid, "new-build", data)
            await m.reply_text("Build command (or `-`):", reply_markup=ForceReply(selective=True))
        return

    if action == "new-docker-cmd":
        v = m.text.strip()
        if v != "-":
            data["dockerCommand"] = v

        set_state(uid, "new-plan", data)
        await m.reply_text("Plan:", reply_markup=ForceReply(selective=True))
        return

    if action == "new-build":
        v = m.text.strip()
        if v != "-":
            data["buildCommand"] = v

        set_state(uid, "new-start", data)
        await m.reply_text("Start command (or `-`):", reply_markup=ForceReply(selective=True))
        return

    if action == "new-start":
        v = m.text.strip()
        if v != "-":
            data["startCommand"] = v

        set_state(uid, "new-plan", data)
        await m.reply_text("Plan:", reply_markup=ForceReply(selective=True))
        return

    if action == "new-plan":
        data["plan"] = m.text.strip()
        set_state(uid, "new-region", data)

        await m.reply_text("Region:", reply_markup=ForceReply(selective=True))
        return

    if action == "new-region":
        data["region"] = m.text.strip()
        set_state(uid, "new-rootdir", data)

        await m.reply_text("Root directory (`-` if none):", reply_markup=ForceReply(selective=True))
        return

    if action == "new-rootdir":
        v = m.text.strip()
        if v != "-":
            data["rootDir"] = v

        set_state(uid, "new-autodeploy", data)
        await m.reply_text("Auto deploy? yes/no:", reply_markup=ForceReply(selective=True))
        return

    if action == "new-autodeploy":
        data["autoDeploy"] = m.text.strip().lower().startswith("y")

        payload = {
            "type": data["type"],
            "name": data["name"],
            "ownerId": get_workspace(uid),
            "repo": data["repo"],
            "branch": data["branch"],
            "autoDeploy": data["autoDeploy"],
            "plan": data["plan"],
            "region": data["region"],
        }

        if "rootDir" in data:
            payload["rootDir"] = data["rootDir"]

        if data["type"] == "static_site":
            payload["buildCommand"] = data.get("buildCommand")
            payload["publishPath"] = data.get("publishPath")
        else:
            payload["env"] = data["env"]

            if data["env"] == "docker":
                if data.get("dockerCommand"):
                    payload["dockerCommand"] = data["dockerCommand"]
            else:
                if data.get("buildCommand"):
                    payload["buildCommand"] = data["buildCommand"]
                if data.get("startCommand"):
                    payload["startCommand"] = data["startCommand"]

        try:
            created = Render(get_api_key(uid)).create_service(payload)
            clear_state(uid)

            msg = (
                f"✅ *Created* `{created.get('name')}`\n"
                f"• id: `{created.get('id')}`\n"
                f"• type: `{created.get('type')}`\n"
                f"• region: `{created.get('region')}`\n"
                f"• repo @ branch: `{created.get('repo')}` @ `{created.get('branch')}`"
            )

            await m.reply_text(
                msg,
                parse_mode=enums.ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🔁 Trigger First Deploy", callback_data=f"svc:deploy:{created['id']}")],
                        [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
                    ]
                )
            )
        except RenderError as re:
            await m.reply_text(
                f"⚠️ Create failed: {format_render_error(re)}\n```\n{sanitize_text(traceback.format_exc(), key)[-3000:]}\n```",
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception as e:
            await m.reply_text(sanitize_exc_msg(e))
        return


if __name__ == "__main__":
    if not BOT_TOKEN:
        print("BOT_TOKEN is required in env; exiting.")
        raise SystemExit(1)

    print("Starting bot...")
    app.run()
