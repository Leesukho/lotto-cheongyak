# 로또청약 알리미 서버

10분마다 청약홈·마이홈 공고를 수집하고 사용자 조건에 맞으면 FCM 푸시를 보낸다.
설계: [docs/모바일앱_설계.md](../docs/모바일앱_설계.md)

| 파일 | 역할 |
|---|---|
| `collector.py` | 실행 진입점: 수집 → 매칭 → 발송 → 대기 알림 발송 → 주간 요약 |
| `normalize.py` | 청약홈·마이홈 응답 → 공통 공고 문서 |
| `match.py` | 사용자 조건 매칭, 사용자별 자격 점검 (PC `eligibility.py` 재사용) |
| `push.py` | 알림 문구, 조용한 시간, FCM 발송, 운영자 텔레그램 |
| `store.py` | 저장소 (로컬 sqlite / Firestore) |
| `firestore.rules` | 앱이 Firestore에 접근할 때의 보안 규칙 |

## 로컬 시험 (Firebase 없이)

프로젝트 폴더에서 실행한다. `.env`의 `DATA_GO_KR_KEY`를 쓰고, 푸시는 보내지 않고 로그로만 남긴다.

```
python -m server.collector --local --add-pc-user me            # PC 설정을 시험 사용자로
python -m server.collector --local                             # 1회 실행 (처음은 기존 공고 기록만)
python -m server.collector --local --forget 5                  # 최근 공고 5건을 잊게 해서
python -m server.collector --local --now "2026-11-03 23:00"    # 새 공고 흐름·조용한 시간 시험
```
결과는 `server/local.db` (sqlite)에 쌓인다. 지우면 처음부터 다시 시작한다.

## 배포 (Google Cloud, 처음 1회)

아래 `PROJECT_ID`는 Firebase 프로젝트 ID, 지역은 서울(`asia-northeast3`)이다.
공공데이터 API가 해외 IP를 막는 경우가 있어 **반드시 서울 지역**을 쓴다.

### 1. Firebase 콘솔 (웹에서 직접)
1. https://console.firebase.google.com 에서 프로젝트 만들기
2. 요금제를 **Blaze(종량제)**로 변경, 결제 수단 등록 → Google Cloud 콘솔 '예산 및 알림'에서 월 예산 알림(예: 5,000원) 설정
3. Firestore 데이터베이스 만들기: 프로덕션 모드, 위치 `asia-northeast3 (서울)`
4. Authentication → 로그인 방법 → **익명** 사용 설정
5. Firestore → 규칙 탭에 `server/firestore.rules` 내용을 붙여 넣고 게시

### 2. gcloud CLI 설치와 로그인
https://cloud.google.com/sdk/docs/install 설치 후:
```
gcloud auth login
gcloud config set project PROJECT_ID
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com fcm.googleapis.com
```

### 3. 비밀 값 등록 (키는 채팅·파일에 남기지 말고 직접 입력)
```
gcloud secrets create DATA_GO_KR_KEY --replication-policy=automatic
gcloud secrets versions add DATA_GO_KR_KEY --data-file=-          # 키를 붙여 넣고 Enter, Ctrl+Z, Enter
gcloud secrets create ADMIN_TELEGRAM_BOT_TOKEN --replication-policy=automatic
gcloud secrets versions add ADMIN_TELEGRAM_BOT_TOKEN --data-file=-
gcloud secrets create ADMIN_TELEGRAM_CHAT_ID --replication-policy=automatic
gcloud secrets versions add ADMIN_TELEGRAM_CHAT_ID --data-file=-
```

### 4. 실행 계정과 권한
```
gcloud iam service-accounts create lotto-collector --display-name "로또청약 수집기"
SA=lotto-collector@PROJECT_ID.iam.gserviceaccount.com
gcloud projects add-iam-policy-binding PROJECT_ID --member serviceAccount:$SA --role roles/datastore.user
gcloud projects add-iam-policy-binding PROJECT_ID --member serviceAccount:$SA --role roles/secretmanager.secretAccessor
gcloud projects add-iam-policy-binding PROJECT_ID --member serviceAccount:$SA --role roles/firebasecloudmessaging.admin
gcloud projects add-iam-policy-binding PROJECT_ID --member serviceAccount:$SA --role roles/run.invoker
```
(PowerShell에서는 `SA=...` 대신 `$SA="..."`)

### 5. 작업 배포 (프로젝트 폴더에서)
```
gcloud run jobs deploy lotto-collector --source . --region asia-northeast3 \
  --service-account $SA --task-timeout 10m --max-retries 0 \
  --set-env-vars GOOGLE_CLOUD_PROJECT=PROJECT_ID \
  --set-secrets DATA_GO_KR_KEY=DATA_GO_KR_KEY:latest,ADMIN_TELEGRAM_BOT_TOKEN=ADMIN_TELEGRAM_BOT_TOKEN:latest,ADMIN_TELEGRAM_CHAT_ID=ADMIN_TELEGRAM_CHAT_ID:latest
gcloud run jobs execute lotto-collector --region asia-northeast3 --wait     # 1회 실행해 확인
```
처음 실행은 기존 공고를 기록만 한다 (푸시 없음). Firestore 콘솔에서 `notices`, `system/collector`를 확인한다.

### 6. 10분마다 실행
```
gcloud scheduler jobs create http lotto-collector-10m --location asia-northeast3 \
  --schedule "*/10 * * * *" --time-zone Asia/Seoul --http-method POST \
  --uri "https://asia-northeast3-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/PROJECT_ID/jobs/lotto-collector:run" \
  --oauth-service-account-email $SA
```

### 코드 수정 후 재배포
5번의 `gcloud run jobs deploy ...`만 다시 실행한다.

## 운영 확인
- `system/collector` 문서: 마지막 실행 시각, 소스별 성공 여부·공고 수, 보낸 푸시 수
- 한 소스가 3회 연속 실패하면 운영자 텔레그램으로 알림
- 로그: Cloud Run → 작업 → lotto-collector → 로그
