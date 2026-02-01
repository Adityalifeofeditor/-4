import os
import sys
import time
import json
import logging
import traceback
import asyncio
from datetime import datetime, timedelta
from typing import List, Set, Dict, Any
from bson import ObjectId

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, UserNotParticipant, ChannelInvalid
from pyrogram.raw import functions, types
import uuid

try:
    from pymongo import MongoClient
    MONGODB_AVAILABLE = True
except ImportError:
    MONGODB_AVAILABLE = False
    print("⚠️ PyMongo not installed. Install with: pip install pymongo")

# ================= CONFIGURATION =================
API_ID = int(os.getenv("API_ID", "27169529"))
API_HASH = os.getenv("API_HASH", "5d67602a4e0bbfabe669c0febeaf63b6")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8539561305:AAGF1JDWkXt3mlSqnD7UnKNoZp7q3HtOhl0")
OWNER_ID = int(os.getenv("OWNER_ID", "6441347235"))
MONGODB_URL = os.getenv("MONGODB_URL", "mongodb+srv://adam822728:iP9ESt5vyfwDRxNB@cluster0.r82vfuz.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
ADMIN_IDS = [OWNER_ID]  # Add more admin IDs like [OWNER_ID, 123456789]

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger("OldUsersBot")

# ================= GLOBAL VARIABLES =================
BOT_START_TIME = time.time()
users_fetch_tasks: Dict[int, Dict] = {}
user_settings_cache: Dict[int, Dict] = {}
global_settings_cache: Dict[str, Any] = {}

# ================= MONGODB SETUP =================
if MONGODB_AVAILABLE:
    try:
        mongo_client = MongoClient(MONGODB_URL, connectTimeoutMS=30000, socketTimeoutMS=30000)
        db = mongo_client["old_users_bot"]
        settings_collection = db["user_settings"]
        global_settings_collection = db["global_settings"]
        
        # Create indexes
        settings_collection.create_index("user_id", unique=True)
        global_settings_collection.create_index("key", unique=True)
        
        logger.info("✅ MongoDB connected successfully")
    except Exception as e:
        logger.error(f"❌ MongoDB connection failed: {e}")
        MONGODB_AVAILABLE = False
else:
    logger.warning("⚠️ MongoDB not available, using default settings")

# ================= DEFAULT SETTINGS =================
DEFAULT_SETTINGS = {
    # PTS Settings
    "pts_max_attempts": 20000,
    "pts_delay": 0.05,
    "pts_flood_wait_multiplier": 1.0,
    
    # Channel Settings
    "channels_max_dialogs": 200,
    "channels_max_members": 100,
    "channels_delay": 0.5,
    
    # History Settings
    "history_limit": 100,
    
    # Timeout Settings
    "input_timeout": 120,
    "ask_timeout": 120,
    
    # Scan Modes
    "enable_pts": True,
    "enable_channels": True,
    "scan_mode": "balanced",  # fast, balanced, deep
    
    # Performance
    "max_scan_time_minutes": 30,
    "min_users_for_deep_scan": 100,
    
    # UI Settings
    "progress_update_interval": 100,  # Update every X PTS attempts
    "show_estimated_time": True,
}

# ================= SETTINGS MANAGEMENT =================
async def get_user_settings(user_id: int) -> Dict[str, Any]:
    """Get user settings from cache or database"""
    if user_id in user_settings_cache:
        return user_settings_cache[user_id]
    
    if MONGODB_AVAILABLE:
        try:
            settings = settings_collection.find_one({"user_id": user_id})
            if settings:
                # Remove MongoDB _id and user_id from settings
                settings.pop("_id", None)
                settings.pop("user_id", None)
                user_settings_cache[user_id] = settings
                return settings
        except Exception as e:
            logger.error(f"Error getting user settings: {e}")
    
    # Return default settings if no user-specific settings found
    user_settings_cache[user_id] = DEFAULT_SETTINGS.copy()
    return DEFAULT_SETTINGS.copy()

async def save_user_settings(user_id: int, settings: Dict[str, Any]) -> bool:
    """Save user settings to database"""
    try:
        if MONGODB_AVAILABLE:
            settings_collection.update_one(
                {"user_id": user_id},
                {"$set": {**settings, "user_id": user_id, "updated_at": datetime.now()}},
                upsert=True
            )
        
        # Update cache
        user_settings_cache[user_id] = settings
        return True
    except Exception as e:
        logger.error(f"Error saving user settings: {e}")
        return False

async def reset_user_settings(user_id: int) -> bool:
    """Reset user settings to defaults"""
    try:
        if MONGODB_AVAILABLE:
            settings_collection.delete_one({"user_id": user_id})
        
        if user_id in user_settings_cache:
            del user_settings_cache[user_id]
        
        return True
    except Exception as e:
        logger.error(f"Error resetting user settings: {e}")
        return False

async def get_global_setting(key: str, default=None):
    """Get global setting from database"""
    if key in global_settings_cache:
        return global_settings_cache[key]
    
    if MONGODB_AVAILABLE:
        try:
            setting = global_settings_collection.find_one({"key": key})
            if setting:
                global_settings_cache[key] = setting.get("value", default)
                return setting.get("value", default)
        except Exception as e:
            logger.error(f"Error getting global setting {key}: {e}")
    
    return default

async def set_global_setting(key: str, value: Any) -> bool:
    """Set global setting in database"""
    try:
        if MONGODB_AVAILABLE:
            global_settings_collection.update_one(
                {"key": key},
                {"$set": {"value": value, "updated_at": datetime.now()}},
                upsert=True
            )
        
        global_settings_cache[key] = value
        return True
    except Exception as e:
        logger.error(f"Error setting global setting {key}: {e}")
        return False

# ================= CREATE BOT =================
app = Client(
    "old_users_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=20
)

# ================= UTILITY FUNCTIONS =================
def format_uptime(seconds: float) -> str:
    """Format seconds to human readable time"""
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    
    return " ".join(parts) if parts else "0s"

def format_time_elapsed(start_time: float) -> str:
    """Format elapsed time"""
    elapsed = time.time() - start_time
    return format_uptime(elapsed)

def save_users_to_file(user_ids: Set[int], filename: str = "broadcast.txt") -> str:
    """Save user IDs to a file"""
    try:
        with open(filename, "w", encoding="utf-8") as f:
            for user_id in sorted(user_ids):
                f.write(f"{user_id}\n")
        
        file_size = os.path.getsize(filename)
        return f"{filename} ({len(user_ids)} users, {file_size} bytes)"
    except Exception as e:
        logger.error(f"Error saving file: {e}")
        return ""

def format_number(num: int) -> str:
    """Format number with commas"""
    return f"{num:,}"

def validate_bot_token(token: str) -> bool:
    """Validate bot token format"""
    if not token or ":" not in token:
        return False
    
    parts = token.split(":")
    if len(parts) != 2:
        return False
    
    # Check if first part is numeric (bot ID)
    if not parts[0].isdigit():
        return False
    
    # Check if second part looks like a valid hash
    if len(parts[1]) < 30:
        return False
    
    return True

def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    return user_id in ADMIN_IDS

# ================= ADMIN SETTINGS COMMANDS =================
@app.on_message(filters.command("settings"))
async def settings_command(client: Client, message: Message):
    """Show and manage settings"""
    user_id = message.from_user.id
    
    if not is_admin(user_id):
        await message.reply("❌ **Admin only!**\n\nThis command is for administrators only.")
        return
    
    settings = await get_user_settings(user_id)
    
    # Create settings keyboard
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ PTS Settings", callback_data="settings_pts"),
            InlineKeyboardButton("📊 Channel Settings", callback_data="settings_channels")
        ],
        [
            InlineKeyboardButton("⏱ Timeout Settings", callback_data="settings_timeouts"),
            InlineKeyboardButton("🔧 Scan Modes", callback_data="settings_scan")
        ],
        [
            InlineKeyboardButton("📈 Performance", callback_data="settings_performance"),
            InlineKeyboardButton("🔄 Reset All", callback_data="settings_reset")
        ],
        [
            InlineKeyboardButton("❌ Close", callback_data="settings_close")
        ]
    ])
    
    settings_text = (
        "⚙️ **Settings Management**\n\n"
        "**Current Settings:**\n"
        f"• PTS Attempts: `{settings['pts_max_attempts']}`\n"
        f"• PTS Delay: `{settings['pts_delay']}s`\n"
        f"• Max Dialogs: `{settings['channels_max_dialogs']}`\n"
        f"• Max Members: `{settings['channels_max_members']}`\n"
        f"• Scan Mode: `{settings['scan_mode']}`\n\n"
        "Select a category to modify settings:"
    )
    
    await message.reply(settings_text, reply_markup=keyboard)

