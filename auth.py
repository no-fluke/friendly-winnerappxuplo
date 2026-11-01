"""
auth.py — Auth command handlers (add/remove/list users).
"""

from pyrogram import Client, filters
from pyrogram.types import Message

from db import db
from vars import OWNER_ID, CREDIT


def owner_only(func):
    """Decorator: restrict handler to OWNER_ID."""
    async def wrapper(app: Client, m: Message):
        if m.from_user and m.from_user.id == OWNER_ID:
            return await func(app, m)
        await m.reply_text("⛔ Owner only.")
    return wrapper


def auth_required(func):
    """Decorator: restrict handler to authorized users."""
    async def wrapper(app: Client, m: Message):
        uid = m.from_user.id if m.from_user else None
        if uid and db.is_authorized(uid):
            return await func(app, m)
        await m.reply_text(
            "⛔ **Not authorized.**\n"
            f"Contact the owner to get access.\n\n**Bot by {CREDIT}**"
        )
    return wrapper


def register_auth_handlers(app: Client):

    @app.on_message(filters.command("addauth") & filters.user(OWNER_ID))
    async def add_auth(_, m: Message):
        args = m.text.split()
        if len(args) < 2 or not args[1].isdigit():
            await m.reply_text("Usage: `/addauth <user_id>`")
            return
        uid = int(args[1])
        db.add_user(uid)
        await m.reply_text(f"✅ User `{uid}` added.")

    @app.on_message(filters.command("rmauth") & filters.user(OWNER_ID))
    async def rm_auth(_, m: Message):
        args = m.text.split()
        if len(args) < 2 or not args[1].isdigit():
            await m.reply_text("Usage: `/rmauth <user_id>`")
            return
        uid = int(args[1])
        if uid == OWNER_ID:
            await m.reply_text("❌ Cannot remove the owner.")
            return
        db.remove_user(uid)
        await m.reply_text(f"✅ User `{uid}` removed.")

    @app.on_message(filters.command("users") & filters.user(OWNER_ID))
    async def list_users(_, m: Message):
        users = db.list_users()
        if not users:
            await m.reply_text("No authorized users.")
            return
        lines = "\n".join(f"• `{u}`" for u in users)
        await m.reply_text(f"**Authorized Users ({len(users)}):**\n{lines}")

    @app.on_message(filters.command("id"))
    async def get_id(_, m: Message):
        uid = m.from_user.id if m.from_user else "unknown"
        cid = m.chat.id
        await m.reply_text(
            f"👤 **Your ID:** `{uid}`\n"
            f"💬 **Chat ID:** `{cid}`"
        )
