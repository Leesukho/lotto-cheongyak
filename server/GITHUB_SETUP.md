# GitHub로 서버 운영하기 (지인 배포 단계)

GitHub Actions가 10분마다 공고를 수집해 `gh-pages` 브랜치에 JSON으로 게시하고,
새 공고를 FCM 토픽으로 보낸다. 앱(APK)은 토픽을 구독해 내 조건에 맞을 때만 직접 알림을 띄운다.
비용은 없다 (공개 저장소 Actions 무료, FCM 무료).

```
.github/workflows/collect.yml  →  python -m server.github_run
   ├─ site/notices.json, status.json ... → gh-pages 브랜치 → https://<아이디>.github.io/<저장소>/notices.json
   └─ FCM 토픽 '{시도}_{유형}' (예: gyeonggi_muns) → 앱
```

## 1. 저장소 만들기
1. https://github.com 가입 (계정이 없다면)
2. 오른쪽 위 **+ → New repository**
   - 이름: 예) `lotto-cheongyak`
   - **Public** (비공개는 Actions 무료 시간이 모자람)
   - README·.gitignore 추가하지 않음 (빈 저장소)
3. 이메일 비공개 설정: **Settings → Emails → Keep my email addresses private** 체크.
   아래에 보이는 `숫자+아이디@users.noreply.github.com` 주소를 커밋 작성자 이메일로 쓴다.

## 2. 코드 올리기
프로젝트 폴더에서 (처음 push 때 브라우저 로그인 창이 뜬다):
```
git config user.name "GitHub 아이디"
git config user.email "숫자+아이디@users.noreply.github.com"
git add -A
git commit -m "로또청약 알리미: GitHub Actions 수집 서버"
git remote add origin https://github.com/아이디/lotto-cheongyak.git
git push -u origin main
```
`.env`(키), `config.json`(내 조건), `release/`(exe)는 `.gitignore`로 빠져 있어 올라가지 않는다.

## 3. 비밀 값 등록
저장소 **Settings → Secrets and variables → Actions → New repository secret**

| 이름 | 값 | 필수 |
|---|---|---|
| `DATA_GO_KR_KEY` | 공공데이터포털 인증키 (`.env`와 같은 값) | 필수 |
| `ADMIN_TELEGRAM_BOT_TOKEN` | 수집 실패 알림을 받을 봇 토큰 (지금 쓰는 봇 가능) | 선택 |
| `ADMIN_TELEGRAM_CHAT_ID` | 그 알림을 받을 내 chat id | 선택 |
| `FCM_SERVICE_ACCOUNT` | Firebase 서비스 계정 JSON 파일 내용 전체 (5번) | 앱 만들 때 |

`FCM_SERVICE_ACCOUNT`가 없으면 푸시는 보내지 않고 로그에만 남긴다 (수집·게시는 정상 동작).

## 4. 첫 실행과 확인
1. 저장소 **Actions** 탭 → (안내가 나오면) 워크플로 사용 허용
2. 왼쪽 **collect** → **Run workflow**
3. 실행 기록에서 `수집·발송` 단계 로그 확인
   - `청약홈 첫 기록: 100건`, `마이홈 첫 기록: n건` → 정상
   - `HTTP Error 403`, 연결 시간 초과 등 → 공공데이터 API가 해외(GitHub 서버) 접속을 막는 경우. 알려 주면 대응한다
4. **Settings → Pages → Build and deployment → Source: Deploy from a branch → `gh-pages` / `(root)` → Save**
5. 몇 분 뒤 `https://아이디.github.io/lotto-cheongyak/status.json` 이 열리면 완료

이후로는 10분마다 자동 실행된다 (GitHub 사정으로 몇 분씩 늦을 수 있음).
첫 실행은 기존 공고를 기록만 하고 푸시는 보내지 않는다.

## 5. Firebase (앱 푸시용, 무료 Spark 요금제)
앱을 만들 때 진행한다.
1. https://console.firebase.google.com → 프로젝트 만들기 (요금제 변경 불필요)
2. 프로젝트 설정 → **서비스 계정** → **새 비공개 키 생성** → JSON 파일 다운로드
3. 파일 내용 전체를 `FCM_SERVICE_ACCOUNT` 비밀 값에 붙여 넣기
4. **다운로드한 JSON 파일은 지운다** (이 키로 누구나 내 앱에 푸시를 보낼 수 있음)

## 게시 파일
| 파일 | 내용 |
|---|---|
| `notices.json` | 최근 공고 (접수 마감 후 30일까지). 앱 목록·상세가 읽음 |
| `status.json` | 마지막 실행 시각과 소스별 결과 (변화가 없으면 1시간마다 갱신) |
| `topics.json` | 토픽 이름 표 (`{sido}_{category}`) |
| `seen.json`, `state.json` | 서버 내부 기록 (같은 공고 중복 발송 방지, 연속 실패 수) |

## 로컬 시험
```
python -m server.github_run --site %TEMP%\site --cache %TEMP%\cache.db
```
FCM 키가 없으면 보낼 토픽과 공고를 로그로만 보여 준다.
