# discordbot.minbot

유튜브 음악 재생과 메이플스토리/FC 온라인/롤(LoL) 데이터 조회를 한 봇에 담은 Discord 봇입니다. 프리픽스(!)와 슬래시 명령을 모두 지원하며 패널 버튼으로 재생/스킵/반복/셔플을 제어할 수 있습니다. 포트폴리오용으로 주요 명령과 설정 방법을 정리했습니다.

## 핵심 기능
- 음악: 유튜브 링크/검색 → 대기열 재생, 패널 버튼(재생·스킵·정지·반복·셔플·대기열 표시)
- 메이플스토리: 기본 정보, 능력치, 장비/스킬, V/HEXA 코어, 경매 시세 등 다수 조회
- FC 온라인: 기본 정보, 최고 등급, 최근 경기/전적, 거래, 메타데이터, 선수 검색
- 롤(LoL): 소환사 최근 5경기 요약, 솔로/자유 랭크 정보, OP.GG 링크
- 제어 제한: 역할 제한(ALLOWED_ROLE), 대기열 제한(MAX_QUEUE, MAX_PER_USER), 쿨다운(CMD_COOLDOWN)
- 상태 저장: bot_state.json에 대기열/반복/셔플/패널 메시지를 저장해 재시작 후 이어서 사용

## 빠른 시작
1) Python 3.11+ 권장, 가상환경 생성 후 활성화
```
python -m venv .venv
.\.venv\Scripts\activate
```
2) 패키지 설치
```
pip install discord.py aiohttp yt-dlp
```
3) .env 작성 후 실행
```
python bot.py
```

### 환경 변수 예시 (.env)
```
DISCORD_TOKEN=your_discord_bot_token
NEXON_API_KEY=your_nexon_open_api_key
FIFA_API_KEY=your_fifa_fc_online_api_key
RIOT_API_KEY=your_riot_api_key
LOL_DEFAULT_REGION=kr
ALLOWED_ROLE=DJ
BOT_VOLUME_DB=-22
MAX_QUEUE=30
MAX_PER_USER=10
CMD_COOLDOWN=2.0
STATE_FILE=bot_state.json
DELETE_COMMANDS=true
```

## 명령어 모음
프리픽스 `!`와 동일한 슬래시 명령을 함께 제공합니다.

### 음악
| 명령 | 인자 | 설명 |
| --- | --- | --- |
| !join / /join | – | 내 음성 채널로 봇 호출 |
| !leave / /leave | – | 봇 퇴장, 대기열 초기화 |
| !play <url> / /play url | 유튜브 URL | 대기열 추가 후 재생 |
| !stop / /stop | – | 재생 중지 + 대기열 삭제 |
| !pause / /pause · !resume / /resume | – | 일시정지/재개 |
| !skip / /skip | – | 현재 트랙 스킵 |
| !queue / /queue | – | 대기열 출력 |
| !clear / /clear | – | 대기열 비우기 |
| !panel / /panel | – | 패널 생성/업데이트(버튼: 재생/스킵/정지/새로고침/대기열/반복/셔플) |
| !move <src> <dst> / /move src dst | 번호 | 대기열 순서 변경(1-based) |
| !remove <index> / /remove index | 번호 | 특정 트랙 제거 |
| !search <키워드> / /search query | 검색어 | 유튜브 검색 5개 표시 + 버튼 선택 |
| !choose <번호> / /choose index | 번호 | 최근 검색 결과에서 선택해 대기열 추가 |

### 메이플스토리 (NEXON_API_KEY 필요)
!msbasic, !msstat, !mspop, !msequip, !msskill, !mslink, !mspet, !msandroid, !msbeauty, !msvmatrix, !mshexa, !mshexastat, !msdojo, !msotherstat, !msauc

### FC 온라인 (FIFA_API_KEY 필요)
!fcbasic, !fcmax, !fcmatch, !fctrade, !fcmatchdetail, !fcplayer, !fcmeta

### 롤 (RIOT_API_KEY 필요)
!lol / !전적 / !롤전적 — 최근 5경기 요약 + 랭크 정보, 기본 지역은 LOL_DEFAULT_REGION(기본 kr), 소환사명 또는 `이름#태그` 지원

### 기타
!ping, !helpme, !미개, !매국

## 운영 팁
- 패널 버튼은 슬래시/프리픽스 모두 사용 가능하며, 슬래시 응답은 기본 ephemeral
- 상태 파일(STATE_FILE)을 볼륨 마운트하면 재시작 후에도 대기열과 반복/셔플 상태 유지
- API 호출 실패 시 응답 메시지에 원인/가이드가 포함됨