@app.on_callback_query(filters.regex(r"^settings_"))
async def settings_callback_handler(client: Client, callback_query):
    """Handle settings callback queries"""
    user_id = callback_query.from_user.id
    data = callback_query.data
    
    if not is_admin(user_id):
        await callback_query.answer("❌ Admin only!", show_alert=True)
        return
    
    settings = await get_user_settings(user_id)
    
    if data == "settings_pts":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("PTS Attempts", callback_data="edit_pts_attempts"),
                InlineKeyboardButton("PTS Delay", callback_data="edit_pts_delay")
            ],
            [
                InlineKeyboardButton("Flood Wait Multiplier", callback_data="edit_pts_flood")
            ],
            [
                InlineKeyboardButton("◀️ Back", callback_data="settings_main"),
                InlineKeyboardButton("❌ Close", callback_data="settings_close")
            ]
        ])
        
        text = (
            "⚡ **PTS Settings**\n\n"
            f"• **Max Attempts:** `{settings['pts_max_attempts']}`\n"
            f"  (Higher = more users, slower)\n\n"
            f"• **Delay:** `{settings['pts_delay']}s`\n"
            f"  (Lower = faster, higher risk of FloodWait)\n\n"
            f"• **Flood Wait Multiplier:** `{settings['pts_flood_wait_multiplier']}x`\n"
            f"  (Multiplies Telegram's FloodWait time)\n\n"
            "Select a setting to edit:"
        )
        
    elif data == "settings_channels":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Max Dialogs", callback_data="edit_channels_dialogs"),
                InlineKeyboardButton("Max Members", callback_data="edit_channels_members")
            ],
            [
                InlineKeyboardButton("Channel Delay", callback_data="edit_channels_delay")
            ],
            [
                InlineKeyboardButton("◀️ Back", callback_data="settings_main"),
                InlineKeyboardButton("❌ Close", callback_data="settings_close")
            ]
        ])
        
        text = (
            "📊 **Channel Settings**\n\n"
            f"• **Max Dialogs:** `{settings['channels_max_dialogs']}`\n"
            f"  (Number of chats to scan)\n\n"
            f"• **Max Members:** `{settings['channels_max_members']}`\n"
            f"  (Members per chat to fetch)\n\n"
            f"• **Delay:** `{settings['channels_delay']}s`\n"
            f"  (Delay between chat processing)\n\n"
            "Select a setting to edit:"
        )
        
    elif data == "settings_timeouts":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Input Timeout", callback_data="edit_input_timeout"),
                InlineKeyboardButton("Ask Timeout", callback_data="edit_ask_timeout")
            ],
            [
                InlineKeyboardButton("Max Scan Time", callback_data="edit_max_scan_time")
            ],
            [
                InlineKeyboardButton("◀️ Back", callback_data="settings_main"),
                InlineKeyboardButton("❌ Close", callback_data="settings_close")
            ]
        ])
        
        text = (
            "⏱ **Timeout Settings**\n\n"
            f"• **Input Timeout:** `{settings['input_timeout']}s`\n"
            f"  (Bot token input timeout)\n\n"
            f"• **Ask Timeout:** `{settings['ask_timeout']}s`\n"
            f"  (Response timeout for questions)\n\n"
            f"• **Max Scan Time:** `{settings['max_scan_time_minutes']}min`\n"
            f"  (Maximum scanning time)\n\n"
            "Select a setting to edit:"
        )
        
    elif data == "settings_scan":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Scan Mode", callback_data="edit_scan_mode"),
                InlineKeyboardButton("Enable PTS", callback_data="toggle_enable_pts")
            ],
            [
                InlineKeyboardButton("Enable Channels", callback_data="toggle_enable_channels")
            ],
            [
                InlineKeyboardButton("◀️ Back", callback_data="settings_main"),
                InlineKeyboardButton("❌ Close", callback_data="settings_close")
            ]
        ])
        
        text = (
            "🔧 **Scan Mode Settings**\n\n"
            f"• **Scan Mode:** `{settings['scan_mode']}`\n"
            f"  (fast/balanced/deep)\n\n"
            f"• **Enable PTS:** `{'✅' if settings['enable_pts'] else '❌'}`\n"
            f"  (Use PTS method)\n\n"
            f"• **Enable Channels:** `{'✅' if settings['enable_channels'] else '❌'}`\n"
            f"  (Use channels method)\n\n"
            "Select a setting to edit:"
        )
        
    elif data == "settings_performance":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Progress Update", callback_data="edit_progress_update"),
                InlineKeyboardButton("Min Users Deep", callback_data="edit_min_users_deep")
            ],
            [
                InlineKeyboardButton("History Limit", callback_data="edit_history_limit")
            ],
            [
                InlineKeyboardButton("◀️ Back", callback_data="settings_main"),
                InlineKeyboardButton("❌ Close", callback_data="settings_close")
            ]
        ])
        
        text = (
            "📈 **Performance Settings**\n\n"
            f"• **Progress Update:** `{settings['progress_update_interval']}` attempts\n"
            f"  (Update progress every X attempts)\n\n"
            f"• **Min Users Deep Scan:** `{settings['min_users_for_deep_scan']}`\n"
            f"  (Switch to deep scan after X users)\n\n"
            f"• **History Limit:** `{settings['history_limit']}`\n"
            f"  (Messages to check in history)\n\n"
            "Select a setting to edit:"
        )
        
    elif data == "settings_reset":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm Reset", callback_data="confirm_reset"),
                InlineKeyboardButton("❌ Cancel", callback_data="settings_main")
            ]
        ])
        
        text = "⚠️ **Reset All Settings**\n\nAre you sure you want to reset ALL settings to defaults?"
        
    elif data == "settings_main":
        await settings_command(client, callback_query.message)
        return
        
    elif data == "settings_close":
        await callback_query.message.delete()
        return
        
    elif data == "confirm_reset":
        await reset_user_settings(user_id)
        await callback_query.answer("✅ Settings reset to defaults!", show_alert=True)
        await settings_command(client, callback_query.message)
        return
        
    elif data.startswith("toggle_"):
        setting_name = data.replace("toggle_", "")
        current_value = settings.get(setting_name, False)
        settings[setting_name] = not current_value
        await save_user_settings(user_id, settings)
        await callback_query.answer(
            f"✅ {setting_name.replace('_', ' ').title()} set to {not current_value}!",
            show_alert=True
        )
        # Refresh the current settings page
        await settings_callback_handler(client, callback_query)
        return
        
    elif data.startswith("edit_"):
        # Extract setting name from callback data
        setting_parts = data.replace("edit_", "").split("_")
        setting_name = "_".join(setting_parts)
        
        # Store which setting we're editing
        await set_global_setting(f"editing_{user_id}", setting_name)
        
        # Get current value and ask for new value
        current_value = settings.get(setting_name, "")
        
        setting_descriptions = {
            "pts_max_attempts": ("PTS Max Attempts", "Enter number (100-100000):", "int"),
            "pts_delay": ("PTS Delay", "Enter delay in seconds (0.01-1.0):", "float"),
            "pts_flood_wait_multiplier": ("Flood Wait Multiplier", "Enter multiplier (0.5-5.0):", "float"),
            "channels_max_dialogs": ("Max Dialogs", "Enter number (10-1000):", "int"),
            "channels_max_members": ("Max Members", "Enter number (10-500):", "int"),
            "channels_delay": ("Channel Delay", "Enter delay in seconds (0.1-5.0):", "float"),
            "history_limit": ("History Limit", "Enter number (10-1000):", "int"),
            "input_timeout": ("Input Timeout", "Enter timeout in seconds (30-300):", "int"),
            "ask_timeout": ("Ask Timeout", "Enter timeout in seconds (30-300):", "int"),
            "max_scan_time_minutes": ("Max Scan Time", "Enter minutes (1-120):", "int"),
            "progress_update_interval": ("Progress Update", "Enter attempts (10-1000):", "int"),
            "min_users_for_deep_scan": ("Min Users Deep", "Enter number (10-10000):", "int"),
            "scan_mode": ("Scan Mode", "Enter mode (fast/balanced/deep):", "str"),
        }
        
        if setting_name in setting_descriptions:
            title, prompt, value_type = setting_descriptions[setting_name]
            
            await callback_query.message.edit_text(
                f"✏️ **Edit {title}**\n\n"
                f"Current value: `{current_value}`\n\n"
                f"{prompt}\n\n"
                f"Type `/cancel` to cancel."
            )
            
            # Wait for user input
            try:
                response = await client.ask(
                    chat_id=callback_query.message.chat.id,
                    text=f"Please enter new value for {title}:",
                    filters=filters.text & filters.private,
                    timeout=60
                )
                
                new_value = response.text.strip()
                
                # Validate and convert based on type
                if value_type == "int":
                    new_value = int(new_value)
                elif value_type == "float":
                    new_value = float(new_value)
                # str type doesn't need conversion
                
                # Validation
                if setting_name == "scan_mode" and new_value not in ["fast", "balanced", "deep"]:
                    raise ValueError("Mode must be fast, balanced, or deep")
                
                # Update setting
                settings[setting_name] = new_value
                await save_user_settings(user_id, settings)
                
                await response.reply(f"✅ **{title}** updated to `{new_value}`")
                await settings_command(client, callback_query.message)
                
            except ValueError:
                await callback_query.message.reply("❌ Invalid value format. Please try again.")
            except asyncio.TimeoutError:
                await callback_query.message.reply("⏰ Timeout! Please try again.")
            except Exception as e:
                logger.error(f"Error editing setting: {e}")
                await callback_query.message.reply("❌ Error updating setting.")
            
            # Clean up
            await set_global_setting(f"editing_{user_id}", None)
            return
    
    # Update message with new keyboard
    await callback_query.message.edit_text(text, reply_markup=keyboard)
    await callback_query.answer()

