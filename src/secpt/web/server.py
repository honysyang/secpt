"""stdlib HTTP 服务：静态页面 + JSON API 路由（零新增依赖）。

路由：
    GET  /                          → static/index.html
    GET  /static/<file>             → 静态资源（路径穿越防护）
    GET  /api/overview              → 计数总览
    GET  /api/assets                → 资产（intel 图）
    GET  /api/findings              → 发现（findings）
    GET  /api/tools                 → 内置工具 + 插件 + 打法技能
    GET  /api/experiences           → 沉淀经验 + 使用统计
    GET  /api/config | POST         → 运行时配置（读/写 var/web/config.json）
    GET  /api/jobs                  → 任务列表
    POST /api/jobs                  → 提交任务 {objective, profile, files[{name,data_b64}], fake}
    GET  /api/jobs/{id}/events?after=N → 事件流增量（执行轨迹）
    GET  /api/jobs/{id}/file?sub=&path= → 意图工作区文件读取（文件卡展开/复制）
    GET  /api/jobs/{id}/workspace   → 作战面板（任务池/假设树/战略文档/成果）
"""

from __future__ import annotations

import contextlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from secpt.web import api

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _var() -> Path:
    import os

    return Path(os.environ.get("SECPT_VAR", "var"))


_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
}


class SecptWebHandler(BaseHTTPRequestHandler):
    server_version = "secpt-web/1.0"

    # ---------------- 基础 ----------------

    def log_message(self, fmt, *args):  # 安静模式：stdout 留给任务输出
        pass

    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 64 * 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # ---------------- GET ----------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/" or path == "/index.html":
                self._serve_static("index.html")
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/") :])
            elif path == "/api/overview":
                self._send_json(api.overview_payload())
            elif path == "/api/assets":
                self._send_json(api.assets_payload())
            elif path.startswith("/api/findings/") and path.endswith("/evidence"):
                fid = path[len("/api/findings/") : -len("/evidence")]
                self._send_json(api.finding_evidence_payload(fid))
            elif path == "/api/findings":
                self._send_json(api.findings_payload())
            elif path == "/api/tools":
                self._send_json(api.tools_payload())
            elif path == "/api/experiences":
                self._send_json(api.experiences_payload())
            elif path == "/api/config":
                self._send_json(api.config_payload())
            elif path == "/api/jobs":
                self._send_json(api.jobs_payload())
            elif path.startswith("/api/jobs/") and path.endswith("/events"):
                job_id = path[len("/api/jobs/") : -len("/events")]
                qs = parse_qs(parsed.query)
                after = int((qs.get("after") or ["0"])[0] or 0)
                before = int((qs.get("before") or ["0"])[0] or 0)
                limit = int((qs.get("limit") or ["400"])[0] or 400)
                self._send_json(api.events_payload(job_id, after=after, before=before, limit=limit))
            elif path.startswith("/api/jobs/") and path.endswith("/file"):
                job_id = path[len("/api/jobs/") : -len("/file")]
                qs = parse_qs(parsed.query)
                self._send_json(
                    api.job_file_payload(
                        job_id,
                        sub=(qs.get("sub") or [""])[0],
                        path=(qs.get("path") or [""])[0],
                    )
                )
            elif path.startswith("/api/jobs/") and path.endswith("/workspace"):
                job_id = path[len("/api/jobs/") : -len("/workspace")]
                self._send_json(api.workspace_payload(job_id))
            elif "/event/" in path and path.split("/event/")[0].startswith("/api/jobs/"):
                # 单事件全量详情：/api/jobs/{id}/event/{seq}（summary 进流、detail 懒加载）
                job_id, seq = path.split("/event/", 1)
                job_id = job_id[len("/api/jobs/") :]
                self._send_json(api.event_detail_payload(job_id, int(seq or 0)))
            elif path.startswith("/api/jobs/") and path.endswith("/stream"):
                job_id = path[len("/api/jobs/") : -len("/stream")]
                qs = parse_qs(parsed.query)
                after = int((qs.get("after") or ["0"])[0] or 0)
                self._serve_event_stream(job_id, after=after)
            else:
                self._send_json({"error": "not found"}, status=404)
        except Exception as e:  # API 层故障不炸连接（观测层纪律）
            self._send_json({"error": str(e)[:300]}, status=500)

    def _serve_event_stream(self, job_id: str, *, after: int = 0) -> None:
        """SSE 实时事件流（流式输出核心）：tail-follow 该 job 的 events.jsonl。

        协议：text/event-stream；事件行 data=<完整事件 JSON>；游标推进靠事件自带
        seq。续传：客户端 Last-Event-ID / ?after=N。job 收口（终态）或客户端断开
        即结束。30s 一条 keep-alive 注释防中间层断链。多客户端互不影响——
        每连接独立文件游标，只读不写。
        """
        import time as _time

        from secpt.core.job.store import JOB_STATUSES, JobStore

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        last_id = self.headers.get("Last-Event-ID") or ""
        if last_id.strip().isdigit():
            after = max(after, int(last_id))
        evpath = _var() / "jobs" / job_id / "events.jsonl"
        last_beat = 0.0
        try:
            while True:
                sent_any = False
                if evpath.is_file():
                    with open(evpath, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                ev = json.loads(line)
                            except Exception:
                                continue
                            seq = int(ev.get("seq") or 0)
                            if seq <= after:
                                continue
                            after = seq
                            payload = json.dumps(ev, ensure_ascii=False)
                            self.wfile.write(f"id: {seq}\ndata: {payload}\n\n".encode())
                            sent_any = True
                self.wfile.flush()
                if sent_any:
                    last_beat = _time.monotonic()
                # job 收口 → 发 bye 并结束（前端回落轮询做最终刷新）
                status = ""
                with contextlib.suppress(Exception):
                    _rec = JobStore().get_job(job_id)
                    status = _rec.status if _rec is not None else ""
                if status and status in JOB_STATUSES - {"running", "awaiting_confirm", "paused"}:
                    self.wfile.write(b"event: bye\ndata: {}\n\n")
                    self.wfile.flush()
                    return
                if _time.monotonic() - last_beat > 30:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    last_beat = _time.monotonic()
                _time.sleep(0.5)
        except (ConnectionError, BrokenPipeError, OSError):
            return  # 客户端断开：正常退出，不写日志

    def _serve_static(self, name: str) -> None:
        target = (_STATIC_DIR / name).resolve()
        try:
            target.relative_to(_STATIC_DIR.resolve())
        except ValueError:
            self._send_json({"error": "forbidden"}, status=403)
            return
        if not target.is_file():
            self._send_json({"error": "not found"}, status=404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _MIME.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/jobs/"):
            job_id = parsed.path[len("/api/jobs/") :]
            result = api.delete_job(job_id)
            self._send_json(result, status=200 if result.get("ok") else 400)
        else:
            self._send_json({"error": "not found"}, status=404)

    # ---------------- POST ----------------

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/jobs":
                body = self._read_body()
                if not isinstance(body, dict) or not (body.get("objective") or "").strip():
                    self._send_json({"error": "objective 不能为空"}, status=400)
                    return
                result = api.create_task(
                    str(body.get("objective") or ""),
                    profile=str(body.get("profile") or "ctf"),
                    files=list(body.get("files") or []),
                    fake=bool(body.get("fake")),
                    budget_steps=int(body.get("budget_steps") or 0),
                    budget_wall_s=float(body.get("budget_wall_s") or 0.0),
                    auto_dispatch=bool(body.get("auto_dispatch", True)),
                )
                self._send_json(result, status=200 if "job_id" in result else 400)
            elif parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/confirm"):
                job_id = parsed.path[len("/api/jobs/") : -len("/confirm")]
                result = api.confirm_task(job_id)
                self._send_json(result, status=200 if result.get("ok") else 400)
            elif parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/cancel"):
                job_id = parsed.path[len("/api/jobs/") : -len("/cancel")]
                result = api.cancel_task(job_id)
                self._send_json(result, status=200 if result.get("ok") else 400)
            elif parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/approve"):
                job_id = parsed.path[len("/api/jobs/") : -len("/approve")]
                body = self._read_body()
                result = api.intercept_decide(
                    str((body or {}).get("pending_id") or ""), True, job_id=job_id
                )
                self._send_json(result, status=200 if result.get("ok") else 400)
            elif parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/deny"):
                job_id = parsed.path[len("/api/jobs/") : -len("/deny")]
                body = self._read_body()
                result = api.intercept_decide(
                    str((body or {}).get("pending_id") or ""), False, job_id=job_id
                )
                self._send_json(result, status=200 if result.get("ok") else 400)
            elif parsed.path.startswith("/api/pending/"):
                pid = parsed.path[len("/api/pending/") :]
                result = api.intercept_pending_one(pid)
                self._send_json(result, status=200 if not result.get("error") else 404)
            elif parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/message"):
                job_id = parsed.path[len("/api/jobs/") : -len("/message")]
                body = self._read_body()
                result = api.inject_message(job_id, str((body or {}).get("content") or ""))
                self._send_json(result, status=200 if result.get("ok") else 400)
            elif parsed.path == "/api/config":
                body = self._read_body()
                self._send_json(api.save_config(body if isinstance(body, dict) else {}))
            else:
                self._send_json({"error": "not found"}, status=404)
        except Exception as e:
            self._send_json({"error": str(e)[:300]}, status=500)


def make_server(host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), SecptWebHandler)


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:  # pragma: no cover
    server = make_server(host, port)
    bound = server.server_address
    print(f"[web] secpt 控制台: http://{bound[0]}:{bound[1]}（Ctrl-C 退出）", flush=True)
    if host not in ("127.0.0.1", "localhost", "::1"):
        # 面板无认证：非回环监听 = 同网段任何人可下发任务/追问/取消任务
        print(
            f"[web] ⚠ 非回环监听（{host}）：控制台无认证，"
            "仅限可信网络；公网暴露请先加反向代理与访问控制",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] 已退出", flush=True)
    finally:
        server.server_close()
