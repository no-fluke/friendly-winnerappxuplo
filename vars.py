"""
vars.py — All configuration read from environment variables.
Bot uses Pyrogram (api_id + api_hash + bot_token) — no StringSession needed.
Bot is admin in the supergroup and uploads directly via MTProto.
"""
import os
from os import environ

API_ID    = int(environ.get("API_ID", "0"))
API_HASH  = environ.get("API_HASH", "")
BOT_TOKEN = environ.get("BOT_TOKEN", "")

OWNER_ID      = int(environ.get("OWNER_ID", "0"))
SUPERGROUP_ID = int(environ.get("SUPERGROUP_ID", "0"))
CREDIT        = environ.get("CREDIT", "AppX Bot")

# MongoDB — required for job persistence & resume-on-restart
MONGO_URL = environ.get("MONGO_URL", "")

# Download temp dir on Heroku dyno
DOWNLOAD_DIR = environ.get("DOWNLOAD_DIR", "/tmp/appx_downloads")

# Comma-separated extra user IDs allowed to use the bot
_auth = environ.get("AUTH_USERS", str(OWNER_ID)).split(",")
AUTH_USERS = list({int(u.strip()) for u in _auth if u.strip().isdigit()})
if OWNER_ID and OWNER_ID not in AUTH_USERS:
    AUTH_USERS.append(OWNER_ID)