# ================= USER DATA FETCHING WITH SETTINGS =================
async def fetch_old_users_pts(bot_token: str, user_id: int = None, max_attempts: int = None) -> Dict[str, Any]:
    """Fetch old users using PTS method with user settings"""
    start_time = time.time()
    current_pts = 1
    users_found = set()
    
    # Get user settings
    settings = await get_user_settings(user_id) if user_id else DEFAULT_SETTINGS
    
    # Use provided max_attempts or from settings
    if max_attempts is None:
        max_attempts = settings["pts_max_attempts"]
    
    pts_delay = settings["pts_delay"]
    flood_multiplier = settings["pts_flood_wait_multiplier"]
    update_interval = settings["progress_update_interval"]
    
    logger.info(f"Starting PTS fetch with settings: attempts={max_attempts}, delay={pts_delay}s")
    
    # Create a temporary client with the provided bot token
    temp_bot = None
    try:
        temp_bot = Client(
            "temp_bot_session",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=bot_token,
            no_updates=True
        )
        
        await temp_bot.start()
        
        for attempt in range(1, max_attempts + 1):
            try:
                # Show progress at intervals
                if attempt % update_interval == 0:
                    elapsed = format_time_elapsed(start_time)
                    logger.info(f"Attempt {attempt}: Found {len(users_found)} users so far ({elapsed})")
                
                result = await temp_bot.invoke(
                    functions.updates.GetDifference(
                        pts=current_pts,
                        date=int(time.time()),
                        qts=1
                    )
                )
                
                # Extract users from result
                users = getattr(result, "users", [])
                if users:
                    for user in users:
                        if isinstance(user, types.User) and user.id > 0:
                            if user.id not in users_found:
                                users_found.add(user.id)
                
                # Update PTS for next iteration
                if hasattr(result, 'state') and result.state:
                    current_pts = result.state.pts
                elif hasattr(result, 'intermediate_state'):
                    current_pts = result.intermediate_state.pts
                else:
                    current_pts += 1
                
                # Use configurable delay
                await asyncio.sleep(pts_delay)
                
            except FloodWait as e:
                wait_time = e.value * flood_multiplier
                logger.warning(f"FloodWait: Sleeping {wait_time} seconds (multiplier: {flood_multiplier}x)")
                await asyncio.sleep(wait_time)
                continue
                
            except Exception as e:
                logger.error(f"Error at PTS {current_pts}: {e}")
                current_pts += 1
                await asyncio.sleep(0.1)
                
    except Exception as e:
        logger.error(f"Error in fetch_old_users_pts: {e}")
        return {
            "success": False,
            "error": str(e),
            "users": [],
            "count": 0,
            "duration": format_time_elapsed(start_time),
            "settings_used": {
                "max_attempts": max_attempts,
                "pts_delay": pts_delay,
                "flood_multiplier": flood_multiplier
            }
        }
        
    finally:
        # Stop the temporary bot client
        if temp_bot and temp_bot.is_connected:
            try:
                await temp_bot.stop()
            except:
                pass
    
    duration = format_time_elapsed(start_time)
    logger.info(f"PTS fetch completed: {len(users_found)} users in {duration}")
    
    return {
        "success": True,
        "users": list(users_found),
        "count": len(users_found),
        "duration": duration,
        "method": "PTS",
        "settings_used": {
            "max_attempts": max_attempts,
            "pts_delay": pts_delay,
            "flood_multiplier": flood_multiplier
        }
    }

