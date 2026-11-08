"""Pytest configuration and fixtures for {{project_name}}."""

import pytest
from click.testing import CliRunner


@pytest.fixture
def cli_runner():
    """Provide a Click CLI test runner."""
    return CliRunner()
