"""The Slack Events API inbound door.

``POST /api/channels/slack/inbound`` — registered ``authed=False`` because
Slack cannot present the deployment api key; the request is authenticated by
the ``X-Slack-Signature`` v0 HMAC instead. Flow:

1. Read a BOUNDED body (actual bytes, never a client ``Content-Length``) —
   an unauthenticated door bounds what it reads into memory BEFORE any HMAC
   work; past the cap it answers a loud 413, never a truncation.
2. Verify the signature over the EXACT raw body bytes — fail CLOSED. An unset
   or empty signing secret is operator misconfiguration: one logged, constant
   JSON 500, never an open door. Even the one-time ``url_verification``
   handshake is verified first: Slack signs it, and an unverified echo would
   let anyone confirm the endpoint.
3. ``url_verification`` -> echo the challenge (the Slack-dashboard handshake).
4. ``event_callback`` -> dedupe on ``event_id`` (Slack retries any delivery
   not acked 2xx within 3 s — immediately, ~1 min, ~5 min), match the reply's
   ``thread_ts`` against the correlation store, forward the typed answer to
   the stored callback URL, ack 200.

The handler works inline — two Redis round-trips plus one loopback POST — so
the 2xx lands well inside Slack's 3-second ack window. Any failure after the
dedupe claim releases the claim before re-raising, so Slack's retry ladder
reprocesses the event instead of losing the answer behind the dedupe key; the
raise itself surfaces as a loud 500 in the server log.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app

from tai42_channel_slack.client import slack_http
from tai42_channel_slack.correlation import claim_dedupe, delete_correlation, get_callback_url, release_dedupe
from tai42_channel_slack.settings import slack_settings

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "X-Slack-Signature"
_TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"
_RETRY_NUM_HEADER = "X-Slack-Retry-Num"
_SIGNATURE_PREFIX = "v0="
_HEX_DIGEST_LEN = hashlib.sha256().digest_size * 2  # 64
# Slack's documented replay window: reject any request whose timestamp is more
# than five minutes from now, in either direction.
_MAX_TIMESTAMP_SKEW_SECONDS = 300
# A Slack event delivery is a few KiB; an unauthenticated door still bounds
# what it reads into memory — loud 413, never a truncation.
_MAX_BODY_BYTES = 1 * 1024 * 1024


class _PayloadTooLarge(Exception):
    """The inbound body exceeded ``_MAX_BODY_BYTES`` -> 413."""


class _SignatureRejected(Exception):
    """An ordinary request-side verification failure -> uniform 401."""


def _misconfigured(env_name: str) -> JSONResponse:
    """Fail CLOSED on operator misconfiguration: one logged, constant JSON 500
    — loud in the server log, no stack leaked to the client."""
    logger.error("slack inbound: %s is unset or empty; failing closed", env_name)
    return JSONResponse({"error": "channel misconfigured"}, status_code=500)


async def _read_bounded_body(request: Request, cap: int) -> bytes:
    """Read the request body on ACTUAL bytes, never a client ``Content-Length``.
    Raise ``_PayloadTooLarge`` past ``cap`` before any HMAC or parse work —
    loud, never truncated."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise _PayloadTooLarge("request body exceeds the configured cap")
        chunks.append(chunk)
    return b"".join(chunks)


