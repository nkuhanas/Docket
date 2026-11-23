"""Dedicated read-only IMAP acquisition process. The CLI never accepts a password argument."""

import argparse
import hashlib
import imaplib
import json
import os
import re
import sqlite3
import ssl
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

MAILBOX = "billing@chacel.ai"
HOST = "imap.purelymail.com"
SECRETS = Path(os.environ.get("FINANCE_COLLECTOR_SECRETS", "/run/financial-collector"))
STATE = Path(os.environ.get("FINANCE_COLLECTOR_STATE", "/var/lib/financial-collector"))
API = os.environ.get("FINANCE_COLLECTOR_API", "http://docket:8000/internal/v1/financial-collector")


def read_secret(name: str) -> str:
    value = (SECRETS / name).read_text().strip()
    if not value:
        raise ValueError("missing secret")
    return value


def open_mail() -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL(HOST, 993, ssl_context=ssl.create_default_context(), timeout=20)
    mail.debug = 0
    try:
        mail.login(MAILBOX, read_secret("imap_password"))
        if mail.select("INBOX", readonly=True)[0] != "OK":
            raise ValueError("mailbox unavailable")
    except Exception:
        mail.shutdown()
        raise
    return mail


def stamp() -> str:
    return datetime.now(UTC).isoformat()


def observe(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO state(key,value) VALUES (?,?)", (key, value))


def scan(
    mail: imaplib.IMAP4_SSL,
    db: sqlite3.Connection,
    admission: dict[str, Any],
    deliver: Callable[[str, bytes], dict[str, Any]],
) -> None:
    if admission["mailbox"] != MAILBOX or admission["folder"] != "INBOX":
        raise ValueError("source mismatch")
    response = mail.response("UIDVALIDITY")[1]
    if not response or not response[0]:
        raise ValueError("missing mailbox epoch")
    epoch = int(response[0])
    checkpoint_key = "checkpoint:" + admission["source"] + ":" + str(epoch)
    row = db.execute("SELECT value FROM state WHERE key=?", (checkpoint_key,)).fetchone()
    last = int(row[0]) if row else 0
    begin = date.fromisoformat(admission["backfill_start"]).strftime("%d-%b-%Y")
    code, listing = mail.uid("SEARCH", "SINCE", begin, "UID", f"{last + 1}:*")
    if code != "OK" or not isinstance(listing[0], bytes):
        raise ValueError("search unavailable")
    observe(db, "last_acquisition", stamp())
    db.commit()
    for uid in sorted({int(v) for v in listing[0].split() if int(v) > last})[:40]:
        delivery = f"{epoch}:{uid}"
        observe(db, "pending_delivery", delivery)
        db.commit()
        code, sizing = mail.uid("FETCH", str(uid), "(RFC822.SIZE)")

        match = (
            re.search(rb"RFC822.SIZE (\d+)", sizing[0])
            if code == "OK" and isinstance(sizing[0], bytes)
            else None
        )
        if not match or not 0 < int(match[1]) <= admission["max_bytes"]:
            raise ValueError("message unavailable or oversized")
        code, parts = mail.uid("FETCH", str(uid), "(BODY.PEEK[])")
        payload = next((p[1] for p in parts if isinstance(p, tuple)), None)
        if code != "OK" or not isinstance(payload, bytes) or len(payload) != int(match[1]):
            raise ValueError("message transfer incomplete")
        receipt = deliver(delivery, payload)
        if not (
            receipt.get("source_accepted")
            and receipt.get("source_ref")
            and receipt.get("original_sha256") == hashlib.sha256(payload).hexdigest()
        ):
            raise ValueError("durable receipt missing")
        with db:
            observe(db, checkpoint_key, str(uid))
            observe(db, "checkpoint", delivery)
            observe(db, "pending_delivery", "")
            observe(db, "last_acceptance", stamp())


def supervise() -> None:
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(STATE / "checkpoint.sqlite")
    db.execute("CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    failures = 0
    while True:
        try:
            with httpx.Client(
                base_url=API + "/",
                headers={"Authorization": "Bearer " + read_secret("collector_token")},
                follow_redirects=False,
                trust_env=False,
                timeout=30,
            ) as client:
                response = client.get("policy")
                response.raise_for_status()
                grant = response.json()

                def deliver(
                    key: str, data: bytes, revision: int = grant["revision"]
                ) -> dict[str, Any]:
                    res = client.post(
                        "messages",
                        content=data,
                        headers={
                            "X-Delivery-ID": key,
                            "X-Policy-Revision": str(revision),
                            "Content-Type": "message/rfc822",
                        },
                    )
                    res.raise_for_status()
                    return dict(res.json())

                with open_mail() as mail:
                    scan(mail, db, grant, deliver)
            failures = 0
        except Exception as exc:
            # Exceptions may carry credentials, message literals or a server greeting.
            failures += 1
            observe(db, "failure_class", type(exc).__name__)
        observe(db, "consecutive_failures", str(failures))
        observe(db, "last_check", stamp())
        db.commit()
        time.sleep(min(600, 30 * 2 ** min(failures, 5)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["test", "run", "status"])
    action = parser.parse_args().action
    if action == "run":
        supervise()
    elif action == "status":
        with sqlite3.connect(f"file:{STATE}/checkpoint.sqlite?mode=ro", uri=True) as db:
            print(
                json.dumps(
                    dict(
                        db.execute("SELECT key,value FROM state WHERE key NOT LIKE 'checkpoint:%'")
                    )
                )
            )
    else:
        try:
            with open_mail() as mail:
                if not mail.response("UIDVALIDITY")[1][0]:
                    raise ValueError
        except Exception:
            raise SystemExit("Read-only IMAP login or mailbox selection failed.") from None
        print("Authenticated dedicated billing mailbox and selected INBOX read-only.")


if __name__ == "__main__":
    main()
