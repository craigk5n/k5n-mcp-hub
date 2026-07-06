import base64
import pytest
from fastapi import HTTPException, Request
from unittest.mock import AsyncMock, MagicMock

from mcp_hub.auth import (
    Authenticator,
    auth_required,
    BasicAuthStrategy,
    NoAuthStrategy,
    build_authenticator,
)
from mcp_hub.config import AuthConfig, BasicAuthConfig


class FakeAuthenticatorReturningFalse:
    async def authenticate(self, request: Request) -> bool:
        return False


class FakeAuthenticatorReturningTrue:
    async def authenticate(self, request: Request) -> bool:
        return True


@pytest.mark.asyncio
async def test_auth_required_raises_401_when_authenticator_returns_false() -> None:
    authenticator = FakeAuthenticatorReturningFalse()
    request = MagicMock(spec=Request)

    dependency = auth_required(authenticator)

    with pytest.raises(HTTPException) as exc_info:
        await dependency(request)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Unauthorized"
    assert exc_info.value.headers is not None
    assert exc_info.value.headers.get("WWW-Authenticate") == 'Basic realm="Restricted"'


@pytest.mark.asyncio
async def test_auth_required_returns_none_when_authenticator_returns_true() -> None:
    authenticator = FakeAuthenticatorReturningTrue()
    request = MagicMock(spec=Request)

    dependency = auth_required(authenticator)

    result: None = await dependency(request)

    assert result is None


@pytest.mark.asyncio
async def test_auth_required_raises_401_when_authenticator_returns_non_bool() -> None:
    class FakeAuthenticatorReturningNonBool:
        async def authenticate(self, request: Request):
            return "true"

    authenticator = FakeAuthenticatorReturningNonBool()
    request = MagicMock(spec=Request)

    dependency = auth_required(authenticator)

    with pytest.raises(HTTPException) as exc_info:
        await dependency(request)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_auth_required_custom_www_authenticate_header() -> None:
    class FakeAuthenticatorReturningFalse:
        async def authenticate(self, request: Request) -> bool:
            return False

    authenticator = FakeAuthenticatorReturningFalse()
    request = MagicMock(spec=Request)

    custom_header = 'Bearer realm="Restricted"'
    dependency = auth_required(authenticator, www_authenticate_header=custom_header)

    with pytest.raises(HTTPException) as exc_info:
        await dependency(request)

    assert exc_info.value.headers.get("WWW-Authenticate") == custom_header


def test_authenticator_protocol_is_runtime_checkable() -> None:
    authenticator_with_method = FakeAuthenticatorReturningTrue()
    assert isinstance(authenticator_with_method, Authenticator)


def test_class_without_authenticate_method_is_not_instance() -> None:
    class NoAuthenticateMethod:
        pass

    obj = NoAuthenticateMethod()
    assert not isinstance(obj, Authenticator)


@pytest.mark.asyncio
async def test_noauth_strategy_always_returns_true() -> None:
    strategy = NoAuthStrategy()
    request = MagicMock(spec=Request)

    result = await strategy.authenticate(request)

    assert result is True


@pytest.mark.asyncio
async def test_noauth_strategy_returns_true_for_any_request() -> None:
    strategy = NoAuthStrategy()

    request_without_headers = MagicMock(spec=Request)
    request_without_headers.headers = {}

    result = await strategy.authenticate(request_without_headers)

    assert result is True


def test_noauth_strategy_is_instance_of_authenticator() -> None:
    strategy = NoAuthStrategy()
    assert isinstance(strategy, Authenticator)


def make_request_with_auth(user: str, password: str) -> MagicMock:
    credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
    request = MagicMock(spec=Request)
    request.headers = {"Authorization": f"Basic {credentials}"}
    return request


@pytest.mark.asyncio
async def test_basic_auth_empty_user_returns_false() -> None:
    strategy = BasicAuthStrategy("", "password")
    request = MagicMock(spec=Request)
    request.headers = {}

    result = await strategy.authenticate(request)

    assert result is False


@pytest.mark.asyncio
async def test_basic_auth_empty_password_returns_false() -> None:
    strategy = BasicAuthStrategy("user", "")
    request = MagicMock(spec=Request)
    request.headers = {}

    result = await strategy.authenticate(request)

    assert result is False


@pytest.mark.asyncio
async def test_basic_auth_empty_user_and_password_returns_false() -> None:
    strategy = BasicAuthStrategy("", "")
    request = MagicMock(spec=Request)
    request.headers = {}

    result = await strategy.authenticate(request)

    assert result is False


