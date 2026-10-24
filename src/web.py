"""
Web control panel: enter the reservation conditions from a phone, start and stop the macro.

    python src/web.py

Then open http://<PC address>:8000 (WEB_PORT) on the phone and log in with WEB_PIN.
Each search runs `src/macro.py` in its own process, so stopping it is just ending
that process; notifications still go to KakaoTalk.
"""
import argparse
import hmac
import html
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from config.settings import settings
from macro import SEAT_OPTIONS, SEAT_STRATEGIES, TRAIN_TYPES, SearchJob, build_job
from services.kakao_service import KakaoAuthError, KakaoService
from utils.logger import get_logger

logger = get_logger(__name__)

MACRO_SCRIPT = Path(__file__).with_name("macro.py")
SESSION_COOKIE = "ktx_session"
MIN_PIN_LENGTH = 4
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 300
MAX_BODY_BYTES = 10_000

# Major KTX stations for the input suggestions (any Korail station name works).
STATIONS = [
    "서울", "용산", "영등포", "광명", "수원", "행신", "청량리", "상봉", "천안아산", "오송", "대전", "서대전",
    "김천구미", "동대구", "서대구", "경주", "울산(통도사)", "부산", "구포", "포항", "밀양", "마산", "창원중앙",
    "창원", "진주", "익산", "전주", "남원", "순천", "여수EXPO", "정읍", "광주송정", "나주", "목포",
    "원주", "평창", "진부(오대산)", "강릉", "동해", "제천", "단양", "영주", "안동",
]
EXIT_TEXT = {0: "예약·결제 완료", 1: "검색 종료 (사유는 카톡·로그 확인)", 2: "설정 오류 (로그 확인)"}
LOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}),\d+ - [\w.]+ - \w+ - (.*)$")
NOTICE_MARK = "[알림] "


# ---------------------------------------------------------------------- jobs

