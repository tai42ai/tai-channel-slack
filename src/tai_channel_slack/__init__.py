"""Slack channel plugin for the TAI ecosystem — delivers ``ask_user`` questions
to a configured Slack channel and bridges threaded replies back through the
interactions callback door.

The runtime discovers this plugin through the manifest's
``channel_modules: [tai_channel_slack]`` — the skeleton imports every module
under the package, and :mod:`tai_channel_slack.register` fires the channel and
inbound-route registrations as a side-effect. Importing this ``__init__``
alone does NOT register (library use).
"""

from tai_channel_slack.channel import SlackChannel
from tai_channel_slack.settings import SlackSettings, slack_settings

__all__ = ["SlackChannel", "SlackSettings", "slack_settings"]
