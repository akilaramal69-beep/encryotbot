import os
import io
import base64
import uuid
import hashlib
import logging
import secrets
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List

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


class ImageStore:
    def __init__(self, connection_string: str, ttl_seconds: int = 3600):
        self._ttl = ttl_seconds
        self._client = MongoClient(connection_string)
        self._db = self._client['secure_image_bot']
        self._collection = self._db['images']
        self._collection.create_index("expire_at", expireAfterSeconds=ttl_seconds)
        logger.info("MongoDB connected successfully")

    def add(self, encrypted_data: bytes, preview_data: bytes, filename: str) -> str:
        image_id = secrets.token_hex(8)
        expire_at = datetime.utcnow() + timedelta(seconds=self._ttl)
        self._collection.insert_one({
            "_id": image_id,
            "encrypted": encrypted_data,
            "preview": preview_data,
            "filename": filename,
            "created_at": datetime.utcnow(),
            "expire_at": expire_at,
        })
        return image_id

    def get(self, image_id: str) -> Optional[dict]:
        result = self._collection.find_one({"_id": image_id})
        if result:
            return {
                "encrypted": result["encrypted"],
                "preview": result["preview"],
                "filename": result["filename"],
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

    def encrypt(self, data: bytes) -> bytes:
        nonce = os.urandom(12)
        ciphertext = self.aesgcm.encrypt(nonce, data, None)
        return nonce + ciphertext

    def decrypt(self, data: bytes) -> bytes:
        nonce = data[:12]
        ciphertext = data[12:]
        return self.aesgcm.decrypt(nonce, ciphertext, None)


def generate_key_from_password(password: str) -> bytes:
    return hashlib.sha256(password.encode()).digest()


def create_preview(image_bytes: bytes, max_size: tuple = (300, 300)) -> bytes:
    img = Image.open(io.BytesIO(image_bytes))
    img.thumbnail(max_size, Image.LANCZOS)
    blurred = img.filter(ImageFilter.GaussianBlur(radius=15))
    output = io.BytesIO()
    blurred.save(output, format="JPEG", quality=60)
    return output.getvalue()


class SecureImageBot:
    def __init__(self):
        self.bot_token = os.environ.get("BOT_TOKEN", "")
        admin_ids_str = os.environ.get("ADMIN_IDS", "")
        encryption_key_str = os.environ.get("ENCRYPTION_KEY", "")
        mongo_uri = os.environ.get("MONGO_URI", "")
        
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
        self.store = ImageStore(mongo_uri, ttl_seconds=3600)
        
        self.admin_ids = set(int(x.strip()) for x in admin_ids_str.split(",") if x.strip())
        self.channel_id = os.environ.get("CHANNEL_ID", "")
        
        self._app: Optional[Application] = None

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        args = context.args
        
        if args:
            if args[0] == f"admin_{user_id}":
                self.admin_ids.add(user_id)
                await update.message.reply_text(
                    "✅ You are now registered as admin!\n\n"
                    "Commands:\n"
                    "/setchannel - Set target channel\n"
                    "/help - Show all commands"
                )
                return
        
        keyboard = [
            [InlineKeyboardButton("📸 Get Image", callback_data="get_image")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🔒 Secure Image Bot\n\n"
            "Send /help for available commands.",
            reply_markup=reply_markup
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        is_admin = user_id in self.admin_ids
        
        admin_help = """
👨‍💼 Admin Commands:
/setchannel - Set target channel (admin only)
/upload - Upload an image to encrypt
/list - List all stored images
""" if is_admin else ""
        
        await update.message.reply_text(
            f"""
📋 Available Commands:

/start - Start the bot
/help - Show this help
/get <image_id> - Get decrypted image
/list - List available images

{admin_help}
            """.strip()
        )

    async def set_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.admin_ids:
            await update.message.reply_text("❌ Unauthorized. Admin only.")
            return
        
        await update.message.reply_text(
            "Please send the channel ID (e.g., -1001234567890)"
        )
        return CHANNEL_SETTING

    async def channel_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            channel_id = update.message.text.strip()
            if channel_id.startswith("-") and channel_id[1:].isdigit():
                self.channel_id = channel_id
                await update.message.reply_text(
                    f"✅ Channel set to: {channel_id}"
                )
            else:
                await update.message.reply_text(
                    "❌ Invalid channel ID. Use format: -1001234567890"
                )
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")
        
        return ConversationHandler.END

    async def upload_image(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        logger.info(f"Upload request from user {user_id}, admins: {self.admin_ids}")
        
        if user_id not in self.admin_ids:
            await update.message.reply_text(f"❌ Unauthorized. Your ID: {user_id} is not admin.")
            return
        
        if not update.message.photo:
            await update.message.reply_text("Please send a photo/image.")
            return
        
        if not self.channel_id:
            await update.message.reply_text("❌ Channel not set. Use /setchannel first.")
            return
        
        logger.info(f"Channel ID: {self.channel_id}, Admin IDs: {self.admin_ids}")
        
        status_msg = await update.message.reply_text("🔄 Downloading image...")
        
        try:
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id)
            image_bytes = await file.download_as_bytearray()
            logger.info(f"Downloaded image: {len(image_bytes)} bytes")
            
            await status_msg.edit_text("🔐 Encrypting image...")
            encrypted = self.encryptor.encrypt(bytes(image_bytes))
            
            await status_msg.edit_text("🖼️ Creating preview...")
            preview = create_preview(bytes(image_bytes))
            filename = f"image_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            
            await status_msg.edit_text("💾 Storing image...")
            image_id = self.store.add(encrypted, preview, filename)
            logger.info(f"Stored image with ID: {image_id}")
            
            keyboard = [
                [
                    InlineKeyboardButton("🔓 Get Original", callback_data=f"req_{image_id}"),
                    InlineKeyboardButton("📋 Copy ID", callback_data=f"copy_{image_id}"),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await status_msg.edit_text("📤 Sending to channel...")
            sent_msg = await context.bot.send_photo(
                chat_id=self.channel_id,
                photo=preview,
                caption=f"🖼️ Secure Image\n"
                        f"ID: `{image_id}`\n\n"
                        f"🔒 Tap 'Get Original' to view",
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
            logger.info(f"Sent to channel: {sent_msg.message_id}")
            
            await status_msg.edit_text(
                f"✅ Image uploaded successfully!\n\n"
                f"Image ID: `{image_id}`\n"
                f"Message ID: {sent_msg.message_id}",
                parse_mode="Markdown"
            )
             
        except Exception as e:
            logger.error(f"Error processing image: {e}", exc_info=True)
            try:
                await status_msg.edit_text(f"❌ Error: {str(e)}")
            except:
                await update.message.reply_text(f"❌ Error: {str(e)}")
              
    async def get_image(self, update: Update, context: ContextTypes.DEFAULT_TYPE, image_id: Optional[str] = None):
        if not image_id and update.message:
            image_id = " ".join(context.args)
        
        if not image_id:
            await update.message.reply_text("Usage: /get <image_id>")
            return
        
        image_id = image_id.strip()
        data = self.store.get(image_id)
        
        if not data:
            msg = "❌ Image not found or expired."
            if update.message:
                await update.message.reply_text(msg)
            return
        
        try:
            decrypted = self.encryptor.decrypt(data["encrypted"])
            
            keyboard = [
                [InlineKeyboardButton("🗑️ Delete", callback_data=f"del_{image_id}")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            if update.message:
                await update.message.reply_photo(
                    photo=io.BytesIO(decrypted),
                    caption=f"📷 {data['filename']}\n⏰ Expires in 1 hour",
                    reply_markup=reply_markup,
                )
            elif update.callback_query:
                await update.callback_query.message.reply_photo(
                    photo=io.BytesIO(decrypted),
                    caption=f"📷 {data['filename']}\n⏰ Expires in 1 hour",
                    reply_markup=reply_markup,
                )
                
        except Exception as e:
            logger.error(f"Error decrypting: {e}")
            msg = "❌ Error decrypting image."
            if update.message:
                await update.message.reply_text(msg)

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
        await query.answer()
        
        data = query.data
        
        if data.startswith("req_"):
            image_id = data[4:]
            await self.get_image(update, context, image_id)
        
        elif data.startswith("copy_"):
            image_id = data[5:]
            await query.message.reply_text(f"Image ID: `{image_id}`", parse_mode="Markdown")
        
        elif data.startswith("del_"):
            image_id = data[4:]
            self.store.remove(image_id)
            await query.message.reply_text("✅ Image deleted from memory.")
        
        elif data == "get_image":
            await query.message.reply_text("Please provide image ID: /get <image_id>")

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
        
        application.add_handler(CommandHandler("upload", self.upload_image))
        application.add_handler(CommandHandler("get", self.get_image))
        application.add_handler(CommandHandler("list", self.list_images))
        
        application.add_handler(MessageHandler(filters.PHOTO, self.upload_image))
        
        application.add_handler(CallbackQueryHandler(self.button_handler))
        
        application.add_error_handler(self.error_handler)
        
        application.run_polling(allowed_updates=Update.ALL_TYPES)


def main():
    bot = SecureImageBot()
    if not bot.bot_token:
        print("Error: BOT_TOKEN not set!")
        return
    bot.run()


if __name__ == "__main__":
    main()
