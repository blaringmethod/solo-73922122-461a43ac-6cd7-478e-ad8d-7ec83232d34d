"""FastAPI web application and API for the laboratory inbox bridge."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .collector import Collector, ScanBusy
from .config import Settings, settings
from .db import Database, RevocationError
from .revocation import RevocationService
from .store import Store

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("bridge.web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _row_to_dict(row) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _message_view(db: Database, row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["detail_list"] = json.loads(item.get("detail") or "[]")
    except json.JSONDecodeError:
        item["detail_list"] = []
    try:
        item["report_nos_list"] = json.loads(item.get("report_nos") or "[]")
    except json.JSONDecodeError:
        item["report_nos_list"] = []
    item["reports"] = [dict(r) for r in db.reports_for_message(item["id"])]
    revocation = db.get_revocation(item["id"])
    item["revocation"] = dict(revocation) if revocation is not None else None
    item["revoked"] = revocation is not None
    return item


def create_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or settings
    db = Database(cfg.database_path)
    store = Store(cfg.storage_dir)
    collector = Collector(cfg, db, store)
    revocations = RevocationService(db=db, cfg=cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init()
        store.init()
        stop = asyncio.Event()
        task: asyncio.Task | None = None
        if cfg.auto_scan_interval > 0:
            task = asyncio.create_task(auto_loop(stop))
        log.info(
            "instance %s ready db=%s storage=%s imap=%s:%s account=%s",
            cfg.instance_id,
            cfg.database_path,
            cfg.storage_dir,
            cfg.imap_host,
            cfg.imap_port,
            cfg.account,
        )
        try:
            yield
        finally:
            stop.set()
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def auto_loop(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.auto_scan_interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.to_thread(run_scan, "auto", False)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - loop must survive
                log.exception("automatic scan failed")

    def run_scan(trigger: str, raise_busy: bool = True):
        try:
            return collector.scan(trigger=trigger).as_dict()
        except ScanBusy:
            if raise_busy:
                raise
            log.info("scan skipped: another instance owns the lease")
            return None

    app = FastAPI(title=cfg.title, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.db = db
    app.state.store = store
    app.state.collector = collector
    app.state.revocations = revocations
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        mark_filter = request.query_params.get("mark") or None
        messages = [_message_view(db, row) for row in db.list_messages(100, mark_filter)]
        reports = revocations.report_views(200)
        state = db.get_state(cfg.account, cfg.imap_mailbox)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "cfg": cfg,
                "stats": db.stats(),
                "messages": messages,
                "reports": reports,
                "scans": [dict(r) for r in db.recent_scans(15)],
                "state": _row_to_dict(state),
                "mark_filter": mark_filter or "",
                "flash": request.query_params.get("flash", ""),
                "error": request.query_params.get("error", ""),
            },
        )

    @app.post("/actions/scan")
    async def action_scan():
        try:
            result = await asyncio.to_thread(run_scan, "web", True)
        except ScanBusy as exc:
            return RedirectResponse(f"/?flash=&error={_q(str(exc))}", status_code=303)
        except Exception as exc:  # surface the failure in the UI
            log.exception("web scan failed")
            return RedirectResponse(f"/?flash=&error={_q(str(exc))}", status_code=303)
        summary = (
            f"扫描完成：读取 {result['scanned']}，收讫 {result['accepted']}，"
            f"拒收 {result['rejected']}，跳过 {result['skipped']}，"
            f"标记 {result['marked']}，复用原件 {result['reused_reports']}"
        )
        if result["status"] != "completed":
            summary += f"；{result['error'] or result['status']}"
            return RedirectResponse(f"/?flash=&error={_q(summary)}", status_code=303)
        return RedirectResponse(f"/?flash={_q(summary)}", status_code=303)

    @app.post("/actions/retry-marks")
    async def action_retry_marks():
        try:
            marked = await asyncio.to_thread(collector.mark_pending_public)
        except ScanBusy as exc:
            return RedirectResponse(f"/?flash=&error={_q(str(exc))}", status_code=303)
        except Exception as exc:
            log.exception("retry marks failed")
            return RedirectResponse(f"/?flash=&error={_q(str(exc))}", status_code=303)
        return RedirectResponse(f"/?flash={_q(f'补标记完成：{marked} 封')}", status_code=303)

    # ------------------------------------------------------------- revocation
    @app.get("/revocations", response_class=HTMLResponse)
    def revocations_page(request: Request):
        # Only received mail can have its supply authorization revoked.
        messages = [
            _message_view(db, row)
            for row in db.list_messages(200, decision="accepted")
        ]
        reports = revocations.report_views(200)
        records = [dict(r) for r in db.list_revocations(200)]
        return templates.TemplateResponse(
            request,
            "revocations.html",
            {
                "cfg": cfg,
                "stats": db.stats(),
                "messages": messages,
                "reports": reports,
                "records": records,
                "flash": request.query_params.get("flash", ""),
                "error": request.query_params.get("error", ""),
            },
        )

    @app.post("/actions/revoke")
    async def action_revoke(message_id: int = Form(...), reason: str = Form(...)):
        try:
            result = await asyncio.to_thread(revocations.revoke, message_id, reason, "web")
        except RevocationError as exc:
            log.info("revocation rejected: %s", exc)
            return RedirectResponse(
                f"/revocations?flash=&error={_q(str(exc))}", status_code=303
            )
        except Exception as exc:  # pragma: no cover - surfaced in the UI
            log.exception("revocation failed")
            return RedirectResponse(
                f"/revocations?flash=&error={_q(f'撤销失败：{exc}')}", status_code=303
            )
        if result["status"] == "already":
            return RedirectResponse(
                f"/revocations?flash={_q('该邮件已撤销，重复提交未产生新记录')}",
                status_code=303,
            )
        return RedirectResponse(
            f"/revocations?flash={_q(f'已撤销邮件 #{message_id} 的供数授权（原因：{reason.strip()}）')}",
            status_code=303,
        )

    # -------------------------------------------------------------------- API
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "instance": cfg.instance_id}

    @app.get("/api/status")
    async def api_status():
        state = db.get_state(cfg.account, cfg.imap_mailbox)
        probe: dict[str, Any]
        try:
            probe = await asyncio.to_thread(collector.probe)
        except Exception as exc:
            probe = {"ok": False, "error": str(exc)}
        return {
            "instance": cfg.instance_id,
            "account": cfg.account,
            "mailbox": cfg.imap_mailbox,
            "stats": db.stats(),
            "mailbox_state": _row_to_dict(state),
            "imap": probe,
            "active_scan": _row_to_dict(db.active_scan()),
        }

    @app.post("/api/scan")
    async def api_scan():
        try:
            result = await asyncio.to_thread(run_scan, "api", True)
        except ScanBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"扫描失败：{exc}") from exc
        # A stale/interrupted scan is not a failure of the request: it reports
        # what was committed, plus why it stopped.
        return result

    @app.post("/api/retry-marks")
    async def api_retry_marks():
        try:
            marked = await asyncio.to_thread(collector.mark_pending_public)
        except ScanBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"补标记失败：{exc}") from exc
        return {"marked": marked}

    @app.get("/api/messages")
    def api_messages(limit: int = 100, mark_state: str | None = None):
        return [_message_view(db, row) for row in db.list_messages(limit, mark_state)]

    @app.get("/api/messages/{message_id}")
    def api_message(message_id: int):
        row = db.get_message(message_id)
        if row is None:
            raise HTTPException(status_code=404, detail="message not found")
        return _message_view(db, row)

    @app.get("/api/messages/{message_id}/original")
    def download_original(message_id: int):
        row = db.get_message(message_id)
        if row is None:
            raise HTTPException(status_code=404, detail="message not found")
        path = store.absolute(row["original_path"])
        if not path.is_file():
            raise HTTPException(status_code=410, detail="原件文件缺失")
        return FileResponse(
            path,
            media_type="message/rfc822",
            filename=f"{row['raw_sha256'][:16]}.eml",
        )

    @app.get("/api/messages/{message_id}/receipt")
    def download_receipt(message_id: int):
        row = db.get_message(message_id)
        if row is None:
            raise HTTPException(status_code=404, detail="message not found")
        path = store.absolute(row["receipt_path"])
        if not path.is_file():
            raise HTTPException(status_code=410, detail="回执文件缺失")
        return FileResponse(
            path,
            media_type="application/json",
            filename=f"receipt-{row['uidvalidity']}-{row['uid']}.json",
        )

    @app.get("/api/reports/{report_id}/attachment")
    def download_attachment(report_id: int):
        row = db.get_report(report_id)
        if row is None:
            raise HTTPException(status_code=404, detail="report not found")
        # New downloads are authorized from committed state at request time.
        # A report whose only sources have been revoked is unavailable even
        # though the archived bytes and its history are retained.
        availability = db.report_availability(report_id)
        if availability is None or not availability["available"]:
            raise HTTPException(
                status_code=403,
                detail="报告当前无有效来源（交付邮件的供数授权已撤销），下载被拒绝",
            )
        path = store.absolute(row["attachment_path"])
        if not path.is_file():
            raise HTTPException(status_code=410, detail="附件文件缺失")
        return FileResponse(
            path,
            media_type="text/csv; charset=utf-8",
            filename=row["attachment_name"],
        )

    @app.get("/api/reports/{report_id}")
    def api_report(report_id: int):
        view = revocations.report_view(report_id)
        if view is None:
            raise HTTPException(status_code=404, detail="report not found")
        return view

    @app.get("/api/reports")
    def api_reports():
        return revocations.report_views(200)

    @app.get("/api/revocations")
    def api_revocations():
        return [dict(r) for r in db.list_revocations(200)]

    @app.post("/api/messages/{message_id}/revoke")
    async def api_revoke(message_id: int, payload: dict[str, Any] | None = None):
        payload = payload or {}
        reason = str(payload.get("reason", "")).strip()
        revoked_by = str(payload.get("revoked_by", "api")).strip() or "api"
        try:
            result = await asyncio.to_thread(
                revocations.revoke, message_id, reason, revoked_by
            )
        except RevocationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return result

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"detail": str(exc)}, status_code=500)

    return app


def _q(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value)


app = create_app()
