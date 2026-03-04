# Secure Image Bot

A Telegram bot that encrypts images sent by admin, stores them in MongoDB, sends pixelated previews to a channel, and serves decrypted images to users with auto-delete countdown.

## Features

- AES-256-GCM encryption for images
- Pixelated preview in channel
- Download protection (configurable via env)
- Dynamic countdown timer (configurable via env)
- Auto-delete after X seconds (configurable via env)
- MongoDB storage (permanent)
- Health check endpoint for Koyeb
- Auto-purge options on deployment (images, limits, users)
- Admin log channel

## Setup

### 1. Create Telegram Bot

1. Message @BotFather on Telegram
2. Send `/newbot` and follow instructions
3. Copy your bot token

### 2. Get Your User ID

1. Message @userinfobot on Telegram
2. Copy your user ID

### 3. Deploy to Koyeb

1. Push to GitHub
2. Create app on Koyeb
3. Configure environment variables
4. Deploy

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | Yes | Telegram bot token |
| `ADMIN_IDS` | Yes | Comma-separated admin user IDs |
| `ENCRYPTION_KEY` | No | 32-byte key for AES-256-GCM (auto-generated if missing) |
| `PRIVILEGED_IDS` | No | Comma-separated privileged user IDs (unlimited downloads) |
| `CHANNEL_ID` | Yes | Target channel ID (e.g., -1001234567890) |
| `LOG_CHANNEL_ID` | No | Admin log channel ID |
| `MONGO_URI` | Yes | MongoDB connection string |
| `PROTECT_CONTENT` | No | `true`/`false` - prevents saving/forwarding |
| `RATE_LIMIT_ENABLED` | No | `true`/`false` - enable rate limiting |
| `RATE_LIMIT_COUNT` | No | Images per hour (default 10) |
| `RATE_LIMIT_WINDOW` | No | Time window in seconds (default 3600) |
| `AUTO_DELETE_SECONDS` | No | Time before image is deleted (default 60) |
| `PURGE_ON_START` | No | Set `true` to purge all images on deploy |
| `PURGE_LIMITS_ON_START` | No | Set `true` to purge all rate limit records on deploy |
| `PURGE_USERS_ON_START` | No | Set `true` to purge all user records on deploy |
| `PORT` | No | Port for health check (default 8080) |

## Usage

### Admin Commands

1. `/start admin_<your_user_id>` - Register as admin
2. `/setchannel` - Set target channel
3. `/setlogchannel` - Set log channel
4. Send photo with caption - Upload image
5. `/list` - List images
6. `/purge [images|limits|users|all]` - Delete data selectively
7. `/health` - Check bot health

### User Commands

- `/start` - Start the bot
- `/get <image_id>` - Get decrypted image

## How It Works

1. **Admin uploads image** → Bot encrypts → Stores in MongoDB
2. **Bot sends pixelated preview** to channel with "Get Original" button
3. **User clicks "Get Original"** → Bot checks channel membership
4. **If member & started bot** → Sends image to user's DM
5. **Dynamic countdown** updates automatically based on `AUTO_DELETE_SECONDS`
6. **Auto-delete** after configured seconds

## Docker Build

```bash
docker build -t secure-image-bot .
docker run -e BOT_TOKEN="..." -e ADMIN_IDS="..." -e CHANNEL_ID="..." -e MONGO_URI="..." secure-image-bot
```
