"""Outbound HTTP for the Slack channel — kit's pooled ``HttpxClient``.

Both outbound calls the plugin makes (``chat.postMessage`` and the loopback
callback forward) go through this accessor, so they share one pooled
``httpx.AsyncClient`` per event loop with a single explicit timeout, budgeted
by ``CHANNEL_SLACK_HTTP_TIMEOUT_SECONDS``. The Events API handler forwards the
answer before acking, and Slack expects the 2xx within 3 s — a forward slower
than the budget surfaces as a loud handler failure, and Slack's retry ladder
plus the dedupe-claim rollback in ``inbound`` recovers the delivery.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager

import httpx
from tai_contract.app import tai_app
from tai_kit.clients.impl.http import HttpxClient

from tai_channel_slack.settings import slack_settings


def slack_http() -> AbstractAsyncContextManager[httpx.AsyncClient]:
    """A pooled outbound client budgeted by ``CHANNEL_SLACK_HTTP_TIMEOUT_SECONDS``."""
    return tai_app.clients.client_ctx(HttpxClient, timeout=slack_settings().http_timeout_seconds)
