import os
import sys
import time
import json
import logging
import traceback
import asyncio
from datetime import datetime, timedelta
from typing import List, Set, Dict, Any

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, UserNotParticipant, ChannelInvalid
from pyrogram.raw import functions, types

# ================= CONFIGURATION =================
API_ID = int(os.getenv("API_ID", "27169529"))  # Your API ID
API_HASH = os.getenv("API_HASH", "5d67602a4e0bbfabe669c0febeaf63b6")  # Your API Hash
BOT_TOKEN = os.getenv("BOT_TOKEN", "8539561305:AAGF1JDWkXt3mlSqnD7UnKNoZp7q3HtOhl0")  # Your bot token
OWNER_ID = int(os.getenv("OWNER_ID", "6441347235"))  # Your user ID

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

# ================= USER DATA FETCHING =================
async def fetch_old_users_pts(bot_token: str, max_attempts: int = 50000) -> Dict[str, Any]:
    """Fetch old users using PTS method"""
    start_time = time.time()
    current_pts = 1
    users_found = set()
    
    logger.info(f"Starting PTS fetch for bot token: {bot_token[:10]}...")
    
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
                # Show progress every 100 attempts
                if attempt % 100 == 0:
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
                
                # Small delay to avoid flooding
                await asyncio.sleep(0.05)
                
            except FloodWait as e:
                logger.warning(f"FloodWait: Sleeping {e.value} seconds")
                await asyncio.sleep(e.value)
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
            "duration": format_time_elapsed(start_time)
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
        "method": "PTS"
    }

async def fetch_old_users_channels(bot_token: str) -> Dict[str, Any]:
    """Fetch users from bot's dialogs/channels"""
    start_time = time.time()
    users_found = set()
    
    logger.info(f"Starting channel fetch for bot token: {bot_token[:10]}...")
    
    temp_bot = None
    try:
        temp_bot = Client(
            "temp_bot_dialog",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=bot_token,
            no_updates=True
        )
        
        await temp_bot.start()
        
        # Get bot info first
        bot_me = await temp_bot.get_me()
        logger.info(f"Bot: @{bot_me.username} (ID: {bot_me.id})")
        
        # Get dialogs
        async for dialog in temp_bot.get_dialogs(limit=200):
            try:
                if dialog.chat:
                    # Get chat members for groups/channels
                    if dialog.chat.type in ["group", "supergroup", "channel"]:
                        try:
                            async for member in temp_bot.get_chat_members(dialog.chat.id, limit=100):
                                if member.user and member.user.id > 0:
                                    users_found.add(member.user.id)
                            
                            # Small delay between chats
                            await asyncio.sleep(0.5)
                            
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
        
        # Also try to get recent messages
        try:
            async for message in temp_bot.get_chat_history("me", limit=100):
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
            "duration": format_time_elapsed(start_time)
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
        "method": "Channels"
    }

async def fetch_old_users_combined(bot_token: str) -> Dict[str, Any]:
    """Try multiple methods to fetch users"""
    logger.info(f"Starting combined fetch for bot token: {bot_token[:10]}...")
    
    all_users = set()
    methods_used = []
    total_duration = ""
    
    # Method 1: PTS
    result1 = await fetch_old_users_pts(bot_token, max_attempts=30000)
    if result1["success"]:
        all_users.update(result1["users"])
        methods_used.append(f"PTS ({result1['count']} users)")
    
    # Method 2: Channels/Dialogs
    result2 = await fetch_old_users_channels(bot_token)
    if result2["success"]:
        all_users.update(result2["users"])
        methods_used.append(f"Channels ({result2['count']} users)")
    
    total_count = len(all_users)
    total_duration = f"{result1.get('duration', '?')} + {result2.get('duration', '?')}"
    
    logger.info(f"Combined fetch completed: {total_count} unique users")
    
    return {
        "success": total_count > 0,
        "users": list(all_users),
        "count": total_count,
        "duration": total_duration,
        "methods": " + ".join(methods_used) if methods_used else "None"
    }

# ================= COMMAND HANDLERS =================
@app.on_message(filters.command("start"))
async def start_command(_, message: Message):
    """Handle /start command"""
    user_id = message.from_user.id
    user_name = message.from_user.first_name
    
    welcome_text = (
        f"👋 **Hello {user_name}!**\n\n"
        "🤖 **Old Users Bot**\n\n"
        "📋 **Available Commands:**\n"
        "• `/old_users` - Fetch old users from a bot\n"
        "• `/stats` - Show bot statistics\n"
        "• `/help` - Show help message\n\n"
        "⚙️ **How to use:**\n"
        "1. Send `/old_users`\n"
        "2. Provide the bot token\n"
        "3. Wait while I fetch users\n"
        "4. Receive broadcast.txt file\n\n"
        "⚠️ **Note:** Only bot owners can fetch their own bot's users."
    )
    
    await message.reply(welcome_text)

