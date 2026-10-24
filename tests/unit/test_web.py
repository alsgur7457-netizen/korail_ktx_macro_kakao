"""Tests for the web control panel: auth, form handling and the macro process manager."""
import http.client
import json
import sys
import threading
import time
from unittest.mock import MagicMock
from urllib.parse import urlencode

import pytest

import web
from web import Auth, FormStore, JobManager, job_from_form, make_server

# Stands in for src/macro.py: prints a notice like the real logger, echoes its args, then waits.
FAKE_MACRO = [sys.executable, "-c", (
    "import sys, time\n"
    "print('2026-10-24 08:00:00,000 - __main__ - INFO - [알림] 🔍 빈자리 검색을 시작합니다', flush=True)\n"
    "print('11/01 08:00~12:00 서울→부산', flush=True)\n"
    "print('2026-10-24 08:00:01,000 - __main__ - INFO - args ' + ' '.join(sys.argv[1:]), flush=True)\n"
    "time.sleep(30)\n"
)]

FORM = {
    "date": "2099-11-01", "src": "서울", "dst": "부산", "time": "08:00", "until": "12:00",
    "train_type": "KTX", "seat": "GENERAL_FIRST", "passengers": "1", "strategy": "consecutive",
}


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class Client:
    def __init__(self, port):
        self.port = port
        self.cookie = None

    def request(self, method, path, form=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        body = None
        if form is not None:
            body = urlencode(form)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self.cookie:
            headers["Cookie"] = self.cookie
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read().decode("utf-8")
        set_cookie = response.getheader("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";", 1)[0]
        conn.close()
        return response.status, response.getheader("Location"), data


@pytest.fixture
def panel(tmp_path):
    manager = JobManager(command=FAKE_MACRO)
    kakao = MagicMock()
    server = make_server(manager, Auth("1234"), FormStore(str(tmp_path / "web.json")), kakao,
                         host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Client(server.server_address[1]), manager, kakao
    manager.stop()
    server.shutdown()
    server.server_close()


class TestAuth:
    def test_lockout_after_repeated_failures(self):
        now = [1000.0]
        auth = Auth("1234", clock=lambda: now[0])
        for _ in range(web.MAX_FAILED_LOGINS - 1):
            with pytest.raises(PermissionError, match="틀렸습니다"):
                auth.login("0000")
        with pytest.raises(PermissionError, match="잠깁니다"):
            auth.login("0000")
        with pytest.raises(PermissionError, match="후 다시"):
            auth.login("1234")  # correct PIN is refused while locked
        now[0] += web.LOCKOUT_SECONDS + 1
        assert auth.valid(auth.login("1234"))

    def test_logout_invalidates_session(self):
        auth = Auth("1234")
        token = auth.login("1234")
        auth.logout(token)
        assert not auth.valid(token)


class TestForm:
    def test_job_from_form_accepts_browser_formats(self):
        job = job_from_form(FORM)
        assert (job.dep_date, job.dep_time, job.max_dep_time) == ("20991101", "080000", "1200")

    def test_empty_until_means_no_limit(self):
        assert job_from_form({**FORM, "until": ""}).max_dep_time == "2400"

    def test_cli_args_round_trip(self):
        import macro
        job = job_from_form({**FORM, "passengers": "3", "strategy": "random", "seat": "SPECIAL_FIRST"})
        again = macro.build_job(macro.parse_args(job.to_cli_args(), env={}))
        assert again == job

    def test_store_remembers_last_values(self, tmp_path):
        store = FormStore(str(tmp_path / "web.json"))
        store.save({**FORM, "src": "용산"})
        assert store.load()["src"] == "용산"

    def test_store_defaults_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRAIN_DATE", "20991101")
        monkeypatch.setenv("TRAIN_TIME", "0730")
        monkeypatch.setenv("TRAIN_UNTIL", "2400")
        values = FormStore(str(tmp_path / "none.json")).load()
        assert (values["date"], values["time"], values["until"]) == ("2099-11-01", "07:30", "")


class TestPanel:
    def test_requires_login(self, panel):
        client, _, _ = panel
        status, _, body = client.request("GET", "/")
        assert status == 200 and "PIN" in body
        assert client.request("GET", "/status")[0] == 401
        status, location, _ = client.request("POST", "/start", FORM)
        assert status == 303 and "err=" in location

    def test_wrong_pin(self, panel):
        client, _, _ = panel
        _, location, _ = client.request("POST", "/login", {"pin": "9999"})
        assert "err=" in location and client.cookie is None

    def test_start_status_stop(self, panel):
        client, manager, _ = panel
        client.request("POST", "/login", {"pin": "1234"})
        status, _, body = client.request("GET", "/")
        assert status == 200 and "예매 조건" in body

        status, location, _ = client.request("POST", "/start", FORM)
        assert status == 303 and "msg=" in location
        assert _wait_for(lambda: len(manager.status()["log"]) >= 3)

        status, _, body = client.request("GET", "/status")
        state = json.loads(body)
        assert state["state"] == "running"
        assert "서울→부산" in state["summary"]
        assert state["notices"][0].startswith("08:00 🔍 빈자리 검색을 시작합니다")
        assert "11/01 08:00~12:00 서울→부산" in state["notices"][0]  # continuation line kept
        assert any("--date 20991101 --from 서울 --to 부산 --time 0800 --until 1200" in l for l in state["log"])

        _, location, _ = client.request("POST", "/start", FORM)
        assert "err=" in location  # only one search at a time

        _, location, _ = client.request("POST", "/stop", {})
        assert "msg=" in location
        state = json.loads(client.request("GET", "/status")[2])
        assert state["state"] == "finished" and state["result"] == "웹에서 중지함"

    def test_invalid_form_is_rejected_and_remembered(self, panel):
        client, manager, _ = panel
        client.request("POST", "/login", {"pin": "1234"})
        _, location, _ = client.request("POST", "/start", {**FORM, "dst": "서울"})
        assert "err=" in location and not manager.running
        _, _, body = client.request("GET", "/")
        assert 'id=dst name=dst list=stations value="서울"' in body

    def test_logout(self, panel):
        client, _, _ = panel
        client.request("POST", "/login", {"pin": "1234"})
        token_cookie = client.cookie
        client.request("POST", "/logout", {})
        client.cookie = token_cookie
        assert client.request("GET", "/status")[0] == 401