async def fetch_old_users_channels(bot_token: str, user_id: int = None) -> Dict[str, Any]:
    """Fetch users from bot's dialogs/channels with user settings"""
    start_time = time.time()
    users_found = set()
    
    # Get user settings
    settings = await get_user_settings(user_id) if user_id else DEFAULT_SETTINGS
    
    max_dialogs = settings["channels_max_dialogs"]
    max_members = settings["channels_max_members"]
    channels_delay = settings["channels_delay"]
    history_limit = settings["history_limit"]
    
    logger.info(f"Starting channel fetch with settings: dialogs={max_dialogs}, members={max_members}")
    
    temp_bot = None
    try:
        session_name = f"temp_{uuid.uuid4().hex}"
        temp_bot = Client(
            session_name,
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=bot_token,
            no_updates=True
        )
        
        await temp_bot.start()
        
        # Get bot info first
        bot_me = await temp_bot.get_me()
        logger.info(f"Bot: @{bot_me.username} (ID: {bot_me.id})")
        
        # Get dialogs with limit from settings
        async for dialog in temp_bot.get_dialogs(limit=max_dialogs):
            try:
                if dialog.chat:
                    # Get chat members for groups/channels
                    if dialog.chat.type in ["group", "supergroup", "channel"]:
                        try:
                            async for member in temp_bot.get_chat_members(dialog.chat.id, limit=max_members):
                                if member.user and member.user.id > 0:
                                    users_found.add(member.user.id)
                            
                            # Use configurable delay between chats
                            await asyncio.sleep(channels_delay)
                            
                        except (UserNotParticipant, ChannelInvalid):
                            continue
                        except FloodWait as e:
                            await asyncio.sleep(e.value)
                        except Exception as e:
                            logger.error(f"Error getting members from {dialog.chat.id}: {e}")
                            continue
                    
                    # For private chats, add the other user
                    elif dialog.chat.type == "private" and dialog.chat.id != bot_me.id:
                        users_found.add(dialog.chat.id)
                
            except Exception as e:
                logger.error(f"Error processing dialog: {e}")
                continue
        
        # Also try to get recent messages with limit from settings
        try:
            async for message in temp_bot.get_chat_history("me", limit=history_limit):
                if message.from_user and message.from_user.id > 0:
                    users_found.add(message.from_user.id)
        except:
            pass
            
    except Exception as e:
        logger.error(f"Error in fetch_old_users_channels: {e}")
        return {
            "success": False,
            "error": str(e),
            "users": [],
            "count": 0,
            "duration": format_time_elapsed(start_time),
            "settings_used": {
                "max_dialogs": max_dialogs,
                "max_members": max_members,
                "channels_delay": channels_delay
            }
        }
        
    finally:
        if temp_bot and temp_bot.is_connected:
            try:
                await temp_bot.stop()
            except:
                pass
    
    duration = format_time_elapsed(start_time)
    logger.info(f"Channel fetch completed: {len(users_found)} users in {duration}")
    
    return {
        "success": True,
        "users": list(users_found),
        "count": len(users_found),
        "duration": duration,
        "method": "Channels",
        "settings_used": {
            "max_dialogs": max_dialogs,
            "max_members": max_members,
            "channels_delay": channels_delay
        }
    }

