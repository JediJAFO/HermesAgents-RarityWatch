"""Durable Discord delivery for rarity-watch onboarding results."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.request
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent
HERMES = ROOT.parent
LEDGER = ROOT / "rarity-watch-onboarding-discord.json"
EXPECTED_CHANNEL_NAME = "nft-price-watch-hermes"


def _channel_id() -> str:
    raw = json.loads((HERMES / "cron" / "jobs.json").read_text(encoding="utf-8"))
    jobs = raw.get("jobs", raw) if isinstance(raw, dict) else raw
    matches = [job for job in jobs if job.get("script") == "run_mcfarlane_exotic_deterministic.py"
               and str(job.get("deliver", "")).startswith("discord:")]
    if len(matches) != 1:
        raise RuntimeError("Exotic Discord destination is not uniquely configured")
    return matches[0]["deliver"].split(":", 1)[1]


@contextmanager
def delivery_lock():
    with open(LEDGER.with_suffix(".lock"), "a+b") as handle:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Rarity Discord delivery already in progress") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic(path: Path, data: dict) -> None:
    fd, temp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def chunks(text: str) -> list[str]:
    result = []
    while text:
        end = min(1800, len(text))
        if end < len(text):
            newline = text.rfind("\n", 0, end)
            if newline >= 0:
                end = newline + 1
        result.append(text[:end])
        text = text[end:]
    return result


class Discord:
    def __init__(self) -> None:
        token = os.getenv("DISCORD_BOT_TOKEN")
        if not token:
            from dotenv import dotenv_values
            token = dotenv_values(HERMES / ".env").get("DISCORD_BOT_TOKEN")
        if not token:
            raise RuntimeError("Discord credential unavailable")
        self.channel = _channel_id()
        self.headers = {"Authorization": "Bot " + token, "User-Agent": "HermesRarityWatch/1.0",
                        "Content-Type": "application/json"}

    def request(self, path: str, payload: dict | None = None) -> dict:
        request = urllib.request.Request(
            "https://discord.com/api/v10/" + path, headers=self.headers,
            data=json.dumps(payload).encode() if payload is not None else None)
        with urllib.request.urlopen(request, timeout=40) as response:
            return json.load(response)

    def deliver(self, text: str, batch_id: str) -> list[str]:
        with delivery_lock():
            ledger = json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else {"batches": {}}
            bodies = chunks(text)
            digest = hashlib.sha256(text.encode()).hexdigest()
            record = ledger["batches"].get(batch_id)
            if record is None:
                record = {"sha256": digest, "messages": {}, "bodies": bodies}
                ledger["batches"][batch_id] = record
            else:
                bodies = record["bodies"]
            if record["sha256"] != digest:
                raise RuntimeError("Discord delivery batch changed content")
            channel = self.request("channels/" + self.channel)
            if channel.get("name") != EXPECTED_CHANNEL_NAME:
                raise RuntimeError("Discord channel identity mismatch")
            verified = []
            for index, body in enumerate(bodies):
                key = str(index)
                saved = record["messages"].get(key)
                if saved and saved.get("id"):
                    message = self.request(f"channels/{self.channel}/messages/{saved['id']}")
                else:
                    if not saved:
                        nonce = str(int(hashlib.sha256(f"{batch_id}:{index}".encode()).hexdigest()[:15], 16))
                        saved = {"nonce": nonce, "attempted_at": time.time()}
                        record["messages"][key] = saved
                        atomic(LEDGER, ledger)
                    elif not 0 <= time.time() - saved.get("attempted_at", 0) <= 60:
                        raise RuntimeError("Discord POST outcome ambiguous; reconcile before retry")
                    message = self.request(f"channels/{self.channel}/messages", {
                        "content": body.strip(), "allowed_mentions": {"parse": []},
                        "nonce": saved["nonce"], "enforce_nonce": True, "tts": False})
                    saved["id"] = message["id"]
                    atomic(LEDGER, ledger)
                    message = self.request(f"channels/{self.channel}/messages/{saved['id']}")
                if (message.get("content") != body.strip() or message.get("channel_id") != self.channel
                        or message.get("id") != saved.get("id")):
                    raise RuntimeError("Discord read-back mismatch")
                saved["verified"] = True
                atomic(LEDGER, ledger)
                verified.append(saved["id"])
            record["complete"] = True
            atomic(LEDGER, ledger)
            return verified
