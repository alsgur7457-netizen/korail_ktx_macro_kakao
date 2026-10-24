"""Kakao "나에게 보내기" messaging service.

Sends text messages to the user's own KakaoTalk chat through the Kakao REST API
(``/v2/api/talk/memo/default/send``). The OAuth token is obtained once with
``src/kakao_auth.py`` and stored in ``KAKAO_TOKEN_FILE``; the access token
(about 6 hours) is refreshed automatically with the refresh token (about 2 months).
"""
import json
import os
import time
from typing import Optional
from urllib.parse import urlencode

import requests

from config.settings import settings
from utils.logger import get_logger

logger = get_logger(__name__)

KAUTH_HOST = "https://kauth.kakao.com"
KAPI_HOST = "https://kapi.kakao.com"
TEXT_LIMIT = 200  # text template limit
AUTH_COMMAND = "python src/kakao_auth.py"
REFRESH_MARGIN_SECONDS = 300


class KakaoAuthError(Exception):
    """Kakao token is missing, expired beyond refresh, or the app settings are wrong.

    The fix is always to re-run ``python src/kakao_auth.py``.
    """


class KakaoService:
    """Service for sending messages to my own KakaoTalk chat."""

    def __init__(
        self,
        rest_api_key: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
        token_file: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ):
        self.rest_api_key = rest_api_key or settings.KAKAO_REST_API_KEY
        self.client_secret = client_secret or settings.KAKAO_CLIENT_SECRET
        self.redirect_uri = redirect_uri or settings.KAKAO_REDIRECT_URI
        self.token_file = token_file or settings.KAKAO_TOKEN_FILE
        self.session = session or requests.Session()
        self._token: Optional[dict] = None

    # ----------------------------------------------------------------- OAuth

    def authorize_url(self) -> str:
        """URL the user opens in a browser to allow `talk_message`."""
        self._require_key()
        query = urlencode({
            "client_id": self.rest_api_key,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "talk_message",
        })
        return f"{KAUTH_HOST}/oauth/authorize?{query}"

    def exchange_code(self, code: str) -> dict:
        """Trade the authorization code for tokens and save them."""
        self._require_key()
        payload = self._token_request({
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
            "code": code,
        })
        if "refresh_token" not in payload:
            raise KakaoAuthError(f"토큰 응답에 refresh_token이 없습니다: {payload}")
        self._save_token(payload)
        return self._token

    def _token_request(self, data: dict) -> dict:
        data = {"client_id": self.rest_api_key, **data}
        if self.client_secret:
            data["client_secret"] = self.client_secret
        try:
            response = self.session.post(f"{KAUTH_HOST}/oauth/token", data=data, timeout=10)
        except requests.RequestException as e:
            raise KakaoAuthError(f"카카오 인증 서버에 연결하지 못했습니다: {e}") from e
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text[:200]}
        if response.status_code != 200 or "access_token" not in payload:
            error = payload.get("error_description") or payload.get("error") or payload
            raise KakaoAuthError(f"카카오 토큰 요청 실패 (HTTP {response.status_code}): {error}")
        return payload

    # ----------------------------------------------------------------- token

    def _require_key(self) -> None:
        if not self.rest_api_key:
            raise KakaoAuthError("KAKAO_REST_API_KEY 환경변수가 필요합니다")

    def _save_token(self, payload: dict) -> None:
        now = time.time()
        token = dict(self._token or {})
        token["access_token"] = payload["access_token"]
        token["expires_at"] = now + int(payload.get("expires_in", 0))
        # Kakao only returns a new refresh token when the old one is close to expiring.
        if payload.get("refresh_token"):
            token["refresh_token"] = payload["refresh_token"]
            token["refresh_token_expires_at"] = now + int(payload.get("refresh_token_expires_in", 0))
        self._token = token

        os.makedirs(os.path.dirname(os.path.abspath(self.token_file)), exist_ok=True)
        fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(token, f)

    def _load_token(self) -> dict:
        if self._token is None:
            try:
                with open(self.token_file, "r", encoding="utf-8") as f:
                    self._token = json.load(f)
            except FileNotFoundError:
                raise KakaoAuthError(
                    f"카카오 토큰 파일이 없습니다 ({self.token_file}). "
                    f"카카오 연결부터 하세요: {AUTH_COMMAND} (윈도우: py -3.12 -m pipenv run {AUTH_COMMAND})"
                ) from None
            except (OSError, json.JSONDecodeError) as e:
                raise KakaoAuthError(f"카카오 토큰 파일을 읽지 못했습니다 ({self.token_file}): {e}") from e
        return self._token

    def _refresh(self) -> None:
        token = self._load_token()
        if not token.get("refresh_token"):
            raise KakaoAuthError(f"refresh_token이 없습니다. 카카오 연결을 다시 하세요: {AUTH_COMMAND}")
        self._require_key()
        payload = self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
        })
        self._save_token(payload)
        logger.info("Kakao access token refreshed")

    def _access_token(self) -> str:
        token = self._load_token()
        if time.time() >= token.get("expires_at", 0) - REFRESH_MARGIN_SECONDS:
            self._refresh()
        return self._token["access_token"]

    def check(self) -> None:
        """Fail fast at startup if the stored token can't be used."""
        self._access_token()

    # --------------------------------------------------------------- message

    def send_message(self, text: str, link_url: Optional[str] = None) -> bool:
        """
        Send a text message to my KakaoTalk chat.

        Never raises: a failed notification must not stop the reservation loop.

        Returns:
            True if the message was sent successfully, False otherwise
        """
        text = text.strip()
        if len(text) > TEXT_LIMIT:
            text = text[:TEXT_LIMIT - 1] + "…"
        url = link_url or settings.KORAIL_PAYMENT_URL
        template = {
            "object_type": "text",
            "text": text,
            "link": {"web_url": url, "mobile_web_url": url},
            "button_title": "코레일 열기",
        }
        data = {"template_object": json.dumps(template, ensure_ascii=False)}

        for attempt in (1, 2):
            try:
                response = self.session.post(
                    f"{KAPI_HOST}/v2/api/talk/memo/default/send",
                    headers={"Authorization": f"Bearer {self._access_token()}"},
                    data=data,
                    timeout=10,
                )
            except KakaoAuthError as e:
                logger.error(f"Kakao message not sent: {e}")
                return False
            except requests.RequestException as e:
                logger.error(f"Kakao message not sent (network): {e}")
                return False

            if response.status_code == 200:
                logger.info("Kakao message sent")
                return True
            if response.status_code == 401 and attempt == 1:
                # Token revoked or expired early: refresh once and retry.
                try:
                    self._refresh()
                except KakaoAuthError as e:
                    logger.error(f"Kakao message not sent: {e}")
                    return False
                continue
            logger.error(f"Kakao message failed (HTTP {response.status_code}): {response.text[:200]}")
            return False
        return False
