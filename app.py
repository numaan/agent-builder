"""The service, as DESIGN.md section 4.1 writes it.

    acme-billing-support/            (domain pack repo)
    ├── pyproject.toml               depends on support-core==1.x
    ├── app.py                       app = create_app(load_pack("./pack"))
    ├── Dockerfile
    └── pack/                        see section 5

Core and the sample pack share one repository until phase 9 (BACKLOG.md decisions log), so this
file stands in for the pack repository's ``app.py``. Which pack, which model provider and what a
new conversation starts out knowing come from :class:`~support_core.api.AppConfig`, so the same
image serves a demo with recorded responses and a deployment with a live model without a code
change::

    SUPPORT_APP_CONFIG=demo/acme_web_chat.json python -m uvicorn app:app

Run one process. The sample pack's billing system and one-time passcodes are in-memory fakes
(``packs/acme_billing/tools``), so a second worker would have a second, different account.
"""

from support_core import load_pack
from support_core.api import AppConfig, create_app

config = AppConfig.from_env()
app = create_app(load_pack(config.pack), config=config)
