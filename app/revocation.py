"""Source-revocation service.

A revocation withdraws a *received* email's authorization to supply report
data. It deliberately does not delete anything: the RFC822 original, the
fixity receipt, the delivery rows and the report history all stay in the
archive. Only authorization changes, and report availability is recomputed
from the committed tables on every read instead of being cached.

Availability rule (see :class:`app.db.Database`): a report remains available
while at least one **not revoked** accepted message delivers an attachment
with the same report number *and* identical bytes (SHA-256). When the last
such source goes away the report becomes unavailable and new attachment
downloads are refused, even though its history is still visible.

All writes are single SQLite ``BEGIN IMMEDIATE`` transactions, so a
revocation interleaved with another instance's collection can only produce a
fully before-commit or fully after-commit view — availability always follows
the actual commit order, never the age of the earliest source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .db import Database, RevocationError

log = logging.getLogger("bridge.revocation")

MIN_REASON_LEN = 2
MAX_REASON_LEN = 500


@dataclass(frozen=True)
class RevocationService:
    db: Database
    cfg: Settings

    def revoke(self, message_id: int, reason: str, revoked_by: str = "web") -> dict[str, Any]:
        reason = (reason or "").strip()
        if len(reason) < MIN_REASON_LEN:
            raise RevocationError(f"撤销原因至少 {MIN_REASON_LEN} 个字符")
        if len(reason) > MAX_REASON_LEN:
            raise RevocationError(f"撤销原因不能超过 {MAX_REASON_LEN} 个字符")
        try:
            message_id = int(message_id)
        except (TypeError, ValueError) as exc:
            raise RevocationError("邮件 id 必须是整数", status_code=400) from exc
        result = self.db.revoke_message(
            message_id=message_id,
            reason=reason,
            revoked_by=revoked_by,
            instance=self.cfg.instance_id,
        )
        if result["status"] == "already":
            log.info(
                "duplicate revoke submit ignored message=%s instance=%s",
                message_id,
                self.cfg.instance_id,
            )
        else:
            log.info(
                "message %s revoked by %s on %s: %s",
                message_id,
                revoked_by,
                self.cfg.instance_id,
                reason,
            )
        return result

    # ----------------------------------------------------------------- views
    def report_view(self, report_id: int) -> dict[str, Any] | None:
        """One report with availability and the sources that justify it."""
        row = self.db.report_availability(report_id)
        if row is None:
            return None
        item = dict(row)
        return self._enrich(item)

    def report_views(self, limit: int = 200) -> list[dict[str, Any]]:
        views: list[dict[str, Any]] = []
        for row in self.db.list_report_views(limit):
            views.append(self._enrich(dict(row)))
        return views

    def _enrich(self, item: dict[str, Any]) -> dict[str, Any]:
        item["report_id"] = item["id"]
        item["available"] = bool(item["available"])
        item["sources"] = [
            self._source_dict(source)
            for source in self.db.report_sources(item["report_no"], item["attachment_sha256"])
        ]
        item["available_sources"] = [s for s in item["sources"] if not s["revoked"]]
        item["revoked_sources"] = [s for s in item["sources"] if s["revoked"]]
        return item

    @staticmethod
    def _source_dict(row) -> dict[str, Any]:
        item = dict(row)
        item["revoked"] = item["revocation_id"] is not None
        return item
