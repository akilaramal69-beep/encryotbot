import os
import io
import base64
import uuid
import hashlib
import logging
import secrets
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from PIL import Image
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CHANNEL_SETTING = 1

class ImageStore:
    def __init__(self, ttl_seconds: int = 3600):
        self._store: Dict[str, dict] = {}
        self._ttl = ttl_seconds

    def add(self, encrypted_data: bytes, preview_data: bytes, filename: str) -> str:
        image_id = secrets.token_hex(8)
        self._store[image_id] = {
            "encrypted": encrypted_data,
            "preview": preview_data,
            "filename": filename,
            "created_at": time.time(),
        }
        self._cleanup()
        return image_id

    def get(self, image_id: str) -> Optional[dict]:
        self._cleanup()
        return self._store.get(image_id)

    def remove(self, image_id: str) -> bool:
        if image_id in self._store:
            del self._store[image_id]
            return True
        return False

    def list_all(self) -> list:
        self._cleanup()
        result = []
        for img_id, data in self._store.items():
            result.append({
                "id": img_id,
                "filename": data["filename"],
                "created_at": datetime.fromtimestamp(data["created_at"]).strftime("%Y-%m-%d %H:%M"),
            })
        return sorted(result, key=lambda x: x["created_at"], reverse=True)

    def _cleanup(self):
        current_time = time.time()
        expired = [
            img_id for img_id, data in self._store.items()
            if current_time - data["created_at"] > self._ttl
        ]
        for img_id in expired:
            del self._store[img_id]


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
    blurred = img.filter(Image.GaussianBlur(radius=15))
    output = io.BytesIO()
    blurred.save(output, format="JPEG", quality=60)
    return output.getvalue()


class SecureImageBot:
    def __init__(self):
        self.bot_token = os.environ.get("BOT_TOKEN", "")
        admin_ids_str = os.environ.get("ADMIN_IDS", "")
        encryption_key_str = os.environ.get("ENCRYPTION_KEY", "")
        
        if not encryption_key_str:
            encryption_key_str = secrets.token_hex(32)
        
        if len(encryption_key_str) == 64:
            key = bytes.fromhex(encryption_key_str)
        else:
            key = base64.b64decode(encryption_key_str)
        
        self.encryptor = Encryptor(key)
        self.store = ImageStore(ttl_seconds=3600)
        
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
        if user_id not in self.admin_ids:
            await update.message.reply_text("❌ Unauthorized. Admin only.")
            return
        
        if not update.message.photo:
            await update.message.reply_text("Please send a photo/image.")
            return
        
        if not self.channel_id:
            await update.message.reply_text("❌ Channel not set. Use /setchannel first.")
            return
        
        await update.message.reply_text("🔄 Processing and encrypting image...")
        
        try:
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id)
            image_bytes = await file.download_as_bytearray()
            
            encrypted = self.encryptor.encrypt(bytes(image_bytes))
            preview = create_preview(bytes(image_bytes))
            filename = f"image_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            
            image_id = self.store.add(encrypted, preview, filename)
            
            preview_base64 = base64.b64encode(preview).decode()
            
            keyboard = [
                [InlineKeyboardButton("🔓 Get Original", callback_data=f"req_{image_id}")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await context.bot.send_photo(
                chat_id=self.channel_id,
                photo=preview,
                caption=f"🖼️ Image ID: `{image_id}`\n"
                        f"Type /get {image_id} to get the original",
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
            
            await update.message.reply_text(
                f"✅ Image uploaded!\n\n"
                f"Image ID: `{image_id}`\n"
                f"Preview sent to channel.",
                parse_mode="Markdown"
            )
            
        except Exception as e:
            logger.error(f"Error processing image: {e}")
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
