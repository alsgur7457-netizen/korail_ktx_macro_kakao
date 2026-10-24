"""
KTX reservation macro with KakaoTalk notifications.

Searches until a seat opens up, reserves it, then tells you on KakaoTalk
("나와의 채팅") and keeps reminding you until the ticket is paid or the
purchase deadline passes.

Every option can come from the command line or from `.env` (TRAIN_* variables):

    python src/macro.py --date 20261101 --from 서울 --to 부산 --time 0800 --until 1200
"""
import argparse
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, List, Optional

from config.settings import settings
from services.kakao_service import KakaoAuthError, KakaoService
from services.korail_service import (
    DuplicateReservationError, KorailAccessRestrictedError, KorailClientUnsupportedError, KorailService,
    ReserveOption, TrainType,
)
from utils.logger import get_logger

logger = get_logger(__name__)

TRAIN_TYPES = {"KTX": (TrainType.KTX, "KTX"), "ALL": (TrainType.ALL, "전체 열차")}
SEAT_OPTIONS = {
    "GENERAL_FIRST": (ReserveOption.GENERAL_FIRST, "일반실 우선"),
    "GENERAL_ONLY": (ReserveOption.GENERAL_ONLY, "일반실만"),
    "SPECIAL_FIRST": (ReserveOption.SPECIAL_FIRST, "특실 우선"),
    "SPECIAL_ONLY": (ReserveOption.SPECIAL_ONLY, "특실만"),
}
SEAT_STRATEGIES = {"consecutive": "연속 좌석", "random": "따로 앉기"}


@dataclass(frozen=True)
class SearchJob:
    """What to look for, already validated."""

    dep_date: str        # YYYYMMDD
    src: str
    dst: str
    dep_time: str        # HHMMSS, earliest departure
    max_dep_time: str    # HHMM, departures before this time ("2400" = no limit)
    train_type: str      # TRAIN_TYPES key
    seat_option: str     # SEAT_OPTIONS key
    passengers: int
    seat_strategy: str   # SEAT_STRATEGIES key

    def summary(self) -> str:
        until = "" if self.max_dep_time == "2400" else f"~{self.max_dep_time[:2]}:{self.max_dep_time[2:]}"
        seats = f"{self.passengers}명" + (f" {SEAT_STRATEGIES[self.seat_strategy]}" if self.passengers > 1 else "")
        return (
            f"{self.dep_date[4:6]}/{self.dep_date[6:]} {self.dep_time[:2]}:{self.dep_time[2:4]}{until} "
            f"{self.src}→{self.dst}\n"
            f"{TRAIN_TYPES[self.train_type][1]}, {SEAT_OPTIONS[self.seat_option][1]}, {seats}"
        )

    def to_cli_args(self) -> List[str]:
        """Command-line options that rebuild this job in another process."""
        return [
            "--date", self.dep_date, "--from", self.src, "--to", self.dst,
            "--time", self.dep_time[:4], "--until", self.max_dep_time,
            "--type", self.train_type, "--seat", self.seat_option,
            "--passengers", str(self.passengers), "--strategy", self.seat_strategy,
        ]


# ------------------------------------------------------------------ arguments

def _clean_date(value: str, today: datetime) -> str:
    digits = value.replace("-", "").replace(".", "").replace("/", "").strip()
    try:
        if len(digits) != 8 or not digits.isdigit():
            raise ValueError
        day = datetime.strptime(digits, "%Y%m%d")
    except ValueError:
        raise ValueError(f"날짜는 YYYYMMDD 형식이어야 합니다 (입력: {value!r})") from None
    if day.date() < today.date():
        raise ValueError(f"이미 지난 날짜입니다: {digits}")
    return digits


def _clean_hhmm(value: str, name: str, allow_2400: bool = False) -> str:
    digits = value.replace(":", "").strip()
    if len(digits) == 6 and digits.endswith("00"):
        digits = digits[:4]
    if len(digits) <= 2 and digits.isdigit():
        digits = digits.zfill(2) + "00"
    if len(digits) != 4 or not digits.isdigit():
        raise ValueError(f"{name}은(는) HHMM 형식이어야 합니다 (입력: {value!r})")
    if allow_2400 and digits == "2400":
        return digits
    if int(digits[:2]) > 23 or int(digits[2:]) > 59:
        raise ValueError(f"{name}이(가) 올바른 시각이 아닙니다 (입력: {value!r})")
    return digits


