"""Registration side-effects: the channel and its inbound door.

Importing this module registers :class:`SlackChannel` under ``"slack"`` on the
app handle's ``channels`` facet and imports :mod:`tai42_channel_slack.inbound` so
the Events API route registers alongside it. The deployment manifest names this
package in ``channel_modules``; the skeleton imports every submodule of the
package at app load, which pulls this module in. Importing the package
``__init__`` alone does NOT register (library use).

No startup hook exists: unlike a medium that needs a set-webhook API call at
boot, the Events API Request URL is configured once in the Slack app dashboard
(see the README's "Slack app setup"), not per process start.
"""

from tai42_contract.app import tai42_app

import tai42_channel_slack.inbound  # noqa: F401  (route-registration side-effect)
from tai42_channel_slack.channel import SlackChannel

tai42_app.channels.register("slack", SlackChannel())