async def fetch_old_users_combined(bot_token: str, user_id: int = None) -> Dict[str, Any]:
    """Try multiple methods to fetch users with user settings"""
    logger.info(f"Starting combined fetch for user {user_id}")
    
    settings = await get_user_settings(user_id) if user_id else DEFAULT_SETTINGS
    
    all_users = set()
    methods_used = []
    total_duration = ""
    
    # Method 1: PTS (if enabled)
    if settings["enable_pts"]:
        # Adjust max_attempts based on scan mode
        scan_mode = settings["scan_mode"]
        base_attempts = settings["pts_max_attempts"]
        
        if scan_mode == "fast":
            max_attempts = int(base_attempts * 0.3)  # 30% for fast mode
        elif scan_mode == "deep":
            max_attempts = int(base_attempts * 1.5)  # 150% for deep mode
        else:  # balanced
            max_attempts = base_attempts
        
        result1 = await fetch_old_users_pts(bot_token, user_id, max_attempts)
        if result1["success"]:
            all_users.update(result1["users"])
            methods_used.append(f"PTS ({result1['count']} users)")
    
    # Method 2: Channels/Dialogs (if enabled)
    if settings["enable_channels"]:
        result2 = await fetch_old_users_channels(bot_token, user_id)
        if result2["success"]:
            all_users.update(result2["users"])
            methods_used.append(f"Channels ({result2['count']} users)")
    
    total_count = len(all_users)
    
    logger.info(f"Combined fetch completed: {total_count} unique users")
    
    return {
        "success": total_count > 0 or (not settings["enable_pts"] and not settings["enable_channels"]),
        "users": list(all_users),
        "count": total_count,
        "duration": total_duration,
        "methods": " + ".join(methods_used) if methods_used else "None",
        "settings_used": settings
    }

# ================= UPDATED COMMAND HANDLERS =================
@app.on_message(filters.command(["start", "help"]))
async def start_help_command(_, message: Message):
    """Handle /start and /help commands"""
    user_id = message.from_user.id
    user_name = message.from_user.first_name
    command = message.command[0]
    
    if command == "start":
        welcome_text = (
            f"👋 **Hello {user_name}!**\n\n"
            "🤖 **Old Users Bot**\n\n"
            "📋 **Available Commands:**\n"
            "• `/old_users` - Fetch old users from a bot\n"
            "• `/settings` - Configure bot settings (Admin)\n"
            "• `/stats` - Show bot statistics\n"
            "• `/help` - Show help message\n\n"
            "⚙️ **How to use:**\n"
            "1. Send `/old_users`\n"
            "2. Provide the bot token\n"
            "3. Wait while I fetch users\n"
            "4. Receive broadcast.txt file\n\n"
            "⚡ **Custom Settings:**\n"
            "Admins can use `/settings` to configure:\n"
            "• PTS scan speed & depth\n"
            "• Channel scanning limits\n"
            "• Timeout values\n"
            "• Scan modes (fast/balanced/deep)\n\n"
            "⚠️ **Note:** Only bot owners can fetch their own bot's users."
        )
    else:
        welcome_text = (
            "❓ **Help - Old Users Bot**\n\n"
            "**📌 Purpose:**\n"
            "Fetch old users from Telegram bots without database\n\n"
            "**🔧 Commands:**\n"
            "• `/old_users` - Start user fetching process\n"
            "• `/settings` - Configure settings (Admin only)\n"
            "• `/stats` - Show bot uptime and statistics\n"
            "• `/cancel` - Cancel current operation\n\n"
            "**⚡ Custom Settings (Admins):**\n"
            "• **PTS Settings:** Max attempts, delay, flood protection\n"
            "• **Channel Settings:** Dialog limits, member limits\n"
            "• **Timeout Settings:** Input timeouts, max scan time\n"
            "• **Scan Modes:** Fast (quick), Balanced, Deep (thorough)\n\n"
            "**📝 Usage:**\n"
            "1. Use `/old_users` command\n"
            "2. Send your bot token when asked\n"
            "3. Wait for processing (time depends on settings)\n"
            "4. Receive `broadcast.txt` with user IDs\n\n"
            "**⏰ Processing Time:**\n"
            "• **Fast Mode:** 1-5 minutes\n"
            "• **Balanced Mode:** 5-15 minutes\n"
            "• **Deep Mode:** 10-30 minutes\n\n"
            "**⚠️ Important:**\n"
            "• Only fetch users from YOUR OWN bots\n"
            "• Don't abuse this service\n"
            "• Respect Telegram ToS\n"
            "• Adjust settings carefully to avoid FloodWait\n\n"
            "**👑 Admin:** Use `/settings` to configure the bot"
        )
    
    await message.reply(welcome_text)

