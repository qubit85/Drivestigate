import json
import threading
import urllib.error
import urllib.request

import pytest
from test_engine import FakeOllama, wait

from drivestigate.config import DEFAULTS
from drivestigate.demo import create_demo
from drivestigate.engine import Engine
from drivestigate.server import AppServer
from drivestigate.store import Store


@pytest.fixture
def server(tmp_path):
    instance = AppServer(("127.0.0.1", 0), Store(tmp_path / "catalog/db"))
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    thread.join(3)


def request(server, path, payload=None, auth=True, **headers):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if auth:
        headers.update(
            {"Cookie": f"{server.cookie_name}={server.token}", "X-Drivestigate-Token": server.token}
        )
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        server.origin + path, data=None if payload is None else json.dumps(payload).encode(), headers=headers
    )
    try:
        with opener.open(req) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers


def test_session_origin_and_host_protection(server):
    assert request(server, "/api/settings", auth=False)[0] == 403
    assert request(server, "/api/settings")[0] == 200
    assert request(server, "/api/settings", Origin="https://evil.example")[0] == 403
    assert request(server, "/api/settings", Host=f"evil.example:{server.server_port}")[0] == 403
    assert request(server, "/api/settings", **{"Sec-Fetch-Site": "cross-site"})[0] == 403
    code, body, headers = request(server, "/", auth=False)
    assert code == 200 and server.token.encode() in body
    assert "SameSite=Strict" in headers["Set-Cookie"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_no_arbitrary_file_read_or_reveal(server):
    assert request(server, "/../../etc/passwd")[0] == 404
    assert request(server, "/api/reveal", {"path": "/etc/passwd"})[0] == 404
    assert request(server, "/api/scans/unknown/reveal", {"kind": "file", "id": 1})[0] == 400


def test_mutations_require_csrf_header(server):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        server.origin + "/api/demo",
        data=b"{}",
        headers={"Cookie": f"{server.cookie_name}={server.token}", "Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        opener.open(req)
    assert exc.value.code == 403


def test_settings_are_validated_at_api_boundary(server):
    assert request(server, "/api/settings", {"ollama_url": "http://evil.test"})[0] == 400
    assert request(server, "/api/settings", {"search_focus": "Find old projects"})[0] == 200
    assert json.loads(request(server, "/api/settings")[1])["settings"]["search_focus"] == "Find old projects"


def test_report_counters_refresh_after_scan_state_changes(server, tmp_path):
    server.engine = Engine(server.store, FakeOllama)
    root = create_demo(tmp_path / "source")
    sid = server.engine.create(root, "Cache", {**DEFAULTS, "max_folders": 1})
    request(server, "/api/scans/" + sid)
    wait(server.engine)
    data = json.loads(request(server, "/api/scans/" + sid)[1])
    assert data["scan"]["status"] == "completed"
    assert data["stats"]["analyzed_folders"] == 1


def test_queue_api_validation_and_persistence(server, tmp_path):
    server.engine = Engine(server.store, FakeOllama)
    assert request(server, "/api/queue/hold", {"paused": "yes"})[0] == 400
    assert request(server, "/api/queue/hold", {"paused": True})[0] == 200
    root = create_demo(tmp_path / "queue-source")
    ids = [
        json.loads(request(server, "/api/scans", {"root": str(root), "name": n})[1])["id"] for n in ("A", "B")
    ]
    assert request(server, "/api/queue/reorder", {"ids": ids[::-1]})[0] == 200
    queue = json.loads(request(server, "/api/queue")[1])
    assert queue["paused"] and [j["id"] for j in queue["jobs"]] == ids[::-1]
    assert request(server, "/api/queue/reorder", {"ids": ids[:1]})[0] == 400
    assert request(server, "/api/queue", auth=False)[0] == 403


def test_detail_graph_and_vault_routes_are_scoped(server, tmp_path):
    import io
    import zipfile

    server.engine = Engine(server.store, FakeOllama)
    root = create_demo(tmp_path / "detail-source")
    sid = server.engine.create(
        root, "Detail", {**DEFAULTS, "investigation_mode": "detail", "detail_use_llm": False}
    )
    wait(server.engine)
    assert server.store.get_scan(sid)["status"] == "completed"
    tree = json.loads(request(server, f"/api/scans/{sid}/tree")[1])
    assert tree["items"][0]["rel"] == "."
    graph = json.loads(request(server, f"/api/scans/{sid}/graph?limit=20")[1])
    fid = next(n["file_id"] for n in graph["nodes"] if n["kind"] == "file")
    assert json.loads(request(server, f"/api/scans/{sid}/file/{fid}")[1])["sha256"]
    assert request(server, f"/api/scans/{sid}/file/-1")[0] == 400
    assert request(server, "/api/scans/unknown/graph")[0] == 400
    assert request(server, f"/api/scans/{sid}/tree?parent=../../etc")[0] == 400
    assert request(server, f"/api/scans/{sid}/obsidian.zip", auth=False)[0] == 403
    code, body, headers = request(server, f"/api/scans/{sid}/obsidian.zip")
    assert code == 200 and headers["Content-Type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert "Drivestigate-Vault/Investigation.md" in archive.namelist()
        assert len([p for p in archive.namelist() if p.startswith("Drivestigate-Vault/Files/")]) == 13
        assert not any(p.endswith(".bin") or "/../" in p for p in archive.namelist())


def test_browser_sessions_are_isolated_by_local_port(server, tmp_path):
    from http.cookiejar import CookieJar

    second = AppServer(("127.0.0.1", 0), Store(tmp_path / "second/db"))
    thread = threading.Thread(target=second.serve_forever, daemon=True)
    thread.start()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(CookieJar())
    )
    try:
        for instance in (server, second):
            opener.open(instance.origin).close()
        for instance in (server, second):
            with opener.open(instance.origin + "/api/settings") as response:
                assert response.status == 200
        assert server.cookie_name != second.cookie_name
    finally:
        second.shutdown()
        second.server_close()
        thread.join(3)


def test_comprehensive_report_routes_include_saved_and_portable_views(server, tmp_path):
    server.engine = Engine(server.store, FakeOllama)
    root = create_demo(tmp_path / "report-source")
    sid = server.engine.create(root, "Consolidated", DEFAULTS)
    wait(server.engine)
    data = json.loads(request(server, f"/api/scans/{sid}/summary")[1])
    assert data["state"] == "ready" and "What is on this drive?" in data["html"]
    code, body, headers = request(server, f"/api/scans/{sid}/comprehensive.html")
    assert code == 200 and "text/html" in headers["Content-Type"]
    assert b"File timeline" in body and b"<style>" in body
    assert request(server, f"/api/scans/{sid}/summary", auth=False)[0] == 403
    root.rename(tmp_path / "report-offline")
    assert request(server, f"/api/scans/{sid}/summary", {})[0] == 200
    wait(server.engine)
    assert server.store.get_scan(sid)["status"] == "completed"
