"""Unit tests for KakaoService token handling and message sending (no network)."""
import json
import os
import time
from unittest.mock import MagicMock

import pytest

from kakao_auth import extract_code
from services.kakao_service import KakaoAuthError, KakaoService


def _response(status, payload):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.text = json.dumps(payload)
    return response


@pytest.fixture
def kakao(tmp_path):
    session = MagicMock()
    svc = KakaoService(
        rest_api_key="rest-key", redirect_uri="http://localhost:8080/kakao",
        token_file=str(tmp_path / "kakao.json"), session=session,
    )
    return svc, session


def _write_token(svc, **overrides):
    token = {"access_token": "old", "refresh_token": "refresh", "expires_at": time.time() + 3600, **overrides}
    with open(svc.token_file, "w") as f:
        json.dump(token, f)


class TestAuth:
    def test_authorize_url_requests_talk_message_scope(self, kakao):
        svc, _ = kakao
        url = svc.authorize_url()
        assert url.startswith("https://kauth.kakao.com/oauth/authorize?")
        assert "client_id=rest-key" in url and "scope=talk_message" in url

    def test_exchange_code_saves_token_privately(self, kakao):
        svc, session = kakao
        session.post.return_value = _response(200, {
            "access_token": "a", "expires_in": 21599, "refresh_token": "r", "refresh_token_expires_in": 5183999,
        })
        svc.exchange_code("the-code")

        sent = session.post.call_args.kwargs["data"]
        assert sent["grant_type"] == "authorization_code" and sent["code"] == "the-code"
        assert "client_secret" not in sent
        saved = json.load(open(svc.token_file))
        assert saved["access_token"] == "a" and saved["refresh_token"] == "r"
        assert os.stat(svc.token_file).st_mode & 0o777 == 0o600

    def test_client_secret_is_sent_when_configured(self, tmp_path):
        session = MagicMock()
        session.post.return_value = _response(200, {"access_token": "a", "expires_in": 1, "refresh_token": "r"})
        svc = KakaoService(rest_api_key="k", client_secret="s", token_file=str(tmp_path / "t.json"), session=session)
        svc.exchange_code("c")
        assert session.post.call_args.kwargs["data"]["client_secret"] == "s"

    def test_token_error_is_reported(self, kakao):
        svc, session = kakao
        session.post.return_value = _response(400, {"error": "invalid_grant", "error_description": "bad code"})
        with pytest.raises(KakaoAuthError, match="bad code"):
            svc.exchange_code("x")

    def test_missing_token_file_points_to_setup(self, kakao):
        svc, _ = kakao
        with pytest.raises(KakaoAuthError, match="kakao-auth"):
            svc.check()

    def test_expired_access_token_is_refreshed_and_refresh_token_kept(self, kakao):
        svc, session = kakao
        _write_token(svc, expires_at=time.time() - 10)
        session.post.return_value = _response(200, {"access_token": "new", "expires_in": 21599})
        svc.check()

        sent = session.post.call_args.kwargs["data"]
        assert sent["grant_type"] == "refresh_token" and sent["refresh_token"] == "refresh"
        saved = json.load(open(svc.token_file))
        assert saved["access_token"] == "new" and saved["refresh_token"] == "refresh"


class TestSendMessage:
    def test_sends_text_template(self, kakao):
        svc, session = kakao
        _write_token(svc)
        session.post.return_value = _response(200, {"result_code": 0})

        assert svc.send_message("안녕") is True
        call = session.post.call_args
        assert call.args[0] == "https://kapi.kakao.com/v2/api/talk/memo/default/send"
        assert call.kwargs["headers"]["Authorization"] == "Bearer old"
        template = json.loads(call.kwargs["data"]["template_object"])
        assert template["object_type"] == "text" and template["text"] == "안녕"
        assert template["link"]["web_url"].startswith("https://")

    def test_long_text_is_truncated_to_limit(self, kakao):
        svc, session = kakao
        _write_token(svc)
        session.post.return_value = _response(200, {"result_code": 0})
        svc.send_message("가" * 500)
        template = json.loads(session.post.call_args.kwargs["data"]["template_object"])
        assert len(template["text"]) == 200

    def test_401_refreshes_once_and_retries(self, kakao):
        svc, session = kakao
        _write_token(svc)
        session.post.side_effect = [
            _response(401, {"code": -401}),
            _response(200, {"access_token": "new", "expires_in": 21599}),
            _response(200, {"result_code": 0}),
        ]
        assert svc.send_message("hi") is True
        assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer new"

    def test_failure_returns_false_instead_of_raising(self, kakao):
        svc, session = kakao
        _write_token(svc)
        session.post.return_value = _response(400, {"code": -2})
        assert svc.send_message("hi") is False

    def test_no_token_returns_false(self, kakao):
        svc, _ = kakao
        assert svc.send_message("hi") is False


class TestExtractCode:
    def test_full_redirect_url(self):
        assert extract_code("http://localhost:8080/kakao?code=abc123") == "abc123"

    def test_bare_code(self):
        assert extract_code("  abc123 \n") == "abc123"

    def test_denied_consent(self):
        with pytest.raises(KakaoAuthError, match="거부"):
            extract_code("http://localhost:8080/kakao?error=access_denied&error_description=User%20denied")
