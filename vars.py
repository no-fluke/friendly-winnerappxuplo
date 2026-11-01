"""
vars.py — All configuration read from environment variables.
"""
import os
from os import environ

API_ID    = int(environ.get("API_ID", "0"))
API_HASH  = environ.get("API_HASH", "")
BOT_TOKEN = environ.get("BOT_TOKEN", "")

OWNER_ID      = int(environ.get("OWNER_ID", "0"))
SUPERGROUP_ID = int(environ.get("SUPERGROUP_ID", "0"))
CREDIT        = environ.get("CREDIT", "AppX Bot")
MONGO_URL     = environ.get("MONGO_URL", "")
DOWNLOAD_DIR  = environ.get("DOWNLOAD_DIR", "/tmp/appx_downloads")
