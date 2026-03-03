# Secure Image Bot

A Telegram bot that encrypts images sent by admin, stores them briefly (ephemeral storage), sends encrypted previews to a channel, and serves decrypted images on request.

## Features

- AES-256-GCM encryption for images
- Ephemeral storage with 1-hour TTL
- Blurred preview sent to channel
- Users request images via ID
- No persistent database - data auto-expires

## Setup

### 1. Create Telegram Bot

1. Message @BotFather on Telegram
2. Send `/newbot` and follow instructions
3. Copy your bot token

### 2. Get Your User ID

1. Message @userinfobot on Telegram
2. Copy your user ID

### 3. Deploy to Koyeb

#### Option A: Deploy from GitHub

1. Push this code to a GitHub repository
2. Sign up at [koyeb.com](https://koyeb.com)
3. Create a new app → select "GitHub"
4. Select your repository
5. Configure environment variables:
   - `BOT_TOKEN`: Your Telegram bot token
   - `ADMIN_IDS`: Your user ID (e.g., `123456789`)
   - `CHANNEL_ID`: Target channel ID (e.g., `-1001234567890`)
   - `ENCRYPTION_KEY`: 64-character hex key (optional, auto-generated)
6. Deploy

#### Option B: Deploy from Docker Hub

1. Build and push to Docker Hub:
   ```bash
   docker build -t yourusername/secure-image-bot .
   docker push yourusername/secure-image-bot
   ```
2. On Koyeb, create app → select "Docker"
3. Enter your Docker image URL
4. Configure same environment variables
5. Deploy

## Usage

### Admin Commands

1. **Register as admin**: Send `/start admin_<your_user_id>`
2. **Set channel**: `/setchannel` then send channel ID
3. **Upload image**: Send `/upload` or just send a photo (if admin)
4. **List images**: `/list`

### User Commands

- `/start` - Start the bot
- `/help` - Show help
- `/get <image_id>` - Get decrypted image
- `/list` - List available images

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | Yes | Telegram bot token |
| `ADMIN_IDS` | Yes | Comma-separated admin user IDs |
| `CHANNEL_ID` | Yes | Target channel ID (e.g., -1001234567890) |
| `LOG_CHANNEL_ID` | No | Admin log channel ID |
| `MONGO_URI` | Yes | MongoDB connection string |
| `ENCRYPTION_KEY` | No | 64-char hex or base64 key (auto-generated if not set) |

## How It Works

1. **Admin uploads image** → Bot encrypts with AES-256-GCM → Stores encrypted data in memory
2. **Bot sends blurred preview** to channel with unique image ID
3. **User requests image** via `/get <id>` → Bot decrypts and sends original
4. **After 1 hour** → Image automatically deleted from memory

## Local Development

```bash
# Clone and setup
pip install -r requirements.txt

# Run locally
export BOT_TOKEN="your_token"
export ADMIN_IDS="123456789"
export CHANNEL_ID="-1001234567890"
python app.py
```

## Docker Build

```bash
docker build -t secure-image-bot .
docker run -e BOT_TOKEN="..." -e ADMIN_IDS="..." -e CHANNEL_ID="..." secure-image-bot
```

## Security Notes

- Encryption key is auto-generated if not provided
- Images stored only in memory (lost on restart/timeout)
- 1-hour TTL on all stored images
- No logs of decrypted image content
