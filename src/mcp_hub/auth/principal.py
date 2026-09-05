from __future__ import annotations

from dataclasses import dataclass, field

# Subject of the principal produced under `auth.type: none` — authenticated by
# policy rather than by identity.
ANONYMOUS_SUBJECT = "anonymous"


@dataclass(frozen=True)
class Principal:
    """The authenticated caller.

    Frozen on purpose: this is threaded down into the outbound auth path
    (Story 5.5) and nothing there may rewrite whose identity is in play.

    ``token`` holds the raw credential the caller presented, because RFC 8693
    sends it back to the IdP as ``subject_token`` (Epic 6). It is therefore
    secret — never log it, trace it, or persist it.
    """

    subject: str
    issuer: str = ""
    scopes: frozenset[str] = field(default_factory=frozenset)
    token: str = ""
    is_anonymous: bool = False

    @classmethod
    def anonymous(cls) -> Principal:
        return cls(subject=ANONYMOUS_SUBJECT, is_anonymous=True)

    def can_act_as_obo_subject(self) -> bool:
        """Whether this identity can be the ``subject_token`` of an exchange.

        Requires a real identity *and* a bearer token to hand the IdP. Basic auth
        names a caller but yields nothing exchangeable, and the anonymous
        principal is not a user at all (ADR 0001, ADR 0004)."""
        return not self.is_anonymous and bool(self.token)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def __repr__(self) -> str:
        # Default dataclass repr would print the raw token into any log line or
        # traceback that formats a Principal.
        return (
            f"Principal(subject={self.subject!r}, issuer={self.issuer!r}, "
            f"scopes={sorted(self.scopes)!r}, token={'<redacted>' if self.token else ''!r}, "
            f"is_anonymous={self.is_anonymous!r})"
        )
