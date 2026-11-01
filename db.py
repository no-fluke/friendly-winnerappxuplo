"""
db.py — MongoDB database wrapper for AppX Downloader Bot.
"""

import time
import certifi
import colorama
from colorama import Fore, Style
from datetime import datetime, timedelta
from typing import Optional, List
from pymongo import MongoClient, errors

from vars import MONGO_URL, OWNER_ID

colorama.init()


class Database:
    def __init__(self, max_retries: int = 3, retry_delay: float = 2.0):
        self.client = None
        self.db = None
        self._users = None
        self._jobs = None
        self._connect(max_retries, retry_delay)

    def _connect(self, max_retries: int, retry_delay: float):
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
                self.client.server_info()  # test connection

                self.db = self.client["appx_bot"]
                self._users = self.db["users"]
                self._jobs = self.db["jobs"]

                print(f"{Fore.GREEN}✓ MongoDB Connected!{Style.RESET_ALL}")
                self._init_indexes()
                return

            except errors.ServerSelectionTimeoutError as e:
                print(f"{Fore.RED}✕ Attempt {attempt} failed: {e}{Style.RESET_ALL}")
                if attempt < max_retries:
                    time.sleep(retry_delay)
                else:
                    raise ConnectionError(f"MongoDB connection failed after {max_retries} attempts") from e
            except Exception as e:
                print(f"{Fore.RED}✕ Unexpected error: {e}{Style.RESET_ALL}")
                raise

    def _init_indexes(self):
        try:
            self._users.create_index("user_id", unique=True, name="user_id_idx")
            self._jobs.create_index("job_id", unique=True, name="job_id_idx")
            print(f"{Fore.GREEN}✓ Indexes ready!{Style.RESET_ALL}")
        except Exception as e:
            print(f"{Fore.YELLOW}⚠ Index warning: {e}{Style.RESET_ALL}")

    # ── Auth ──────────────────────────────────────────────────────────────────

    def is_authorized(self, user_id: int) -> bool:
        if user_id == OWNER_ID:
            return True
        try:
            doc = self._users.find_one({"user_id": user_id})
            if not doc:
                return False
            expiry = doc.get("expiry_date")
            if expiry and expiry < datetime.utcnow():
                return False
            return True
        except Exception as e:
            print(f"{Fore.RED}Auth check error: {e}{Style.RESET_ALL}")
            return False

    def add_user(self, user_id: int, name: str, days: int = 36500):
        try:
            expiry = datetime.utcnow() + timedelta(days=days)
            result = self._users.update_one(
                {"user_id": user_id},
                {"$set": {
                    "user_id": user_id,
                    "name": name,
                    "expiry_date": expiry,
                    "added_date": datetime.utcnow(),
                    "days": days,
                }},
                upsert=True,
            )
            return result.acknowledged, expiry
        except Exception as e:
            print(f"{Fore.RED}Add user error: {e}{Style.RESET_ALL}")
            return False, None

    def remove_user(self, user_id: int) -> bool:
        try:
            result = self._users.delete_one({"user_id": user_id})
            return result.deleted_count > 0
        except Exception as e:
            print(f"{Fore.RED}Remove user error: {e}{Style.RESET_ALL}")
            return False

    def list_users(self) -> List[dict]:
        try:
            return list(self._users.find({}, {"_id": 0}))
        except Exception as e:
            print(f"{Fore.RED}List users error: {e}{Style.RESET_ALL}")
            return []

    # ── Jobs ──────────────────────────────────────────────────────────────────

    def save_job(self, job: dict):
        try:
            self._jobs.update_one(
                {"job_id": job["job_id"]},
                {"$set": job},
                upsert=True,
            )
        except Exception as e:
            print(f"{Fore.RED}Save job error: {e}{Style.RESET_ALL}")

    def get_job(self, job_id: str) -> Optional[dict]:
        try:
            return self._jobs.find_one({"job_id": job_id}, {"_id": 0})
        except Exception as e:
            print(f"{Fore.RED}Get job error: {e}{Style.RESET_ALL}")
            return None

    def get_pending_jobs(self) -> List[dict]:
        try:
            return list(self._jobs.find({"status": "running"}, {"_id": 0}))
        except Exception as e:
            print(f"{Fore.RED}Get pending jobs error: {e}{Style.RESET_ALL}")
            return []

    def delete_job(self, job_id: str):
        try:
            self._jobs.delete_one({"job_id": job_id})
        except Exception as e:
            print(f"{Fore.RED}Delete job error: {e}{Style.RESET_ALL}")

    def update_job_status(self, job_id: str, status: str):
        try:
            self._jobs.update_one({"job_id": job_id}, {"$set": {"status": status}})
        except Exception as e:
            print(f"{Fore.RED}Update job status error: {e}{Style.RESET_ALL}")

    def mark_item_done(self, job_id: str, idx: int):
        try:
            self._jobs.update_one(
                {"job_id": job_id},
                {"$set": {f"progress.{idx}": "done"}},
            )
        except Exception as e:
            print(f"{Fore.RED}Mark item done error: {e}{Style.RESET_ALL}")

    def close(self):
        if self.client:
            self.client.close()
            print(f"{Fore.YELLOW}✓ MongoDB connection closed{Style.RESET_ALL}")


# ── Global singleton ──────────────────────────────────────────────────────────
try:
    database = Database(max_retries=3, retry_delay=2)
except Exception as e:
    print(f"{Fore.RED}✕ FATAL: DB init failed → {e}{Style.RESET_ALL}")
    raise
