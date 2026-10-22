# 경북학숙 급식 디스코드 봇

kydel.or.kr 주간메뉴에서 식단을 읽어 디스코드로 알려주는 봇입니다. 전부 무료.

## 기능
| 사용법 | 결과 |
|---|---|
| `/급식` | 오늘 세 끼 전체 |
| `/급식 날짜:내일 끼니:아침` | 내일 아침만 |
| `/아침` `/점심` `/저녁` (날짜 선택) | 해당 끼니만 |
| `@급식봇 내일 저녁`, `@급식봇 10월 25일 아침 점심` | 자연어 (DM에선 멘션 없이) |
| 메시지 아래 버튼 | 오늘 아침 / 점심 / 저녁 / 내일 급식 (누른 사람에게만 보임) |
| `/알림설정` | 지금 채널에 매일 06:30 아침 · 11:30 점심 · 17:30 저녁 자동 전송 (서버 관리 권한) |
| `/알림해제` | 채널 알림 끄기 |
| `/알림구독` / `/알림구독해제` | 나에게 DM으로 자동 알림 켜기/끄기 |

날짜 표현: 그제, 어제, 오늘, 내일, 모레, `M월 D일` / 끼니: 아침·조식, 점심·중식, 저녁·석식

## 1. 디스코드 봇 만들기
1. https://discord.com/developers/applications → **New Application** (이름: 급식봇)
2. 왼쪽 **Bot** → **Reset Token** → 토큰 복사 (= `DISCORD_TOKEN`, 절대 공유 금지)
   - Privileged Gateway Intents 는 **모두 꺼둬도 됩니다**
3. 왼쪽 **OAuth2 → URL Generator**
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Read Message History`
   - 아래 생성된 URL을 열어 내 서버에 초대

## 2. 실행해 보기 (내 PC)
```bash
pip install -r requirements.txt
# Windows:  set DISCORD_TOKEN=토큰     /  Mac·Linux: export DISCORD_TOKEN=토큰
python bot.py
```
`[bot] 로그인: 급식봇#1234` 가 뜨면 성공. 슬래시 명령은 처음 반영에 몇 분 걸릴 수 있어요.

## 3. 24시간 무료로 켜두기 (택1)
봇은 계속 켜져 있어야 응답하고 알림도 보냅니다.

**A. 집 PC / 노트북 / 라즈베리파이** — 가장 간단. 꺼지면 봇도 멈춤.

**B. Oracle Cloud 무료 VM (추천)** — 평생 무료 서버, 한국(서울·춘천) 리전 선택 가능.
가입 시 카드 인증만 하고 과금은 없음(Always Free 자원만 사용할 때).
```bash
sudo apt update && sudo apt install -y python3-pip unzip
pip3 install -r requirements.txt
# 계속 실행: 아래처럼 서비스 등록
sudo tee /etc/systemd/system/mealbot.service <<'EOT'
[Unit]
After=network-online.target
[Service]
WorkingDirectory=/home/ubuntu/meal-discord-bot
Environment=DISCORD_TOKEN=여기에토큰
ExecStart=/usr/bin/python3 bot.py
Restart=always
[Install]
WantedBy=multi-user.target
EOT
sudo systemctl enable --now mealbot
journalctl -u mealbot -f   # 로그 보기
```

**C. Render 무료 웹서비스** — 가입만 하면 됨. 대신 외부 핑 필요.
- New → Web Service → GitHub 저장소 선택
- Build: `pip install -r requirements.txt`, Start: `python bot.py`
- 환경변수: `DISCORD_TOKEN`, 그리고 알림 채널 ID `NOTIFY_CHANNEL_IDS` (아래 참고)
- cron-job.org 로 `https://이름.onrender.com/` 을 10분마다 호출 (안 하면 15분 뒤 잠듦)
- Render 는 재시작·재배포 때 파일이 지워집니다. `/알림설정`·`/알림구독` 기록을 지키려면
  **설정 백업 채널**을 하나 만드세요.
  1. 내 서버에 비공개 텍스트 채널 생성 (예: `#급식봇-설정`), 봇에게 메시지 보내기·파일 첨부·기록 보기 권한
  2. 채널 ID 복사 (디스코드 설정 → 고급 → 개발자 모드 켜기 → 채널 우클릭 → ID 복사)
  3. Render 환경변수 `STORAGE_CHANNEL_ID` = 그 ID
  → 누가 알림을 켜고 끌 때마다 그 채널에 설정 파일이 올라가고, 재시작하면 거기서 복원합니다.
- `NOTIFY_CHANNEL_IDS` 는 선택입니다. 비워 두면 각 서버에서 `/알림설정` 으로 자유롭게 추가됩니다.
  (항상 알림 받을 채널을 코드 밖에서 고정하고 싶을 때만 쉼표로 채널 ID 입력)

## 동작 원리
- 식단 글은 하루 1개, 제목 "10월 22일", 본문은 빈 줄로 나뉜 조식/중식/석식.
- 글 번호(wr_id)가 날짜순으로 1씩 증가 → 기준 글(4616 = 2026-10-22)에서 날짜 차이만큼 이동해
  글을 열고 제목 날짜로 검증·보정. 찾은 글은 기준점(`meal_state.json`)과 1시간 캐시에 저장.
- 오늘·내일 식단은 30분마다 미리 받아 둠.
- 그날 글이 없거나 해당 끼니를 못 찾으면 자동 알림은 건너뜀.

## 사이트 구조가 바뀌면
- 기준점이 크게 어긋나면 `meal.py`의 `DEFAULT_ANCHOR`를 최신 글 번호/날짜로 바꾸고 `meal_state.json` 삭제.
- 본문을 못 읽으면 `meal.py`의 `_content_lines()` 선택자를 실제 HTML에 맞게 수정.