def _verify_signature(raw: bytes, headers: Mapping[str, str], secret: str) -> None:
    """Authenticate ``raw`` against Slack's v0 signing scheme with ``secret``,
    or raise.

    Every request-side defect raises :class:`_SignatureRejected`, which the
    handler maps to one constant 401 body — no oracle distinguishing a missing
    header from a malformed or stale timestamp from a digest mismatch.
    """
    timestamp = headers.get(_TIMESTAMP_HEADER)
    if timestamp is None:
        raise _SignatureRejected(f"missing {_TIMESTAMP_HEADER} header")
    # Gate to ASCII digits BEFORE parsing: bare int() also accepts surrounding
    # Unicode whitespace and non-ASCII decimal digits, which the ascii-encode
    # of the signing base below cannot carry — a malformed timestamp must be
    # the same uniform reject as every other tampered header.
    if not (timestamp.isascii() and timestamp.isdigit()):
        raise _SignatureRejected(f"{_TIMESTAMP_HEADER} is not an ASCII-digit integer")
    ts_value = int(timestamp)
    if abs(time.time() - ts_value) > _MAX_TIMESTAMP_SKEW_SECONDS:
        raise _SignatureRejected("request timestamp outside the replay window")

    signature = headers.get(_SIGNATURE_HEADER)
    if signature is None:
        raise _SignatureRejected(f"missing {_SIGNATURE_HEADER} header")
    if not signature.startswith(_SIGNATURE_PREFIX):
        raise _SignatureRejected(f"{_SIGNATURE_HEADER} is not prefixed {_SIGNATURE_PREFIX!r}")
    provided_hex = signature[len(_SIGNATURE_PREFIX) :]
    if len(provided_hex) != _HEX_DIGEST_LEN:
        raise _SignatureRejected(f"{_SIGNATURE_HEADER} digest is not {_HEX_DIGEST_LEN} hex characters")
    try:
        provided_digest = bytes.fromhex(provided_hex)
    except ValueError as exc:
        raise _SignatureRejected(f"{_SIGNATURE_HEADER} digest is not valid hex") from exc

    base = b"v0:" + timestamp.encode("ascii") + b":" + raw
    expected_digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).digest()
    # Constant-time compare over the raw digest bytes so a mismatch position
    # cannot be timed out.
    if not hmac.compare_digest(provided_digest, expected_digest):
        raise _SignatureRejected(f"{_SIGNATURE_HEADER} digest mismatch")


@tai42_app.http.custom_route(
    "/api/channels/slack/inbound",
    methods=["POST"],
    summary="Slack Events API inbound door (signature-authenticated)",
    tags=["channels"],
    response_model=None,
    authed=False,
)
async def slack_inbound(request: Request) -> Response:
    """Receive a Slack Events API delivery, verify it, and bridge a correlated
    threaded reply to its interaction callback URL.

    Unverifiable requests get a constant 401; an unset or empty signing secret
    is answered with a logged, constant 500. Verified non-answer traffic
    (edits, bot echoes, other channels, top-level messages, threads with no
    pending question) is acked 200 ``ignored`` — Slack must receive a 2xx or
    it retries and eventually disables the subscription; the waiting ask
    surfaces such cases through its own timeout, never a hung Slack app.
    """
    try:
        raw = await _read_bounded_body(request, _MAX_BODY_BYTES)
    except _PayloadTooLarge:
        # Bounded BEFORE any HMAC work — an oversized body never costs a
        # signature computation, and the reject is a loud 413, never a
        # truncated read.
        return JSONResponse({"error": "payload too large"}, status_code=413)
    # Resolve the signing secret before any verification work. Unset or empty
    # fails CLOSED — an empty key would make the HMAC forgeable by anyone,
    # never a silently open door.
    signing_secret = slack_settings().signing_secret
    secret = signing_secret.get_secret_value() if signing_secret is not None else ""
    if not secret:
        return _misconfigured("CHANNEL_SLACK_SIGNING_SECRET")
    try:
        _verify_signature(raw, request.headers, secret)
    except _SignatureRejected as exc:
        # Constant body for every request-side defect; the specific reason is
        # confined to the log.
        logger.warning("slack inbound rejected: %s", exc)
        return JSONResponse({"error": "signature verification failed"}, status_code=401)

    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge")
        if not isinstance(challenge, str) or not challenge:
            return JSONResponse({"error": "url_verification without challenge"}, status_code=400)
        return JSONResponse({"challenge": challenge})

    if payload.get("type") != "event_callback":
        # A signed envelope of a kind we did not subscribe to answer (e.g.
        # app_rate_limited) — ack so Slack does not retry it.
        return JSONResponse({"status": "ignored"})

    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        return JSONResponse({"error": "event_callback without event_id"}, status_code=400)

    if not await claim_dedupe(event_id):
        # A retry of an event already processed (or still in flight): ack it.
        # If the first attempt is mid-forward, the callback door's single-use
        # claim is the final guard.
        logger.info(
            "slack inbound duplicate event %s (retry-num=%s)",
            event_id,
            request.headers.get(_RETRY_NUM_HEADER),
        )
        return JSONResponse({"status": "duplicate"})

    try:
        return await _process_event(payload)
    except BaseException:
        # Processing failed AFTER the dedupe claim: release it so Slack's retry
        # ladder reprocesses the event, then re-raise — the 500 is the correct
        # signal to Slack (retry) and the loud signal in our own log.
        await release_dedupe(event_id)
        raise


