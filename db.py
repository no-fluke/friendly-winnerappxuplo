"""
db.py — MongoDB wrapper for AppX Bot.
"""
import time
import certifi
import colorama
from colorama import Fore, Style
from datetime import datetime, timedelta
from typing import Optional
from pymongo import MongoClient, errors
from vars import MONGO_URL, OWNER_ID

colorama.init()


class Database:
    def __init__(self, max_retries: int = 3, retry_delay: float = 2.0):
        self.client = None
        self.db     = None
        self._connect(max_retries, retry_delay)

    def _connect(self, max_retries, retry_delay):
        for attempt in range(1, max_retries + 1):
            try:
                print(f"{Fore.YELLOW}⌛ Attempt {attempt}/{max_retries}: Connecting to MongoDB...{Style.RESET_ALL}")
                self.client = MongoClient(
                    MONGO_URL,
                    serverSelectionTimeoutMS=20000,
                    connectTimeoutMS=20000,
                    socketTimeoutMS=30000,
                    tlsCAFile=certifi.where(),
                    retryWrites=True,
                    retryReads=True,
                )
                self.client.server_info()
                self.db = self.client["appx_bot"]
                print(f"{Fore.GREEN}✓ MongoDB Connected!{Style.RESET_ALL}")
                self._ensure_owner()
                return
            except errors.ServerSelectionTimeoutError as e:
                print(f"{Fore.RED}✕ Attempt {attempt} failed: {e}{Style.RESET_ALL}")
                if attempt < max_retries:
                    time.sleep(retry_delay)
                else:
                    raise ConnectionError(f"MongoDB connection failed after {max_retries} attempts") from e

    def _ensure_owner(self):
        if OWNER_ID:
            self.db["users"].update_one(
                {"user_id": OWNER_ID},
                {"$setOnInsert": {
                    "user_id": OWNER_ID, "name": "Owner",
                    "added_on": datetime.utcnow(), "is_owner": True,
                    "expiry_date": datetime(2099, 12, 31),
                }},
                upsert=True,
            )

    def is_authorized(self, user_id: int) -> bool:
        if user_id == OWNER_ID:
            return True
        doc = self.db["users"].find_one({"user_id": user_id})
        if not doc:
            return False
        expiry = doc.get("expiry_date")
        if expiry and expiry < datetime.utcnow():
            return False
        return True

    def add_user(self, user_id: int, name: str = "", days: int = 36500):
        try:
            expiry = datetime.utcnow() + timedelta(days=days)
            self.db["users"].update_one(
                {"user_id": user_id},
                {"$set": {
                    "user_id": user_id, "name": name or f"User {user_id}",
                    "expiry_date": expiry, "added_on": datetime.utcnow(), "days": days,
                }},
                upsert=True,
            )
            return True, expiry
        except Exception as e:
            print(f"{Fore.RED}Add user error: {e}{Style.RESET_ALL}")
            return False, None

    def remove_user(self, user_id: int) -> bool:
        result = self.db["users"].delete_one({"user_id": user_id})
        return result.deleted_count > 0

    def list_users(self) -> list:
        return list(self.db["users"].find(
            {"user_id": {"$ne": OWNER_ID}},
            {"_id": 0, "user_id": 1, "name": 1, "expiry_date": 1}
        ))

    def save_job(self, job: dict):
        self.db["jobs"].update_one({"job_id": job["job_id"]}, {"$set": job}, upsert=True)

    def get_job(self, job_id: str) -> Optional[dict]:
        return self.db["jobs"].find_one({"job_id": job_id})

    def get_pending_jobs(self) -> list:
        return list(self.db["jobs"].find({"status": {"$in": ["pending", "running"]}}))

    def mark_item_done(self, job_id: str, item_index: int):
        self.db["jobs"].update_one({"job_id": job_id}, {"$set": {f"progress.{item_index}": "done"}})

    def update_job_status(self, job_id: str, status: str):
        self.db["jobs"].update_one({"job_id": job_id},
            {"$set": {"status": status, "updated_at": datetime.utcnow()}})

    def delete_job(self, job_id: str):
        self.db["jobs"].delete_one({"job_id": job_id})

    def get_topic_id(self, job_id: str, topic_name: str) -> Optional[int]:
        doc = self.db["topics"].find_one({"job_id": job_id, "topic_name": topic_name})
        return doc["topic_id"] if doc else None

    def set_topic_id(self, job_id: str, topic_name: str, topic_id: int):
        self.db["topics"].update_one(
            {"job_id": job_id, "topic_name": topic_name},
            {"$set": {"topic_id": topic_id}}, upsert=True)


db = Database(max_retries=3, retry_delay=2)