@app.on_message(filters.command("stats"))
async def stats_command(_, message: Message):
    """Handle /stats command"""
    user_id = message.from_user.id
    
    # Calculate uptime
    uptime_seconds = time.time() - BOT_START_TIME
    uptime_str = format_uptime(uptime_seconds)
    
    # Get memory usage
    try:
        import psutil
        process = psutil.Process(os.getpid())
        memory_mb = process.memory_info().rss / 1024 / 1024
        memory_str = f"{memory_mb:.2f} MB"
    except:
        memory_str = "N/A"
    
    # Count active tasks
    active_tasks = len(users_fetch_tasks)
    
    # MongoDB status
    db_status = "✅ Connected" if MONGODB_AVAILABLE else "❌ Not Available"
    
    stats_text = (
        "📊 **Bot Statistics**\n\n"
        f"**🤖 Bot Info:**\n"
        f"• ⏱ Uptime: `{uptime_str}`\n"
        f"• 🧠 Memory: `{memory_str}`\n"
        f"• ⚡ Active Tasks: `{active_tasks}`\n"
        f"• 👤 Your ID: `{user_id}`\n"
        f"• 📊 MongoDB: `{db_status}`\n\n"
        
        f"**📈 System:**\n"
        f"• 🐍 Python: `{sys.version.split()[0]}`\n"
        f"• 📅 Started: `{datetime.fromtimestamp(BOT_START_TIME).strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
        
        f"**🔧 Features:**\n"
        "• ✅ PTS Method (Configurable)\n"
        "• ✅ Channels Method (Configurable)\n"
        "• ✅ Flood Protection (Configurable)\n"
        "• ✅ Custom Settings System\n"
        "• ✅ MongoDB Storage\n"
        "• ✅ Admin Settings Panel\n"
        "• ✅ Progress Tracking\n"
        "• ✅ File Export\n"
    )
    
    # Add admin-only info
    if is_admin(user_id):
        settings = await get_user_settings(user_id)
        stats_text += f"\n**👑 Your Settings:**\n"
        stats_text += f"• Scan Mode: `{settings['scan_mode']}`\n"
        stats_text += f"• PTS Attempts: `{settings['pts_max_attempts']}`\n"
        stats_text += f"• Max Dialogs: `{settings['channels_max_dialogs']}`\n"
    
    await message.reply(stats_text)

@app.on_message(filters.command("old_users"))
async def old_users_command(client: Client, message: Message):
    """Handle /old_users command with user settings"""
    user_id = message.from_user.id
    
    # Check if user is already processing
    if user_id in users_fetch_tasks:
        await message.reply("⏳ **Already processing!**\n\nPlease wait for current operation to complete.")
        return
    
    try:
        # Get user settings
        settings = await get_user_settings(user_id)
        
        # Ask user for bot token using app.ask() with configurable timeout
        ask_msg = await client.ask(
            chat_id=message.chat.id,
            text=(
                "🔑 **Enter Bot Token**\n\n"
                "Please send me the bot token you want to fetch users from:\n\n"
                "**Format:** `1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZ`\n\n"
                f"⚡ **Current Settings:**\n"
                f"• Scan Mode: `{settings['scan_mode']}`\n"
                f"• Timeout: `{settings['input_timeout']}s`\n\n"
                "⚠️ **Important:**\n"
                "• Only use YOUR OWN bot tokens\n"
                "• This may take several minutes\n"
                "• Do NOT share others' bot tokens\n\n"
                "Type `/cancel` to cancel this request.\n"
                "Admins: Use `/settings` to change parameters."
            ),
            filters=filters.text & filters.private,
            timeout=settings["input_timeout"],
            reply_to_message_id=message.id
        )
        
        bot_token = ask_msg.text.strip()
        
        # Validate token format
        if not validate_bot_token(bot_token):
            await message.reply("❌ **Invalid bot token format!**\n\nPlease provide a valid bot token in the format: `1234567890:ABC...`\n\nUse `/old_users` to try again.")
            return
        
        # Show settings summary for admins
        if is_admin(user_id):
            settings_summary = (
                f"\n\n⚙️ **Active Settings:**\n"
                f"• Mode: `{settings['scan_mode']}`\n"
                f"• PTS: `{'✅' if settings['enable_pts'] else '❌'}` ({settings['pts_max_attempts']} attempts)\n"
                f"• Channels: `{'✅' if settings['enable_channels'] else '❌'}` ({settings['channels_max_dialogs']} dialogs)\n"
                f"• Max Time: `{settings['max_scan_time_minutes']} minutes`"
            )
        else:
            settings_summary = ""
        
        # Acknowledge receipt
        status_msg = await message.reply(
            "✅ **Token received!**\n\n"
            "🔄 Starting user fetch process...\n"
            f"⏳ Estimated time depends on settings.{settings_summary}\n\n"
            "📊 **Status:** Initializing..."
        )
        
        # Start the fetch process with user settings
        asyncio.create_task(fetch_and_send_users(client, user_id, message.chat.id, bot_token, status_msg.id))
        
    except asyncio.TimeoutError:
        await message.reply(f"⏰ **Request timed out!**\n\nYou took too long to reply. Timeout: {settings['input_timeout']} seconds\n\nPlease try again with `/old_users`")
    except Exception as e:
        logger.error(f"Error in old_users_command: {e}")
        logger.error(traceback.format_exc())
        await message.reply("❌ **Error occurred!**\n\nPlease try again.")

