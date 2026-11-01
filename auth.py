"""
auth.py — Auth command handlers.
"""
from pyrogram import Client, filters
from pyrogram.types import Message
from datetime import datetime

from vars import OWNER_ID, CREDIT


def check_auth():
    def decorator(func):
        async def wrapper(client: Client, message: Message, *args, **kwargs):
            from db import db
            if not db.is_authorized(message.from_user.id if message.from_user else 0):
                await message.reply_text(
                    "**❌ Access Denied!**\n\n"
                    "<blockquote>You are not authorized.\n"
                    "Ask the owner to add you with /addauth.</blockquote>\n\n"
                    f"Your ID: `{message.from_user.id if message.from_user else 'unknown'}`"
                )
                return
            return await func(client, message, *args, **kwargs)
        return wrapper
    return decorator


async def add_auth_cmd(client: Client, message: Message):
    from db import db
    if not message.from_user or message.from_user.id != OWNER_ID:
        await message.reply_text("⛔ Owner only.")
        return
    args = message.command[1:]
    if len(args) < 1:
        await message.reply_text("**Usage:** `/addauth <user_id> [days]`\nExample: `/addauth 123456789 30`")
        return
    try:
        new_user_id = int(args[0])
        days = int(args[1]) if len(args) >= 2 else 36500
    except ValueError:
        await message.reply_text("❌ user_id and days must be numbers.")
        return
    try:
        user = await client.get_users(new_user_id)
        name = user.first_name + (f" {user.last_name}" if user.last_name else "")
    except Exception:
        name = f"User {new_user_id}"
    success, expiry = db.add_user(new_user_id, name, days)
    if success:
        expiry_str = expiry.strftime("%d-%m-%Y %H:%M:%S")
        await message.reply_text(
            f"**✅ User Added!**\n\n"
            f"<blockquote>👤 Name: {name}\n🆔 ID: `{new_user_id}`\n"
            f"⏳ Days: {days}\n📅 Expiry: {expiry_str}</blockquote>"
        )
        try:
            await client.send_message(new_user_id,
                f"**🎉 Access Granted!**\n\n<blockquote>You have been authorized.\n"
                f"Expires on: **{expiry_str}**\n\nType /start to begin!</blockquote>")
        except Exception:
            pass
    else:
        await message.reply_text("❌ Failed to add user.")


async def rm_auth_cmd(client: Client, message: Message):
    from db import db
    if not message.from_user or message.from_user.id != OWNER_ID:
        await message.reply_text("⛔ Owner only.")
        return
    args = message.command[1:]
    if len(args) < 1:
        await message.reply_text("**Usage:** `/rmauth <user_id>`")
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await message.reply_text("❌ Invalid user ID.")
        return
    if user_id == OWNER_ID:
        await message.reply_text("❌ Cannot remove the owner.")
        return
    if db.remove_user(user_id):
        await message.reply_text(f"✅ User `{user_id}` removed.")
        try:
            await client.send_message(user_id, "**⚠️ Subscription Ended**\n\nYour access has been revoked.")
        except Exception:
            pass
    else:
        await message.reply_text(f"❌ User `{user_id}` not found.")


async def list_users_cmd(client: Client, message: Message):
    from db import db
    if not message.from_user or message.from_user.id != OWNER_ID:
        await message.reply_text("⛔ Owner only.")
        return
    users = db.list_users()
    if not users:
        await message.reply_text("📝 No authorized users found.")
        return
    text = "**📝 Authorized Users List**\n\n"
    for u in users:
        expiry = u.get("expiry_date")
        if isinstance(expiry, str):
            expiry = datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S")
        days_left = (expiry - datetime.utcnow()).days if expiry else 0
        status = "✅ Active" if days_left > 0 else "❌ Expired"
        text += (f"• **{u.get('name', 'Unknown')}**\n  ID: `{u['user_id']}`\n"
                 f"  Expires: {expiry.strftime('%d-%m-%Y') if expiry else 'N/A'} ({days_left}d) {status}\n──────\n")
    if len(text) > 4000:
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            await message.reply_text(chunk)
    else:
        await message.reply_text(text)


async def my_plan_cmd(client: Client, message: Message):
    from db import db
    if not message.from_user:
        return
    uid = message.from_user.id
    if uid == OWNER_ID:
        await message.reply_text("**👑 You are the Owner — Unlimited Access!**")
        return
    doc = db.db["users"].find_one({"user_id": uid})
    if not doc:
        await message.reply_text(
            f"**❌ No Active Plan**\n\n<blockquote>You don't have a subscription.\n"
            f"Contact admin to get access.</blockquote>\n\nYour ID: `{uid}`")
        return
    expiry = doc.get("expiry_date")
    days_left = (expiry - datetime.utcnow()).days if expiry else 0
    status = "✅ Active" if days_left > 0 else "❌ Expired"
    await message.reply_text(
        f"**📱 Your Plan**\n\n<blockquote>👤 Name: {doc.get('name', 'Unknown')}\n"
        f"🆔 ID: `{uid}`\n📅 Expires: {expiry.strftime('%d-%m-%Y') if expiry else 'N/A'}\n"
        f"⏳ Days Left: {days_left}\n🔰 Status: {status}</blockquote>")


def register_auth_handlers(app: Client):
    app.on_message(filters.command("addauth") & filters.private)(add_auth_cmd)
    app.on_message(filters.command("rmauth")  & filters.private)(rm_auth_cmd)
    app.on_message(filters.command("users")   & filters.private)(list_users_cmd)
    app.on_message(filters.command("plan")    & filters.private)(my_plan_cmd)
