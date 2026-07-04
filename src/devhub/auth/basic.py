from __future__ import annotations

import base64
import binascii
import secrets

from fastapi import Request


class BasicAuthStrategy:
    def __init__(self, user: str, password: str) -> None:
        self.user = user
        self.password = password

    async def authenticate(self, request: Request) -> bool:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            return False

        encoded_creds = auth_header[6:]

        try:
            decoded_creds = base64.b64decode(encoded_creds).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return False

        parts = decoded_creds.split(":", 1)
        if len(parts) != 2:
            return False

        candidate_user, candidate_password = parts

        user_matches = secrets.compare_digest(self.user, candidate_user)
        password_matches = secrets.compare_digest(self.password, candidate_password)

        return user_matches and password_matches