@app.on_message(filters.command("help"))
async def help_command(_, message: Message):
    """Handle /help command"""
    help_text = (
        "❓ **Help - Old Users Bot**\n\n"
        "**📌 Purpose:**\n"
        "Fetch old users from Telegram bots without database\n\n"
        "**🔧 Commands:**\n"
        "• `/old_users` - Start user fetching process\n"
        "• `/stats` - Show bot uptime and statistics\n"
        "• `/cancel` - Cancel current operation\n\n"
        "**📝 Usage:**\n"
        "1. Use `/old_users` command\n"
        "2. Send your bot token when asked\n"
        "3. Wait for processing (may take time)\n"
        "4. Receive `broadcast.txt` with user IDs\n\n"
        "**⏰ Processing Time:**\n"
        "• Small bots: 1-5 minutes\n"
        "• Large bots: 5-30 minutes\n\n"
        "**⚠️ Important:**\n"
        "• Only fetch users from YOUR OWN bots\n"
        "• Don't abuse this service\n"
        "• Respect Telegram ToS\n"
        "• Large bots may take longer\n\n"
        "**👑 Owner:** Only bot owner can use this bot"
    )
    
    await message.reply(help_text)

@app.on_message(filters.command("stats"))
async def stats_command(_, message: Message):
    """Handle /stats command"""
    user_id = message.from_user.id
    
    # Only owner can see stats
    if user_id != OWNER_ID:
        await message.reply("❌ This command is for bot owner only!")
        return
    
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
    
    stats_text = (
        "📊 **Bot Statistics**\n\n"
        f"**🤖 Bot Info:**\n"
        f"• ⏱ Uptime: `{uptime_str}`\n"
        f"• 🧠 Memory: `{memory_str}`\n"
        f"• ⚡ Active Tasks: `{active_tasks}`\n"
        f"• 👤 Your ID: `{user_id}`\n\n"
        
        f"**📈 System:**\n"
        f"• 🐍 Python: `{sys.version.split()[0]}`\n"
        f"• 📅 Started: `{datetime.fromtimestamp(BOT_START_TIME).strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
        
        f"**🔧 Features:**\n"
        "• ✅ PTS Method\n"
        "• ✅ Channels Method\n"
        "• ✅ Flood Protection\n"
        "• ✅ Progress Tracking\n"
        "• ✅ File Export\n"
    )
    
    await message.reply(stats_text)

@app.on_message(filters.command("cancel"))
async def cancel_command(_, message: Message):
    """Handle /cancel command"""
    user_id = message.from_user.id
    
    if user_id in users_fetch_tasks:
        users_fetch_tasks[user_id]["cancelled"] = True
        await message.reply("🛑 **Operation cancelled!**\n\nYour fetch task has been stopped.")
    else:
        await message.reply("ℹ️ **No active operation to cancel.**")

@app.on_message(filters.command("old_users"))
async def old_users_command(client: Client, message: Message):
    """Handle /old_users command"""
    user_id = message.from_user.id
    
    # Check if user is already processing
    if user_id in users_fetch_tasks:
        await message.reply("⏳ **Already processing!**\n\nPlease wait for current operation to complete.")
        return
    
    try:
        # Ask user for bot token using app.ask()
        ask_msg = await client.ask(
            chat_id=message.chat.id,
            text=(
                "🔑 **Enter Bot Token**\n\n"
                "Please send me the bot token you want to fetch users from:\n\n"
                "**Format:** `1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZ`\n\n"
                "⚠️ **Important:**\n"
                "• Only use YOUR OWN bot tokens\n"
                "• This may take several minutes\n"
                "• Do NOT share others' bot tokens\n\n"
                "Type `/cancel` to cancel this request."
            ),
            filters=filters.text & filters.private,
            timeout=120,
            reply_to_message_id=message.id
        )
        
        bot_token = ask_msg.text.strip()
        
        # Validate token format
        if not validate_bot_token(bot_token):
            await message.reply("❌ **Invalid bot token format!**\n\nPlease provide a valid bot token in the format: `1234567890:ABC...`\n\nUse `/old_users` to try again.")
            return
        
        # Acknowledge receipt
        status_msg = await message.reply(
            "✅ **Token received!**\n\n"
            "🔄 Starting user fetch process...\n"
            "⏳ This may take several minutes.\n\n"
            "📊 **Status:** Initializing..."
        )
        
        # Start the fetch process
        asyncio.create_task(fetch_and_send_users(client, user_id, message.chat.id, bot_token, status_msg.id))
        
    except asyncio.TimeoutError:
        await message.reply("⏰ **Request timed out!**\n\nYou took too long to reply. Please try again with `/old_users`")
    except Exception as e:
        logger.error(f"Error in old_users_command: {e}")
        logger.error(traceback.format_exc())
        await message.reply("❌ **Error occurred!**\n\nPlease try again.")

