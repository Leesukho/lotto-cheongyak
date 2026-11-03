# 로또청약 알리미

청약홈 무순위·계약취소 재공급 공고와 마이홈포털 공공주택(행복주택·국민임대·공공분양 등) 공고를
모아서 내 조건에 맞는 것만 알려 주는 프로젝트.

| 구성 | 위치 | 상태 |
|---|---|---|
| Windows 프로그램 (트레이 아이콘 + 텔레그램 알림) | `gui.py`, `main.py`, `build.py` | 사용 중 (v1.3) |
| 수집 서버 (GitHub Actions → JSON 게시 + FCM 토픽) | `server/github_run.py`, `.github/workflows/collect.yml` | [설정 방법](server/GITHUB_SETUP.md) |
| 수집 서버 (Firebase Cloud Run, 사용자 DB 방식) | `server/collector.py` | 보류 (사용자가 늘면) |
| 안드로이드 앱 (Flutter) | 예정 | 설계: [docs/모바일앱_설계.md](docs/모바일앱_설계.md) |

공공 데이터: 공공데이터포털(청약홈 분양정보, 마이홈포털 공공주택 모집공고, 국토교통부 실거래가).
시세·자격 판정은 참고용 추정치이며 최종 판단은 모집공고문 기준이다.
