import pytest
from fastapi.testclient import TestClient

from devhub.app import create_app
from devhub.models.server import FaultInjection, RegisteredServer


class TestFaultsTemplate:
    def test_faults_form_posts_to_correct_endpoint(self) -> None:
        app = create_app()
        templates = app.state.templates

        template = templates.get_template("faults.html")
        html = template.render(
            server_id="test-server-123",
            enabled=True,
            timeout_enabled=True,
            timeout_millis=5000,
            malformed_json=False,
            invalid_method=True,
            sse_interrupt=False,
        )

        assert 'action="/ui/server/test-server-123/faults"' in html

    def test_faults_form_checkbox_enabled_checked_when_true(self) -> None:
        app = create_app()
        templates = app.state.templates

        template = templates.get_template("faults.html")
        html = template.render(
            server_id="test-server",
            enabled=True,
            timeout_enabled=False,
            timeout_millis=1000,
            malformed_json=False,
            invalid_method=False,
            sse_interrupt=False,
        )

        assert 'id="enabled"' in html
        assert 'name="enabled"' in html
        assert "checked" in html

    def test_faults_form_checkbox_enabled_unchecked_when_false(self) -> None:
        app = create_app()
        templates = app.state.templates

        template = templates.get_template("faults.html")
        html = template.render(
            server_id="test-server",
            enabled=False,
            timeout_enabled=False,
            timeout_millis=1000,
            malformed_json=False,
            invalid_method=False,
            sse_interrupt=False,
        )

        assert 'id="enabled"' in html
        assert 'name="enabled"' in html
        assert "checked" not in html

    def test_faults_form_checkbox_timeout_enabled(self) -> None:
        app = create_app()
        templates = app.state.templates

        template = templates.get_template("faults.html")
        html = template.render(
            server_id="test-server",
            enabled=False,
            timeout_enabled=True,
            timeout_millis=2000,
            malformed_json=False,
            invalid_method=False,
            sse_interrupt=False,
        )

        assert 'id="timeout_enabled"' in html
        assert 'name="timeout_enabled"' in html
        assert "checked" in html

    def test_faults_form_checkbox_malformed_json(self) -> None:
        app = create_app()
        templates = app.state.templates

        template = templates.get_template("faults.html")
        html = template.render(
            server_id="test-server",
            enabled=False,
            timeout_enabled=False,
            timeout_millis=1000,
            malformed_json=True,
            invalid_method=False,
            sse_interrupt=False,
        )

        assert 'id="malformed_json"' in html
        assert 'name="malformed_json"' in html
        assert "checked" in html

    def test_faults_form_checkbox_invalid_method(self) -> None:
        app = create_app()
        templates = app.state.templates

        template = templates.get_template("faults.html")
        html = template.render(
            server_id="test-server",
            enabled=False,
            timeout_enabled=False,
            timeout_millis=1000,
            malformed_json=False,
            invalid_method=True,
            sse_interrupt=False,
        )

        assert 'id="invalid_method"' in html
        assert 'name="invalid_method"' in html
        assert "checked" in html

    def test_faults_form_checkbox_sse_interrupt(self) -> None:
        app = create_app()
        templates = app.state.templates

        template = templates.get_template("faults.html")
        html = template.render(
            server_id="test-server",
            enabled=False,
            timeout_enabled=False,
            timeout_millis=1000,
            malformed_json=False,
            invalid_method=False,
            sse_interrupt=True,
        )

        assert 'id="sse_interrupt"' in html
        assert 'name="sse_interrupt"' in html
        assert "checked" in html

    def test_faults_form_timeout_millis_bounds(self) -> None:
        app = create_app()
        templates = app.state.templates

        template = templates.get_template("faults.html")
        html = template.render(
            server_id="test-server",
            enabled=False,
            timeout_enabled=False,
            timeout_millis=15000,
            malformed_json=False,
            invalid_method=False,
            sse_interrupt=False,
        )

        assert 'id="timeout_millis"' in html
        assert 'name="timeout_millis"' in html
        assert 'min="1"' in html
        assert 'max="60000"' in html
        assert 'value="15000"' in html


class TestFaultsEndpoint:
    @pytest.mark.asyncio
    async def test_get_faults_returns_form(self) -> None:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        registry = app.state.registry
        server = RegisteredServer(
            id="test-server-456",
            url="http://localhost:8080",
            name="Test Server",
            fault_injection=FaultInjection(
                enabled=True,
                timeout_enabled=True,
                timeout_millis=3000,
                malformed_json=False,
                invalid_method=True,
                sse_interrupt=False,
            ),
        )
        await registry.register(server)

        response = client.get("/ui/server/test-server-456/faults")

        assert response.status_code == 200
        assert 'action="/ui/server/test-server-456/faults"' in response.text
        assert 'name="enabled"' in response.text
        assert "checked" in response.text
        assert 'name="timeout_enabled"' in response.text
        assert "checked" in response.text
        assert 'name="malformed_json"' in response.text
        assert 'name="invalid_method"' in response.text
        assert "checked" in response.text
        assert 'name="sse_interrupt"' in response.text
        assert 'name="timeout_millis"' in response.text
        assert 'min="1"' in response.text
        assert 'max="60000"' in response.text
        assert 'value="3000"' in response.text

    @pytest.mark.asyncio
    async def test_get_faults_returns_404_for_unknown_server(self) -> None:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/ui/server/nonexistent-server/faults")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_faults_timeout_millis_zero_defaults_to_2000(self) -> None:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        registry = app.state.registry
        server = RegisteredServer(
            id="test-server-zero-timeout",
            url="http://localhost:8080",
            name="Test Server",
            fault_injection=FaultInjection(
                enabled=True,
                timeout_enabled=True,
                timeout_millis=0,
                malformed_json=False,
                invalid_method=False,
                sse_interrupt=False,
            ),
        )
        await registry.register(server)

        response = client.get("/ui/server/test-server-zero-timeout/faults")

        assert response.status_code == 200
        assert 'value="2000"' in response.text

    @pytest.mark.asyncio
    async def test_get_faults_timeout_millis_negative_defaults_to_2000(self) -> None:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        registry = app.state.registry
        server = RegisteredServer(
            id="test-server-negative-timeout",
            url="http://localhost:8080",
            name="Test Server",
            fault_injection=FaultInjection(
                enabled=True,
                timeout_enabled=True,
                timeout_millis=-100,
                malformed_json=False,
                invalid_method=False,
                sse_interrupt=False,
            ),
        )
        await registry.register(server)

        response = client.get("/ui/server/test-server-negative-timeout/faults")

        assert response.status_code == 200
        assert 'value="2000"' in response.text


