"""Authenticated loopback HTTP application; no arbitrary source-file serving."""

import argparse
import hmac
import http.client
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import webbrowser
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .config import DEFAULTS, DEPTHS
from .engine import Engine
from .llm import Ollama
from .reports import csv_report, html_report, json_report
from .store import Store

STATIC = Path(__file__).parent / "static"
MAX_BODY = 128 * 1024


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, engine=None):
        super().__init__(address, Handler)
        self.store = store
        self.engine = engine or Engine(store)
        self.token = secrets.token_urlsafe(32)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.cookie_name = f"dv_session_{self.server_port}"
        self.engine.origin = self.origin
        self.stats_cache = {}


class Handler(BaseHTTPRequestHandler):
    server_version = "Drivestigate/" + __version__

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, fmt, *args):
        # Request paths can contain search terms; do not persist them in logs.
        pass

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' "
            "'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'; object-src 'none'",
        )

    def reply(self, data, status=200, mime="application/json; charset=utf-8", cookie=False):
        if not isinstance(data, (str, bytes)):
            data = json.dumps(data, ensure_ascii=True)
        body = data.encode("utf-8") if isinstance(data, str) else data
        self.send_response(status)
        self.security_headers()
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header(
                "Set-Cookie",
                f"{self.server.cookie_name}={self.server.token}; Path=/; HttpOnly; SameSite=Strict",
            )
        self.end_headers()
        self.wfile.write(body)

    def authorized(self, write=False):
        port = self.server.server_port
        if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {self.server.origin, f"http://localhost:{port}"}:
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            name = self.server.cookie_name
            token = cookie[name].value if name in cookie else ""
            return hmac.compare_digest(token, self.server.token) and (
                not write
                or hmac.compare_digest(self.headers.get("X-Drivestigate-Token", ""), self.server.token)
            )
        except (ValueError, TypeError):
            return False

    def do_GET(self):
        try:
            parts = urlsplit(self.path)
            path = parts.path
            port = self.server.server_port
            if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                return self.reply({"error": "Invalid host."}, 403)
            if path in {
                "/",
                "/app.js",
                "/visuals.js",
                "/comprehensive.js",
                "/folders.js",
                "/sizetree.js",
                "/report.css",
                "/style.css",
                "/icon.svg",
            }:
                if self.headers.get("Sec-Fetch-Dest") in {"iframe", "frame"}:
                    return self.reply({"error": "Embedding is not allowed."}, 403)
                filename = {"/": "index.html"}.get(path, path[1:])
                mime = {
                    "/": "text/html",
                    "/app.js": "text/javascript",
                    "/visuals.js": "text/javascript",
                    "/comprehensive.js": "text/javascript",
                    "/folders.js": "text/javascript",
                    "/sizetree.js": "text/javascript",
                    "/report.css": "text/css",
                    "/style.css": "text/css",
                    "/icon.svg": "image/svg+xml",
                }[path]
                body = (STATIC / filename).read_text().replace("__CSRF_TOKEN__", self.server.token)
                return self.reply(body, mime=mime + "; charset=utf-8", cookie=path == "/")
            if not self.authorized():
                return self.reply({"error": "Please open Drivestigate in your local browser."}, 403)
            query = parse_qs(parts.query)
            store, engine = self.server.store, self.server.engine
            if path == "/api/settings":
                return self.reply({"settings": store.settings(), "defaults": DEFAULTS, "depths": DEPTHS})
            if path == "/api/models":
                return self.reply({"models": Ollama(store.settings()).models()})
            if path == "/api/volumes":
                roots = []
                if sys.platform == "darwin":
                    for volume in Path("/Volumes").iterdir():
                        try:
                            info = os.statvfs(volume)
                            roots.append(
                                {
                                    "name": volume.name,
                                    "path": str(volume.resolve()),
                                    "bytes": info.f_blocks * info.f_frsize,
                                }
                            )
                        except OSError:
                            pass
                return self.reply({"volumes": roots})
            if path == "/api/scans":
                return self.reply(
                    {
                        "scans": store.scans(),
                        "version": __version__,
                        "eject_supported": sys.platform == "darwin",
                    }
                )
            if path == "/api/queue":
                return self.reply(store.queue())
            segments = path.strip("/").split("/")
            if len(segments) >= 3 and segments[:2] == ["api", "scans"]:
                scan_id = segments[2]
                scan = store.get_scan(scan_id)
                if len(segments) == 3:
                    cached = self.server.stats_cache.get(scan_id)
                    if not cached or time.monotonic() - cached[0] > 3 or cached[1] != scan["updated"]:
                        cached = (time.monotonic(), scan["updated"], store.stats(scan_id))
                        self.server.stats_cache[scan_id] = cached
                    return self.reply(
                        {
                            "scan": scan,
                            "stats": cached[2],
                            "connected": engine.connected(scan),
                            "progress": engine.progress(scan),
                            "report_state": (store.report(scan_id) or {}).get("state", "preview"),
                        }
                    )
                if segments[3] in {"summary", "comprehensive.html"}:
                    from .summary import catalog_report, render_report, report_payload

                    if segments[3] == "summary":
                        return self.reply(report_payload(store, scan_id, self.server.origin))
                    with store.snapshot() as snapshot:
                        saved = snapshot.report(scan_id)
                        data = saved["data"] if saved and saved["data"] else catalog_report(snapshot, scan_id)
                        return self.reply(
                            render_report(data, self.server.origin, standalone=True),
                            mime="text/html; charset=utf-8",
                        )
                if segments[3] == "directory" and len(segments) == 6 and segments[5] == "summary":
                    from .folders import folder_summary

                    return self.reply(folder_summary(store, scan_id, int(segments[4])))
                if segments[3] == "directory" and len(segments) == 5:
                    with store.connect() as db:
                        row = db.execute(
                            "SELECT rel FROM directories WHERE scan_id=? AND id=?",
                            (scan_id, int(segments[4])),
                        ).fetchone()
                    if not row:
                        raise ValueError("Folder not found in this investigation.")
                    return self.reply({"rel": row["rel"]})
                if segments[3] == "tree":
                    return self.reply(
                        store.tree(
                            scan_id,
                            query.get("parent", ["."])[0],
                            max(0, int(query.get("offset", [0])[0])),
                            150,
                        )
                    )
                if segments[3] == "size-tree":
                    from .sizetree import size_tree

                    return self.reply(
                        size_tree(
                            store,
                            scan_id,
                            query.get("parent", ["."])[0],
                            query.get("offset", [0])[0],
                            sort=query.get("sort", ["size"])[0],
                        )
                    )
                if segments[3] == "graph":
                    from .knowledge import graph_data

                    return self.reply(
                        graph_data(
                            store,
                            scan_id,
                            query.get("q", [""])[0],
                            min(400, max(20, int(query.get("limit", [200])[0]))),
                            query.get("folder", [None])[0],
                        )
                    )
                if segments[3] == "file" and len(segments) == 5:
                    return self.reply(store.detail_file(scan_id, int(segments[4])))
                if segments[3] == "obsidian.zip":
                    from .knowledge import vault_path

                    root = vault_path(store, scan_id)
                    with tempfile.TemporaryFile() as archive_stream:
                        with zipfile.ZipFile(archive_stream, "w", zipfile.ZIP_DEFLATED) as archive:
                            for entry in root.rglob("*"):
                                if (
                                    entry.is_symlink()
                                    or not entry.is_file()
                                    or not entry.resolve().is_relative_to(root)
                                ):
                                    continue
                                archive.write(
                                    entry, "Drivestigate-Vault/" + entry.relative_to(root).as_posix()
                                )
                        size = archive_stream.tell()
                        archive_stream.seek(0)
                        self.send_response(200)
                        self.security_headers()
                        self.send_header("Content-Type", "application/zip")
                        self.send_header("Content-Length", str(size))
                        self.send_header(
                            "Content-Disposition",
                            f'attachment; filename="Drivestigate-Vault-{scan_id[:8]}.zip"',
                        )
                        self.end_headers()
                        while chunk := archive_stream.read(1024 * 1024):
                            self.wfile.write(chunk)
                    return
                if segments[3] == "findings":
                    return self.reply(
                        store.findings(
                            scan_id,
                            max(0, int(query.get("offset", [0])[0])),
                            min(250, max(1, int(query.get("limit", [50])[0]))),
                            query.get("q", [""])[0][:200],
                            query.get("priority", [None])[0],
                        )
                    )
                if segments[3] == "finding" and len(segments) == 5:
                    finding_id = int(segments[4])
                    with store.connect() as db:
                        row = db.execute(
                            "SELECT f.*,d.rel,(SELECT COUNT(*) FROM files x WHERE x.dir_id=d.id) "
                            "file_count,(SELECT COALESCE(SUM(size),0) FROM files x WHERE x.dir_id=d.id) "
                            "bytes FROM findings f JOIN directories d ON d.id=f.dir_id "
                            "WHERE f.id=? AND f.scan_id=?",
                            (finding_id, scan_id),
                        ).fetchone()
                    if not row:
                        raise ValueError("Finding not found.")
                    finding = dict(row)
                    finding["samples"] = json.loads(finding["samples"])
                    finding["references"] = json.loads(finding.pop("references_json"))
                    return self.reply(finding)
                if segments[3] == "events":
                    return self.reply({"events": store.events(scan_id, 250)})
                if segments[3] in {"report.html", "report.csv", "report.json"}:
                    typ = segments[3].split(".")[-1]
                    with store.snapshot() as snapshot:
                        return self.export_report(snapshot, scan_id, typ)

            self.reply({"error": "Not found."}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
            self.reply({"error": str(exc)[:500]}, 400)

    def export_report(self, store, scan_id, typ):
        iterator = (
            html_report(store, scan_id, self.server.origin)
            if typ == "html"
            else csv_report(store, scan_id)
            if typ == "csv"
            else json_report(store, scan_id)
        )
        self.send_response(200)
        self.security_headers()
        self.send_header(
            "Content-Type",
            {"html": "text/html", "csv": "text/csv", "json": "application/json"}[typ] + "; charset=utf-8",
        )
        self.send_header("Content-Disposition", f'attachment; filename="Drivestigate-{scan_id[:8]}.{typ}"')
        self.end_headers()
        for chunk in iterator:
            self.wfile.write(chunk.encode("utf-8"))
        return

    def do_POST(self):
        if not self.authorized(write=True):
            return self.reply({"error": "Invalid local session."}, 403)
        try:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.reply({"error": "JSON is required."}, 415)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                return self.reply({"error": "Request is empty or too large."}, 413)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("A JSON object is required.")
            store, engine = self.server.store, self.server.engine
            path = urlsplit(self.path).path
            if path == "/api/settings":
                return self.reply({"settings": store.save_settings(data)})
            if path == "/api/queue/reorder":
                engine.reorder(data.get("ids"))
                return self.reply(store.queue())
            if path == "/api/queue/move":
                engine.move(data.get("id"), data.get("direction"))
                return self.reply(store.queue())
            if path == "/api/queue/hold":
                if type(data.get("paused")) is not bool:
                    raise ValueError("Queue hold state must be a boolean.")
                engine.hold_queue(data["paused"])
                return self.reply(store.queue())
            if path == "/api/model-test":
                return self.reply(Ollama(data.get("settings", store.settings())).test())
            if path == "/api/models":
                return self.reply({"models": Ollama(data.get("settings", store.settings())).models()})
            if path == "/api/pick-folder":
                if sys.platform != "darwin":
                    raise ValueError("The folder picker requires macOS. Enter a path instead.")
                result = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        "POSIX path of (choose folder with prompt "
                        '"Choose a drive or folder for Drivestigate")',
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                return self.reply({"path": result.stdout.strip() if result.returncode == 0 else ""})
            if path == "/api/demo":
                from .demo import create_demo

                folder = Path(tempfile.mkdtemp(prefix="Drivestigate-Demo-"))
                create_demo(folder)
                return self.reply({"path": str(folder)})
            if path == "/api/scans":
                settings = store.settings()
                settings["investigation_mode"] = data.get("mode", settings["investigation_mode"])
                if "search_focus" in data:
                    settings["search_focus"] = data["search_focus"]
                scan_id = engine.create(str(data.get("root", "")), str(data.get("name", "")), settings)
                return self.reply({"id": scan_id}, 201)
            segments = path.strip("/").split("/")
            if len(segments) == 4 and segments[:2] == ["api", "scans"]:
                scan_id, action = segments[2:]
                if action == "delete":
                    if data.get("confirm") != scan_id:
                        raise ValueError("Confirm deletion of this investigation.")
                    result = engine.delete(scan_id)
                    self.server.stats_cache.pop(scan_id, None)
                    return self.reply(result, 202 if result["pending"] else 200)
                elif action in {"pause", "stop"}:
                    engine.interrupt(scan_id, stop=action == "stop")
                elif action == "eject":
                    from .drives import eject_drive

                    if data.get("confirm") != scan_id:
                        raise ValueError("Confirm ejection of this investigation's source drive.")
                    return self.reply(eject_drive(engine, scan_id))
                elif action == "focus":
                    engine.refocus(scan_id, data)
                elif action == "resume":
                    engine.start(scan_id)
                elif action == "summary":
                    engine.start(scan_id, report_only=True)
                elif action == "structure":
                    engine.start(scan_id, structure_only=True)
                elif action == "deepen":
                    engine.start(scan_id, finding_id=int(data["finding_id"]))
                elif action == "reveal":
                    target = engine.path_for(scan_id, data["kind"], int(data["id"]))
                    if sys.platform != "darwin":
                        raise ValueError("Finder integration requires macOS. Copy the path instead.")
                    command = ["/usr/bin/open"] + (["-R"] if data["kind"] == "file" else []) + ["--", target]
                    result = subprocess.run(command, capture_output=True, timeout=10)
                    if result.returncode:
                        raise ValueError("Finder could not open the path.")
                elif action == "open-vault":
                    from .knowledge import vault_path

                    root = vault_path(store, scan_id)
                    if sys.platform != "darwin":
                        raise ValueError("Open the downloaded vault folder in Obsidian.")
                    result = subprocess.run(["/usr/bin/open", "--", str(root)], timeout=10)
                    if result.returncode:
                        raise ValueError("Finder could not open the vault folder.")
                else:
                    return self.reply({"error": "Action not found."}, 404)
                return self.reply({"ok": True})
            return self.reply({"error": "Not found."}, 404)
        except (ValueError, KeyError, OSError, TypeError, OverflowError, subprocess.TimeoutExpired) as exc:
            self.reply({"error": str(exc)[:500]}, 400)


def default_data_dir():
    if os.environ.get("DRIVESTIGATE_DATA_DIR"):
        return Path(os.environ["DRIVESTIGATE_DATA_DIR"]).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Drivestigate"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "drivestigate"


def running_instance(port):
    if not port:
        return False
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        return (
            response.status == 200
            and response.getheader("Server", "").startswith("Drivestigate/")
            and b"drivestigate-token" in response.read(4096)
        )
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description="Drivestigate — local drive investigation")
    parser.add_argument("--port", type=int, default=8741)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    os.umask(0o077)
    if running_instance(args.port):
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{args.port}")
        print(f"Drivestigate is already running at http://127.0.0.1:{args.port}", flush=True)
        return
    args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    instance_lock = (args.data_dir / "instance.lock").open("a")
    try:
        import fcntl

        fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.exit(1, "Drivestigate is already using this catalog. Open the existing instance.\n")
    except ImportError:
        parser.exit(1, "This release supports macOS and Linux.\n")
    try:
        server = AppServer(("127.0.0.1", args.port), Store(args.data_dir / "catalog.sqlite3"))
    except OSError as exc:
        parser.exit(1, f"Drivestigate could not start: {exc}\n")
    print(
        f"Drivestigate {__version__} · {server.origin}\nCatalog: {args.data_dir}\nCtrl+C stops the application.",
        flush=True,
    )
    if not args.no_browser:
        webbrowser.open(server.origin)
    server.engine.start_scheduler()

    def shutdown(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        server.engine.close()
        server.server_close()


if __name__ == "__main__":
    main()