# ================= UPDATED MAIN FETCH FUNCTION =================
async def fetch_and_send_users(client: Client, user_id: int, chat_id: int, bot_token: str, status_msg_id: int):
    """Main function to fetch users and send results with user settings"""
    start_time = time.time()
    
    # Get user settings
    settings = await get_user_settings(user_id)
    max_scan_time = settings["max_scan_time_minutes"] * 60  # Convert to seconds
    
    # Create task entry
    users_fetch_tasks[user_id] = {
        "started": time.time(),
        "cancelled": False,
        "status": "Starting...",
        "users_found": 0,
        "settings": settings
    }
    
    try:
        # Update status with settings info
        status_msg = await client.get_messages(chat_id, status_msg_id)
        
        settings_info = ""
        if is_admin(user_id):
            settings_info = (
                f"\n⚙️ **Active Settings:**\n"
                f"• Mode: `{settings['scan_mode']}`\n"
                f"• PTS Attempts: `{settings['pts_max_attempts']}`\n"
                f"• Max Time: `{settings['max_scan_time_minutes']}min`"
            )
        
        # Method 1: Try PTS method first (if enabled)
        if settings["enable_pts"]:
            await status_msg.edit(
                "🔍 **Fetching Users**\n\n"
                f"🔄 **Status:** Starting PTS method...\n"
                f"👥 **Users Found:** 0\n"
                f"⏱ **Elapsed:** 0s\n"
                f"📊 **Mode:** {settings['scan_mode']}\n"
                f"⏳ **Max Time:** {settings['max_scan_time_minutes']} minutes{settings_info}\n\n"
                "Please wait..."
            )
            
            # Adjust max_attempts based on scan mode
            scan_mode = settings["scan_mode"]
            base_attempts = settings["pts_max_attempts"]
            
            if scan_mode == "fast":
                max_attempts = int(base_attempts * 0.3)
                mode_text = "Fast (30% attempts)"
            elif scan_mode == "deep":
                max_attempts = int(base_attempts * 1.5)
                mode_text = "Deep (150% attempts)"
            else:  # balanced
                max_attempts = base_attempts
                mode_text = "Balanced (100% attempts)"
            
            result = await fetch_old_users_pts(bot_token, user_id, max_attempts)
            
            # Update task status
            users_fetch_tasks[user_id]["status"] = f"PTS completed: {result['count']} users"
            users_fetch_tasks[user_id]["users_found"] = result["count"]
            
            elapsed = format_time_elapsed(start_time)
            
            if result["success"] and result["count"] > 0:
                await status_msg.edit(
                    f"✅ **PTS Method Complete**\n\n"
                    f"🔄 **Status:** PTS scan finished\n"
                    f"👥 **Users Found:** {format_number(result['count'])}\n"
                    f"⏱ **Elapsed:** {elapsed}\n"
                    f"📊 **Mode:** {mode_text}\n"
                    f"🎯 **Attempts:** {max_attempts}\n\n"
                    f"🔄 Preparing next step..."
                )
                
                # Check if we should do channels method
                if settings["enable_channels"] and (settings["scan_mode"] != "fast" or result["count"] < settings["min_users_for_deep_scan"]):
                    # Method 2: Try channels method for more users
                    await asyncio.sleep(1)
                    result2 = await fetch_old_users_channels(bot_token, user_id)
                    
                    if result2["success"] and result2["count"] > 0:
                        # Combine results
                        combined_users = set(result["users"])
                        combined_users.update(result2["users"])
                        total_count = len(combined_users)
                        
                        elapsed_total = format_time_elapsed(start_time)
                        
                        await status_msg.edit(
                            f"✅ **Both Methods Complete**\n\n"
                            f"🔄 **Status:** All scans finished\n"
                            f"👥 **Users Found:** {format_number(total_count)}\n"
                            f"⏱ **Elapsed:** {elapsed_total}\n"
                            f"📊 **Methods:** PTS + Channels\n"
                            f"📈 **Unique Users:** {format_number(total_count)}\n\n"
                            f"💾 Saving to file..."
                        )
                        
                        # Save to file
                        filename = f"broadcast_{int(time.time())}.txt"
                        save_users_to_file(combined_users, filename)
                        
                        # Send file with caption including settings info
                        caption = (
                            f"📁 **User Data File**\n\n"
                            f"👥 **Total Users:** {format_number(total_count)}\n"
                            f"⏱ **Fetch Time:** {elapsed_total}\n"
                            f"📊 **Methods Used:** PTS + Channels\n"
                            f"⚙️ **Scan Mode:** {settings['scan_mode']}\n"
                            f"📅 **Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                            f"📝 **Format:** One user ID per line\n"
                            f"💾 **File:** broadcast.txt"
                        )
                        
                        await client.send_document(
                            chat_id=chat_id,
                            document=filename,
                            caption=caption,
                            reply_to_message_id=status_msg_id
                        )
                        
                        # Clean up file
                        try:
                            os.remove(filename)
                        except:
                            pass
                        
                        # Final status
                        final_text = (
                            f"🎉 **Complete!**\n\n"
                            f"✅ **Successfully fetched users!**\n"
                            f"👥 **Total Users:** {format_number(total_count)}\n"
                            f"⏱ **Total Time:** {elapsed_total}\n"
                            f"📊 **Scan Mode:** {settings['scan_mode']}\n"
                            f"📁 **File Sent:** broadcast.txt\n\n"
                            f"📊 **Breakdown:**\n"
                            f"• PTS method: {format_number(result['count'])} users\n"
                            f"• Channels method: {format_number(result2['count'])} users\n"
                            f"• Unique total: {format_number(total_count)} users\n\n"
                            f"✅ **Done!**"
                        )
                        
                        if is_admin(user_id):
                            final_text += f"\n\n⚙️ **Settings Used:**\n"
                            final_text += f"• PTS Attempts: {max_attempts}\n"
                            final_text += f"• Max Dialogs: {settings['channels_max_dialogs']}\n"
                            final_text += f"• PTS Delay: {settings['pts_delay']}s"
                        
                        await status_msg.edit(final_text)
                        
                    else:
                        # Only PTS results
                        await handle_pts_only_result(client, chat_id, status_msg_id, result, settings, elapsed)
                else:
                    # Only PTS, channels disabled or fast mode with few users
                    await handle_pts_only_result(client, chat_id, status_msg_id, result, settings, elapsed)
            
            else:
                # PTS failed or no users
                elapsed = format_time_elapsed(start_time)
                await status_msg.edit(
                    f"❌ **No Users Found**\n\n"
                    f"🔍 **Status:** PTS method completed\n"
                    f"👥 **Users Found:** 0\n"
                    f"⏱ **Elapsed:** {elapsed}\n"
                    f"📊 **Mode:** {settings['scan_mode']}\n"
                    f"⚠️ **Issue:** No users found via PTS\n\n"
                    f"Maybe the bot has no history?\n"
                    f"Try with a different bot token or different settings."
                )
        else:
            # Only channels method enabled
            await status_msg.edit(
                "🔍 **Fetching Users**\n\n"
                f"🔄 **Status:** Starting Channels method (PTS disabled)...\n"
                f"👥 **Users Found:** 0\n"
                f"⏱ **Elapsed:** 0s\n"
                f"📊 **Mode:** {settings['scan_mode']}\n"
                f"⏳ **Max Time:** {settings['max_scan_time_minutes']} minutes{settings_info}\n\n"
                "Please wait..."
            )
            
            result = await fetch_old_users_channels(bot_token, user_id)
            elapsed = format_time_elapsed(start_time)
            
            if result["success"] and result["count"] > 0:
                await handle_channels_only_result(client, chat_id, status_msg_id, result, settings, elapsed)
            else:
                await status_msg.edit(
                    f"❌ **No Users Found**\n\n"
                    f"🔍 **Status:** Channels method completed\n"
                    f"👥 **Users Found:** 0\n"
                    f"⏱ **Elapsed:** {elapsed}\n"
                    f"📊 **Mode:** {settings['scan_mode']}\n"
                    f"⚠️ **Issue:** No users found via Channels\n\n"
                    f"Maybe the bot has no dialogs?\n"
                    f"Try enabling PTS method in settings."
                )
    
    except FloodWait as e:
        logger.warning(f"FloodWait in fetch_and_send_users: {e.value} seconds")
        status_msg = await client.get_messages(chat_id, status_msg_id)
        await status_msg.edit(
            f"⏳ **Flood Wait**\n\n"
            f"Telegram requires us to wait {e.value} seconds.\n\n"
            f"🔄 Auto-resuming after wait..."
        )
        await asyncio.sleep(e.value)
        
    except Exception as e:
        logger.error(f"Error in fetch_and_send_users: {e}")
        logger.error(traceback.format_exc())
        
        try:
            status_msg = await client.get_messages(chat_id, status_msg_id)
            elapsed = format_time_elapsed(start_time)
            await status_msg.edit(
                f"❌ **Error Occurred**\n\n"
                f"⚠️ **Status:** Fetch failed\n"
                f"⏱ **Elapsed:** {elapsed}\n"
                f"💥 **Error:** {str(e)[:100]}\n\n"
                f"Possible issues:\n"
                f"• Invalid bot token\n"
                f"• Bot has no users\n"
                f"• Server error\n"
                f"• Settings too aggressive (try slower settings)\n\n"
                f"Please try again or adjust settings."
            )
        except:
            pass
    
    finally:
        # Clean up
        if user_id in users_fetch_tasks:
            del users_fetch_tasks[user_id]

