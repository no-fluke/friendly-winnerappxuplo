"""
db.py — MongoDB wrapper for AppX Bot.
Handles:
  - Auth user management
  - Job state persistence (resume-on-restart)
  - Forum topic ID cache (persistent across restarts)
"""

import time
import certifi
from datetime import datetime
from pymongo import MongoClient, errors
from vars import MONGO_URL, OWNER_ID


class Database:
    def __init__(self):
        self.client = None
        self.db     = None
        self._connect()

    def _connect(self):
        for attempt in range(1, 4):
            try:
                print(f"[DB] Connecting to MongoDB (attempt {attempt}/3)...")
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
                print("[DB] ✓ Connected!")
                self._ensure_owner()
                return
            except errors.ServerSelectionTimeoutError as e:
                print(f"[DB] ✗ Attempt {attempt} failed: {e}")
                if attempt < 3:
                    time.sleep(2)
                else:
                    raise ConnectionError("MongoDB connection failed after 3 attempts") from e

    def _ensure_owner(self):
        if OWNER_ID:
            self.db["users"].update_one(
                {"user_id": OWNER_ID},
                {"$setOnInsert": {"user_id": OWNER_ID, "added_on": datetime.utcnow(), "is_owner": True}},
                upsert=True,
            )

    # ── Auth ──────────────────────────────────────────────────────────────────

    def is_authorized(self, user_id: int) -> bool:
        doc = self.db["users"].find_one({"user_id": user_id})
        return bool(doc)

    def add_user(self, user_id: int):
        self.db["users"].update_one(
            {"user_id": user_id},
            {"$setOnInsert": {"user_id": user_id, "added_on": datetime.utcnow()}},
            upsert=True,
        )

    def remove_user(self, user_id: int):
        self.db["users"].delete_one({"user_id": user_id})

    def list_users(self) -> list:
        return [d["user_id"] for d in self.db["users"].find({}, {"user_id": 1})]

    # ── Job state (resume-on-restart) ─────────────────────────────────────────

    def save_job(self, job: dict):
        """Upsert a job document. job must have 'job_id' key."""
        self.db["jobs"].update_one(
            {"job_id": job["job_id"]},
            {"$set": job},
            upsert=True,
        )

    def get_job(self, job_id: str) -> dict | None:
        return self.db["jobs"].find_one({"job_id": job_id})

    def get_pending_jobs(self) -> list:
        """Return all jobs that are not completed."""
        return list(self.db["jobs"].find({"status": {"$in": ["pending", "running"]}}))

    def mark_item_done(self, job_id: str, item_index: int):
        self.db["jobs"].update_one(
            {"job_id": job_id},
            {"$set": {f"progress.{item_index}": "done"}},
        )

    def update_job_status(self, job_id: str, status: str):
        self.db["jobs"].update_one(
            {"job_id": job_id},
            {"$set": {"status": status, "updated_at": datetime.utcnow()}},
        )

    def delete_job(self, job_id: str):
        self.db["jobs"].delete_one({"job_id": job_id})

    # ── Forum topic cache ────────────────────────────────────────────────────

    def get_topic_id(self, job_id: str, topic_name: str) -> int | None:
        doc = self.db["topics"].find_one({"job_id": job_id, "topic_name": topic_name})
        return doc["topic_id"] if doc else None

    def set_topic_id(self, job_id: str, topic_name: str, topic_id: int):
        self.db["topics"].update_one(
            {"job_id": job_id, "topic_name": topic_name},
            {"$set": {"topic_id": topic_id}},
            upsert=True,
        )


db = Database()
