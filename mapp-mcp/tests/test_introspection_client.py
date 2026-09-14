"""What the introspection client hands upward, and what it refuses to.

Narrow on purpose: the transport is exercised against the real component later.
These pin the two decisions the client makes on its own -- that an inactive
answer carries nothing forward, and that being unable to ask is not the same as
being told no.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from introspection_client import INACTIVE  # noqa: E402
from introspection_client import IntrospectionClient  # noqa: E402
from introspection_client import MAX_CACHE_SECONDS  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class RecordingClient(IntrospectionClient):
    """Replaces only the network, so the caching and normalising are the real ones."""

    def __init__(self, answers, **kwargs):
        super().__init__("http://mcp-auth:8080", client_id="mapp-mcp",
                         client_secret="secret", resource="http://mcp.localhost/mcp",
                         **kwargs)
        self.answers = list(answers)
        self.posts = []

    def _post(self, fields):
        self.posts.append(fields)
        return self.answers.pop(0)


class NormalisationTests(unittest.TestCase):
    def test_an_inactive_answer_carries_nothing_forward(self) -> None:
        """A record that says inactive must not also hand over its claims.

        Passing the payload through survived mutation until this existed. The
        scopes and subject of a token the component just declined are exactly
        the values a caller must not be able to read off it.
        """
        client = RecordingClient([
            {"active": False, "scope": "apply", "sub": "oauth:someone", "aud": "x"}
        ])
        self.assertEqual(INACTIVE, client.introspect("mapp_a_revoked"))

    def test_the_resource_is_sent_on_every_call(self) -> None:
        """So the component compares audiences for us, on every call."""
        client = RecordingClient([{"active": True}])
        client.introspect("mapp_a_live")
        self.assertEqual("http://mcp.localhost/mcp", client.posts[0]["resource"])


class CacheTests(unittest.TestCase):
    def test_a_repeat_within_the_window_does_not_ask_again(self) -> None:
        clock = FakeClock()
        client = RecordingClient([{"active": True, "scope": "mcp:connect"}],
                                 cache_seconds=30, clock=clock)
        client.introspect("mapp_a_live")
        client.introspect("mapp_a_live")
        self.assertEqual(1, len(client.posts))

    def test_the_window_expires(self) -> None:
        clock = FakeClock()
        answers = [{"active": True, "scope": "mcp:connect"}, {"active": False}]
        client = RecordingClient(answers, cache_seconds=30, clock=clock)
        client.introspect("mapp_a_live")
        clock.now += 31
        self.assertEqual(INACTIVE, client.introspect("mapp_a_live"))
        self.assertEqual(2, len(client.posts))

    def test_the_window_cannot_be_configured_longer_than_the_bound(self) -> None:
        """A revoked grant has to stop working promptly, and this is the whole
        of the window in which it does not."""
        client = RecordingClient([{"active": True}], cache_seconds=3600)
        self.assertEqual(MAX_CACHE_SECONDS, client._cache_seconds)

    def test_two_tokens_do_not_share_an_answer(self) -> None:
        client = RecordingClient([
            {"active": True, "scope": "mcp:connect"},
            {"active": False},
        ])
        self.assertTrue(client.introspect("mapp_a_one")["active"])
        self.assertEqual(INACTIVE, client.introspect("mapp_a_two"))


if __name__ == "__main__":
    unittest.main()
