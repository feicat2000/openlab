"""
Audit Trail – tamper-evident, hash-chained log of all critical OpenLab actions.

Each entry is SHA-256 chained to the previous entry so that any modification
to historical records is detectable by ``verify_integrity()``.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class AuditEntry:
    entry_id: str              # UUID4 hex
    timestamp: float
    operator: str
    action: str                # "protocol_started" | "step_completed" | "estop" | ...
    resource: str              # run_id, protocol_name, equipment_name
    detail: Dict[str, Any]     # action-specific payload
    previous_hash: str         # SHA-256 of previous entry (or "GENESIS")
    hash: str                  # SHA-256 of key fields


# ── Hash helper ───────────────────────────────────────────────────────────────

def _compute_hash(e: AuditEntry) -> str:
    payload = (
        f"{e.entry_id}{e.timestamp}{e.operator}"
        f"{e.action}{e.resource}{e.previous_hash}"
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ── AuditTrail ────────────────────────────────────────────────────────────────

class AuditTrail:
    """
    Append-only, hash-chained audit trail.

    Usage::

        trail = AuditTrail(Path("runs/audit.jsonl"))
        trail.log("alice", "protocol_started", "run_001", {"protocol": "DNA Extraction"})
        ok, violations = trail.verify_integrity()
    """

    def __init__(self, trail_path: Optional[Path] = None) -> None:
        self._entries: List[AuditEntry] = []
        if trail_path and Path(trail_path).exists():
            self.load(Path(trail_path))

    # ── Write ─────────────────────────────────────────────────────────────────

    def log(
        self,
        operator: str,
        action: str,
        resource: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> AuditEntry:
        previous_hash = self._entries[-1].hash if self._entries else "GENESIS"
        entry_id = uuid.uuid4().hex

        entry = AuditEntry(
            entry_id=entry_id,
            timestamp=time.time(),
            operator=operator,
            action=action,
            resource=resource,
            detail=detail or {},
            previous_hash=previous_hash,
            hash="",
        )
        entry.hash = _compute_hash(entry)
        self._entries.append(entry)
        logger.debug(f"Audit: [{action}] by {operator} on {resource}")
        return entry

    # ── Integrity ─────────────────────────────────────────────────────────────

    def verify_integrity(self) -> Tuple[bool, List[str]]:
        """
        Re-compute every hash in the chain.
        Returns (all_ok, list_of_violation_messages).
        """
        violations: List[str] = []
        for i, entry in enumerate(self._entries):
            expected_prev = self._entries[i - 1].hash if i > 0 else "GENESIS"
            if entry.previous_hash != expected_prev:
                violations.append(
                    f"Entry {i} ({entry.entry_id[:8]}…): "
                    f"previous_hash mismatch (expected {expected_prev[:16]}…)"
                )
            computed = _compute_hash(entry)
            if computed != entry.hash:
                violations.append(
                    f"Entry {i} ({entry.entry_id[:8]}…): "
                    f"hash mismatch – tampered data detected"
                )
        return len(violations) == 0, violations

    # ── Query ─────────────────────────────────────────────────────────────────

    def search(
        self,
        action: Optional[str] = None,
        operator: Optional[str] = None,
        resource: Optional[str] = None,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
    ) -> List[AuditEntry]:
        results = self._entries
        if action:
            results = [e for e in results if e.action == action]
        if operator:
            results = [e for e in results if e.operator == operator]
        if resource:
            results = [e for e in results if e.resource == resource]
        if start_ts is not None:
            results = [e for e in results if e.timestamp >= start_ts]
        if end_ts is not None:
            results = [e for e in results if e.timestamp <= end_ts]
        return results

    def get(self, entry_id: str) -> Optional[AuditEntry]:
        for e in self._entries:
            if e.entry_id == entry_id:
                return e
        return None

    # ── Signing ───────────────────────────────────────────────────────────────

    def sign(
        self,
        entry_id: str,
        operator: str,
        signature_text: str,
    ) -> AuditEntry:
        """Append a signature entry referencing a specific audit entry."""
        ref_entry = self.get(entry_id)
        if ref_entry is None:
            raise ValueError(f"Entry {entry_id} not found in audit trail")
        sig_entry = self.log(
            operator=operator,
            action="signature",
            resource=entry_id,
            detail={
                "signature_text": signature_text,
                "signed_entry_id": entry_id,
                "signed_action": ref_entry.action,
            },
        )
        return sig_entry

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as fh:
            for entry in self._entries:
                fh.write(json.dumps(asdict(entry)) + "\n")
        logger.info(f"Audit trail saved: {path} ({len(self._entries)} entries)")

    def load(self, path: Path) -> None:
        path = Path(path)
        self._entries = []
        with open(path) as fh:
            for line_num, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    self._entries.append(AuditEntry(**data))
                except Exception as exc:
                    logger.warning(f"Audit trail: skipping corrupted entry at line {line_num}: {exc}")
        logger.info(f"Audit trail loaded: {len(self._entries)} entries from {path}")

    # ── Export ────────────────────────────────────────────────────────────────

    def export_report(self, path: Path, fmt: str = "html") -> None:
        path = Path(path)
        if fmt == "csv":
            self._export_csv(path)
        else:
            self._export_html(path)
        logger.info(f"Audit report exported: {path}")

    def _export_html(self, path: Path) -> None:
        import html as _html
        rows = ""
        for e in self._entries:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.timestamp))
            detail_str = _html.escape(json.dumps(e.detail))
            rows += (
                f"<tr><td>{ts}</td><td>{_html.escape(e.operator)}</td>"
                f"<td>{_html.escape(e.action)}</td>"
                f"<td>{_html.escape(e.resource)}</td><td><code>{detail_str}</code></td>"
                f"<td><code>{_html.escape(e.hash[:16])}…</code></td></tr>\n"
            )
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>OpenLab Audit Trail</title>
<style>
  body {{ font-family: sans-serif; padding: 1em; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ccc; padding: 0.4em 0.6em; text-align: left; }}
  th {{ background: #2c3e50; color: white; }}
  tr:nth-child(even) {{ background: #f5f5f5; }}
  code {{ font-size: 0.85em; }}
</style></head>
<body>
<h1>OpenLab Audit Trail</h1>
<p>Total entries: {len(self._entries)}</p>
<table>
<thead><tr><th>Timestamp</th><th>Operator</th><th>Action</th>
<th>Resource</th><th>Detail</th><th>Hash</th></tr></thead>
<tbody>
{rows}
</tbody></table>
</body></html>"""
        path.write_text(html)

    def _export_csv(self, path: Path) -> None:
        import csv
        with open(path, "w", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["entry_id", "timestamp", "operator", "action",
                            "resource", "detail", "previous_hash", "hash"],
            )
            writer.writeheader()
            for e in self._entries:
                row = asdict(e)
                row["detail"] = json.dumps(row["detail"])
                writer.writerow(row)

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> List[AuditEntry]:
        return list(self._entries)
