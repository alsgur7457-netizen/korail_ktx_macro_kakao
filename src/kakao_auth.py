"""
One-time Kakao login to get a "나에게 보내기" token.

    python src/kakao_auth.py          # login and send a test message
    python src/kakao_auth.py --test   # only send a test message with the saved token

Open the printed URL, allow "카카오톡 메시지 전송", then paste the address the
browser was redirected to (the page itself may fail to load - that's fine).
"""
import argparse
import sys
from urllib.parse import parse_qs, urlparse

from services.kakao_service import KakaoAuthError, KakaoService


def extract_code(pasted: str) -> str:
    """Accept either the full redirect URL or the bare code."""
    pasted = pasted.strip()
    if "://" not in pasted and "?" not in pasted:
        return pasted
    query = parse_qs(urlparse(pasted).query)
    if "error" in query:
        reason = query.get("error_description", query["error"])[0]
        raise KakaoAuthError(f"카카오 로그인이 거부되었습니다: {reason}")
    if "code" not in query:
        raise KakaoAuthError("붙여넣은 주소에 code 값이 없습니다")
    return query["code"][0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="카카오톡 '나에게 보내기' 토큰 발급")
    parser.add_argument("--test", action="store_true", help="저장된 토큰으로 테스트 메시지만 보냅니다")
    args = parser.parse_args(argv)

    kakao = KakaoService()
    try:
        if not args.test:
            print("1) 아래 주소를 브라우저에서 열고 카카오 로그인 후 '카카오톡 메시지 전송'에 동의하세요.\n")
            print(f"   {kakao.authorize_url()}\n")
            print(f"2) {kakao.redirect_uri} 로 이동한 주소창의 전체 주소를 붙여넣으세요.")
            print("   (페이지가 열리지 않아도 괜찮습니다)\n")
            code = extract_code(input("주소 또는 code: "))
            kakao.exchange_code(code)
            print(f"\n✅ 토큰을 저장했습니다: {kakao.token_file}")
        kakao.check()
    except KakaoAuthError as e:
        print(f"\n❌ {e}", file=sys.stderr)
        return 1

    if not kakao.send_message("[KTX 매크로] 카카오톡 알림 연결 완료! 예약에 성공하면 여기로 알려드릴게요."):
        print("❌ 테스트 메시지 전송에 실패했습니다. 로그를 확인하세요.", file=sys.stderr)
        return 1
    print("✅ 카카오톡 '나와의 채팅'으로 테스트 메시지를 보냈습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
