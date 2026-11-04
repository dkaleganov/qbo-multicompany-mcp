"""A separate *process* that refreshes one realm, for the cross-process lock test.

This file exists because threads cannot exercise the file lock. ``canonical_lock``
holds an in-process mutex as well as an ``flock``, and the mutex alone serialises
two threads in one interpreter -- so a thread-based test passes with the ``flock``
deleted. Only a second interpreter, with its own mutex registry, contends for the
lock itself.

Run as ``python -m tests.refresh_child`` (never collected by pytest: the name does
not start with ``test_``). It writes one JSON object to ``--out``:

``submitted``
    every refresh token this process handed to the token endpoint, in order
``in_section`` / ``out_of_section``
    wall-clock bounds of the part of the refresh that runs *inside* the
    canonical lock, so the parent can see whether the two processes overlapped
``error``
    the exception text, if the refresh raised
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-dir", required=True)
    parser.add_argument("--realm", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--ready", required=True)
    parser.add_argument("--go", required=True)
    parser.add_argument("--hold-seconds", type=float, default=0.25)
    args = parser.parse_args()

    from qbo_multicompany_mcp import oauth
    from qbo_multicompany_mcp.crypto import KeySource
    from qbo_multicompany_mcp.store import AppCredentials, RefreshActor, TokenStore

    submitted: list[str] = []
    bounds: list[float] = []

    def fake_refresh(**kwargs: object) -> oauth.TokenResponse:
        """Chain from whatever token was submitted, slowly.

        Slowly on purpose: the sleep is the window in which a second process
        that is *not* excluded would read the same generation and fork the
        chain. ``refresh-N`` in produces ``refresh-N+1`` out, so the submitted
        sequence alone shows whether the two processes serialised. This runs
        inside the canonical lock, so its bounds are the critical section.
        """
        bounds.append(time.time())
        token = str(kwargs["refresh_token"])
        submitted.append(token)
        time.sleep(args.hold_seconds)
        number = int(token.rsplit("-", 1)[1])
        now = datetime.now(UTC)
        bounds.append(time.time())
        return oauth.TokenResponse(
            access_token=f"access-{number + 1}",
            refresh_token=f"refresh-{number + 1}",
            access_token_expires_at=now + timedelta(hours=1),
            refresh_token_expires_at=now + timedelta(days=100),
            synthetic=True,
        )

    oauth.refresh_tokens = fake_refresh  # type: ignore[assignment]

    store = TokenStore(
        Path(args.store_dir),
        environment="sandbox",
        key_source=KeySource.ENV,
        allow_plaintext=False,
        credentials=AppCredentials(
            client_id="test-client-id", client_secret="test-client-secret"
        ),
    )

    Path(args.ready).write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 30
    while not Path(args.go).exists():
        if time.monotonic() > deadline:
            raise SystemExit("the parent never said go")
        time.sleep(0.005)

    import qbo_multicompany_mcp

    record: dict[str, object] = {"role": args.role, "module_file": qbo_multicompany_mcp.__file__}
    try:
        store.refresh(args.realm, actor=RefreshActor.here(args.role), force=True)
    except Exception as exc:  # recorded, not raised: the parent asserts on it
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["submitted"] = submitted
    record["in_section"] = bounds[0] if bounds else None
    record["out_of_section"] = bounds[-1] if bounds else None
    Path(args.out).write_text(json.dumps(record), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
