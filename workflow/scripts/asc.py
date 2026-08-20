"""Minimal App Store Connect client, shared by the TestFlight scripts.

Reads credentials from the environment rather than a file, because in CI they
arrive as Environment secrets and never touch disk.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request

import jwt

BASE = "https://api.appstoreconnect.apple.com"


def _key() -> str:
    """The .p8, from base64 in CI or from a path locally."""
    encoded = os.environ.get("APP_STORE_CONNECT_P8_BASE64")
    if encoded:
        return base64.b64decode(encoded).decode()
    path = os.environ["APP_STORE_CONNECT_P8_PATH"]
    with open(path) as handle:
        return handle.read()


def token() -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": os.environ["APP_STORE_CONNECT_ISSUER_ID"],
            "iat": now,
            "exp": now + 600,
            "aud": "appstoreconnect-v1",
        },
        _key(),
        algorithm="ES256",
        headers={"kid": os.environ["APP_STORE_CONNECT_KEY_ID"], "typ": "JWT"},
    )



def _decode(raw: bytes, status: int, method: str, path: str):
    """Apple's body as JSON, or a loud failure naming what came back instead.

    A proxy, a captive portal or an Apple incident page answers with HTML. That
    used to raise JSONDecodeError from inside the error handler, which reads as
    a bug in this script rather than as "the thing that answered was not Apple".
    """
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        preview = raw[:200].decode("utf-8", "replace")
        die(
            f"{method} {path} returned {status} with a body that is not JSON. "
            f"Something other than App Store Connect answered. First 200 bytes: "
            f"{preview!r}"
        )


def request(method: str, path: str, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=body,
        method=method,
        headers={
            "Authorization": "Bearer " + token(),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            return response.status, _decode(raw, response.status, method, path)
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, _decode(raw, error.code, method, path)
    except urllib.error.URLError as error:
        # Never reached Apple at all: DNS, TLS, timeout, no route. This fails
        # CLOSED already — nothing here can be mistaken for an empty result —
        # but it used to fail as a traceback, and this module's own die() says
        # why that is not good enough: a step that fails with a stack trace is a
        # step somebody re-runs rather than fixes. On the mint path, re-running
        # is how a second artifact gets created.
        die(
            f"could not reach App Store Connect for {method} {path}: "
            f"{error.reason}. Nothing was sent, so nothing was created — this "
            "is safe to retry once the network is back."
        )


def get(path):
    return request("GET", path)


def post(path, payload):
    return request("POST", path, payload)


def die(message: str) -> None:
    """Fail loudly and actionably.

    Every failure in this lane must name what a human should do about it. A
    step that fails with a stack trace is a step somebody will re-run rather
    than fix.
    """
    raise SystemExit(f"::error::{message}")
