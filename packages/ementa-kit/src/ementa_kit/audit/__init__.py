"""Tamper-evident audit trail: append-only events, each chained to the one before by SHA-256."""

from ementa_kit.audit.chain import (
    AuditEvent,
    AuditRecord,
    BrokenLink,
    ChainCheck,
    Outcome,
    canonical,
    chain_hash,
    check_event,
    verify_chain,
)
from ementa_kit.audit.store import AuditLog, MemoryAuditLog, PgAuditLog

__all__ = [
    "AuditEvent",
    "AuditLog",
    "AuditRecord",
    "BrokenLink",
    "ChainCheck",
    "MemoryAuditLog",
    "Outcome",
    "PgAuditLog",
    "canonical",
    "chain_hash",
    "check_event",
    "verify_chain",
]