class JobManager:
    """Runs one macro process at a time and keeps its recent output."""

    def __init__(self, command: Optional[List[str]] = None, log_lines: int = 300):
        self._command = command or [sys.executable, str(MACRO_SCRIPT)]
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._job: Optional[SearchJob] = None
        self._started_at: Optional[datetime] = None
        self._stopped_by_user = False
        self._log = deque(maxlen=log_lines)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, job: SearchJob) -> None:
        with self._lock:
            if self.running:
                raise RuntimeError("이미 검색 중입니다. 먼저 중지하세요.")
            self._log.clear()
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
            self._proc = subprocess.Popen(
                self._command + job.to_cli_args(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            self._job = job
            self._started_at = datetime.now()
            self._stopped_by_user = False
            threading.Thread(target=self._pump, args=(self._proc,), daemon=True).start()
        logger.info(f"Macro started (pid {self._proc.pid}): {job.summary().replace(chr(10), ' / ')}")

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            line = line.rstrip()
            print(line, flush=True)
            self._log.append(line)
        proc.wait()

    def stop(self) -> bool:
        with self._lock:
            if not self.running:
                return False
            self._stopped_by_user = True
            proc = self._proc
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        logger.info("Macro stopped from the web panel")
        return True

    @staticmethod
    def _pretty(line: str) -> str:
        match = LOG_LINE.match(line)
        return f"{match.group(1)} {match.group(2)}" if match else line

    def _notices(self, lines: List[str]) -> List[str]:
        """Notification texts (they may span several output lines), newest first."""
        notices: List[List[str]] = []
        current: Optional[List[str]] = None
        for line in lines:
            match = LOG_LINE.match(line)
            if match:
                message = match.group(2)
                if message.startswith(NOTICE_MARK):
                    current = [f"{match.group(1)[:5]} {message[len(NOTICE_MARK):]}"]
                    notices.append(current)
                else:
                    current = None
            elif current is not None and line.strip():
                current.append(line.strip())
        return ["\n".join(n) for n in reversed(notices)][:5]

    def status(self) -> dict:
        proc = self._proc
        if proc is None:
            state, result = "idle", ""
        elif proc.poll() is None:
            state, result = "running", ""
        else:
            state = "finished"
            result = "웹에서 중지함" if self._stopped_by_user else EXIT_TEXT.get(
                proc.returncode, f"종료됨 (코드 {proc.returncode})")
        lines = list(self._log)
        return {
            "state": state,
            "result": result,
            "summary": self._job.summary() if self._job else "",
            "started_at": self._started_at.strftime("%m/%d %H:%M") if self._started_at else "",
            "notices": self._notices(lines),
            "log": [self._pretty(line) for line in lines[-80:]],
        }


# ---------------------------------------------------------------------- auth

class Auth:
    """PIN login with a lockout after repeated failures; sessions live in memory."""

    def __init__(self, pin: str, clock: Callable[[], float] = time.time):
        self._pin = pin.encode("utf-8")
        self._clock = clock
        self._sessions = set()
        self._failed = 0
        self._locked_until = 0.0
        self._lock = threading.Lock()

    def login(self, pin: str) -> str:
        """Return a session token, or raise PermissionError with a message for the user."""
        with self._lock:
            now = self._clock()
            if now < self._locked_until:
                raise PermissionError(f"PIN을 여러 번 틀렸습니다. {int(self._locked_until - now) + 1}초 후 다시 시도하세요.")
            if hmac.compare_digest(pin.encode("utf-8"), self._pin):
                self._failed = 0
                token = secrets.token_urlsafe(32)
                self._sessions.add(token)
                return token
            self._failed += 1
            if self._failed >= MAX_FAILED_LOGINS:
                self._failed = 0
                self._locked_until = now + LOCKOUT_SECONDS
                raise PermissionError(f"PIN을 {MAX_FAILED_LOGINS}번 틀려 {LOCKOUT_SECONDS // 60}분간 잠깁니다.")
            raise PermissionError("PIN이 틀렸습니다.")

    def logout(self, token: Optional[str]) -> None:
        with self._lock:
            self._sessions.discard(token)

    def valid(self, token: Optional[str]) -> bool:
        return bool(token) and token in self._sessions


# --------------------------------------------------------------------- form

FORM_FIELDS = ("date", "src", "dst", "time", "until", "train_type", "seat", "passengers", "strategy")


class FormStore:
    """Remembers the last submitted form so the page opens pre-filled."""

    def __init__(self, path: str):
        self.path = path

    def defaults(self) -> dict:
        def hhmm(value: str) -> str:
            value = (value or "").replace(":", "")
            return f"{value[:2]}:{value[2:4]}" if len(value) >= 4 and value[:4] != "2400" else ""

        date = os.environ.get("TRAIN_DATE", "").replace("-", "")
        return {
            "date": f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else "",
            "src": os.environ.get("TRAIN_FROM", ""),
            "dst": os.environ.get("TRAIN_TO", ""),
            "time": hhmm(os.environ.get("TRAIN_TIME", "")),
            "until": hhmm(os.environ.get("TRAIN_UNTIL", "")),
            "train_type": os.environ.get("TRAIN_TYPE", "KTX").upper(),
            "seat": os.environ.get("TRAIN_SEAT", "GENERAL_FIRST").upper(),
            "passengers": os.environ.get("TRAIN_PASSENGERS", "1"),
            "strategy": os.environ.get("TRAIN_SEAT_STRATEGY", "consecutive").lower(),
        }

    def load(self) -> dict:
        values = self.defaults()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            values.update({k: str(v) for k, v in saved.items() if k in FORM_FIELDS})
        except (OSError, ValueError):
            pass
        return values

    def save(self, values: dict) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({k: values.get(k, "") for k in FORM_FIELDS}, f, ensure_ascii=False)
        except OSError as e:
            logger.warning(f"Could not save form values to {self.path}: {e}")


def job_from_form(values: dict) -> SearchJob:
    args = argparse.Namespace(
        date=values.get("date", ""), src=values.get("src", ""), dst=values.get("dst", ""),
        time=values.get("time", ""), until=values.get("until", ""),
        train_type=values.get("train_type", "KTX"), seat=values.get("seat", "GENERAL_FIRST"),
        passengers=values.get("passengers", "1"), strategy=values.get("strategy", "consecutive"),
    )
    return build_job(args)


# --------------------------------------------------------------------- pages

CSS = """
:root{--bg:#f4f5f7;--card:#fff;--text:#1d1f23;--muted:#6b7280;--line:#e3e5e8;--accent:#2563eb;--danger:#dc2626;
--ok:#16a34a;--warn:#d97706}
@media (prefers-color-scheme:dark){:root{--bg:#111316;--card:#1b1e23;--text:#e8eaed;--muted:#9aa0a6;--line:#2c3036}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic",sans-serif}
main{max-width:520px;margin:0 auto;padding:16px}h1{font-size:20px;margin:4px 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}
.card h2{font-size:15px;margin:0 0 12px;color:var(--muted);font-weight:600}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
input,select,button{font:inherit;width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:10px;
background:var(--bg);color:var(--text)}
.row{display:flex;gap:8px;align-items:flex-end}.row>div{flex:1;min-width:0}
.swap{flex:0 0 44px;padding:11px 0;margin-bottom:0}
button{background:var(--accent);color:#fff;border:0;font-weight:600;margin-top:16px;cursor:pointer}
button.secondary{background:transparent;color:var(--muted);border:1px solid var(--line)}
button.danger{background:var(--danger)}button:disabled{opacity:.45}
fieldset{border:0;padding:0;margin:0}[hidden]{display:none}details{margin-top:12px}fieldset:disabled{opacity:.6}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-size:13px;font-weight:600;color:#fff}
.idle{background:var(--muted)}.running{background:var(--ok)}.finished{background:var(--warn)}
.summary{white-space:pre-line;margin:10px 0 0}.muted{color:var(--muted);font-size:13px}
ul{list-style:none;padding:0;margin:10px 0 0}li{white-space:pre-line;border-top:1px solid var(--line);padding:8px 0;
font-size:14px}pre{white-space:pre-wrap;word-break:break-all;font-size:12px;max-height:300px;overflow:auto;
background:var(--bg);padding:8px;border-radius:8px}
.flash{padding:10px 12px;border-radius:10px;margin-bottom:14px;font-size:14px}
.flash.ok{background:#dcfce7;color:#14532d}.flash.err{background:#fee2e2;color:#7f1d1d}
.hint{font-size:12px;color:var(--muted);margin-top:4px}
"""


def _page(body: str) -> bytes:
    return (
        "<!doctype html><html lang=ko><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>KTX 예약 매크로</title><style>{CSS}</style></head><body><main>{body}</main></body></html>"
    ).encode("utf-8")


def _flash(query: dict) -> str:
    if query.get("err"):
        return f"<div class='flash err'>{html.escape(query['err'][0])}</div>"
    if query.get("msg"):
        return f"<div class='flash ok'>{html.escape(query['msg'][0])}</div>"
    return ""


def login_page(query: dict) -> bytes:
    return _page(
        "<h1>🚄 KTX 예약 매크로</h1>" + _flash(query) +
        "<form class=card method=post action=/login><h2>로그인</h2>"
        "<label for=pin>PIN</label><input id=pin name=pin type=password inputmode=numeric "
        "autocomplete=current-password required autofocus><button>들어가기</button></form>"
    )


def _options(choices: dict, selected: str) -> str:
    return "".join(
        f"<option value='{key}'{' selected' if key == selected else ''}>{html.escape(label)}</option>"
        for key, label in choices.items()
    )


def main_page(values: dict, status: dict, query: dict) -> bytes:
    v = {k: html.escape(str(values.get(k, "")), quote=True) for k in FORM_FIELDS}
    train_types = {k: label for k, (_, label) in TRAIN_TYPES.items()}
    seats = {k: label for k, (_, label) in SEAT_OPTIONS.items()}
    passengers = {str(n): f"{n}명" for n in range(1, 10)}
    stations = "".join(f"<option value='{html.escape(s)}'>" for s in STATIONS)
    running = status["state"] == "running"
    initial = html.escape(json.dumps(status, ensure_ascii=False), quote=True)

    return _page(f"""
<h1>🚄 KTX 예약 매크로</h1>{_flash(query)}
<section class=card id=status data-initial="{initial}">
  <h2>진행 상태</h2>
  <span id=badge class="badge idle">대기 중</span> <span id=result class=muted></span>
  <p id=summary class=summary></p><p id=started class=muted></p>
  <ul id=notices></ul>
  <form method=post action=/stop><button id=stop class=danger {'' if running else 'hidden'}>검색 중지</button></form>
  <details><summary class=muted>실행 로그 보기</summary><pre id=log></pre></details>
</section>
<form class=card method=post action=/start id=job>
  <h2>예매 조건</h2>
  <fieldset id=fields {'disabled' if running else ''}>
  <label for=date>출발 날짜</label><input id=date name=date type=date value="{v['date']}" required>
  <div class=row>
    <div><label for=src>출발역</label><input id=src name=src list=stations value="{v['src']}" required></div>
    <button type=button class="secondary swap" id=swap aria-label="출발역과 도착역 바꾸기">⇄</button>
    <div><label for=dst>도착역</label><input id=dst name=dst list=stations value="{v['dst']}" required></div>
  </div>
  <datalist id=stations>{stations}</datalist>
  <div class=row>
    <div><label for=time>이 시각 이후 출발</label><input id=time name=time type=time value="{v['time']}"></div>
    <div><label for=until>이 시각 전 출발까지</label><input id=until name=until type=time value="{v['until']}"></div>
  </div>
  <p class=hint>비워두면 제한 없음 (예: 08:00 ~ 12:00)</p>
  <div class=row>
    <div><label for=train_type>열차</label><select id=train_type name=train_type>{_options(train_types, values.get('train_type'))}</select></div>
    <div><label for=passengers>인원 (어른)</label><select id=passengers name=passengers>{_options(passengers, values.get('passengers'))}</select></div>
  </div>
  <label for=seat>좌석</label><select id=seat name=seat>{_options(seats, values.get('seat'))}</select>
  <label for=strategy>2명 이상일 때</label><select id=strategy name=strategy>{_options(SEAT_STRATEGIES, values.get('strategy'))}</select>
  <button>검색 시작</button>
  </fieldset>
</form>
<form method=post action=/logout><button class=secondary>로그아웃</button></form>
<script>
const LABEL={{idle:"대기 중",running:"검색 중",finished:"종료"}};
function render(s){{
  const b=document.getElementById("badge");b.textContent=LABEL[s.state];b.className="badge "+s.state;
  document.getElementById("result").textContent=s.result;
  document.getElementById("summary").textContent=s.summary;
  document.getElementById("started").textContent=s.started_at?("시작: "+s.started_at):"";
  const ul=document.getElementById("notices");ul.replaceChildren(...s.notices.map(t=>{{const li=document.createElement("li");li.textContent=t;return li}}));
  document.getElementById("log").textContent=s.log.join("\\n");
  const running=s.state==="running";
  document.getElementById("stop").hidden=!running;document.getElementById("fields").disabled=running;
}}
render(JSON.parse(document.getElementById("status").dataset.initial));
setInterval(()=>fetch("/status").then(r=>r.ok?r.json():Promise.reject(r.status)).then(render).catch(e=>{{if(e===401)location.reload()}}),4000);
document.getElementById("swap").onclick=()=>{{const a=document.getElementById("src"),b=document.getElementById("dst");[a.value,b.value]=[b.value,a.value]}};
document.getElementById("stop").form.onsubmit=()=>confirm("검색을 중지할까요?");
</script>""")


# ------------------------------------------------------------------- server

def make_handler(manager: JobManager, auth: Auth, store: FormStore, kakao: Optional[KakaoService]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "KTXMacro"

        def log_message(self, fmt, *args):  # status polling would flood the console
            pass

        # -- helpers
        def _token(self) -> Optional[str]:
            jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return jar[SESSION_COOKIE].value if SESSION_COOKIE in jar else None

        def _send(self, code: int, body: bytes, content_type: str = "text/html; charset=utf-8", headers=None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, msg: str = "", err: str = "", headers=None):
            query = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
            self._send(303, b"", headers={"Location": "/" + (f"?{query}" if query else ""), **(headers or {})})

        def _form(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                return {}
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            return {k: v[0].strip() for k, v in parse_qs(raw, keep_blank_values=True).items()}

        # -- routes
        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            authed = auth.valid(self._token())
            if url.path == "/":
                body = main_page(store.load(), manager.status(), query) if authed else login_page(query)
                self._send(200, body)
            elif url.path == "/status":
                if not authed:
                    self._send(401, b"{}", "application/json")
                else:
                    body = json.dumps(manager.status(), ensure_ascii=False).encode("utf-8")
                    self._send(200, body, "application/json; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            path = urlparse(self.path).path
            form = self._form()
            if path == "/login":
                try:
                    token = auth.login(form.get("pin", ""))
                except PermissionError as e:
                    return self._redirect(err=str(e))
                cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"
                return self._redirect(headers={"Set-Cookie": cookie})

            if not auth.valid(self._token()):
                return self._redirect(err="로그인이 필요합니다.")

            if path == "/logout":
                auth.logout(self._token())
                return self._redirect(headers={"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0"})
            if path == "/start":
                values = {k: form.get(k, "") for k in FORM_FIELDS}
                store.save(values)
                try:
                    job = job_from_form(values)
                    manager.start(job)
                except (ValueError, RuntimeError) as e:
                    return self._redirect(err=str(e))
                return self._redirect(msg="검색을 시작했습니다. 결과는 카카오톡으로도 알려드려요.")
            if path == "/stop":
                if not manager.stop():
                    return self._redirect(err="실행 중인 검색이 없습니다.")
                # On Windows the process is killed without running its own "중단" notice.
                if os.name == "nt" and kakao:
                    kakao.send_message("⏹ 웹에서 검색을 중지했습니다")
                return self._redirect(msg="검색을 중지했습니다.")
            self._send(404, b"not found", "text/plain")

    return Handler


def make_server(manager, auth, store, kakao=None, host=None, port=None) -> ThreadingHTTPServer:
    host = settings.WEB_HOST if host is None else host
    port = settings.WEB_PORT if port is None else port
    server = ThreadingHTTPServer((host, port), make_handler(manager, auth, store, kakao))
    server.daemon_threads = True
    return server


def local_addresses() -> List[str]:
    addresses = set()
    try:
        addresses.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # picks the outgoing interface; nothing is sent
            addresses.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(a for a in addresses if not a.startswith("127."))


def main() -> int:
    if len(settings.WEB_PIN) < MIN_PIN_LENGTH:
        print(f"설정 오류: .env에 WEB_PIN(웹페이지 로그인 PIN, {MIN_PIN_LENGTH}자리 이상)을 넣으세요", file=sys.stderr)
        return 2
    if not settings.KORAIL_ID or not settings.KORAIL_PW:
        print("설정 오류: KORAIL_ID / KORAIL_PW 환경변수가 필요합니다", file=sys.stderr)
        return 2
    kakao = KakaoService()
    try:
        kakao.check()
    except KakaoAuthError as e:
        print(f"카카오 설정 오류: {e}", file=sys.stderr)
        return 2

    manager = JobManager()
    try:
        server = make_server(manager, Auth(settings.WEB_PIN), FormStore(settings.WEB_STATE_FILE), kakao)
    except OSError as e:
        print(f"웹서버를 열 수 없습니다 (포트 {settings.WEB_PORT}): {e}", file=sys.stderr)
        return 1

    port = server.server_address[1]
    print("=" * 60)
    print("KTX 예약 매크로 웹페이지가 열렸습니다. 휴대폰 브라우저에서 접속하세요:")
    print(f"  - 이 PC에서: http://localhost:{port}")
    for address in local_addresses():
        where = "밖에서 (Tailscale)" if address.startswith("100.") else "같은 와이파이에서"
        print(f"  - {where}: http://{address}:{port}")
    print("끝내려면 이 창에서 Ctrl+C 를 누르세요.")
    print("=" * 60, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if manager.stop():
            kakao.send_message("⏹ 웹페이지를 종료해 검색도 중지했습니다")
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