class TestFaultsPostEndpoint:
    @pytest.mark.asyncio
    async def test_post_faults_persists_settings(self) -> None:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        registry = app.state.registry
        server = RegisteredServer(
            id="test-server-post",
            url="http://localhost:8080",
            name="Test Server",
        )
        await registry.register(server)

        response = client.post(
            "/ui/server/test-server-post/faults",
            data={
                "enabled": "on",
                "timeout_enabled": "on",
                "timeout_millis": "5000",
                "malformed_json": "on",
                "invalid_method": "on",
                "sse_interrupt": "on",
            },
        )

        assert response.status_code == 200
        updated_server = await registry.get("test-server-post")
        assert updated_server is not None
        assert updated_server.fault_injection.enabled is True
        assert updated_server.fault_injection.timeout_enabled is True
        assert updated_server.fault_injection.timeout_millis == 5000
        assert updated_server.fault_injection.malformed_json is True
        assert updated_server.fault_injection.invalid_method is True
        assert updated_server.fault_injection.sse_interrupt is True

    @pytest.mark.asyncio
    async def test_post_faults_timeout_millis_clamped_to_max(self) -> None:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        registry = app.state.registry
        server = RegisteredServer(
            id="test-server-clamp",
            url="http://localhost:8080",
            name="Test Server",
        )
        await registry.register(server)

        response = client.post(
            "/ui/server/test-server-clamp/faults",
            data={
                "enabled": "on",
                "timeout_enabled": "on",
                "timeout_millis": "99999",
            },
        )

        assert response.status_code == 200
        updated_server = await registry.get("test-server-clamp")
        assert updated_server is not None
        assert updated_server.fault_injection.timeout_millis == 60000

    @pytest.mark.asyncio
    async def test_post_faults_returns_404_for_unknown_server(self) -> None:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/nonexistent-server-post/faults",
            data={
                "enabled": "on",
                "timeout_millis": "5000",
            },
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_post_faults_timeout_millis_defaults_to_2000_when_missing(
        self,
    ) -> None:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        registry = app.state.registry
        server = RegisteredServer(
            id="test-server-default-timeout",
            url="http://localhost:8080",
            name="Test Server",
        )
        await registry.register(server)

        response = client.post(
            "/ui/server/test-server-default-timeout/faults",
            data={
                "enabled": "on",
                "timeout_enabled": "on",
            },
        )

        assert response.status_code == 200
        updated_server = await registry.get("test-server-default-timeout")
        assert updated_server is not None
        assert updated_server.fault_injection.timeout_millis == 2000

    @pytest.mark.asyncio
    async def test_post_faults_timeout_millis_defaults_to_2000_when_zero(self) -> None:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        registry = app.state.registry
        server = RegisteredServer(
            id="test-server-zero-timeout-post",
            url="http://localhost:8080",
            name="Test Server",
        )
        await registry.register(server)

        response = client.post(
            "/ui/server/test-server-zero-timeout-post/faults",
            data={
                "enabled": "on",
                "timeout_enabled": "on",
                "timeout_millis": "0",
            },
        )

        assert response.status_code == 200
        updated_server = await registry.get("test-server-zero-timeout-post")
        assert updated_server is not None
        assert updated_server.fault_injection.timeout_millis == 2000

    @pytest.mark.asyncio
    async def test_post_faults_timeout_millis_defaults_to_2000_when_negative(
        self,
    ) -> None:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        registry = app.state.registry
        server = RegisteredServer(
            id="test-server-negative-timeout-post",
            url="http://localhost:8080",
            name="Test Server",
        )
        await registry.register(server)

        response = client.post(
            "/ui/server/test-server-negative-timeout-post/faults",
            data={
                "enabled": "on",
                "timeout_enabled": "on",
                "timeout_millis": "-100",
            },
        )

        assert response.status_code == 200
        updated_server = await registry.get("test-server-negative-timeout-post")
        assert updated_server is not None
        assert updated_server.fault_injection.timeout_millis == 2000

    @pytest.mark.asyncio
    async def test_post_faults_checkboxes_default_to_false_when_not_present(
        self,
    ) -> None:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        registry = app.state.registry
        server = RegisteredServer(
            id="test-server-checkbox-defaults",
            url="http://localhost:8080",
            name="Test Server",
        )
        await registry.register(server)

        response = client.post(
            "/ui/server/test-server-checkbox-defaults/faults",
            data={
                "enabled": "on",
            },
        )

        assert response.status_code == 200
        updated_server = await registry.get("test-server-checkbox-defaults")
        assert updated_server is not None
        assert updated_server.fault_injection.enabled is True
        assert updated_server.fault_injection.timeout_enabled is False
        assert updated_server.fault_injection.malformed_json is False
        assert updated_server.fault_injection.invalid_method is False
        assert updated_server.fault_injection.sse_interrupt is False
