"""SQLite WAL-backed at-least-once MQTT store-and-forward queue."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt


LOGGER = logging.getLogger("offline_buffer")
DEFAULT_MAX_BYTES = 500 * 1024 * 1024
DEFAULT_BATCH_SIZE = 50


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class OfflineBuffer:
    """Persist all outbound messages before asynchronous MQTT delivery."""

    def __init__(
        self,
        path: str | Path = "edge_buffer.db",
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        batch_size: int = DEFAULT_BATCH_SIZE,
        publish_timeout: float = 5.0,
        retry_interval: float = 1.0,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.batch_size = min(batch_size, DEFAULT_BATCH_SIZE)
        self.publish_timeout = publish_timeout
        self.retry_interval = retry_interval
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._connection = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS outbound_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    retain INTEGER NOT NULL DEFAULT 0,
                    synced INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbound_pending ON outbound_messages(synced, id)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbound_timestamp ON outbound_messages(timestamp_utc, id)"
            )
            self._connection.commit()
            self._connection.execute(f"PRAGMA journal_size_limit={self.max_bytes}")

    def enqueue(
        self,
        topic: str,
        payload: str | bytes | dict[str, Any] | list[Any],
        *,
        retain: bool = False,
        timestamp_utc: str | None = None,
    ) -> int:
        if not topic or "\0" in topic or "+" in topic or "#" in topic:
            raise ValueError("topic must be a concrete MQTT publish topic")
        if isinstance(payload, bytes):
            payload_text = payload.decode("utf-8")
        elif isinstance(payload, str):
            payload_text = payload
        else:
            payload_text = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        estimated_bytes = len(topic.encode("utf-8")) + len(payload_text.encode("utf-8")) + 256
        if estimated_bytes > self.max_bytes:
            raise ValueError("message is larger than the configured offline buffer")
        timestamp = timestamp_utc or utc_timestamp()
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp_utc must be an ISO-8601 timestamp") from exc
        if parsed_timestamp.tzinfo is None:
            parsed_timestamp = parsed_timestamp.replace(tzinfo=timezone.utc)
        timestamp = parsed_timestamp.astimezone(timezone.utc).isoformat()
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT INTO outbound_messages (timestamp_utc, topic, payload, retain, synced)
                VALUES (?, ?, ?, ?, 0)
                """,
                (timestamp, topic, payload_text, int(retain)),
            )
            self._connection.commit()
            self._prune_if_needed()
            row_id = int(cursor.lastrowid)
        self._wake.set()
        return row_id

    def publish_or_queue(
        self,
        client: mqtt.Client,
        topic: str,
        payload: str | bytes | dict[str, Any] | list[Any],
        *,
        retain: bool = False,
        timestamp_utc: str | None = None,
    ) -> int:
        """Durably enqueue a message and wake the non-polling replay worker."""

        row_id = self.enqueue(topic, payload, retain=retain, timestamp_utc=timestamp_utc)
        if self.is_connected(client):
            self._wake.set()
        return row_id

    @staticmethod
    def is_connected(client: mqtt.Client) -> bool:
        try:
            return bool(client.is_connected())
        except (AttributeError, OSError, mqtt.MQTTException):
            return False

    def pending_count(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM outbound_messages WHERE synced = 0"
            ).fetchone()
            return int(row["count"])

    def drain_once(self, client: mqtt.Client) -> int:
        """Replay one FIFO batch; mark only broker-acknowledged messages synced."""

        if not self.is_connected(client):
            return 0
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, topic, payload, retain
                FROM outbound_messages
                WHERE synced = 0
                ORDER BY timestamp_utc ASC, id ASC
                LIMIT ?
                """,
                (self.batch_size,),
            ).fetchall()
        synced_ids: list[int] = []
        for row in rows:
            if not self.is_connected(client):
                break
            try:
                result = client.publish(
                    row["topic"],
                    payload=row["payload"],
                    qos=1,
                    retain=bool(row["retain"]),
                )
                if result.rc != mqtt.MQTT_ERR_SUCCESS:
                    LOGGER.warning("MQTT rejected queued message %s with rc=%s", row["id"], result.rc)
                    break
                result.wait_for_publish(timeout=self.publish_timeout)
                if hasattr(result, "is_published") and not result.is_published():
                    LOGGER.warning("MQTT acknowledgment timed out for queued message %s", row["id"])
                    break
                synced_ids.append(int(row["id"]))
            except (OSError, RuntimeError, ValueError, mqtt.MQTTException):
                LOGGER.exception("Unable to replay queued MQTT message %s", row["id"])
                break

        if synced_ids:
            with self._lock:
                self._connection.executemany(
                    "UPDATE outbound_messages SET synced = 1 WHERE id = ?",
                    ((row_id,) for row_id in synced_ids),
                )
                self._connection.commit()
        return len(synced_ids)

    def start(self, client: mqtt.Client) -> None:
        """Start a background FIFO drain loop independent of sensor polling."""

        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._worker_loop,
            args=(client,),
            name="mqtt-offline-buffer",
            daemon=True,
        )
        self._worker.start()
        self._wake.set()

    def _worker_loop(self, client: mqtt.Client) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self.retry_interval)
            self._wake.clear()
            while not self._stop.is_set() and self.is_connected(client):
                try:
                    sent = self.drain_once(client)
                except (OSError, sqlite3.Error):
                    LOGGER.exception("Offline buffer replay failed")
                    break
                if sent == 0 or sent < self.batch_size:
                    break

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=max(timeout, self.publish_timeout + 1.0))
        with self._lock:
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._connection.close()

    def _disk_usage_bytes(self) -> int:
        total = 0
        for path in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                pass
        return total

    def _prune_if_needed(self) -> None:
        usage = self._disk_usage_bytes()
        if usage < self.max_bytes:
            return
        target = int(self.max_bytes * 0.8)
        while usage >= target:
            oldest = self._connection.execute(
                "SELECT id, length(topic) + length(payload) + 256 AS bytes FROM outbound_messages ORDER BY timestamp_utc ASC, id ASC LIMIT 1"
            ).fetchone()
            if oldest is None:
                break
            self._connection.execute("DELETE FROM outbound_messages WHERE id = ?", (oldest["id"],))
            self._connection.commit()
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._connection.execute("PRAGMA incremental_vacuum(256)")
            usage = self._disk_usage_bytes()
        if usage >= self.max_bytes:
            raise OSError(
                f"offline buffer disk usage remains above limit ({usage} >= {self.max_bytes} bytes)"
            )

    def __enter__(self) -> OfflineBuffer:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
