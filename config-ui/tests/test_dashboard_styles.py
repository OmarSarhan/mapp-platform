"""Every class the dashboard renders must be defined in the stylesheet.

The approvals panel shipped with fourteen class names and no CSS behind any of
them.  Fifty-nine component tests passed throughout: they assert structure and
behaviour through the testing library, which neither loads nor consults
``app.css``.  Nothing in either suite related the two files, so an unstyled
panel was indistinguishable from a styled one.

This derives the used set from the sources rather than listing it, so a class
added to the markup tomorrow is checked tomorrow.
"""

from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCES = ROOT / "src"
STYLESHEET = ROOT / "static" / "app.css"

#: Interpolation stands in for a value only known at runtime.  The fragments
#: either side of it are still literal, so they are still checked -- as
#: prefixes rather than whole names.
HOLE = "\x00"

#: Classes the markup already used with no rule behind them before this guard
#: existed.  They are pre-existing platform UI, left alone deliberately rather
#: than styled speculatively: this pin exists so the set cannot quietly grow,
#: not to bless it.  Removing a name from here after styling it is correct;
#: adding one means an unstyled class reached the markup.
KNOWN_UNSTYLED = frozenset({
    "categorized-theme-controls",
    "filter-panel-controls",
    "geometry-swatch-controls",
    "legend-panel-controls",
    "loading",
    "status-region",
    "style-elements-order",
    "style-panel-controls",
    "theme-usage",
    "validation-hint",
})

#: ``className="a b"``, ``className={`a ${x}`}`` and ``className={'a'}``.  A
#: className built by a helper or a bare ternary is not matched; this
#: under-detects rather than reporting a class that is not really used.
CLASS_ATTRIBUTE = re.compile(
    r"""className=(?:"([^"]*)"|\{`([^`]*)`\}|\{'([^']*)'\})"""
)
INTERPOLATION = re.compile(r"\$\{[^}]*\}")
SELECTOR = re.compile(r"\.(-?[_a-zA-Z][\w-]*)")


def defined_classes() -> set[str]:
    return set(SELECTOR.findall(STYLESHEET.read_text()))


def used_classes() -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for source in sorted(SOURCES.rglob("*.jsx")):
        if source.name.endswith(".test.jsx"):
            continue
        text = source.read_text()
        for match in CLASS_ATTRIBUTE.finditer(text):
            literal = next(group for group in match.groups() if group is not None)
            for token in INTERPOLATION.sub(HOLE, literal).split():
                used.setdefault(token, set()).add(source.name)
    return used


def is_satisfied(token: str, defined: set[str]) -> bool:
    if HOLE not in token:
        return token in defined
    # ``approval-risk-${risk}`` is satisfied by any rule for a name starting
    # ``approval-risk-``, since which one applies depends on the value.
    prefix = token.split(HOLE)[0]
    if not prefix:
        return True
    return any(name.startswith(prefix) for name in defined)


class StylesheetCoverageTests(unittest.TestCase):
    def test_every_rendered_class_has_a_rule(self) -> None:
        defined = defined_classes()
        undefined = {
            token: sources
            for token, sources in used_classes().items()
            if not is_satisfied(token, defined)
            and token not in KNOWN_UNSTYLED
        }
        self.assertEqual(
            {},
            undefined,
            "these classes are rendered but have no rule in app.css, so they "
            "render unstyled: "
            + ", ".join(
                f"{token} ({', '.join(sorted(sources))})"
                for token, sources in sorted(undefined.items())
            ),
        )

    def test_the_pin_does_not_outlive_what_it_pins(self) -> None:
        """A name styled later must leave the pin, or the pin stops meaning
        'unstyled' and starts meaning 'unchecked'."""
        defined = defined_classes()
        self.assertEqual(
            set(),
            KNOWN_UNSTYLED & defined,
            "these are pinned as unstyled but now have a rule; drop them from "
            "KNOWN_UNSTYLED",
        )

    def test_the_pin_does_not_outlive_the_markup(self) -> None:
        """A name no longer rendered must leave the pin too."""
        used = set(used_classes())
        self.assertEqual(
            set(),
            KNOWN_UNSTYLED - used,
            "these are pinned as unstyled but no longer appear in any .jsx; "
            "drop them from KNOWN_UNSTYLED",
        )


if __name__ == "__main__":
    unittest.main()
