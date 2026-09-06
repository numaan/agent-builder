"""FastAPI app factory, channel webhooks, desk API, health, replay. Implements DESIGN.md section
4.1 (the service), 12 (channels) and 15 (replay endpoint).

Phase W delivers ``create_app``, the health endpoint, the web chat WebSocket and webhook, and the
drain worker that makes a non-blocking inbound handler safe (phase 2 review finding R7). The desk
API, the email webhook, tracing, metrics and the replay endpoint are phase 7 and extend this
factory rather than replacing it.
"""

from support_core.api.app import create_app, new_session_key
from support_core.api.config import AppConfig, ConfigError
from support_core.api.drain import DrainQueue, DrainStats
from support_core.api.runtime import Accepted, AppRuntime, build_runtime

__all__ = [
    "Accepted",
    "AppConfig",
    "AppRuntime",
    "ConfigError",
    "DrainQueue",
    "DrainStats",
    "build_runtime",
    "create_app",
    "new_session_key",
]
