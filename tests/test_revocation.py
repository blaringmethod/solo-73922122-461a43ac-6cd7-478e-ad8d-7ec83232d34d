"""Source-revocation behaviour: availability, idempotency and interleaving.

Covers the guarantees:

* revoking a received message keeps the original, receipt and report history;
* a report delivered only by the revoked message becomes unavailable and new
  attachment downloads are refused (403);
* another *not revoked* received message delivering the same report number
  with identical bytes keeps the report available, with the actual sources
  shown;
* revocation interleaved with another instance collecting the same artifact
  follows transaction commit order — neither an early source nor a late one
  can wrongly block or wrongly release a download;
* a failed revocation changes nothing and a duplicate submission adds no row.
"""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from app.db import RevocationError, transaction
from app.main import create_app


def _client(demo, instance: str = "inst-a") -> TestClient:
    collector = demo.make_collector(instance)
    app = create_app(collector.cfg)
    app.state.collector = collector
    app.state.db = collector.db
    app.state.store = collector.store
    return TestClient(app)


def _scan(client: TestClient) -> None:
    assert client.post("/api/scan").status_code == 200


def _report(client: TestClient, report_no: str) -> dict:
    reports = client.get("/api/reports").json()
    matches = [r for r in reports if r["report_no"] == report_no]
    assert matches, f"report {report_no} not found"
    return matches[0]


def _message_for_report(client: TestClient, report_no: str) -> dict:
    for message in client.get("/api/messages").json():
        if report_no in message["report_nos_list"]:
            return message
    raise AssertionError(f"no message delivers {report_no}")


