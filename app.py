import os
import io
import asyncio
import base64
import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List
from aiohttp import web

from PIL import Image, ImageFilter
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
)

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CHANNEL_SETTING = 1
LOG_CHANNEL_SETTING = 2


class ImageStore:
    def __init__(self, connection_string: str):
        self._client = MongoClient(connection_string)
        self._db = self._client['secure_image_bot']
        self._collection = self._db['images']
        self._collection.create_index("_id")
        logger.info("MongoDB connected successfully")

    def purge_all(self) -> int:
        result = self._collection.delete_many({})
        logger.info(f"Purged {result.deleted_count} images from database")
        return result.deleted_count

    def add(self, encrypted_data: bytes, preview_data: bytes, filename: str, caption: Optional[str] = None) -> str:
        image_id = secrets.token_hex(8)
        self._collection.insert_one({
            "_id": image_id,
            "encrypted": encrypted_data,
            "preview": preview_data,
            "filename": filename,
            "caption": caption,
            "created_at": datetime.utcnow(),
        })
        return image_id

    def get(self, image_id: str) -> Optional[dict]:
        result = self._collection.find_one({"_id": image_id})
        if result:
            return {
                "encrypted": result["encrypted"],
                "preview": result["preview"],
                "filename": result["filename"],
                "caption": result.get("caption"),
                "created_at": result["created_at"].timestamp(),
            }
        return None

    def remove(self, image_id: str) -> bool:
        result = self._collection.delete_one({"_id": image_id})
        return result.deleted_count > 0

    def list_all(self) -> List[dict]:
        results = self._collection.find().sort("created_at", -1).limit(20)
        return [
            {
                "id": r["_id"],
                "filename": r["filename"],
                "created_at": r["created_at"].strftime("%Y-%m-%d %H:%M"),
            }
            for r in results
        ]


class Encryptor:
    def __init__(self, key: bytes):
        self.aesgcm = AESGCM(key)

    async def encrypt(self, data: bytes) -> bytes:
        nonce = os.urandom(12)
        ciphertext = await asyncio.to_thread(self.aesgcm.encrypt, nonce, data, None)
        return nonce + ciphertext

    async def decrypt(self, data: bytes) -> bytes:
        nonce = data[:12]
        ciphertext = data[12:]
        return await asyncio.to_thread(self.aesgcm.decrypt, nonce, ciphertext, None)


