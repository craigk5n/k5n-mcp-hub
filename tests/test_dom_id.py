import re

from mcp_hub.utils import dom_id

# A DOM id / CSS-selector-safe token: starts with a letter, then only [A-Za-z0-9_-].
_SAFE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class TestDomId:
    def test_plain_id_is_safe(self) -> None:
        assert _SAFE.match(dom_id("my-server"))

    def test_id_with_space_is_safe(self) -> None:
        # The exact bug: "k5n.us webcalendar" produced "#tools-k5n.us webcalendar",
        # an invalid selector that raised htmx:targetError.
        token = dom_id("k5n.us webcalendar")
        assert _SAFE.match(token)
        assert " " not in token
        assert "." not in token

    def test_id_with_dot_slash_colon_is_safe(self) -> None:
        for raw in ("k5n.us", "ns/tool", "a:b", "tool.name", "weird id!"):
            token = dom_id(raw)
            assert _SAFE.match(token), f"unsafe token for {raw!r}: {token!r}"

    def test_selector_built_from_token_is_valid(self) -> None:
        # Emulates the template usage: "#tools-" ~ (id | dom_id) must be a single-id selector.
        selector = f"#tools-{dom_id('k5n.us webcalendar')}"
        assert re.match(r"^#[A-Za-z][A-Za-z0-9_-]*$", selector)

    def test_deterministic(self) -> None:
        assert dom_id("k5n.us webcalendar") == dom_id("k5n.us webcalendar")

    def test_distinct_ids_that_slugify_alike_stay_unique(self) -> None:
        # "k5n.us webcalendar" and "k5n us.webcalendar" slugify to the same slug;
        # the hash suffix must keep them distinct so panels don't cross-wire.
        assert dom_id("k5n.us webcalendar") != dom_id("k5n us.webcalendar")

    def test_starts_with_letter_even_for_numeric_id(self) -> None:
        token = dom_id("123")
        assert _SAFE.match(token)

    def test_accepts_non_str(self) -> None:
        assert _SAFE.match(dom_id(123))
