import pytest

from ementa import services
from ementa_kit.audit import MemoryAuditLog


@pytest.fixture(autouse=True)
def audit_trail_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every request to an app made here goes to a throwaway list, not to `audit.events` in the
    configured database: its rows are append-only and could never be cleaned up. A test that
    reads the events passes its own `audit=` to `create_app`."""
    monkeypatch.setattr(services, "audit_log", MemoryAuditLog)
