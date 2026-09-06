"""Configuration: which pack, which provider, and what a new conversation is allowed to know.

The provider choice is the one this phase promised: the same application runs against recorded
responses without an API key and against the live model with one, chosen by configuration rather
than by a code change.
"""

import json
from pathlib import Path

import pytest

from support_core import load_pack
from support_core.api import AppConfig, ConfigError, build_runtime
from support_core.api.config import (
    ENV_CASSETTES,
    ENV_CONFIG,
    ENV_PACK,
    ENV_PROVIDER,
    ProviderChoice,
)
from support_core.api.runtime import build_provider
from support_core.channels import InboundMessage
from support_core.llm.recording import Cassette, load_cassettes
from support_core.llm.types import CompletionRequest, CompletionResponse, SystemBlock
from tests.app_support import ACME, CASSETTES, DEMO_CONFIG, TEST_PACKS

QUEUE_PACK = TEST_PACKS / "queue_pack"


def test_the_demo_configuration_reads() -> None:
    config = AppConfig.from_file(DEMO_CONFIG)
    assert config.pack == Path("packs/acme_billing")
    assert config.cassette_dir == Path("tests/cassettes")
    assert config.lock_wait_seconds == 0.0, "the HTTP path queues and returns by default"


def test_the_environment_overrides_the_file() -> None:
    env = {
        ENV_CONFIG: str(DEMO_CONFIG),
        ENV_PACK: str(QUEUE_PACK),
        ENV_PROVIDER: "none",
        ENV_CASSETTES: str(CASSETTES),
    }
    config = AppConfig.from_env(env)
    assert config.pack == QUEUE_PACK
    assert config.provider == "none"
    assert config.cassette_dir == CASSETTES
    assert config.suggestions, "the rest of the file still applies"


def test_with_no_environment_at_all_there_is_a_default() -> None:
    config = AppConfig.from_env({})
    assert config.provider == "auto"
    assert config.serve_client is True


@pytest.mark.parametrize(
    ("provider", "key", "cassettes", "expected"),
    [
        ("auto", "sk-live", True, "anthropic"),
        ("auto", "", True, "replay"),
        ("auto", "", False, "none"),
        ("auto", "sk-live", False, "anthropic"),
        ("replay", "sk-live", True, "replay"),
        ("anthropic", "", False, "anthropic"),
        ("none", "sk-live", True, "none"),
    ],
)
def test_the_provider_is_a_matter_of_configuration(
    provider: ProviderChoice, key: str, cassettes: bool, expected: str
) -> None:
    """A deployment with a key runs against the model; the same image with no key runs against
    the recording. Nothing about the pack, the prompts or the nodes differs between them."""
    config = AppConfig(
        pack=ACME,
        provider=provider,
        cassette_dir=CASSETTES if cassettes else None,
    )
    env = {"ANTHROPIC_API_KEY": key} if key else {}
    assert config.resolve_provider(env) == expected


def test_replay_without_cassettes_is_refused_at_startup() -> None:
    config = AppConfig(pack=ACME, provider="replay")
    with pytest.raises(ConfigError, match="cassette_dir"):
        build_provider(config, "replay")


def test_a_cassette_directory_with_nothing_in_it_is_refused(tmp_path: Path) -> None:
    config = AppConfig(pack=ACME, provider="replay", cassette_dir=tmp_path)
    with pytest.raises(ConfigError, match="cannot load cassettes"):
        build_provider(config, "replay")


def test_the_replay_provider_holds_every_recorded_conversation() -> None:
    """A person typing at the running service picks which recorded conversation to have, so the
    provider has to hold all of them - unlike a test, which replays exactly one."""
    merged = load_cassettes(CASSETTES)
    files = sorted(CASSETTES.glob("*.json"))
    separate = sum(len(Cassette.load(path).interactions) for path in files)
    assert len(merged.interactions) <= separate
    assert len(merged.interactions) > 0


def test_two_recordings_that_disagree_about_one_request_are_refused(tmp_path: Path) -> None:
    """Merging is safe because the key is the request fingerprint. A genuine disagreement means
    one of the files is stale, and guessing by directory order would hide it."""
    request = CompletionRequest(
        model="m", purpose="node", system=[SystemBlock(text="hello")], messages=[]
    )
    for name, text in (("a.json", "one"), ("b.json", "two")):
        cassette = Cassette()
        cassette.put(request, CompletionResponse(model="m", text=text))
        cassette.save(tmp_path / name)

    with pytest.raises(ValueError, match="different responses"):
        load_cassettes(tmp_path)


# -- what a deployment may say about the customer ------------------------------------------


def test_a_configured_context_may_not_verify_its_own_identity() -> None:
    """DESIGN.md section 10: ``identity_verified`` is set by the verification workflow and by
    nothing else. A configuration file that could set it would open every identity gate in the
    pack, quietly, at startup."""
    config = AppConfig(
        pack=QUEUE_PACK,
        provider="none",
        new_conversation_context={"customer": {"ref": "cus_1", "identity_verified": True}},
    )
    with pytest.raises(ConfigError, match="identity_verified"):
        build_runtime(load_pack(QUEUE_PACK), config)


def test_a_configured_context_has_to_be_one() -> None:
    config = AppConfig(
        pack=QUEUE_PACK, provider="none", new_conversation_context={"customer": "Sam"}
    )
    with pytest.raises(ConfigError, match="ConversationContext"):
        build_runtime(load_pack(QUEUE_PACK), config)


def test_a_context_that_is_a_customer_record_is_accepted() -> None:
    runtime = build_runtime(
        load_pack(QUEUE_PACK),
        AppConfig(
            pack=QUEUE_PACK,
            provider="none",
            new_conversation_context=json.loads(DEMO_CONFIG.read_text(encoding="utf-8"))[
                "new_conversation_context"
            ],
        ),
    )
    assert runtime.config.new_conversation_context["customer"]["ref"] == "cus_acme_1"
    assert runtime.provider_name == "none"
    assert runtime.llm is None


def test_a_channel_the_pack_did_not_enable_is_not_served() -> None:
    """DESIGN.md section 12: "Packs enable channels in ``pack.yaml``."

    ``queue_pack`` declares ``web_chat`` only, so an email adapter is refused rather than served
    under a pack whose timeouts, persona and policies were written for a browser.
    """

    class Mail:
        channel = "email"

        def conversation_key(self, raw: object) -> str:  # pragma: no cover - never reached
            raise NotImplementedError

        async def parse_inbound(self, raw: object) -> InboundMessage:  # pragma: no cover
            raise NotImplementedError

        async def send(self, conversation: object, msg: object) -> None:  # pragma: no cover
            raise NotImplementedError

    config = AppConfig(pack=QUEUE_PACK, provider="none")
    with pytest.raises(ConfigError, match="does not enable"):
        build_runtime(load_pack(QUEUE_PACK), config, adapters=[Mail()])


def test_the_sample_pack_enables_web_chat() -> None:
    runtime = build_runtime(load_pack(ACME), AppConfig(pack=ACME, provider="none"))
    assert sorted(runtime.hub.adapters) == ["web_chat"]
    assert "web_chat" in load_pack(ACME).manifest.channels
