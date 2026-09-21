"""The health budget must outlast what startup does before it serves.

Two numbers in two files decide whether this service can start at all, and
nothing related them.

`app.py` waits up to `FEDERATION_VERIFY_STARTUP_GRACE_SECONDS` for the first
federation pass before it calls `serve_forever`. The `Dockerfile`'s
`HEALTHCHECK` decides how long Docker will wait for `/healthz` before marking
the container unhealthy. When the first exceeds the second, a deployment that
actually uses the grace -- a cold start with two federated sources, or one
unreachable alias at five seconds per connect -- is marked unhealthy a moment
before it begins serving, and everything with `depends_on: service_healthy`
refuses to start.

It shipped that way: a 60 second grace against a 65 second budget. The symptom
gives no hint of the cause, because compose reports "dependency failed to
start" about caddy, whose only fault was depending on this.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"

#: How much room the budget must have over the startup wait. A margin rather
#: than a bare inequality: equality is a race, and the grace is a *floor* on
#: how long startup takes, not a ceiling -- the pass still has to finish
#: whatever it is doing when the timer fires.
REQUIRED_MARGIN_SECONDS = 20


def health_budget():
    """Seconds Docker waits before calling this container unhealthy.

    `--start-period` is grace during which a failing probe neither counts
    against retries nor marks the container unhealthy, so the budget is that
    plus the retries it will then tolerate.
    """
    directive = re.search(r"HEALTHCHECK([^\n]*)\\", DOCKERFILE.read_text())
    assert directive, "no HEALTHCHECK in the Dockerfile"
    options = dict(
        re.findall(r"--(interval|timeout|start-period|retries)=(\w+)",
                   directive.group(1))
    )
    seconds = lambda value: int(value.rstrip("s"))  # noqa: E731
    return (
        seconds(options["start-period"])
        + seconds(options["retries"]) * seconds(options["interval"])
    )


class StartupBudgetTests(unittest.TestCase):
    def test_the_health_budget_outlasts_the_federation_wait(self) -> None:
        import app

        grace = app.FEDERATION_VERIFY_STARTUP_GRACE_SECONDS
        budget = health_budget()
        self.assertGreaterEqual(
            budget - grace,
            REQUIRED_MARGIN_SECONDS,
            f"startup waits up to {grace}s for federation before it serves,"
            f" and the healthcheck allows {budget}s. Raise --start-period in"
            " config-ui/Dockerfile, or lower the grace.",
        )

    def test_the_start_period_alone_covers_the_wait(self) -> None:
        """Retries are for a service that is up and briefly failing. A service
        that has not begun listening should be inside its start period, or the
        first probes burn retries that a later hiccup then cannot afford."""
        import app

        directive = re.search(r"HEALTHCHECK([^\n]*)\\", DOCKERFILE.read_text())
        start_period = int(
            re.search(r"--start-period=(\d+)s", directive.group(1)).group(1)
        )
        self.assertGreater(
            start_period, app.FEDERATION_VERIFY_STARTUP_GRACE_SECONDS
        )


if __name__ == "__main__":
    unittest.main()