def _clean_station(value: str, name: str) -> str:
    station = (value or "").strip()
    if station.endswith("역") and len(station) > 2:
        station = station[:-1]
    if not station:
        raise ValueError(f"{name}을(를) 입력하세요")
    return station


def build_job(args: argparse.Namespace, today: Optional[datetime] = None) -> SearchJob:
    """Validate parsed arguments into a SearchJob (raises ValueError with a Korean message)."""
    today = today or datetime.now()
    if not args.date:
        raise ValueError("출발 날짜(--date 또는 TRAIN_DATE)를 입력하세요")
    dep_date = _clean_date(args.date, today)
    dep_time = _clean_hhmm(args.time or "0000", "출발 시각")
    max_dep_time = _clean_hhmm(args.until or "2400", "마지막 출발 시각", allow_2400=True)
    if int(max_dep_time) <= int(dep_time):
        raise ValueError("마지막 출발 시각(--until)은 출발 시각(--time)보다 늦어야 합니다")

    train_type = args.train_type.strip().upper()
    if train_type not in TRAIN_TYPES:
        raise ValueError(f"열차 종류는 {', '.join(TRAIN_TYPES)} 중 하나입니다 (입력: {args.train_type!r})")
    seat_option = args.seat.strip().upper()
    if seat_option not in SEAT_OPTIONS:
        raise ValueError(f"좌석 옵션은 {', '.join(SEAT_OPTIONS)} 중 하나입니다 (입력: {args.seat!r})")
    seat_strategy = args.strategy.strip().lower()
    if seat_strategy not in SEAT_STRATEGIES:
        raise ValueError(f"좌석 배치는 {', '.join(SEAT_STRATEGIES)} 중 하나입니다 (입력: {args.strategy!r})")
    try:
        passengers = int(args.passengers)
    except (TypeError, ValueError):
        raise ValueError(f"인원은 숫자여야 합니다 (입력: {args.passengers!r})") from None
    if not 1 <= passengers <= 9:
        raise ValueError("인원은 1~9명이어야 합니다")

    src = _clean_station(args.src, "출발역(--from)")
    dst = _clean_station(args.dst, "도착역(--to)")
    if src == dst:
        raise ValueError("출발역과 도착역이 같습니다")

    return SearchJob(
        dep_date=dep_date, src=src, dst=dst, dep_time=dep_time + "00", max_dep_time=max_dep_time,
        train_type=train_type, seat_option=seat_option, passengers=passengers, seat_strategy=seat_strategy,
    )


def parse_args(argv: Optional[List[str]] = None, env=os.environ) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="매진된 KTX 좌석을 자동으로 예약하고 카카오톡으로 알려줍니다.",
        epilog="모든 옵션은 .env의 TRAIN_* 변수로도 지정할 수 있습니다.",
    )
    parser.add_argument("--date", default=env.get("TRAIN_DATE"), help="출발 날짜 YYYYMMDD (TRAIN_DATE)")
    parser.add_argument("--from", dest="src", default=env.get("TRAIN_FROM"), help="출발역, 예: 서울 (TRAIN_FROM)")
    parser.add_argument("--to", dest="dst", default=env.get("TRAIN_TO"), help="도착역, 예: 부산 (TRAIN_TO)")
    parser.add_argument("--time", default=env.get("TRAIN_TIME", "0000"),
                        help="이 시각 이후 출발 HHMM (TRAIN_TIME, 기본 0000)")
    parser.add_argument("--until", default=env.get("TRAIN_UNTIL", "2400"),
                        help="이 시각 전에 출발하는 열차만 HHMM (TRAIN_UNTIL, 기본 제한 없음)")
    parser.add_argument("--type", dest="train_type", default=env.get("TRAIN_TYPE", "KTX"),
                        help="KTX 또는 ALL (TRAIN_TYPE, 기본 KTX)")
    parser.add_argument("--seat", default=env.get("TRAIN_SEAT", "GENERAL_FIRST"),
                        help="GENERAL_FIRST, GENERAL_ONLY, SPECIAL_FIRST, SPECIAL_ONLY (TRAIN_SEAT)")
    parser.add_argument("--passengers", default=env.get("TRAIN_PASSENGERS", "1"),
                        help="어른 인원 1~9 (TRAIN_PASSENGERS, 기본 1)")
    parser.add_argument("--strategy", default=env.get("TRAIN_SEAT_STRATEGY", "consecutive"),
                        help="consecutive(연속 좌석) 또는 random(따로 앉기) (TRAIN_SEAT_STRATEGY)")
    parser.add_argument("--dry-run", action="store_true", help="설정만 확인하고 검색하지 않습니다")
    return parser.parse_args(argv)


