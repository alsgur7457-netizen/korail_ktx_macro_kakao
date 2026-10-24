"""Unit tests for the macro's argument validation and reservation/payment flow (no network)."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import macro
from macro import MacroRunner, build_job, parse_args
from services.korail_service import (
    DuplicateReservationError, KorailAccessRestrictedError, MultiReservationResult, TrainType
)

TODAY = datetime(2026, 10, 24, 9, 0)


def _job(**overrides):
    args = parse_args([], env={"TRAIN_DATE": "20261101", "TRAIN_FROM": "서울", "TRAIN_TO": "부산", **overrides})
    return build_job(args, today=TODAY)


def _reservation(rsv_id="R1", limit="20261024", limit_time="092000"):
    train = MagicMock()
    train.summary.return_value = "[KTX 101] 11/01 08:00~10:40  서울~부산"
    return SimpleNamespace(
        train=train, rsv_id=rsv_id, price=59800, seat_no_count=1,
        buy_limit_date=limit, buy_limit_time=limit_time, is_waiting=False,
    )


class FakeClock:
    def __init__(self, start=TODAY):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def runner():
    korail = MagicMock()
    korail.login.return_value = True
    kakao = MagicMock()
    clock = FakeClock()
    r = MacroRunner(_job(), korail, kakao, "010-0000-0000", "pw", sleep=clock.sleep, clock=clock)
    return r, korail, kakao


def _messages(kakao):
    return [c.args[0] for c in kakao.send_message.call_args_list]


class TestBuildJob:
    def test_defaults_from_env(self):
        job = _job()
        assert (job.dep_date, job.src, job.dst) == ("20261101", "서울", "부산")
        assert (job.dep_time, job.max_dep_time) == ("000000", "2400")
        assert (job.train_type, job.seat_option, job.passengers, job.seat_strategy) == (
            "KTX", "GENERAL_FIRST", 1, "consecutive")

    def test_cli_overrides_env_and_normalizes(self):
        args = parse_args(
            ["--date", "2026-11-01", "--from", "서울역", "--to", "동대구", "--time", "08:00", "--until", "12",
             "--type", "all", "--seat", "special_first", "--passengers", "2", "--strategy", "RANDOM"],
            env={"TRAIN_FROM": "용산"},
        )
        job = build_job(args, today=TODAY)
        assert (job.dep_date, job.src, job.dst) == ("20261101", "서울", "동대구")
        assert (job.dep_time, job.max_dep_time) == ("080000", "1200")
        assert (job.train_type, job.seat_option, job.passengers, job.seat_strategy) == (
            "ALL", "SPECIAL_FIRST", 2, "random")

    @pytest.mark.parametrize("overrides, message", [
        ({"TRAIN_DATE": ""}, "출발 날짜"),
        ({"TRAIN_DATE": "20261023"}, "지난 날짜"),
        ({"TRAIN_DATE": "2026111"}, "YYYYMMDD"),
        ({"TRAIN_TIME": "2500"}, "올바른 시각"),
        ({"TRAIN_TIME": "1200", "TRAIN_UNTIL": "1000"}, "늦어야"),
        ({"TRAIN_TYPE": "SRT"}, "열차 종류"),
        ({"TRAIN_SEAT": "WINDOW"}, "좌석 옵션"),
        ({"TRAIN_PASSENGERS": "10"}, "1~9"),
        ({"TRAIN_SEAT_STRATEGY": "any"}, "좌석 배치"),
        ({"TRAIN_TO": "서울"}, "같습니다"),
    ])
    def test_rejects_bad_input(self, overrides, message):
        with pytest.raises(ValueError, match=message):
            _job(**overrides)

    def test_summary_mentions_route_and_window(self):
        text = _job(TRAIN_TIME="0800", TRAIN_UNTIL="1200").summary()
        assert "11/01 08:00~12:00 서울→부산" in text and "KTX" in text


class TestRun:
    def test_login_failure_is_notified(self, runner):
        r, korail, kakao = runner
        korail.login.return_value = False
        korail.last_login_error = "비밀번호 오류"
        assert r.run() == 1
        assert "비밀번호 오류" in _messages(kakao)[0]
        korail.search_and_reserve_loop.assert_not_called()

    def test_search_uses_job_parameters(self, runner):
        r, korail, kakao = runner
        korail.search_and_reserve_loop.return_value = _reservation()
        korail.payment_state.return_value = "paid"
        assert r.run() == 0
        kwargs = korail.search_and_reserve_loop.call_args.kwargs
        assert kwargs["train_type"] == TrainType.KTX
        assert kwargs["reserve_option"] == "GENERAL_FIRST"
        assert (kwargs["src_locate"], kwargs["dst_locate"], kwargs["dep_date"]) == ("서울", "부산", "20261101")

    def test_success_then_paid(self, runner):
        r, korail, kakao = runner
        korail.search_and_reserve_loop.return_value = _reservation()
        korail.payment_state.return_value = "paid"
        assert r.run() == 0
        msgs = _messages(kakao)
        assert "예약 성공" in msgs[1] and "09:20까지" in msgs[1] and "59,800원" in msgs[1]
        assert "결제 완료" in msgs[-1]

    def test_reminds_until_deadline_then_reports_expiry(self, runner, monkeypatch):
        r, korail, kakao = runner
        monkeypatch.setattr(macro.settings, "PAYMENT_CHECK_SECONDS", 60)
        monkeypatch.setattr(macro.settings, "PAYMENT_REMINDER_MINUTES", 3)
        korail.search_and_reserve_loop.return_value = _reservation()
        states = iter(["pending"] * 9 + ["gone"])
        korail.payment_state.side_effect = lambda rsv_id: next(states)

        assert r.run() == 1
        msgs = _messages(kakao)
        reminders = [m for m in msgs if m.startswith("⏰ 결제 대기")]
        assert len(reminders) == 3  # at +3, +6, +9 minutes
        assert "취소" in msgs[-1]

    def test_duplicate_is_notified_once_and_search_continues(self, runner):
        r, korail, kakao = runner
        korail.search_and_reserve_loop.side_effect = [
            DuplicateReservationError("dup"), DuplicateReservationError("dup"), _reservation(),
        ]
        korail.payment_state.return_value = "paid"
        assert r.run() == 0
        assert sum("이미 내 예약" in m for m in _messages(kakao)) == 1
        assert korail.search_and_reserve_loop.call_count == 3

    def test_access_restriction_stops(self, runner):
        r, korail, kakao = runner
        korail.search_and_reserve_loop.side_effect = KorailAccessRestrictedError("HTTP 403 (-8202)")
        assert r.run() == 1
        assert "이용 제한" in _messages(kakao)[-1]

    def test_past_departure_stops(self, runner):
        r, korail, kakao = runner
        korail.search_and_reserve_loop.side_effect = ValueError("출발 시각이 이미 지났습니다")
        assert r.run() == 1
        assert "이미 지났습니다" in _messages(kakao)[-1]

    def test_random_seats_are_followed_individually(self, runner):
        r, korail, kakao = runner
        korail.search_and_reserve_loop.return_value = MultiReservationResult(
            [_reservation("R1"), _reservation("R2")], 2)
        korail.payment_state.side_effect = lambda rsv_id: "paid"
        assert r.run() == 0
        msgs = _messages(kakao)
        assert any("좌석 1/2" in m for m in msgs) and any("좌석 2/2" in m for m in msgs)
        assert sum("결제 완료" in m for m in msgs) == 2