def _create_preview_sync(image_bytes: bytes, max_size: tuple = (300, 300)) -> bytes:
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    
    img.thumbnail(max_size, Image.LANCZOS)
    
    pixel_size = 8
    img_small = img.resize(
        (max(1, img.width // pixel_size), max(1, img.height // pixel_size)),
        Image.NEAREST
    )
    pixelated = img_small.resize(img.size, Image.NEAREST)
    
    output = io.BytesIO()
    pixelated.save(output, format="JPEG", quality=60)
    return output.getvalue()

async def create_preview(image_bytes: bytes, max_size: tuple = (300, 300)) -> bytes:
    return await asyncio.to_thread(_create_preview_sync, image_bytes, max_size)



class SecureImageBot:
    def __init__(self):
        self.bot_token = os.environ.get("BOT_TOKEN", "")
        admin_ids_str = os.environ.get("ADMIN_IDS", "")
        encryption_key_str = os.environ.get("ENCRYPTION_KEY", "")
        mongo_uri = os.environ.get("MONGO_URI", "")
        self.protect_content = os.environ.get("PROTECT_CONTENT", "true").lower() == "true"
        
        if not encryption_key_str:
            encryption_key_str = secrets.token_hex(32)
        
        try:
            if len(encryption_key_str) == 64:
                key = bytes.fromhex(encryption_key_str)
            else:
                key = base64.b64decode(encryption_key_str)
        except Exception:
            key = hashlib.sha256(encryption_key_str.encode()).digest()
        
        self.encryptor = Encryptor(key)
        self.store = ImageStore(mongo_uri)
        
        if os.environ.get("PURGE_ON_START", "").lower() == "true":
            purged = self.store.purge_all()
            logger.info(f"Auto-purged {purged} images on startup")
        
        self.admin_ids = set(int(x.strip()) for x in admin_ids_str.split(",") if x.strip())
        
        privileged_ids_str = os.environ.get("PRIVILEGED_IDS", "")
        self.privileged_ids = set(int(x.strip()) for x in privileged_ids_str.split(",") if x.strip())
        
        self.channel_id = os.environ.get("CHANNEL_ID", "")
        self.log_channel_id = os.environ.get("LOG_CHANNEL_ID", "")
        self.rate_limit_enabled = os.environ.get("RATE_LIMIT_ENABLED", "true").lower() == "true"
        self.rate_limit_count = int(os.environ.get("RATE_LIMIT_COUNT", "10"))
        self.rate_limit_window = int(os.environ.get("RATE_LIMIT_WINDOW", "3600"))
        self.auto_delete_seconds = int(os.environ.get("AUTO_DELETE_SECONDS", "60"))
        
        self._users_collection = self.store._db['users']
        self._users_collection.create_index("username")
        
        self._rate_collection = self.store._db['rate_limits']
        try:
            self._rate_collection.create_index("user_id", unique=True)
        except Exception as e:
            if "IndexKeySpecsConflict" in str(e) or "already exists" in str(e) or "same name" in str(e):
                logger.info("Dropping old non-unique rate limit index to upgrade to unique...")
                try:
                    self._rate_collection.drop_index("user_id_1")
                    self._rate_collection.create_index("user_id", unique=True)
                except Exception as drop_e:
                    logger.error(f"Failed to recreate unique index: {drop_e}")
            else:
                logger.error(f"Index creation error: {e}")
        
        logger.info(f"Rate limit config: enabled={self.rate_limit_enabled}, count={self.rate_limit_count}, window={self.rate_limit_window}")
        logger.info(f"Admin IDs: {self.admin_ids}")
        logger.info(f"Privileged IDs: {self.privileged_ids}")
        
        self._app: Optional[Application] = None

    async def send_rate_limit_countdown(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, download_count: int, timeout_seconds: int):
        """Sends a rate limit message that updates every second"""
        try:
            # Format initial time remaining
            minutes, seconds = divmod(timeout_seconds, 60)
            hours, minutes = divmod(minutes, 60)
            
            time_str = ""
            if hours > 0:
                time_str += f"{hours}h "
            if minutes > 0:
                time_str += f"{minutes}m "
            time_str += f"{seconds}s"
            
            message_text = f"⚠️ Download limit reached!\n\nYou have used {download_count}/{self.rate_limit_count} downloads per {self.rate_limit_window // 3600} hour(s).\nTry again in {time_str}."
            
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=message_text
            )
            
            async def update_timer():
                remaining = timeout_seconds
                message_id = sent_msg.message_id
                
                while remaining > 0:
                    await asyncio.sleep(5) # Update every 5 seconds to avoid API limits
                    remaining -= 5
                    
                    if remaining <= 0:
                        try:
                            await context.bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=message_id,
                                text="✅ Your download limit has been reset! You can now request images."
                            )
                        except Exception as e:
                            logger.error(f"Failed to edit rate limit message on reset: {e}")
                        break
                        
                    # Format remaining time
                    m, s = divmod(remaining, 60)
                    h, m = divmod(m, 60)
                    
                    t_str = ""
                    if h > 0: t_str += f"{h}h "
                    if m > 0: t_str += f"{m}m "
                    t_str += f"{s}s"
                    
                    new_text = f"⏳ Download limit reached!\n\nYou have used {download_count}/{self.rate_limit_count} downloads per {self.rate_limit_window // 3600} hour(s).\nTry again in {t_str}."
                    
                    # Only edit if text changed
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=new_text
                        )
                    except Exception as e:
                        # Ignore "Message is not modified" errors
                        if "Message is not modified" not in str(e):
                            logger.error(f"Failed to update rate limit timer: {e}")
            
            # Start timer as background task
            asyncio.create_task(update_timer())
            return True
        except Exception as e:
            logger.error(f"Failed to send rate limit countdown: {e}")
            return False


    def check_rate_limit(self, user_id: int) -> tuple:
        try:
            if user_id in self.admin_ids:
                logger.info(f"User {user_id} is admin - bypass rate limit")
                return True, None, 0, 0
            
            if user_id in self.privileged_ids:
                logger.info(f"User {user_id} is privileged - bypass rate limit")
                return True, None, 0, 0
            
            if not self.rate_limit_enabled:
                return True, None, 0, 0
            
            from pymongo import ReturnDocument
            now = datetime.utcnow()
            
            doc = self._rate_collection.find_one({"user_id": user_id})
            
            if not doc:
                self._rate_collection.insert_one({
                    "user_id": user_id,
                    "first_download": now,
                    "count": 1
                })
                logger.info(f"Rate limit: First download for user {user_id}")
                return True, f"Downloaded! Remaining: {self.rate_limit_count-1}/{self.rate_limit_count}", 0, 1
            
            first_download = doc.get("first_download", now)
            elapsed = (now - first_download).total_seconds()
            
            if elapsed >= self.rate_limit_window:
                self._rate_collection.update_one(
                    {"user_id": user_id},
                    {"$set": {"first_download": now, "count": 1}}
                )
                logger.info(f"Rate limit window reset for user {user_id}")
                return True, f"Downloaded! Remaining: {self.rate_limit_count-1}/{self.rate_limit_count}", 0, 1
            
            current_count = doc.get("count", 0)
            if current_count >= self.rate_limit_count:
                reset_in = max(0, int(self.rate_limit_window - elapsed))
                error_msg = f"⚠️ Download limit reached!\n\nYou have used {current_count} downloads.\nTry again in {reset_in} seconds."
                logger.info(f"Rate limit HIT for user {user_id}, reset_in={reset_in}s")
                return False, error_msg, reset_in, current_count
                
            updated_doc = self._rate_collection.find_one_and_update(
                {
                    "user_id": user_id,
                    "count": {"$lt": self.rate_limit_count}
                },
                {"$inc": {"count": 1}},
                return_document=ReturnDocument.AFTER
            )
            
            if updated_doc:
                new_count = updated_doc.get("count", 1)
                remaining = self.rate_limit_count - new_count
                logger.info(f"Rate limit: User {user_id} downloaded. Remaining: {remaining}")
                return True, f"Downloaded! Remaining: {remaining}/{self.rate_limit_count}", 0, new_count
            else:
                reset_in = max(0, int(self.rate_limit_window - elapsed))
                error_msg = f"⚠️ Download limit reached!\n\nYou have used {self.rate_limit_count} downloads.\nTry again in {reset_in} seconds."
                logger.info(f"Rate limit HIT (concurrent) for user {user_id}")
                return False, error_msg, reset_in, self.rate_limit_count
                
        except Exception as e:
            logger.error(f"Rate limit check failed: {e}")
            return True, None, 0, 0

    async def track_user(self, user_id: int, username: str, first_name: str):
        try:
            self._users_collection.update_one(
                {"_id": user_id},
                {"$set": {"username": username, "first_name": first_name, "last_active": datetime.utcnow()}},
                upsert=True
            )
        except Exception as e:
            logger.error(f"Failed to track user: {e}")

    async def get_stats(self) -> dict:
        try:
            total_users = self._users_collection.count_documents({})
            return {"total_users": total_users}
        except:
            return {"total_users": 0}

    async def log(self, context: ContextTypes.DEFAULT_TYPE, message: str):
        if self.log_channel_id:
            try:
                await context.bot.send_message(
                    chat_id=self.log_channel_id,
                    text=f"📝 {message}"
                )
            except Exception as e:
                logger.error(f"Failed to send log: {e}")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        args = context.args
        user = update.effective_user
        
        await self.track_user(user_id, user.username or "", user.first_name or "")
        
        if args:
            if args[0] == f"admin_{user_id}":
                self.admin_ids.add(user_id)
                await update.message.reply_text("✅ You are now registered as admin!")
                return
        
        await update.message.reply_text(
            f"🔒 Secure Image Bot\n\n"
            f"Welcome! You can now request images from the channel.\n\n"
            f"Use /get <image_id> to view images or click from the channel."
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        is_admin = user_id in self.admin_ids
        
        if is_admin:
            await update.message.reply_text(
                "👨‍💼 Admin Commands:\n\n"
                "/start - Start the bot\n"
                "/help - Show help\n"
                "/setchannel - Set target channel\n"
                "/setlogchannel - Set log channel\n"
                "/upload - Upload image (send photo)\n"
                "/list - List images\n"
                "/health - Check health\n"
                "/stats - View statistics\n"
                "/purge - Delete all images\n\n"
                "To upload: send photo with caption"
            )
        else:
            await update.message.reply_text(
                "🔒 Secure Image Bot\n\n"
                "Get images from the channel using:\n"
                "/get <image_id>\n\n"
                "Or tap 'Get Original' in channel"
            )

    async def set_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.admin_ids:
            await update.message.reply_text("❌ Unauthorized. Admin only.")
            return
        
        await update.message.reply_text("Please send the channel ID (e.g., -1001234567890)")
        return CHANNEL_SETTING

    async def channel_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            channel_id = update.message.text.strip()
            if channel_id.startswith("-") and channel_id[1:].isdigit():
                self.channel_id = channel_id
                await update.message.reply_text(f"✅ Channel set to: {channel_id}")
            else:
                await update.message.reply_text("❌ Invalid channel ID. Use format: -1001234567890")
        except Exception as e:
            await self.log(context, f"❌ Set channel error: {str(e)}")
            await update.message.reply_text("❌ Error setting channel.")
        
        return ConversationHandler.END

    async def set_log_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.admin_ids:
            await update.message.reply_text("❌ Unauthorized. Admin only.")
            return
        
        await update.message.reply_text("Please send the log channel ID (e.g., -1001234567890)")
        return LOG_CHANNEL_SETTING

    async def log_channel_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            channel_id = update.message.text.strip()
            if channel_id.startswith("-") and channel_id[1:].isdigit():
                self.log_channel_id = channel_id
                await update.message.reply_text(f"✅ Log channel set to: {channel_id}")
            else:
                await update.message.reply_text("❌ Invalid channel ID. Use format: -1001234567890")
        except Exception as e:
            await self.log(context, f"❌ Set log channel error: {str(e)}")
            await update.message.reply_text("❌ Error setting log channel.")
        
        return ConversationHandler.END

    async def upload_image(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if user_id not in self.admin_ids:
            await update.message.reply_text(f"❌ Unauthorized. Your ID: {user_id} is not admin.")
            return
        
        if not update.message.photo:
            await update.message.reply_text("Please send a photo/image.")
            return
        
        if not self.channel_id:
            await update.message.reply_text("❌ Channel not set. Use /setchannel first.")
            return
        
        status_msg = await update.message.reply_text("🔄 Downloading image...")
        
        try:
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id)
            image_bytes = await file.download_as_bytearray()
            
            await status_msg.edit_text("🔐 Encrypting image...")
            encrypted = await self.encryptor.encrypt(bytes(image_bytes))
            
            await status_msg.edit_text("🖼️ Creating preview...")
            preview = await create_preview(bytes(image_bytes))
            filename = f"image_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            
            custom_caption = None
            if update.message.caption:
                custom_caption = update.message.caption.strip()
            
            await status_msg.edit_text("💾 Storing image...")
            image_id = self.store.add(encrypted, preview, filename, custom_caption)
            
            keyboard = [
                [InlineKeyboardButton("🔓 Get Original (Uncensored)", callback_data=f"req_{image_id}")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await status_msg.edit_text("📤 Sending to channel...")
            bot_username = context.bot.username
            
            caption_text = f"🖼️ Secure Image\n"
            if custom_caption:
                caption_text += f"\n{custom_caption}\n"
            caption_text += f"ID: `{image_id}`\n\n🔒 Tap 'Get Original' to view\n⚠️ Start @{bot_username} first!"
            
            sent_msg = await context.bot.send_photo(
                chat_id=self.channel_id,
                photo=preview,
                caption=caption_text,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
            
            await status_msg.edit_text(f"✅ Image uploaded!\n\nID: `{image_id}`", parse_mode="Markdown")
            
            await self.log(
                context,
                f"📤 New image uploaded\nID: `{image_id}`\nFile: {filename}\nCaption: {custom_caption or 'None'}"
            )
               
        except Exception as e:
            logger.error(f"Error processing image: {e}", exc_info=True)
            await self.log(context, f"❌ Upload error: {str(e)}")
            try:
                await status_msg.edit_text("❌ Error uploading image. Check log channel.")
            except:
                pass

    async def get_image(self, update: Update, context: ContextTypes.DEFAULT_TYPE, image_id: Optional[str] = None):
        if not image_id and update.message:
            image_id = " ".join(context.args)
        
        if not image_id:
            return
        
        image_id = image_id.strip()
        data = self.store.get(image_id)
        
        if not data:
            await self.log(context, f"⚠️ Image not found: {image_id}")
            return
            
        user_id = update.effective_user.id
        
        if self.rate_limit_enabled and user_id not in self.admin_ids and user_id not in self.privileged_ids:
            allowed, error_msg, remaining_secs, dl_count = self.check_rate_limit(user_id)
            logger.info(f"Rate limit check for /get command: user={user_id}, allowed={allowed}")
            if not allowed:
                await self.send_rate_limit_countdown(context, user_id, dl_count, remaining_secs)
                return
        
        try:
            decrypted = self.encryptor.decrypt(data["encrypted"])
            caption = data.get("caption") or data["filename"]
            
            is_privileged = user_id in self.admin_ids or user_id in self.privileged_ids
            auto_delete_caption = f"🔒 Auto-delete in {self.auto_delete_seconds}s" if not is_privileged else "🔓 No auto-delete"
            
            sent_msg = await context.bot.send_photo(
                chat_id=update.effective_user.id,
                photo=io.BytesIO(decrypted),
                caption=f"📷 {caption}\n\n{auto_delete_caption}",
                protect_content=self.protect_content
            )
            
            if not is_privileged:
                message_id = sent_msg.message_id
                chat_id = update.effective_user.id
                caption_text = caption
                
                halfway = self.auto_delete_seconds // 2
                
                async def update_countdown():
                    try:
                        await asyncio.sleep(halfway)
                        await context.bot.edit_message_caption(
                            chat_id=chat_id,
                            message_id=message_id,
                            caption=f"📷 {caption_text}\n\n🔒 Auto-delete in {self.auto_delete_seconds - halfway}s"
                        )
                    except Exception as e:
                        logger.error(f"Countdown update failed: {e}")
                
                async def delete_after():
                    try:
                        await asyncio.sleep(self.auto_delete_seconds)
                        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
                    except Exception as e:
                        logger.error(f"Delete failed: {e}")
                
                asyncio.create_task(update_countdown())
                asyncio.create_task(delete_after())
            
            await self.log(context, 
                f"✅ Image sent via /get\n"
                f"User ID: {update.effective_user.id}\n"
                f"Image ID: `{image_id}`\n"
                f"File: {data['filename']}"
            )
            
        except Exception as e:
            logger.error(f"Error decrypting: {e}")
            await self.log(context, f"❌ Decrypt error: {str(e)[:100]}")

    async def purge_data(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.admin_ids:
            return
        
        purged = self.store.purge_all()
        await update.message.reply_text(f"✅ Purged {purged} images from database.")
        await self.log(context, f"🗑️ Admin purged {purged} images")

    async def health_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("✅ Bot is running!")

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.admin_ids:
            await update.message.reply_text("❌ Unauthorized.")
            return
        
        stats = await self.get_stats()
        image_count = len(self.store.list_all())
        
        await update.message.reply_text(
            f"📊 Bot Statistics\n\n"
            f"👥 Total Users: {stats['total_users']}\n"
            f"🖼️ Total Images: {image_count}\n"
            f"🔒 Protect Content: {self.protect_content}\n"
            f"⚡ Rate Limit: {self.rate_limit_count}/hour (Enabled: {self.rate_limit_enabled})"
        )

    async def list_images(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        images = self.store.list_all()
        
        if not images:
            await update.message.reply_text("No images available.")
            return
        
        text = "📸 Available Images:\n\n"
        for img in images[:20]:
            text += f"• `{img['id']}` - {img['filename']} ({img['created_at']})\n"
        
        await update.message.reply_text(text, parse_mode="Markdown")

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user_id = query.from_user.id
        await query.answer()
        
        callback_data = query.data
        
        if callback_data.startswith("req_"):
            image_id = callback_data[4:]
            
            if not self.channel_id:
                logger.error("Channel not set! Admin needs to run /setchannel")
                await query.answer("❌ Bot not configured. Contact admin.", show_alert=True)
                return
            
            try:
                member = await context.bot.get_chat_member(self.channel_id, user_id)
            except Exception as e:
                logger.error(f"Channel membership check failed: {e}")
                member = None
            
            user_name = query.from_user.first_name or "User"
            user_username = f"@{query.from_user.username}" if query.from_user.username else ""
            
            if not member or member.status in ['left', 'kicked']:
                await self.log(context, f"⚠️ User {user_name} {user_username} not in channel")
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="❌ You must join the channel first to view images!"
                    )
                except:
                    pass
                return
            
            image_data = self.store.get(image_id)
            if not image_data:
                await self.log(context, f"⚠️ Image not found: {image_id}")
                return
                
            if self.rate_limit_enabled and user_id not in self.admin_ids and user_id not in self.privileged_ids:
                allowed, error_msg, remaining_secs, dl_count = self.check_rate_limit(user_id)
                logger.info(f"Rate limit check: user={user_id}, allowed={allowed}, msg={error_msg}")
                if not allowed:
                    # Notify and send countdown message
                    await query.answer("⚠️ Download limit reached! Check your messages.", show_alert=True)
                    await self.send_rate_limit_countdown(context, user_id, dl_count, remaining_secs)
                    return
            
            try:
                decrypted = await self.encryptor.decrypt(image_data["encrypted"])
                
                caption = image_data.get("caption") or image_data["filename"]
                
                is_privileged = user_id in self.admin_ids or user_id in self.privileged_ids
                auto_delete_caption = f"🔒 Auto-delete in {self.auto_delete_seconds}s" if not is_privileged else "🔓 No auto-delete"
                
                sent_msg = await context.bot.send_photo(
                    chat_id=user_id,
                    photo=io.BytesIO(decrypted),
                    caption=f"📷 {caption}\n\n{auto_delete_caption}",
                    protect_content=self.protect_content
                )
                
                if not is_privileged:
                    message_id = sent_msg.message_id
                    chat_id = user_id
                    caption_text = caption
                    
                    halfway = self.auto_delete_seconds // 2
                    
                    async def update_countdown():
                        try:
                            await asyncio.sleep(halfway)
                            await context.bot.edit_message_caption(
                                chat_id=chat_id,
                                message_id=message_id,
                                caption=f"📷 {caption_text}\n\n🔒 Auto-delete in {self.auto_delete_seconds - halfway}s"
                            )
                        except Exception as e:
                            logger.error(f"Countdown update failed: {e}")
                    
                    async def delete_after():
                        try:
                            await asyncio.sleep(self.auto_delete_seconds)
                            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
                        except Exception as e:
                            if "Message to delete not found" not in str(e):
                                logger.error(f"Delete failed: {e}")
                    
                    asyncio.create_task(update_countdown())
                    asyncio.create_task(delete_after())
                
                await self.log(context, 
                    f"✅ Image sent\n"
                    f"User: {user_name} {user_username}\n"
                    f"ID: {user_id}\n"
                    f"Image ID: `{image_id}`\n"
                    f"File: {image_data['filename']}"
                )
            except Exception as e:
                logger.error(f"Error sending image: {e}")
                await query.answer("❌ Cannot send image", show_alert=True)
        
        elif callback_data.startswith("del_"):
            image_id = callback_data[4:]
            self.store.remove(image_id)
            await self.log(context, f"🗑️ Image deleted: {image_id}")
        
        elif callback_data == "get_image":
            pass

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.error(f"Update {update} caused error {context.error}")

    def run(self):
        application = Application.builder().token(self.bot_token).build()
        self._app = application
        
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("help", self.help_command))
        
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("setchannel", self.set_channel)],
            states={
                CHANNEL_SETTING: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.channel_received)
                ],
            },
            fallbacks=[],
        )
        application.add_handler(conv_handler)
        
        log_conv_handler = ConversationHandler(
            entry_points=[CommandHandler("setlogchannel", self.set_log_channel)],
            states={
                LOG_CHANNEL_SETTING: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.log_channel_received)
                ],
            },
            fallbacks=[],
        )
        application.add_handler(log_conv_handler)
        
        application.add_handler(CommandHandler("upload", self.upload_image))
        application.add_handler(CommandHandler("get", self.get_image))
        application.add_handler(CommandHandler("list", self.list_images))
        application.add_handler(CommandHandler("health", self.health_check))
        application.add_handler(CommandHandler("stats", self.stats_command))
        application.add_handler(CommandHandler("purge", self.purge_data))
        
        application.add_handler(MessageHandler(filters.PHOTO, self.upload_image))
        
        application.add_handler(CallbackQueryHandler(self.button_handler))
        
        application.add_error_handler(self.error_handler)
        
        async def run_bot():
            await application.initialize()
            await application.start()
            
            async def health(request):
                return web.Response(text="OK")
            
            health_app = web.Application()
            health_app.router.add_get('/health', health)
            health_app.router.add_get('/', health)
            
            runner = web.AppRunner(health_app)
            await runner.setup()
            site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8080)))
            await site.start()
            logger.info("Health server started on port 8080")
            
            await application.updater.start_polling()
            logger.info("Bot polling started")
            
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
            finally:
                await application.updater.stop()
                await application.stop()
                await application.shutdown()
        
        logger.info("Starting bot")
        asyncio.run(run_bot())


def main():
    bot = SecureImageBot()
    if not bot.bot_token:
        print("Error: BOT_TOKEN not set!")
        return
    bot.run()


if __name__ == "__main__":
    main()
