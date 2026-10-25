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
        headers.update({"Cookie": f"dv_session={server.token}", "X-Drivestigate-Token": server.token})
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
        headers={"Cookie": f"dv_session={server.token}", "Content-Type": "application/json"},
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