async def handle_pts_only_result(client: Client, chat_id: int, status_msg_id: int, result: Dict, settings: Dict, elapsed: str):
    """Handle PTS-only result"""
    filename = f"broadcast_{int(time.time())}.txt"
    save_users_to_file(set(result["users"]), filename)
    
    caption = (
        f"📁 **User Data File**\n\n"
        f"👥 **Total Users:** {format_number(result['count'])}\n"
        f"⏱ **Fetch Time:** {elapsed}\n"
        f"📊 **Method Used:** PTS only\n"
        f"⚙️ **Scan Mode:** {settings['scan_mode']}\n"
        f"📅 **Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"💾 **File:** broadcast.txt"
    )
    
    if not settings["enable_channels"]:
        caption += f"\n⚠️ **Note:** Channels method is disabled in settings"
    
    await client.send_document(
        chat_id=chat_id,
        document=filename,
        caption=caption,
        reply_to_message_id=status_msg_id
    )
    
    try:
        os.remove(filename)
    except:
        pass
    
    final_text = (
        f"✅ **Partial Complete**\n\n"
        f"👥 **Users Found:** {format_number(result['count'])}\n"
        f"⏱ **Time:** {elapsed}\n"
        f"📊 **Method:** PTS only\n"
        f"⚙️ **Mode:** {settings['scan_mode']}\n"
        f"✅ **File sent!**"
    )
    
    if not settings["enable_channels"]:
        final_text += f"\n\nℹ️ Channels method is disabled. Enable it in `/settings`"
    
    status_msg = await client.get_messages(chat_id, status_msg_id)
    await status_msg.edit(final_text)

async def handle_channels_only_result(client: Client, chat_id: int, status_msg_id: int, result: Dict, settings: Dict, elapsed: str):
    """Handle channels-only result"""
    filename = f"broadcast_{int(time.time())}.txt"
    save_users_to_file(set(result["users"]), filename)
    
    caption = (
        f"📁 **User Data File**\n\n"
        f"👥 **Total Users:** {format_number(result['count'])}\n"
        f"⏱ **Fetch Time:** {elapsed}\n"
        f"📊 **Method Used:** Channels only\n"
        f"⚙️ **Scan Mode:** {settings['scan_mode']}\n"
        f"📅 **Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"💾 **File:** broadcast.txt"
    )
    
    await client.send_document(
        chat_id=chat_id,
        document=filename,
        caption=caption,
        reply_to_message_id=status_msg_id
    )
    
    try:
        os.remove(filename)
    except:
        pass
    
    final_text = (
        f"✅ **Complete (Channels Only)**\n\n"
        f"👥 **Users Found:** {format_number(result['count'])}\n"
        f"⏱ **Time:** {elapsed}\n"
        f"📊 **Method:** Channels only\n"
        f"⚙️ **Mode:** {settings['scan_mode']}\n"
        f"✅ **File sent!**"
    )
    
    status_msg = await client.get_messages(chat_id, status_msg_id)
    await status_msg.edit(final_text)

# ================= MAIN =================
if __name__ == "__main__":
    # Print startup info
    print("=" * 50)
    print("🤖 Old Users Bot with Admin Settings")
    print("📝 Fetch old users from Telegram bots")
    print(f"👑 Owner ID: {OWNER_ID}")
    print(f"🔧 MongoDB: {'✅ Connected' if MONGODB_AVAILABLE else '❌ Not Available'}")
    print("=" * 50)
    
    # Start the bot
    logger.info("Starting bot with admin settings system...")
    app.run()