async def _process_event(payload: dict) -> Response:
    """Correlate one verified, deduped ``event_callback`` and forward it."""
    # A question is only ever delivered to the operator default recipient or
    # an allowlisted one, so those conversations are the only ones a
    # correlated reply can arrive from. No recipient configured at all is
    # operator misconfiguration — raise loudly (the release-and-reraise guard
    # frees the dedupe claim; the 500 is the signal).
    settings = slack_settings()
    recipients = set(settings.allowed_recipients)
    if settings.default_recipient is not None:
        recipients.add(settings.default_recipient)
    if not recipients:
        raise ValueError(
            "the slack channel is not configured: set CHANNEL_SLACK_DEFAULT_RECIPIENT"
            " or CHANNEL_SLACK_ALLOWED_RECIPIENTS"
        )
    event = payload.get("event")
    if not isinstance(event, dict):
        raise ValueError("event_callback without an event object")

    if event.get("type") != "message" or "subtype" in event or event.get("bot_id") is not None:
        # Not a plain human-typed message: edits/joins/file shares carry a
        # subtype, and the bot's own question post echoes back with a bot_id.
        # Filtering subtypes here is what guarantees a correlated event always
        # carries real ``text``.
        return JSONResponse({"status": "ignored"})
    if event.get("channel") not in recipients:
        return JSONResponse({"status": "ignored"})

    thread_ts = event.get("thread_ts")
    if not isinstance(thread_ts, str) or not thread_ts:
        # A top-level channel message, not a thread reply — no correlation key.
        return JSONResponse({"status": "ignored"})

    callback_url = await get_callback_url(thread_ts)
    if callback_url is None:
        # Thread chatter with no pending question (already answered, expired,
        # or a thread that was never ours).
        return JSONResponse({"status": "ignored"})

    text = event.get("text")
    if not isinstance(text, str) or not text:
        # A CORRELATED reply we cannot extract an answer from is an error, not
        # ignorable chatter: raising (-> 500 -> Slack retries, then gives up
        # loudly in the log) beats ever resolving the ask with None.
        raise ValueError(f"correlated slack reply in thread {thread_ts} carries no text")

    async with slack_http() as client:
        response = await client.post(callback_url, json={"answer": text})
    if response.status_code == 200:
        await delete_correlation(thread_ts)
        return JSONResponse({"status": "forwarded"})
    if response.status_code == 404:
        # The ticket is terminally gone (expired/pruned/already claimed) —
        # retrying the SAME answer can never succeed. Drop the correlation and
        # ack Slack 200 so its retry ladder does not hammer a dead ticket.
        logger.warning(
            "slack inbound: callback door returned terminal HTTP 404 for thread_ts=%s; dropping correlation",
            thread_ts,
        )
        await delete_correlation(thread_ts)
        return JSONResponse({"status": "stale"})
    if response.status_code == 400:
        # The door rejected THIS answer (e.g. not one of the select options).
        # Keep the correlation so the human can reply again in the same
        # thread; ack Slack 200 — redelivering the same rejected text is
        # pointless.
        logger.warning(
            "slack inbound: callback door rejected the answer for thread_ts=%s (400); correlation kept",
            thread_ts,
        )
        return JSONResponse({"status": "rejected"})
    # 401/5xx: transient or misconfiguration — log loud by raising, keep the
    # correlation, and let the release-and-reraise guard free the dedupe claim
    # so Slack's retry ladder re-attempts the forward.
    raise RuntimeError(f"callback forward failed: HTTP {response.status_code} from the interactions door")
