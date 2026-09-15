"""A pack's own front end, shipped in the bundle (DESIGN.md section 12's "own front end").

When a pack has a ``ui/`` directory it is served at ``/app`` under the same ``serve_client`` gate
as the built-in demo pages, and the pack's branding (title, accent, starter messages) is read
from ``GET /channels/ag_ui``. A pack that ships no UI mounts nothing. The manifest constrains the
two fields a browser sees literally - the accent to a hex colour, the directory to a single
segment inside the pack - so neither is an injection or a traversal.
"""

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.api import AppConfig
from support_core.graph.manifest import PackUI
from tests.app_support import (
    ACME,
    CASSETTES,
    DEMO_CONFIG,
    TEST_PACKS,
    build_app,
    serving,
)

QUEUE_PACK = TEST_PACKS / "queue_pack"


def acme_config() -> AppConfig:
    config = AppConfig.from_file(DEMO_CONFIG)
    return config.model_copy(update={"pack": ACME, "cassette_dir": CASSETTES})


# -- serving the bundled UI ----------------------------------------------------------------


async def test_the_pack_ui_is_served_at_app(engine: AsyncEngine) -> None:
    """The Acme pack ships ``ui/``; it is served at ``/app`` and needs no network."""
    app = build_app(ACME, engine, config=acme_config())
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        page = await client.get("/app/")
        script = await client.get("/app/app.js")

    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert script.status_code == 200
    assert "channels/ag_ui" in script.text  # it drives the AG-UI endpoint
    for body in (page.text, script.text):
        assert "https://" not in body
        assert "http://" not in body


async def test_the_info_endpoint_carries_the_pack_ui_branding(engine: AsyncEngine) -> None:
    app = build_app(ACME, engine, config=acme_config())
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        info = (await client.get("/channels/ag_ui")).json()

    ui = info["ui"]
    assert ui["title"] == "Acme Billing"
    assert ui["accent"] == "#1f6feb"
    assert ui["suggestions"][0].startswith("I got charged twice")
    assert ui["dir"] == "ui"


async def test_a_pack_without_a_ui_directory_mounts_nothing(engine: AsyncEngine) -> None:
    """The queue pack ships no UI, so ``/app`` is not served - but the channel still is."""
    app = build_app(QUEUE_PACK, engine)
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        app_page = await client.get("/app/")
        info = await client.get("/channels/ag_ui")

    assert app_page.status_code == 404
    assert info.status_code == 200
    assert info.json()["ui"]["title"] is None  # default PackUI, nothing declared


async def test_the_pack_ui_is_off_when_the_client_is_off(engine: AsyncEngine) -> None:
    """A deployment with its own hosting turns the demo pages and the pack UI off together."""
    config = acme_config().model_copy(update={"serve_client": False})
    app = build_app(ACME, engine, config=config)
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        app_page = await client.get("/app/")
        health = await client.get("/healthz")

    assert app_page.status_code == 404
    assert health.status_code == 200


# -- the manifest constraints --------------------------------------------------------------


def test_pack_ui_rejects_a_non_hex_accent() -> None:
    """The accent is echoed to a browser; anything but a hex colour is refused."""
    with pytest.raises(ValidationError):
        PackUI(accent="red; } body { display: none")
    with pytest.raises(ValidationError):
        PackUI(accent="rgb(1,2,3)")


@pytest.mark.parametrize("bad", ["..", "../secrets", "a/b", "a\\b", ".", ""])
def test_pack_ui_rejects_a_dir_that_is_not_a_segment_inside_the_pack(bad: str) -> None:
    with pytest.raises(ValidationError):
        PackUI(dir=bad)


def test_pack_ui_accepts_a_hex_accent_and_a_plain_dir() -> None:
    ui = PackUI(title="Acme", accent="#1f6feb", dir="web")
    assert ui.accent == "#1f6feb"
    assert ui.dir == "web"


def test_pack_ui_defaults_are_empty() -> None:
    """A pack that declares no ``ui`` still has one, and it serves and shows nothing."""
    ui = PackUI()
    assert ui.title is None
    assert ui.accent is None
    assert ui.suggestions == []
    assert ui.dir == "ui"
