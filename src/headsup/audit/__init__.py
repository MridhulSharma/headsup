"""Hash-chained JSONL audit log, its verifier, and the after-action report.

Import from this package rather than from the submodules:

    from headsup.audit import AuditLog, verify, write_report

`chain` is the record: one JSON line per bus entry in `data/audit/<run_id>.jsonl`,
each line carrying `sha256(prev_hash + canonical_json(record))`, so an edit
anywhere shows up at the line it was made on. `report` is what the log is for:
`hu audit report` verifies the chain, digests the run and has the judgment model
write a Markdown after-action that cites log entries by their chain hash.
`headsup.agents.auditor` is the agent that fills the log off the bus.
"""

from __future__ import annotations

from headsup.audit.chain import (
    CITE_LENGTH,
    DEFAULT_AUDIT_DIR,
    ENV_AUDIT_DIR,
    ENV_RUN_ID,
    GENESIS,
    HASH_FIELD,
    AuditLog,
    AuditRecord,
    Break,
    BrokenLine,
    ChainError,
    ChainReport,
    Hex64,
    audit_dir,
    body_of,
    canonical_json,
    chain_hash,
    check_run_id,
    log_path,
    new_run_id,
    read_lines,
    records,
    run_id_from_env,
    runs,
    verify,
    verify_file,
)
from headsup.audit.report import (
    AFTER_ACTION_RULES,
    DECISIVE_STREAMS,
    MAX_HIGHLIGHTS,
    PURPOSE,
    AfterAction,
    Citations,
    Finding,
    Highlight,
    LogDigest,
    after_action,
    digest,
    facts_of,
    highlights,
    markdown,
    report_path,
    resolve,
    rules_prose,
    write_report,
)

__all__ = [
    "AFTER_ACTION_RULES",
    "CITE_LENGTH",
    "DECISIVE_STREAMS",
    "DEFAULT_AUDIT_DIR",
    "ENV_AUDIT_DIR",
    "ENV_RUN_ID",
    "GENESIS",
    "HASH_FIELD",
    "MAX_HIGHLIGHTS",
    "PURPOSE",
    "AfterAction",
    "AuditLog",
    "AuditRecord",
    "Break",
    "BrokenLine",
    "ChainError",
    "ChainReport",
    "Citations",
    "Finding",
    "Hex64",
    "Highlight",
    "LogDigest",
    "after_action",
    "audit_dir",
    "body_of",
    "canonical_json",
    "chain_hash",
    "check_run_id",
    "digest",
    "facts_of",
    "highlights",
    "log_path",
    "markdown",
    "new_run_id",
    "read_lines",
    "records",
    "report_path",
    "resolve",
    "rules_prose",
    "run_id_from_env",
    "runs",
    "verify",
    "verify_file",
    "write_report",
]