@pytest.mark.asyncio
async def test_basic_auth_valid_credentials_returns_true() -> None:
    strategy = BasicAuthStrategy("admin", "secret")
    request = make_request_with_auth("admin", "secret")

    result = await strategy.authenticate(request)

    assert result is True


@pytest.mark.asyncio
async def test_basic_auth_wrong_password_returns_false() -> None:
    strategy = BasicAuthStrategy("admin", "secret")
    request = make_request_with_auth("admin", "wrongpassword")

    result = await strategy.authenticate(request)

    assert result is False


@pytest.mark.asyncio
async def test_basic_auth_wrong_user_returns_false() -> None:
    strategy = BasicAuthStrategy("admin", "secret")
    request = make_request_with_auth("wronguser", "secret")

    result = await strategy.authenticate(request)

    assert result is False


@pytest.mark.asyncio
async def test_basic_auth_missing_header_returns_false() -> None:
    strategy = BasicAuthStrategy("admin", "secret")
    request = MagicMock(spec=Request)
    request.headers = {}

    result = await strategy.authenticate(request)

    assert result is False


@pytest.mark.asyncio
async def test_basic_auth_wrong_scheme_returns_false() -> None:
    strategy = BasicAuthStrategy("admin", "secret")
    request = MagicMock(spec=Request)
    request.headers = {"Authorization": "Bearer sometoken"}

    result = await strategy.authenticate(request)

    assert result is False


@pytest.mark.asyncio
async def test_basic_auth_malformed_base64_returns_false() -> None:
    strategy = BasicAuthStrategy("admin", "secret")
    request = MagicMock(spec=Request)
    request.headers = {"Authorization": "Basic notvalidbase64!!!"}

    result = await strategy.authenticate(request)

    assert result is False


@pytest.mark.asyncio
async def test_basic_auth_no_colon_in_decoded_returns_false() -> None:
    strategy = BasicAuthStrategy("admin", "secret")
    encoded = base64.b64encode(b"nocolonhere").decode()
    request = MagicMock(spec=Request)
    request.headers = {"Authorization": f"Basic {encoded}"}

    result = await strategy.authenticate(request)

    assert result is False


@pytest.mark.asyncio
async def test_basic_auth_both_wrong_returns_false() -> None:
    strategy = BasicAuthStrategy("admin", "secret")
    request = make_request_with_auth("wrong", "wrong")

    result = await strategy.authenticate(request)

    assert result is False


def test_basic_auth_strategy_is_instance_of_authenticator() -> None:
    strategy = BasicAuthStrategy("user", "pass")
    assert isinstance(strategy, Authenticator)


def test_build_authenticator_type_none_returns_noauth_strategy() -> None:
    settings = AuthConfig(type="none")
    authenticator = build_authenticator(settings)
    assert isinstance(authenticator, NoAuthStrategy)


def test_build_authenticator_type_noauth_returns_noauth_strategy() -> None:
    settings = AuthConfig(type="noauth")
    authenticator = build_authenticator(settings)
    assert isinstance(authenticator, NoAuthStrategy)


def test_build_authenticator_type_basic_returns_basic_auth_strategy() -> None:
    settings = AuthConfig(
        type="basic", basic_auth=BasicAuthConfig(register_user="user", register_pass="pass")
    )
    authenticator = build_authenticator(settings)
    assert isinstance(authenticator, BasicAuthStrategy)


def test_build_authenticator_type_empty_returns_basic_auth_strategy() -> None:
    settings = AuthConfig(
        type="", basic_auth=BasicAuthConfig(register_user="admin", register_pass="secret")
    )
    authenticator = build_authenticator(settings)
    assert isinstance(authenticator, BasicAuthStrategy)


def test_build_authenticator_unknown_type_raises_value_error() -> None:
    with pytest.raises(Exception) as exc_info:
        AuthConfig(type="oidc")
    assert "oidc" in str(exc_info.value)


@pytest.mark.asyncio
async def test_build_authenticator_basic_auth_with_valid_credentials() -> None:
    settings = AuthConfig(
        type="basic", basic_auth=BasicAuthConfig(register_user="testuser", register_pass="testpass")
    )
    authenticator = build_authenticator(settings)
    request = make_request_with_auth("testuser", "testpass")
    result = await authenticator.authenticate(request)
    assert result is True
