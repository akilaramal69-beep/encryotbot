# Secure Image Bot - Specification

## Project Overview
- **Project Name**: Secure Image Bot
- **Type**: Telegram Bot with Image Encryption
- **Core Functionality**: Encrypt images sent by admin, store briefly, send encrypted preview to channel, decrypt on user request
- **Target Users**: Privacy-conscious channel admins and users

## Functionality Specification

### Core Features

1. **Admin Management**
   - Admin authenticates using `/start` command with admin token
   - Admin can set target channel with `/setchannel`
   - Admin can upload images for encryption

2. **Image Encryption**
   - AES-256-GCM encryption for images
   - Generate encrypted preview (low quality version)
   - Store encrypted data briefly (TTL: 1 hour)
   - Each image gets unique ID for retrieval

3. **Channel Integration**
   - Send encrypted preview to assigned channel
   - Include unique image ID in message
   - Preview shows blurred/low-res version

4. **User Image Request**
   - Channel users request via inline button or command `/get <image_id>`
   - Bot decrypts and sends original image
   - Auto-delete after serving (ephemeral)

### Data Flow
1. Admin sends image → Bot encrypts → Stores encrypted + preview → Sends preview to channel
2. User requests image → Bot fetches by ID → Decrypts → Serves to user → Deletes from memory

### Storage
- In-memory dictionary (ephemeral, cleared on restart)
- TTL-based auto-expiration (1 hour)
- No persistent database

## Technical Stack
- Python 3.11
- python-telegram-bot
- cryptography (AES-256-GCM)
- Pillow (image processing)
- Docker + Koyeb deployment

## Environment Variables
- `BOT_TOKEN`: Telegram bot token
- `ADMIN_IDS`: Comma-separated admin user IDs
- `ENCRYPTION_KEY`: 32-byte encryption key (base64 encoded)

## API Commands
- `/start` - Start bot, register as admin if token matches
- `/setchannel` - Set target channel (admin only)
- `/get <id>` - Get decrypted image by ID
- `/list` - List available images
- `/help` - Show help

## Acceptance Criteria
1. Admin can authenticate and set channel
2. Images are encrypted before storage
3. Encrypted preview sent to channel
4. Users can request and receive decrypted images
5. Data auto-expires after 1 hour
6. Docker container runs successfully
7. Koyeb deployment works
