"""
db.py — MongoDB database wrapper for AppX Downloader Bot.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from pymongo import MongoClient

from vars import MONGO_URL

log = logging.getLogger(__name__)


class Database:
    def __init__(self, mongo_url: str):
        self.client = MongoClient(mongo_url)
        self.db = self.client["appx_bot"]
        self._users = self.db["users"]
        self._jobs  = self.db["jobs"]
        log.info("MongoDB connected.")

    # ── Auth ──────────────────────────────────────────────────────────────────

    def is_authorized(self, user_id: int) -> bool:
        doc = self._users.find_one({"user_id": user_id})
        if not doc:
            return False
        expiry = doc.get("expiry_date")
        if expiry and expiry < datetime.utcnow():
            return False
        return True

    def add_user(self, user_id: int, name: str, days: int = 36500):
        expiry = datetime.utcnow() + timedelta(days=days)
        result = self._users.update_one(
            {"user_id": user_id},
            {"$set": {"user_id": user_id, "name": name, "expiry_date": expiry}},
            upsert=True,
        )
        return result.acknowledged, expiry

    def remove_user(self, user_id: int) -> bool:
        result = self._users.delete_one({"user_id": user_id})
        return result.deleted_count > 0

    def list_users(self) -> list:
        return list(self._users.find({}, {"_id": 0}))

    # ── Jobs ──────────────────────────────────────────────────────────────────

    def save_job(self, job: dict):
        self._jobs.update_one(
            {"job_id": job["job_id"]},
            {"$set": job},
            upsert=True,
        )

    def get_job(self, job_id: str) -> Optional[dict]:
        return self._jobs.find_one({"job_id": job_id}, {"_id": 0})

    def get_pending_jobs(self) -> list:
        return list(self._jobs.find({"status": "running"}, {"_id": 0}))

    def delete_job(self, job_id: str):
        self._jobs.delete_one({"job_id": job_id})

    def update_job_status(self, job_id: str, status: str):
        self._jobs.update_one({"job_id": job_id}, {"$set": {"status": status}})

    def mark_item_done(self, job_id: str, idx: int):
        self._jobs.update_one(
            {"job_id": job_id},
            {"$set": {f"progress.{idx}": "done"}},
        )


database = Database(MONGO_URL)
