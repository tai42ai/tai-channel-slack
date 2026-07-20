"""The Slack ``Channel`` — outbound sends via ``chat.postMessage``: question
delivery (``deliver``) and fire-and-forget notifications (``notify``).

Delivery contract: failure of ANY step raises
:class:`~tai_contract.channels.ChannelDeliveryError` — the ask_user helper
prunes the interaction and re-raises, so an undeliverable question is always a
loud failure, never a silent drop. Slack reports most send failures as
HTTP 200 with ``{"ok": false, "error": …}``, so success is decided by the JSON
``ok`` field, never the HTTP status code. ``notify`` shares the same recipient
resolution and send validation but posts a plain message: no correlation
state, no deadline, no reply expected.

The ``confirm`` and ``external`` formats skip the correlation flow (Tier-1):
their answers travel through the callback door directly, never as a typed
chat reply — ``external`` because its answer is a structured POST a human
cannot type into a chat, ``confirm`` because the door accepts only a bool for
it, recorded from the empty-body tap on the link. Both are delivered with the
callback URL as a plain text link — no correlation state, no threaded reply
expected. Only ``text`` and ``select`` take the Tier-2 typed-reply path.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import SecretStr
from tai_contract.channels import ChannelDelivery, ChannelDeliveryError, ChannelNotification

from tai_channel_slack.client import slack_http
from tai_channel_slack.correlation import remaining_seconds, store_correlation
from tai_channel_slack.settings import SlackSettings, require, require_secret, slack_settings

# Formats answered through the callback door directly (tappable plain link),
# never as a typed thread reply: ``confirm`` because the door accepts only a
# bool for it (recorded from the empty-body tap), ``external`` because its
# answer is a structured POST a human cannot type into a chat.
_TIER1_FORMATS = frozenset({"confirm", "external"})


def _require_for_delivery[T](value: T | None, env_name: str) -> T:
    """The configured value, or :class:`ChannelDeliveryError` naming the env
    var.

    A configuration gap discovered on the deliver/notify path IS a delivery
    failure — the send can never happen — so it carries the delivery error
    type, raised before any network call. Wraps the shared ``require``
    validation; only the error type differs.
    """
    try:
        return require(value, env_name)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


def _require_secret_for_delivery(value: SecretStr | None, env_name: str) -> str:
    """The secret's plaintext, or :class:`ChannelDeliveryError` on unset or
    EMPTY — fail closed, raised before any network call.

    Wraps the shared ``require_secret`` validation; only the error type
    differs. The message names the env var only, never the secret value.
    """
    try:
        return require_secret(value, env_name)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


def _render_text(delivery: ChannelDelivery) -> str:
    """The question as Slack message text. For the Tier-2 reply-typed formats
    (``text``, ``select``) the thread IS the correlation key, so the
    reply-in-thread instruction is part of the delivery, not decoration; for
    the Tier-1 formats (``confirm``, ``external``) the callback URL is the
    answer path and is delivered as a plain link (Slack auto-links bare URLs
    in message text). ``select`` degrades to plain text (options enumerated
    inline); the answer is validated server-side at the callback door, never
    here."""
    lines = [delivery.question]
    if delivery.answer_format in _TIER1_FORMATS:
        lines.append(f"Answer here: {delivery.callback_url}")
        lines.append(f"Deadline: {delivery.timeout_at.isoformat()}")
        return "\n".join(lines)
    if delivery.options:
        lines.append("Options: " + ", ".join(delivery.options))
    lines.append(f"Reply in this thread. Deadline: {delivery.timeout_at.isoformat()}")
    return "\n".join(lines)


def _resolve_recipient(settings: SlackSettings, requested: str | None) -> str:
    """The conversation id to send to, resolved from the recipient policy.

    A caller-requested recipient must sit on the operator-set allowlist — an
    unlisted value is refused loudly, fail CLOSED (an empty allowlist rejects
    every request). No requested recipient falls to the operator-set default,
    which is trusted without an allowlist check (the allowlist gates only
    caller-supplied values); neither set raises :class:`ChannelDeliveryError`
    naming the env var. Every branch of this policy fails as a delivery
    failure — there is nowhere to send, so the delivery cannot happen.
    """
    if requested is None:
        return _require_for_delivery(settings.default_recipient, "CHANNEL_SLACK_DEFAULT_RECIPIENT")
    if requested not in set(settings.allowed_recipients):
        raise ChannelDeliveryError(
            f"recipient {requested!r} is not on CHANNEL_SLACK_ALLOWED_RECIPIENTS; refusing to send"
        )
    return requested


async def _post_message(token: str, target: str, text: str) -> dict[str, Any]:
    """POST one ``chat.postMessage`` and return its validated JSON body.

    Any failure — transport error, non-200 status, non-JSON body, or
    ``ok`` not true — raises :class:`ChannelDeliveryError`. Slack's Web API
    answers HTTP 200 even for channel_not_found / not_in_channel /
    invalid_auth, so the JSON ``ok`` field is the ONLY success signal, and a
    false is raised loudly with Slack's error code.
    """
    try:
        async with slack_http() as client:
            response = await client.post(
                f"{slack_settings().api_base_url}/chat.postMessage",
                headers={"Authorization": f"Bearer {token}"},
                json={"channel": target, "text": text},
            )
    except httpx.HTTPError as exc:
        raise ChannelDeliveryError(f"chat.postMessage transport failure: {exc}") from exc
    if response.status_code != 200:
        raise ChannelDeliveryError(f"chat.postMessage returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise ChannelDeliveryError("chat.postMessage returned a non-JSON body") from exc
    if body.get("ok") is not True:
        raise ChannelDeliveryError(f"chat.postMessage failed: {body.get('error', 'unknown error')}")
    return body


class SlackChannel:
    """Registered under ``"slack"``; satisfies ``tai_contract.channels.Channel``."""

    async def deliver(self, delivery: ChannelDelivery) -> None:
        # Settings are read fresh on every call — never cached on the channel
        # object — so a rotated bot token or a changed recipient policy takes
        # effect on the next question after a settings reset instead of
        # silently reusing stale configuration for the process lifetime. An
        # unconfigured channel (missing token, no recipient to resolve) is a
        # delivery failure: ChannelDeliveryError naming the env var, raised
        # before any network call.
        settings = slack_settings()
        token = _require_secret_for_delivery(settings.bot_token, "CHANNEL_SLACK_BOT_TOKEN")
        target = _resolve_recipient(settings, delivery.recipient)
        if remaining_seconds(delivery.timeout_at) <= 0:
            # Never post a question whose budget is already spent — the human
            # would see a prompt that can no longer be answered.
            raise ChannelDeliveryError(
                f"question budget already expired (timeout_at={delivery.timeout_at.isoformat()})"
            )
        body = await _post_message(token, target, _render_text(delivery))
        if delivery.answer_format in _TIER1_FORMATS:
            # Tier-1 (confirm/external): the plain link in the message IS the
            # answer path — the human opens the callback door directly (a tap
            # records the bool for confirm; the structured POST answers
            # external). No correlation entry, no threaded reply expected;
            # later chatter in the message's thread is ignorable (no
            # correlation key ever exists for it).
            return
        ts = body.get("ts")
        if not isinstance(ts, str) or not ts:
            raise ChannelDeliveryError("chat.postMessage ok response carried no ts")
        await store_correlation(ts, delivery.callback_url, delivery.timeout_at)

    async def notify(self, notification: ChannelNotification) -> None:
        """Post one plain fire-and-forget message via ``chat.postMessage``.

        No interaction, no correlation state, no deadline, no reply expected —
        the message text is sent as-is. The recipient is resolved through the
        same allowlist policy as ``deliver``. A plain return means Slack
        ACCEPTED the message (not that a human saw it); any failure raises
        :class:`~tai_contract.channels.ChannelDeliveryError`. One send attempt,
        no retry. Settings are read fresh on every call, so a rotated bot
        token or a changed recipient policy takes effect on the next
        notification after a settings reset.
        """
        settings = slack_settings()
        token = _require_secret_for_delivery(settings.bot_token, "CHANNEL_SLACK_BOT_TOKEN")
        target = _resolve_recipient(settings, notification.recipient)
        await _post_message(token, target, notification.message)
