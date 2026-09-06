from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# A server that always returns a cursor would otherwise loop forever. Generous enough
# that no realistic capability list hits it, low enough that a broken server costs a
# bounded number of round-trips rather than a hung discovery pass.
MAX_PAGES = 100

CURSOR_KEYS = ("nextCursor", "next_cursor")


def next_cursor_from(result: Any) -> str | None:
    """The cursor for the following page, in either spelling.

    The wire format is `nextCursor`; the `mcp` 2.x models expose `next_cursor`. Both
    are accepted so this works whichever side it is handed."""
    if isinstance(result, dict):
        for key in CURSOR_KEYS:
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
        return None
    for key in CURSOR_KEYS:
        value = getattr(result, key, None)
        if isinstance(value, str) and value:
            return value
    return None


async def collect_pages(
    fetch: Callable[[str | None], Awaitable[Any]],
    *,
    merge: Callable[[Any, Any], Any],
    label: str = "list",
) -> Any:
    """Follow `nextCursor` until the server stops offering one.

    ``fetch`` takes a cursor (None for the first page) and returns that page.
    ``merge`` folds a page into the accumulated result. Bounded by MAX_PAGES.
    """
    accumulated = await fetch(None)
    cursor = next_cursor_from(accumulated)
    pages = 1

    while cursor:
        if pages >= MAX_PAGES:
            logger.warning(
                "%s: stopping after %d pages; the server is still offering a cursor",
                label,
                pages,
            )
            break
        page = await fetch(cursor)
        accumulated = merge(accumulated, page)
        next_ = next_cursor_from(page)
        if next_ == cursor:
            # A server repeating the same cursor is not making progress.
            logger.warning("%s: server repeated cursor %r; stopping", label, cursor)
            break
        cursor = next_
        pages += 1

    return accumulated