# --------------------------------------------------------------------- runner

def describe(reservation) -> str:
    """One or two lines about a pykorail Reservation."""
    try:
        return f"{reservation.train.summary()}\n{reservation.price:,}원 ({reservation.seat_no_count}석)"
    except Exception:
        return str(reservation)


def purchase_deadline(reservation, fallback: datetime) -> datetime:
    try:
        return datetime.strptime(reservation.buy_limit_date + reservation.buy_limit_time[:4], "%Y%m%d%H%M")
    except Exception:
        return fallback


class MacroRunner:
    """Search → reserve → notify → follow payment."""

    DUPLICATE_RETRY_SECONDS = 10
    GONE_GRACE = timedelta(minutes=2)  # stop trusting failed lookups this long after the deadline

    def __init__(
        self,
        job: SearchJob,
        korail: KorailService,
        kakao: KakaoService,
        username: str,
        password: str,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.job = job
        self.korail = korail
        self.kakao = kakao
        self.username = username
        self.password = password
        self.sleep = sleep
        self.clock = clock

    def notify(self, text: str) -> None:
        logger.info(f"[알림] {text}")
        self.kakao.send_message(text)

    def run(self) -> int:
        """Returns the process exit code: 0 when a reservation was made and paid."""
        if not self.korail.login(self.username, self.password):
            self.notify(f"❌ 코레일 로그인 실패\n{self.korail.last_login_error or '알 수 없는 오류'}")
            return 1

        self.notify(f"🔍 빈자리 검색을 시작합니다\n{self.job.summary()}")
        try:
            result = self._search()
        except KorailClientUnsupportedError:
            self.notify(
                "🚫 코레일 앱 보안 업데이트로 열차 조회가 거부되어 검색을 멈췄습니다 (-8204).\n"
                "pykorail이 새 보안 방식을 지원하기 전까지는 동작하지 않습니다."
            )
            return 1
        except KorailAccessRestrictedError as e:
            self.notify(
                f"🚫 코레일 이용 제한으로 검색을 멈췄습니다\n{e}\n"
                "VPN·클라우드가 아닌 가정용 회선에서, 공인 IP가 바뀐 뒤 다시 실행하세요."
            )
            return 1
        except ValueError as e:
            self.notify(f"⚠️ 검색을 멈췄습니다\n{e}")
            return 1
        except Exception as e:
            logger.error("Unexpected error in reservation loop", exc_info=True)
            self.notify(f"❌ 예상치 못한 오류로 검색을 멈췄습니다\n{type(e).__name__}: {e}")
            return 1

        if not result:
            self.notify("❌ 알 수 없는 이유로 예약에 실패했습니다. 코레일 앱에서 예약 내역을 확인하세요.")
            return 1

        reservations = list(getattr(result, "_all_reservations", [result]))
        return self._follow_payment(reservations)

    def _search(self):
        job = self.job
        duplicate_notified = False
        while True:
            try:
                return self.korail.search_and_reserve_loop(
                    dep_date=job.dep_date,
                    src_locate=job.src,
                    dst_locate=job.dst,
                    dep_time=job.dep_time,
                    max_dep_time=job.max_dep_time,
                    train_type=TRAIN_TYPES[job.train_type][0],
                    reserve_option=SEAT_OPTIONS[job.seat_option][0],
                    passenger_count=job.passengers,
                    seat_strategy=job.seat_strategy,
                )
            except DuplicateReservationError:
                if not duplicate_notified:
                    duplicate_notified = True
                    self.notify(
                        "⚠️ 같은 열차에 이미 내 예약이 있습니다.\n"
                        "기존 예약이 취소될 때까지 계속 검색합니다."
                    )
                self.sleep(self.DUPLICATE_RETRY_SECONDS)

    def _follow_payment(self, reservations: list) -> int:
        now = self.clock()
        fallback = now + timedelta(minutes=settings.PAYMENT_TIMEOUT_MINUTES)
        pending = {}
        for i, res in enumerate(reservations, 1):
            deadline = purchase_deadline(res, fallback)
            head = "🎉 열차 예약 성공!" if len(reservations) == 1 else f"🎉 좌석 {i}/{len(reservations)} 예약 성공!"
            if getattr(res, "is_waiting", False) is True:
                self.notify(f"{head} (예약대기)\n{describe(res)}\n좌석이 배정되면 코레일 앱에서 결제하세요.")
                continue
            self.notify(f"{head}\n{describe(res)}\n⏰ {deadline:%H:%M}까지 코레일톡 앱에서 결제하세요!")
            pending[res.rsv_id] = (res, deadline)

        reminder_every = timedelta(minutes=max(settings.PAYMENT_REMINDER_MINUTES, 1))
        next_reminder = now + reminder_every
        all_paid = True

        while pending:
            self.sleep(settings.PAYMENT_CHECK_SECONDS)
            now = self.clock()
            for rsv_id, (res, deadline) in list(pending.items()):
                try:
                    state = self.korail.payment_state(rsv_id)
                except KorailAccessRestrictedError as e:
                    logger.error(f"Payment check blocked: {e}")
                    self.notify("⚠️ 결제 확인을 할 수 없어 알림을 멈춥니다. 코레일 앱에서 직접 확인하세요.")
                    return 0 if all_paid else 1
                if state == "paid":
                    self.notify(f"✅ 결제 완료를 확인했습니다\n{describe(res)}")
                    del pending[rsv_id]
                elif state == "gone" or (state == "unknown" and now > deadline + self.GONE_GRACE):
                    self.notify(f"⌛ 결제 기한이 지나 예약이 취소되었습니다\n{describe(res)}")
                    del pending[rsv_id]
                    all_paid = False

            if pending and now >= next_reminder:
                deadline = min(d for _, d in pending.values())
                minutes_left = int((deadline - now).total_seconds() // 60)
                if minutes_left >= 0:
                    self.notify(
                        f"⏰ 결제 대기 중인 예약 {len(pending)}건\n"
                        f"{deadline:%H:%M}까지 결제하세요 (약 {minutes_left}분 남음)"
                    )
                next_reminder = now + reminder_every

        return 0 if all_paid else 1


def _stop(signum, frame):
    raise KeyboardInterrupt


def main(argv: Optional[List[str]] = None) -> int:
    signal.signal(signal.SIGTERM, _stop)  # `docker stop` -> same path as Ctrl+C
    args = parse_args(argv)
    try:
        job = build_job(args)
    except ValueError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
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

    logger.info("=" * 60)
    logger.info("KTX 예약 매크로 (카카오톡 알림)")
    for line in job.summary().splitlines():
        logger.info(f"  {line}")
    logger.info(f"  검색 간격: {settings.KORAIL_SEARCH_INTERVAL}초")
    logger.info("=" * 60)
    if settings.KORAIL_SEARCH_INTERVAL < 3:
        logger.warning("SEARCH_INTERVAL이 3초보다 짧습니다. 매크로로 판정돼 IP가 차단될 수 있습니다.")

    if args.dry_run:
        logger.info("--dry-run: 설정 확인만 하고 종료합니다")
        return 0

    runner = MacroRunner(job, KorailService(), kakao, settings.KORAIL_ID, settings.KORAIL_PW)
    try:
        return runner.run()
    except KeyboardInterrupt:
        logger.info("사용자가 중단했습니다")
        runner.notify("⏹ 매크로를 중단했습니다")
        return 130


if __name__ == "__main__":
    sys.exit(main())
