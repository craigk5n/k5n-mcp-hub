"""The one error type the OpenAPI loader raises."""

from __future__ import annotations

# Validator messages echo values out of the document. A spec is untrusted, so an error
# shown to an admin is capped rather than allowed to carry a megabyte of it.
MAX_MESSAGE_CHARS = 500


class SpecError(Exception):
    """A spec the hub will not accept, and why.

    ``pointer`` is the RFC 6901 JSON pointer to the offending value when one is known
    (``/info/title``), and ``""`` when the problem is with the document as a whole or
    was found before it was parsed.
    """

    def __init__(self, message: str, *, pointer: str = "") -> None:
        if len(message) > MAX_MESSAGE_CHARS:
            message = message[: MAX_MESSAGE_CHARS - 1] + "…"
        super().__init__(message)
        self.message = message
        self.pointer = pointer

    def __str__(self) -> str:
        return f"{self.message} (at {self.pointer})" if self.pointer else self.message
