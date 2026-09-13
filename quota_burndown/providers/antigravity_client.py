"""Antigravity language server quota client.

Communicates over localhost HTTPS with Antigravity's language_server process
via the RetrieveUserQuotaSummary RPC.
"""
from __future__ import annotations

import json
import ssl
import urllib.request
from datetime import datetime
from typing import Any

from ..capacity import Observation
from ..util import now_utc, parse_iso

ENDPOINT = "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"


def fetch_quota_summary(port: int, token: str, timeout: float = 5.0) -> dict | None:
    """Query RetrieveUserQuotaSummary from the local Antigravity language server."""
    url = f"https://127.0.0.1:{port}{ENDPOINT}"
    ctx = ssl._create_unverified_context()
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "x-codeium-csrf-token": token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError, TimeoutError):
        return None


def observations_from_summary(
    data: dict[str, Any],
    account_scope: str = "local",
    observed_at: datetime | None = None,
) -> list[Observation]:
    """Convert RetrieveUserQuotaSummary JSON response into capacity Observations."""
    if not isinstance(data, dict):
        return []
    response = data.get("response") if isinstance(data.get("response"), dict) else data
    groups = response.get("groups") if isinstance(response, dict) else None
    if not isinstance(groups, list):
        flat_buckets = response.get("buckets") or response.get("userQuotaSummaries")
        if isinstance(flat_buckets, list):
            groups = [{"displayName": "Gemini Models", "buckets": flat_buckets}]
        else:
            return []

    now = observed_at or now_utc()
    out: list[Observation] = []

    for group in groups:
        if not isinstance(group, dict):
            continue
        display_name = str(group.get("displayName") or "").lower()
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue

        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            bucket_id = str(bucket.get("bucketId") or bucket.get("name") or "").lower()
            window_type = str(bucket.get("window") or "").lower()

            if "gemini" in display_name or "gemini" in bucket_id:
                limit_id = "gemini"
            elif "3p" in bucket_id or "claude" in display_name or "gpt" in display_name:
                limit_id = "3p"
            else:
                continue

            # Only Gemini limits map to the capacity matrix slots currently
            if limit_id != "gemini":
                continue

            if window_type == "5h" or "5h" in bucket_id:
                minutes = 300
            elif window_type in ("weekly", "7d") or "weekly" in bucket_id:
                minutes = 10080
            else:
                continue

            remaining = bucket.get("remainingFraction")
            used_pct = None
            if remaining is not None and not isinstance(remaining, bool):
                try:
                    frac = float(remaining)
                    used_pct = round(max(0.0, min(100.0, (1.0 - frac) * 100.0)), 2)
                except (TypeError, ValueError):
                    pass

            reset_time = bucket.get("resetTime")
            resets_at = parse_iso(reset_time) if reset_time else None

            out.append(
                Observation(
                    provider="antigravity",
                    account_scope=account_scope,
                    limit_id=limit_id,
                    window=f"{minutes}m",
                    window_min=minutes,
                    used_pct=used_pct,
                    resets_at=resets_at,
                    observed_at=now,
                    received_at=now,
                    source="app-server",
                    reset_provenance="reported",
                    mapping_confidence="reported",
                    complete_snapshot=True,
                    observation_time_provenance="read_completed",
                )
            )

    return out
