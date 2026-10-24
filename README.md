# 코레일 KTX 예매 매크로 (카카오톡 알림)

매진된 KTX 열차를 계속 조회하다가 좌석이 나오면 자동으로 예약하고, **카카오톡 '나와의 채팅'** 으로 알려주는 프로그램입니다.
예약 후에는 결제가 확인될 때까지 몇 분마다 결제 독촉 알림을 보내고, 결제 완료나 기한 만료도 알려줍니다.

> [GeunSam2/korail_KTX_macro_telegrambot](https://github.com/GeunSam2/korail_KTX_macro_telegrambot)(텔레그램 봇)을 포크해 카카오톡 알림 + 설정 파일 방식으로 바꾼 버전입니다.

## 빠른 시작

```bash
git clone https://github.com/alsgur7457-netizen/korail_ktx_macro_telegrambot.git
cd korail_ktx_macro_telegrambot

make setup        # pipenv 설치 (처음 한 번)
make install      # 패키지 설치
cp .env.example .env   # 코레일 계정, 예매 조건, 카카오 키 입력
make kakao-auth   # 카카오 토큰 발급 + 테스트 메시지 (처음 한 번)
make check        # 설정 확인 (검색 안 함)
make run          # 매크로 실행
```

## 1. 카카오 앱 준비 (처음 한 번)

봇이 직접 '나에게 보내기' REST API를 호출하므로 개인용 카카오 앱이 하나 필요합니다. 무료이고 5분 정도 걸립니다.
(카카오 개발자 콘솔의 메뉴 이름은 개편에 따라 조금 다를 수 있습니다.)

1. <https://developers.kakao.com> 에 카카오 계정으로 로그인 → **내 애플리케이션 → 애플리케이션 추가** (이름은 아무거나, 예: `KTX 알림`)
2. **앱 키 → REST API 키** 를 복사해 `.env`의 `KAKAO_REST_API_KEY`에 넣습니다.
3. **카카오 로그인 → 활성화 설정 ON**, **Redirect URI** 에 `http://localhost:8080/kakao` 를 등록합니다. (`.env`의 `KAKAO_REDIRECT_URI`와 같아야 합니다.)
4. **카카오 로그인 → 동의항목 → 카카오톡 메시지 전송(talk_message)** 을 '선택 동의'로 설정합니다.
5. **플랫폼 → Web → 사이트 도메인** 에 `https://www.letskorail.com` 을 등록합니다. (알림의 '코레일 열기' 버튼 링크용)
6. **보안 → Client Secret** 을 '사용함'으로 켰다면 그 값을 `KAKAO_CLIENT_SECRET`에 넣습니다.
7. `make kakao-auth` 를 실행하고 안내대로 진행합니다.
   - 출력된 주소를 브라우저로 열어 로그인하고 동의합니다.
   - `http://localhost:8080/kakao?code=...` 로 이동하면(페이지가 안 열려도 정상) **주소창 전체를 복사해 붙여넣습니다.**
   - 카톡 '나와의 채팅'에 테스트 메시지가 오면 끝입니다.

토큰은 `~/.korail_bot_kakao.json`(Docker는 `./data/kakao_token.json`)에 저장되고, 액세스 토큰은 자동으로 갱신됩니다.
리프레시 토큰은 약 2개월 유효하며 쓰는 동안 자동 연장됩니다. 만료되면 `make kakao-auth` 만 다시 하면 됩니다.

## 2. 예매 조건 설정

`.env` (또는 명령행 옵션):

| 변수 | 옵션 | 설명 | 기본값 |
|---|---|---|---|
| `KORAIL_ID` / `KORAIL_PW` | | 코레일 아이디(휴대폰번호·이메일·회원번호) / 비밀번호 | (필수) |
| `TRAIN_DATE` | `--date` | 출발 날짜 `YYYYMMDD` | (필수) |
| `TRAIN_FROM` / `TRAIN_TO` | `--from` / `--to` | 출발역 / 도착역 (`역` 제외, 예: 서울, 동대구) | (필수) |
| `TRAIN_TIME` | `--time` | 이 시각 이후 출발 `HHMM` | `0000` |
| `TRAIN_UNTIL` | `--until` | 이 시각 **전에** 출발하는 열차만 `HHMM` | `2400`(제한 없음) |
| `TRAIN_TYPE` | `--type` | `KTX` 또는 `ALL` | `KTX` |
| `TRAIN_SEAT` | `--seat` | `GENERAL_FIRST` `GENERAL_ONLY` `SPECIAL_FIRST` `SPECIAL_ONLY` | `GENERAL_FIRST` |
| `TRAIN_PASSENGERS` | `--passengers` | 어른 인원 1~9 | `1` |
| `TRAIN_SEAT_STRATEGY` | `--strategy` | `consecutive`(붙어 앉기) / `random`(따로라도 1석씩) | `consecutive` |
| `SEARCH_INTERVAL` | | 조회 간격(초). **3초 미만 금지** | `3` |
| `PAYMENT_REMINDER_MINUTES` | | 결제 독촉 알림 간격(분) | `3` |
| `PAYMENT_CHECK_SECONDS` | | 결제 여부 확인 간격(초) | `60` |

명령행 예시:

```bash
pipenv run python src/macro.py --date 20261101 --from 서울 --to 부산 --time 0800 --until 1200
```

## 3. 받게 되는 알림

- 🔍 검색 시작 (조건 요약)
- ⚠️ 같은 열차에 이미 내 예약이 있음 (취소될 때까지 계속 검색)
- 🎉 예약 성공: 열차, 금액, **결제 기한**
- ⏰ 결제 대기 독촉 (`PAYMENT_REMINDER_MINUTES` 마다)
- ✅ 결제 완료 확인 / ⌛ 기한 만료로 취소
- 🚫 코레일 이용 제한(IP 차단) · ❌ 로그인 실패 등으로 중단

결제는 **코레일톡 앱이나 홈페이지에서 직접** 합니다. 결제가 끝나거나 기한이 지나면 프로그램이 종료됩니다.

## 4. PC를 꺼도 돌리려면

검색은 이 프로그램이 도는 기기에서 일어납니다. 그래서 **24시간 켜둘 기기**가 필요합니다.
코레일은 AWS 같은 클라우드·VPN IP를 차단(`-8202`)하므로 **집 인터넷에 연결된 기기**여야 합니다.
라즈베리파이, 시놀로지 같은 NAS, 안 쓰는 노트북이나 미니PC가 적당합니다.

Docker가 되는 기기라면:

```bash
cp .env.example .env && vi .env        # 설정 입력
mkdir -p data
docker compose build
docker compose run --rm --entrypoint python macro /app/src/kakao_auth.py   # 카카오 토큰 (처음 한 번)
docker compose up -d                   # 백그라운드 실행
docker compose logs -f                 # 로그 보기
```

- 컨테이너는 한 번의 예매가 끝나면(성공·차단·로그인 실패 등) 종료되고 **자동 재시작하지 않습니다.**
  재시작하면 표를 하나 더 예약하거나 로그인 시도를 반복할 수 있기 때문입니다. 다음 예매는 `.env`를 고치고 `docker compose up -d` 를 다시 실행하세요.
- 기기가 재부팅되면 `docker compose up -d` 를 다시 실행해야 합니다.

## 주의사항

1. 귀경길 기차 예매를 하지 못한 안타까운 영혼들을 위해 만든 프로그램입니다. 상업적 목적의 이용을 엄중히 금합니다.
2. 기본값인 3초에 1번(`SEARCH_INTERVAL=3`)보다 빠르게 조회하지 마십시오. 짧은 조회 간격은 매크로 판정과 IP·계정 차단의 주된 원인입니다.
3. 한 번 차단된 IP는 코드를 고쳐도 풀리지 않으며, 공인 IP가 바뀌어야 합니다.
4. 코레일 API 클라이언트로 [devgyurak/pykorail](https://github.com/devgyurak/pykorail)을 사용합니다 (코레일+ 7.0.8 규격). 코레일 쪽 변경으로 언제든 동작하지 않을 수 있습니다.
5. DynaPath 기기 식별자는 `KORAIL_DEVICE_FILE`(기본 `~/.korail_bot_device.json`)에 저장되어 매 실행마다 같은 기기로 보이게 합니다.

## 개발

```bash
make test     # 단위 테스트 (네트워크 불필요)
```

- `src/macro.py`: 실행 진입점. 설정 검증 → 검색·예약 → 알림 → 결제 추적
- `src/kakao_auth.py`: 카카오 토큰 최초 발급과 테스트 메시지
- `src/services/korail_service.py`: pykorail 래퍼 (로그인, 재로그인, 조회, 예약, 결제 상태)
- `src/services/kakao_service.py`: 카카오 '나에게 보내기'와 토큰 자동 갱신
