import pathlib

import pytest


class TestReadmeSections:
    REQUIRED_SECTIONS = [
        "Overview",
        "Quick Start",
        "Configuration",
        "Tests",
        "License",
    ]

    def test_readme_exists(self):
        repo_root = pathlib.Path(__file__).parent.parent
        readme_path = repo_root / "README.md"

        assert readme_path.exists(), "README.md must exist at repository root"

    def test_readme_contains_all_six_sections(self):
        repo_root = pathlib.Path(__file__).parent.parent
        readme_path = repo_root / "README.md"

        content = readme_path.read_text()

        for section in self.REQUIRED_SECTIONS:
            assert section in content, f"README.md must contain '{section}' section"

    def test_readme_has_section_headings(self):
        repo_root = pathlib.Path(__file__).parent.parent
        readme_path = repo_root / "README.md"

        content = readme_path.read_text()

        for section in self.REQUIRED_SECTIONS:
            heading_pattern = f"## {section}"
            assert heading_pattern in content, f"README.md must have '## {section}' heading"

    def test_readme_has_required_content_patterns(self):
        repo_root = pathlib.Path(__file__).parent.parent
        readme_path = repo_root / "README.md"

        content = readme_path.read_text()

        assert "pip install -e .[dev]" in content, "Quick Start must contain install command"
        assert "k5n-mcp-hub" in content, "Quick Start must mention running k5n-mcp-hub"
        assert "http://localhost:8080" in content, "Quick Start must contain localhost URL"
        assert "config.yaml" in content, "Configuration must reference config.yaml"
        assert "SERVER_HTTP_PORT" in content, "Configuration must document SERVER_HTTP_PORT"
        assert "MCPHUB_" in content, "Configuration must document MCPHUB_ prefix"
        assert "ruff check ." in content, "Tests must contain ruff check command"
        assert "ruff format --check ." in content, "Tests must contain ruff format check"
        assert "mypy" in content and "src" in content, "Tests must contain mypy command"
        assert "pytest -v" in content, "Tests must contain pytest command"
