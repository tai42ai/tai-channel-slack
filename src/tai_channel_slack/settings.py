"""Slack channel settings.

Two env groups under the ``CHANNEL_SLACK_`` prefix: the channel credentials and
recipient policy (``SlackSettings``) and the plugin-owned correlation-store
connection (``SlackRedisSettings``). Both are reached through
``settings_cache`` accessors, so a settings reset drops them with every other
group — a rotated bot token or signing secret is picked up on the very next
call, never pinned to a stale object held by the channel.

Credentials are operator-bound environment configuration, never LLM-visible
tool parameters. The recipient is resolved per delivery: a caller-requested
recipient must be on the operator-set ``allowed_recipients`` whitelist (an
unlisted request is refused loudly, fail closed), and a delivery without a
requested recipient goes to ``default_recipient``. Fields default unset so
importing the package never demands configuration; the ``require`` /
``require_secret`` helpers raise loudly, naming the missing env var, wherever a
value is actually needed.
"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import NoDecode, SettingsConfigDict
from tai_kit.clients import RedisConnectionSettings
from tai_kit.settings import TaiBaseSettings, settings_cache


class SlackSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHANNEL_SLACK_")

    # The bot token (``xoxb-…``, OAuth scope ``chat:write``). SecretStr so it
    # never surfaces in a repr, log line, traceback, or model_dump; the
    # plaintext is read only when building the Authorization header.
    bot_token: SecretStr | None = None
    # The Events API signing secret keying the ``v0`` HMAC-SHA256 scheme.
    signing_secret: SecretStr | None = None
    # Operator whitelist of Slack conversation ids — channel (``C…``) or DM
    # (``D…``) — a caller-requested recipient may name. A requested recipient
    # not on this list is refused loudly, fail closed; an empty list rejects
    # every caller-requested recipient. ``NoDecode`` hands the raw env string
    # to the before-validator, which accepts a comma-separated string or a
    # JSON list.
    allowed_recipients: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # The channel (``C…``) or DM (``D…``) id a question WITHOUT a
    # caller-requested recipient is delivered to. Operator-set, so it is
    # trusted without an allowlist check; unset plus no requested recipient
    # raises loudly at delivery time.
    default_recipient: str | None = None
    # Slack Web API origin. Overridable so a stub server can stand in for Slack
    # (the e2e harness records against a local endpoint); production never
    # changes it. ``chat.postMessage`` is addressed as ``{api_base_url}/chat.postMessage``.
    api_base_url: str = "https://slack.com/api"
    # Wall-clock budget for one outbound HTTP call (chat.postMessage / the
    # callback forward). Must be positive.
    http_timeout_seconds: float = Field(default=30, gt=0)

    @field_validator("allowed_recipients", mode="before")
    @classmethod
    def _parse_allowed_recipients(cls, value: object) -> object:
        """Parse the allowlist from either accepted shape — anything else
        raises loudly.

        A string bracketed like JSON is decoded as a JSON list; any other
        string is split on commas. Every list — decoded, split, or passed
        directly — is normalized the same way: string items are stripped and
        empties dropped, so a padded or blank entry can never sit on the
        allowlist silently refusing to match a caller recipient or an inbound
        channel id. A non-string item passes through unchanged so the
        ``list[str]`` item validation raises loudly — never coerced.
        """
        if isinstance(value, str):
            stripped = value.strip()
            value = json.loads(stripped) if stripped.startswith("[") else stripped.split(",")
        if isinstance(value, list):
            normalized: list[object] = []
            for item in value:
                if isinstance(item, str):
                    item = item.strip()
                    if not item:
                        continue
                normalized.append(item)
            return normalized
        raise ValueError("allowed_recipients must be a comma-separated string or a list")


class SlackRedisSettings(RedisConnectionSettings):
    """The correlation store connection — ``CHANNEL_SLACK_REDIS_URL`` plus the
    inherited tuning fields (``CHANNEL_SLACK_REDIS_MAX_CONNECTIONS``,
    ``CHANNEL_SLACK_SOCKET_TIMEOUT``, ``CHANNEL_SLACK_RETRY_ATTEMPTS``, …)."""

    model_config = SettingsConfigDict(env_prefix="CHANNEL_SLACK_")


@settings_cache
def slack_settings() -> SlackSettings:
    return SlackSettings()


@settings_cache
def slack_redis_settings() -> SlackRedisSettings:
    return SlackRedisSettings()


def require[T](value: T | None, env_name: str) -> T:
    """Return the configured value, or raise naming the missing env var.

    A channel named in the manifest but missing its configuration is an
    operator error that must surface loudly at the point of use — never a
    silent no-op delivery.
    """
    if value is None:
        raise ValueError(f"the slack channel is not configured: set {env_name}")
    return value


def require_secret(value: SecretStr | None, env_name: str) -> str:
    """Return the secret's plaintext, or raise on unset/EMPTY — fail CLOSED.

    An empty secret is as dangerous as a missing one (an empty signing secret
    would make the inbound HMAC forgeable by anyone), so both raise.
    """
    secret = require(value, env_name).get_secret_value()
    if not secret:
        raise ValueError(f"{env_name} is set but empty")
    return secret
