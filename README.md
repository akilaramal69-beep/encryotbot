# Secure Image Bot

A Telegram bot that encrypts images sent by admin, stores them in MongoDB, sends mosaic previews to a channel, and serves decrypted view-once images to users.

## Features

- AES-256-GCM encryption for images
- Mosaic/pixelated preview in channel
- Download protection (configurable)
- Dynamic countdown timer (60s → 1s)
- Auto-delete after 60 seconds
- MongoDB storage (permanent)
- Health check endpoint for Koyeb
- Auto-purge on deployment option
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
3. Configure environment variables:
   - `BOT_TOKEN`: Your Telegram bot token
   - `ADMIN_IDS`: Your user ID
   - `CHANNEL_ID`: Target channel ID (e.g., -1001234567890)
   - `LOG_CHANNEL_ID`: Admin log channel (optional)
   - `MONGO_URI`: MongoDB connection string
   - `PURGE_ON_START`: true (optional, clears all images on deploy)
4. Deploy

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | Yes | Telegram bot token |
| `ADMIN_IDS` | Yes | Comma-separated admin user IDs |
| `CHANNEL_ID` | Yes | Target channel ID |
| `LOG_CHANNEL_ID` | No | Admin log channel ID |
| `MONGO_URI` | Yes | MongoDB connection string |
| `PROTECT_CONTENT` | No | `true` or `false` - prevents saving/forwarding |
| `PURGE_ON_START` | No | Set `true` to purge all images on deploy |
| `PORT` | No | Port for health check (default 8080) |

## Usage

### Admin Commands

1. **Register as admin**: `/start admin_<your_user_id>`
2. **Set channel**: `/setchannel` → send channel ID
3. **Set log channel**: `/setlogchannel` → send channel ID
4. **Upload image**: Send photo with caption
5. **List images**: `/list`
6. **Purge all**: `/purge`

### User Commands

- `/start` - Start the bot
- `/get <image_id>` - Get decrypted image

## How It Works

1. **Admin uploads image** → Bot encrypts with AES-256-GCM → Stores in MongoDB
2. **Bot sends mosaic preview** to channel with inline button
3. **User clicks "Get Original"** → Bot checks channel membership
4. **If member & started bot** → Sends view-once image to user's DM
5. **Auto-delete** after 60 minutes

## Docker Build

```bash
docker build -t secure-image-bot .
docker run -e BOT_TOKEN="..." -e ADMIN_IDS="..." -e CHANNEL_ID="..." -e MONGO_URI="..." secure-image-bot
```