# ================= MAIN FETCH FUNCTION =================
async def fetch_and_send_users(client: Client, user_id: int, chat_id: int, bot_token: str, status_msg_id: int):
    """Main function to fetch users and send results"""
    start_time = time.time()
    
    # Create task entry
    users_fetch_tasks[user_id] = {
        "started": time.time(),
        "cancelled": False,
        "status": "Starting...",
        "users_found": 0
    }
    
    try:
        # Update status
        status_msg = await client.get_messages(chat_id, status_msg_id)
        
        # Method 1: Try PTS method first
        await status_msg.edit(
            "🔍 **Fetching Users**\n\n"
            "🔄 **Status:** Trying PTS method...\n"
            "👥 **Users Found:** 0\n"
            "⏱ **Elapsed:** 0s\n"
            "📊 **Method:** PTS scanning\n\n"
            "⏳ Please wait..."
        )
        
        result = await fetch_old_users_pts(bot_token, max_attempts=20000)
        
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
                f"📊 **Method:** {result['method']}\n\n"
                f"🔄 Trying Channels method now..."
            )
            
            # Method 2: Try channels method for more users
            await asyncio.sleep(1)
            result2 = await fetch_old_users_channels(bot_token)
            
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
                
                # Send file with caption
                caption = (
                    f"📁 **User Data File**\n\n"
                    f"👥 **Total Users:** {format_number(total_count)}\n"
                    f"⏱ **Fetch Time:** {elapsed_total}\n"
                    f"📊 **Methods Used:** PTS + Channels\n"
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
                await status_msg.edit(
                    f"🎉 **Complete!**\n\n"
                    f"✅ **Successfully fetched users!**\n"
                    f"👥 **Total Users:** {format_number(total_count)}\n"
                    f"⏱ **Total Time:** {elapsed_total}\n"
                    f"📁 **File Sent:** broadcast.txt\n\n"
                    f"📊 **Breakdown:**\n"
                    f"• PTS method: {format_number(result['count'])} users\n"
                    f"• Channels method: {format_number(result2['count'])} users\n"
                    f"• Unique total: {format_number(total_count)} users\n\n"
                    f"✅ **Done!**"
                )
                
            else:
                # Only PTS results
                filename = f"broadcast_{int(time.time())}.txt"
                save_users_to_file(set(result["users"]), filename)
                
                caption = (
                    f"📁 **User Data File**\n\n"
                    f"👥 **Total Users:** {format_number(result['count'])}\n"
                    f"⏱ **Fetch Time:** {elapsed}\n"
                    f"📊 **Method Used:** PTS only\n"
                    f"📅 **Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"⚠️ **Note:** Channels method failed\n"
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
                
                await status_msg.edit(
                    f"✅ **Partial Complete**\n\n"
                    f"👥 **Users Found:** {format_number(result['count'])}\n"
                    f"⏱ **Time:** {elapsed}\n"
                    f"📊 **Method:** PTS only\n"
                    f"⚠️ Channels method failed\n\n"
                    f"✅ **File sent!**"
                )
        
        else:
            # PTS failed or no users
            elapsed = format_time_elapsed(start_time)
            await status_msg.edit(
                f"❌ **No Users Found**\n\n"
                f"🔍 **Status:** PTS method completed\n"
                f"👥 **Users Found:** 0\n"
                f"⏱ **Elapsed:** {elapsed}\n"
                f"⚠️ **Issue:** No users found via PTS\n\n"
                f"Maybe the bot has no history?\n"
                f"Try with a different bot token."
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
                f"• Server error\n\n"
                f"Please try again."
            )
        except:
            pass
    
    finally:
        # Clean up
        if user_id in users_fetch_tasks:
            del users_fetch_tasks[user_id]

# ================= ERROR HANDLER =================
async def error_handler(_, __, ___):
    """Global error handler"""
    pass

# ================= BOT STARTUP =================
def notify_owner():
    """Notify owner when bot starts"""
    try:
        import requests
        
        bot_name = "Old Users Bot"
        start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        message = (
            f"✅ **{bot_name} Started**\n\n"
            f"📅 Time: {start_time}\n"
            f"🤖 Bot: @{(app.get_me()).username}\n"
            f"👑 Owner: {OWNER_ID}\n"
            f"🐍 Python: {sys.version.split()[0]}\n\n"
            f"🚀 Ready to fetch users!"
        )
        
        logger.info(f"Bot started at {start_time}")
        
    except Exception as e:
        logger.error(f"Owner notification failed: {e}")

# ================= MAIN =================
if __name__ == "__main__":
    # Print startup info
    print("=" * 50)
    print("🤖 Old Users Bot")
    print("📝 Fetch old users from Telegram bots")
    print(f"👑 Owner ID: {OWNER_ID}")
    print("=" * 50)
    
    # Notify owner
    notify_owner()
    
    # Start the bot
    logger.info("Starting bot...")
    app.run()
