import pytest

from polyglot.config import settings


@pytest.fixture
def configure():
    """Override settings (a frozen dataclass) for one test and restore them afterwards."""
    saved = {}

    def apply(**values):
        for name, value in values.items():
            saved.setdefault(name, getattr(settings, name))
            object.__setattr__(settings, name, value)

    yield apply
    for name, value in saved.items():
        object.__setattr__(settings, name, value)