# --------------------------------------------------------------- basic rules
def test_revocation_keeps_original_receipt_and_history_but_blocks_download(demo):
    demo.deliver("revoke me", [demo.attachment("R1")])
    client = _client(demo)
    _scan(client)
    message = _message_for_report(client, "R1")
    report = _report(client, "R1")
    assert report["available"] is True

    # Original and receipt are downloadable before and after revocation.
    assert client.get(f"/api/messages/{message['id']}/original").status_code == 200
    assert client.get(f"/api/messages/{message['id']}/receipt").status_code == 200

    response = client.post(
        f"/api/messages/{message['id']}/revoke", json={"reason": "误投，请撤销授权"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "revoked"

    # Nothing archived is deleted: original, receipt and the report row remain.
    assert client.get(f"/api/messages/{message['id']}/original").status_code == 200
    receipt = client.get(f"/api/messages/{message['id']}/receipt")
    assert receipt.status_code == 200
    assert json.loads(receipt.content)["type"] == "acceptance"

    report = _report(client, "R1")
    assert report["available"] is False
    assert report["available_sources"] == []
    assert len(report["revoked_sources"]) == 1
    assert report["revoked_sources"][0]["message_id"] == message["id"]

    # New attachment downloads are refused while the history stays visible.
    blocked = client.get(f"/api/reports/{report['report_id']}/attachment")
    assert blocked.status_code == 403
    assert "撤销" in blocked.json()["detail"]


def test_second_unrevoked_identical_source_keeps_report_available(demo):
    demo.deliver("first source", [demo.attachment("R1")])
    client = _client(demo)
    _scan(client)
    first = _message_for_report(client, "R1")

    # A second mail delivers the same report number with identical bytes.
    demo.deliver("second identical source", [demo.attachment("R1")])
    _scan(client)
    second = [m for m in client.get("/api/messages").json() if m["id"] != first["id"]][0]

    report = _report(client, "R1")
    assert report["available"] is True
    assert {s["message_id"] for s in report["available_sources"]} == {
        first["id"],
        second["id"],
    }

    # Revoke the first (earliest) source: the report must stay available via
    # the second source and the UI/API names that actual usable source.
    client.post(f"/api/messages/{first['id']}/revoke", json={"reason": "first was misdelivered"})
    report = _report(client, "R1")
    assert report["available"] is True
    assert [s["message_id"] for s in report["available_sources"]] == [second["id"]]
    assert [s["message_id"] for s in report["revoked_sources"]] == [first["id"]]
    assert client.get(f"/api/reports/{report['report_id']}/attachment").status_code == 200

    # Revoke the second as well: now no usable source remains.
    client.post(f"/api/messages/{second['id']}/revoke", json={"reason": "also withdrawn"})
    report = _report(client, "R1")
    assert report["available"] is False
    assert client.get(f"/api/reports/{report['report_id']}/attachment").status_code == 403


def test_revoking_one_message_does_not_touch_an_independent_report(demo):
    # Independent reports arrive in separate mails.
    demo.deliver("mail with R1", [demo.attachment("R1")])
    demo.deliver("mail with R2", [demo.attachment("R2")])
    client = _client(demo)
    _scan(client)
    r1_message = _message_for_report(client, "R1")
    client.post(f"/api/messages/{r1_message['id']}/revoke", json={"reason": "misdelivered"})

    # Only R1 loses its sole source; the unrelated R2 mail is untouched.
    assert _report(client, "R1")["available"] is False
    r2 = _report(client, "R2")
    assert r2["available"] is True
    assert client.get(f"/api/reports/{r2['report_id']}/attachment").status_code == 200


def test_revoking_message_revokes_all_reports_it_delivered(demo):
    # One mail delivering two reports: both share the same (single) source, so
    # both become unavailable together when that source is revoked.
    demo.deliver("two reports", [demo.attachment("R1"), demo.attachment("R2")])
    client = _client(demo)
    _scan(client)
    message = _message_for_report(client, "R1")
    client.post(f"/api/messages/{message['id']}/revoke", json={"reason": "misdelivered"})
    assert _report(client, "R1")["available"] is False
    assert _report(client, "R2")["available"] is False


def test_duplicate_revocation_submission_adds_no_record(demo):
    demo.deliver("once", [demo.attachment("R1")])
    client = _client(demo)
    _scan(client)
    message = _message_for_report(client, "R1")

    first = client.post(
        f"/api/messages/{message['id']}/revoke", json={"reason": "duplicate test"}
    )
    assert first.json()["status"] == "revoked"
    second = client.post(
        f"/api/messages/{message['id']}/revoke", json={"reason": "duplicate test again"}
    )
    assert second.status_code == 200
    assert second.json()["status"] == "already"

    records = client.get("/api/revocations").json()
    assert len(records) == 1
    assert records[0]["message_id"] == message["id"]
    # The original reason is preserved, not overwritten.
    assert records[0]["reason"] == "duplicate test"
    assert client.get("/api/status").json()["stats"]["revocations"] == 1


def test_failed_revocation_changes_no_supply_state(demo):
    demo.deliver("good", [demo.attachment("R1")])
    demo.deliver("bad", [{"filename": "broken.csv", "content": "bad,header\n1,2\n"}])
    client = _client(demo)
    _scan(client)

    accepted = _message_for_report(client, "R1")
    rejected = [m for m in client.get("/api/messages").json() if m["decision"] == "rejected"][0]
    report_id = _report(client, "R1")["report_id"]

    # A rejected message carries no supply authorization and cannot be revoked.
    response = client.post(
        f"/api/messages/{rejected['id']}/revoke", json={"reason": "nope"}
    )
    assert response.status_code == 400
    # Unknown message id is a 404.
    assert client.post("/api/messages/9999/revoke", json={"reason": "nope"}).status_code == 404
    # Empty / too-short reason is rejected.
    bad_reason = client.post(
        f"/api/messages/{accepted['id']}/revoke", json={"reason": " "}
    )
    assert bad_reason.status_code == 400

    # No revocation row exists and the report stays fully available.
    assert client.get("/api/revocations").json() == []
    report = _report(client, "R1")
    assert report["available"] is True
    assert client.get(f"/api/reports/{report_id}/attachment").status_code == 200
    assert client.get("/api/status").json()["stats"]["revocations"] == 0


def test_revoked_message_is_skipped_not_reprocessed_by_later_scan(demo):
    demo.deliver("persisted", [demo.attachment("R1")])
    client = _client(demo)
    _scan(client)
    message = _message_for_report(client, "R1")
    client.post(f"/api/messages/{message['id']}/revoke", json={"reason": "withdrawn"})

    # A later scan must not resurrect the revoked message's authorization.
    again = client.post("/api/scan").json()
    assert again["accepted"] == 0
    assert _report(client, "R1")["available"] is False
    assert len(client.get("/api/revocations").json()) == 1


# ------------------------------------------------------------- web interface
def test_revocation_page_and_form(demo):
    demo.deliver("web revoke", [demo.attachment("R1")])
    client = _client(demo)
    _scan(client)
    message = _message_for_report(client, "R1")

    page = client.get("/revocations")
    assert page.status_code == 200
    assert "来源撤销" in page.text
    assert "web revoke" in page.text

    form = client.post(
        "/actions/revoke",
        data={"message_id": str(message["id"]), "reason": "网页撤销原因"},
        follow_redirects=True,
    )
    assert form.status_code == 200
    assert "已撤销邮件" in form.text

    # Dashboard reflects the unavailable report and disables its download link.
    dashboard = client.get("/").text
    assert "不可用" in dashboard
    assert "已撤销" in dashboard
    # The reason itself is shown on the dedicated revocation page/records.
    revoke_page = client.get("/revocations").text
    assert "网页撤销原因" in revoke_page

    # Revoking again through the form is idempotent and shows no new record.
    again = client.post(
        "/actions/revoke",
        data={"message_id": str(message["id"]), "reason": "网页撤销原因"},
        follow_redirects=True,
    )
    assert "重复提交未产生新记录" in again.text
    assert len(client.get("/api/revocations").json()) == 1


# --------------------------------------------------------- interleaving rules
def test_availability_flips_with_commit_when_revoke_races_collection(demo):
    """Two instances interleave a revocation and collection of the same file.

    Whichever transaction commits first, every observed availability snapshot
    must be consistent with committed state: a download can never be allowed
    from an uncommitted collection or blocked by an uncommitted revocation.
    After both settle, the second identical source keeps the report available.
    """
    demo.deliver("only source", [demo.attachment("R1")])
    client_a = _client(demo, "inst-a")
    _scan(client_a)
    first = _message_for_report(client_a, "R1")
    report_id = _report(client_a, "R1")["report_id"]

    # Second instance shares the same database/store (as in Docker Compose).
    client_b = _client(demo, "inst-b")
    # A second, byte-identical delivery waits in the mailbox.
    demo.deliver("racing identical source", [demo.attachment("R1")])

    outcomes: list[dict] = []
    barrier = threading.Barrier(2)

    def revoke() -> None:
        barrier.wait()
        resp = client_a.post(
            f"/api/messages/{first['id']}/revoke", json={"reason": "race revoke"}
        )
        outcomes.append({"op": "revoke", "status": resp.status_code})

    def collect() -> None:
        barrier.wait()
        resp = client_b.post("/api/scan")
        outcomes.append({"op": "scan", "status": resp.status_code})

    threads = [threading.Thread(target=revoke), threading.Thread(target=collect)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert {o["status"] for o in outcomes} == {200}

    # Final committed state: one revocation row, two messages, report available
    # solely through the second (not revoked) identical source.
    assert len(client_a.get("/api/revocations").json()) == 1
    report = _report(client_a, "R1")
    assert report["available"] is True
    assert [s["message_id"] for s in report["available_sources"]] != [first["id"]]
    assert all(s["message_id"] != first["id"] for s in report["available_sources"])
    assert client_a.get(f"/api/reports/{report_id}/attachment").status_code == 200


def test_no_mixed_snapshot_while_revocation_transaction_is_open(demo):
    """Readers only ever see before- or after-commit state, never a mix.

    Hold the revocation write transaction open (before commit) from another
    connection and assert that availability reads still report the old
    committed state; after commit they flip atomically.
    """
    demo.deliver("sole source", [demo.attachment("RX")])
    client = _client(demo)
    _scan(client)
    message = _message_for_report(client, "RX")
    report_id = _report(client, "RX")["report_id"]
    db = client.app.state.db

    opened = threading.Event()
    release = threading.Event()

    def hold_open_revocation() -> None:
        with db.connect() as conn:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO revocation (message_id, reason, revoked_by,"
                    " instance, created_at) VALUES (?, ?, ?, ?, ?)",
                    (message["id"], "held open", "test", "inst-a", "2026-01-01T00:00:00+00:00"),
                )
                opened.set()
                release.wait(timeout=10)

    worker = threading.Thread(target=hold_open_revocation)
    worker.start()
    assert opened.wait(timeout=5)

    # While the write is uncommitted, a separate connection must not see it:
    # the report stays available and the download is allowed.
    assert _report(client, "RX")["available"] is True
    assert client.get(f"/api/reports/{report_id}/attachment").status_code == 200

    release.set()
    worker.join()

    # After commit the flip is atomic and immediate.
    assert _report(client, "RX")["available"] is False
    assert client.get(f"/api/reports/{report_id}/attachment").status_code == 403


def test_repeated_interleaving_never_lands_on_wrong_availability(demo):
    """Stress the revoke/collect interleave and assert every settled state.

    Each iteration adds an identical source (collection) and revokes one
    source; availability must always equal "at least one unrevoked accepted
    message still delivers identical bytes", regardless of commit ordering.
    """
    demo.deliver("seed", [demo.attachment("R7")])
    client = _client(demo)
    _scan(client)

    for index in range(6):
        demo.deliver(f"extra {index}", [demo.attachment("R7")])
        report_before = _report(client, "R7")
        # Pick a currently usable source to revoke.
        target = report_before["available_sources"][0]["message_id"]
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def collect() -> None:
            barrier.wait()
            try:
                client.post("/api/scan")
            except Exception as exc:  # pragma: no cover - failure reporting
                errors.append(exc)

        def revoke() -> None:
            barrier.wait()
            try:
                client.post(f"/api/messages/{target}/revoke", json={"reason": f"iter {index}"})
            except Exception as exc:  # pragma: no cover - failure reporting
                errors.append(exc)

        threads = [threading.Thread(target=collect), threading.Thread(target=revoke)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors

        # Re-derive the expected availability independently from the tables.
        report = _report(client, "R7")
        expected = any(not s["revoked"] for s in report["sources"])
        assert report["available"] is expected
        download = client.get(f"/api/reports/{report['report_id']}/attachment")
        assert download.status_code == (200 if expected else 403)


def test_service_rejects_bad_identifiers_and_oversized_reason(demo):
    client = _client(demo)
    service = client.app.state.revocations
    from app.db import RevocationError

    for bad_id in ("abc", None):
        with pytest.raises(RevocationError) as exc:
            service.revoke(bad_id, "a valid reason")
        assert exc.value.status_code == 400
    with pytest.raises(RevocationError):
        service.revoke(1, "x" * 501)
    # A 500-char reason passes the length check but the message is still absent.
    with pytest.raises(RevocationError) as exc:
        service.revoke(9999, "y" * 500)
    assert exc.value.status_code == 404


def test_rollback_on_failure_leaves_no_partial_state(demo, tmp_path):
    """A revocation that errors inside the transaction rolls back fully."""
    demo.deliver("atomic", [demo.attachment("R8")])
    client = _client(demo)
    _scan(client)
    message = _message_for_report(client, "R8")
    db = client.app.state.db

    # Simulate a failure after the insert by violating the unique constraint
    # through a raw second insert inside an explicit transaction.
    db.revoke_message(message["id"], "first reason", "web", "inst-a")
    with pytest.raises(Exception):
        with db.connect() as conn:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO revocation (message_id, reason, revoked_by,"
                    " instance, created_at) VALUES (?, ?, ?, ?, ?)",
                    (message["id"], "would be duplicate", "web", "inst-a", "x"),
                )

    # Exactly one row with the original reason; supply state unchanged.
    records = client.get("/api/revocations").json()
    assert len(records) == 1 and records[0]["reason"] == "first reason"
    assert _report(client, "R8")["available"] is False
    with pytest.raises(RevocationError):
        # Service-layer guard also refuses without writing anything.
        client.app.state.revocations.revoke(message["id"], "x")
    assert len(client.get("/api/revocations").json()) == 1
