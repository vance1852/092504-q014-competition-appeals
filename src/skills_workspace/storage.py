"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS competitions (
    competition_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS competitors (
    competitor_id TEXT PRIMARY KEY,
    competition_id TEXT NOT NULL REFERENCES competitions(competition_id),
    person_name TEXT NOT NULL,
    eligible INTEGER NOT NULL CHECK(eligible IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(competition_id, competitor_id)
);
CREATE TABLE IF NOT EXISTS score_versions (
    version_id TEXT PRIMARY KEY,
    competition_id TEXT NOT NULL REFERENCES competitions(competition_id),
    version_label TEXT NOT NULL,
    published_at TEXT NOT NULL,
    appeal_deadline TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS score_entries (
    entry_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES score_versions(version_id),
    competitor_id TEXT NOT NULL REFERENCES competitors(competitor_id),
    score_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(version_id, competitor_id)
);
CREATE TABLE IF NOT EXISTS adjudicators (
    adjudicator_id TEXT PRIMARY KEY,
    competition_id TEXT NOT NULL REFERENCES competitions(competition_id),
    display_name TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(competition_id, adjudicator_id)
);
CREATE TABLE IF NOT EXISTS conflict_declarations (
    conflict_id TEXT PRIMARY KEY,
    competition_id TEXT NOT NULL REFERENCES competitions(competition_id),
    adjudicator_id TEXT NOT NULL REFERENCES adjudicators(adjudicator_id),
    competitor_id TEXT NOT NULL REFERENCES competitors(competitor_id),
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(adjudicator_id, competitor_id)
);
CREATE TABLE IF NOT EXISTS appeal_cases (
    case_id TEXT PRIMARY KEY,
    case_number TEXT NOT NULL UNIQUE,
    version_id TEXT NOT NULL REFERENCES score_versions(version_id),
    competitor_id TEXT NOT NULL REFERENCES competitors(competitor_id),
    grounds TEXT NOT NULL,
    grounds_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    handler_id TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    leased_until TEXT,
    reviewer_id TEXT,
    recommendation TEXT,
    recommendation_basis_digest TEXT,
    recommendation_basis_ref TEXT,
    proposed_score_ref TEXT,
    recommended_at TEXT,
    final_outcome TEXT,
    final_basis_digest TEXT,
    final_basis_ref TEXT,
    corrected_score_ref TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_appeal_cases_version_competitor ON appeal_cases(version_id, competitor_id);
CREATE TABLE IF NOT EXISTS case_evidence (
    evidence_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES appeal_cases(case_id),
    version_seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    storage_ref TEXT NOT NULL,
    media_type TEXT,
    byte_size INTEGER,
    submitted_by TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(case_id, version_seq)
);
CREATE TABLE IF NOT EXISTS case_screenings (
    screening_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES appeal_cases(case_id),
    adjudicator_id TEXT NOT NULL REFERENCES adjudicators(adjudicator_id),
    status TEXT NOT NULL,
    reason TEXT,
    checked_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT NOT NULL,
    UNIQUE(case_id, adjudicator_id)
);
CREATE TABLE IF NOT EXISTS evidence_requests (
    request_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES appeal_cases(case_id),
    note TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_assignments (
    assignment_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES appeal_cases(case_id),
    adjudicator_id TEXT NOT NULL REFERENCES adjudicators(adjudicator_id),
    generation INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    released_at TEXT,
    release_reason TEXT,
    UNIQUE(case_id, generation)
);
CREATE INDEX IF NOT EXISTS idx_assignments_case ON case_assignments(case_id);
CREATE TABLE IF NOT EXISTS score_reference_updates (
    update_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES appeal_cases(case_id),
    version_id TEXT NOT NULL REFERENCES score_versions(version_id),
    competitor_id TEXT NOT NULL REFERENCES competitors(competitor_id),
    previous_ref TEXT NOT NULL,
    new_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
