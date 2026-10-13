"""Mobile push notifications (PWA) for the ISIS Beam Monitor.

Receives HMAC-signed notifications from the monitor daemon's [PUSH] webhook
and delivers them as Web Push messages to subscribed devices.
"""

__version__ = "1.1.0"
