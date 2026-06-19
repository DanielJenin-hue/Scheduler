#!/usr/bin/env python3
"""Minimal Stripe webhook service for subscription activation."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lab_scheduler.billing import process_stripe_webhook  # noqa: E402
from lab_scheduler.billing.feature_gates import ensure_billing_schema  # noqa: E402


def _db_path() -> Path:
    return Path(os.environ.get("LAB_SCHEDULER_DB_PATH", str(ROOT / "demo.sqlite3")))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.execute("PRAGMA foreign_keys = ON;")
    ensure_billing_schema(conn)
    return conn


def create_app():
    try:
        from fastapi import FastAPI, HTTPException, Request
    except ImportError as exc:
        raise SystemExit(
            "Install billing extras: pip install -e '.[billing]'"
        ) from exc

    app = FastAPI(title="Lab Staffing Scheduler Billing Webhook")

    @app.post("/stripe/webhook")
    async def stripe_webhook(request: Request) -> dict[str, str]:
        payload = await request.body()
        signature = request.headers.get("stripe-signature", "")
        conn = _connect()
        try:
            message = process_stripe_webhook(
                conn,
                payload=payload,
                signature_header=signature,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            conn.close()
        return {"status": message}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("stripe_webhook:app", host="0.0.0.0", port=port, reload=False)
