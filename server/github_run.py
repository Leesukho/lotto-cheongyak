"""GitHub Actions용 실행: 수집 → site/ 에 JSON 게시 → 새 공고를 FCM 토픽으로 발송.

사용자 DB가 없다. 내 조건은 앱(휴대폰)에만 있고, 앱은 지역×유형 토픽을 구독한 뒤
차익·자격 같은 세부 조건은 받은 데이터로 직접 걸러 알림을 띄운다.

  python -m server.github_run --site site --cache .cache/cache.db

site/ (gh-pages 브랜치, GitHub Pages로 공개)
  notices.json   최근 공고 전체 (앱 목록·상세)
  seen.json      지금까지 본 공고 ID와 변경 감지값 (같은 공고를 다시 알리지 않기 위함)
  state.json     수집 상태 (처음 기록 여부, 연속 실패 수)
  status.json    마지막 실행 결과 (앱의 '서버 상태' 표시용)
  topics.json    토픽 이름 표

환경 변수: DATA_GO_KR_KEY, FCM_SERVICE_ACCOUNT(서비스 계정 JSON 내용), ADMIN_TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_CHAT_ID
"""
import argparse
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from server import collector, push, topics

log = logging.getLogger("collector")
KEEP_CLOSED_DAYS = 30      # 접수가 끝난 공고를 notices.json에 남겨 둘 기간
STATUS_EVERY_MIN = 60      # 데이터 변화가 없어도 status.json을 갱신하는 주기 (커밋 수 줄이기)
FCM_MAX_BYTES = 3800       # FCM 데이터 메시지 4KB 제한


class GitHubStore:
    """collector의 수집 함수가 쓰는 메서드만 구현한 파일 저장소"""

    def __init__(self, site, cache_path):
        self.site = Path(site)
        self.site.mkdir(parents=True, exist_ok=True)
        self.notices = {d["id"]: d for d in self._load("notices.json", [])}
        self.seen = self._load("seen.json", {})
        self.state = self._load("state.json", {})
        self.status = self._load("status.json", {})
        self.changed = False
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        self.cache = sqlite3.connect(cache_path)
        self.cache.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT, at TEXT)")

    def _load(self, name, default):
        path = self.site / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def _write(self, name, data):
        (self.site / name).write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

    # 공고: notices.json에서 지운 오래된 공고도 seen.json의 변경 감지값으로 '이미 봄'을 판단한다
    def get_notice(self, nid):
        return self.notices.get(nid) or self.seen.get(nid)

    def put_notice(self, doc):
        self.notices[doc["id"]] = doc
        self.seen[doc["id"]] = {"rawHash": doc.get("rawHash"), "hash": doc.get("hash"), "firstSeen": doc.get("firstSeen")}
        self.changed = True

    def get_state(self, k, default=None):
        return self.state.get(k, default)

    def set_state(self, k, v):
        if self.state.get(k) != v:
            self.state[k] = v
            self.changed = True

    # price.PriceLookup 캐시 (Actions 캐시로 실행 간 유지, 사라져도 다시 조회할 뿐)
    def get(self, k):
        row = self.cache.execute("SELECT v, at FROM cache WHERE k=?", (k,)).fetchone()
        return (json.loads(row[0]), datetime.fromisoformat(row[1])) if row else None

    def set(self, k, v):
        self.cache.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                           (k, json.dumps(v, ensure_ascii=False), datetime.now().isoformat()))
        self.cache.commit()

    def save(self, now, status):
        """바뀐 것이 있거나 상태 갱신 주기가 지났을 때만 파일을 쓴다 (git 커밋을 줄이기 위해)."""
        cutoff = (now - timedelta(days=KEEP_CLOSED_DAYS)).date().isoformat()
        before = len(self.notices)
        self.notices = {k: d for k, d in self.notices.items() if (d["dates"].get("applyEnd") or "9999") >= cutoff}
        self.changed |= len(self.notices) != before
        last = self.status.get("lastPublished")
        stale = not last or now - datetime.fromisoformat(last) >= timedelta(minutes=STATUS_EVERY_MIN)
        if self.changed:
            docs = sorted(self.notices.values(), key=lambda d: (d["dates"].get("announce") or ""), reverse=True)
            self._write("notices.json", docs)
            self._write("seen.json", self.seen)
            self._write("state.json", self.state)
            self._write("topics.json", topics.table())
        if self.changed or stale:
            self._write("status.json", {**status, "lastPublished": now.isoformat(timespec="seconds"),
                                        "noticeCount": len(self.notices)})
        return self.changed or stale


# ---------- FCM 토픽 발송 ----------

def topic_data(doc):
    """앱이 받아서 직접 조건을 걸러 알림을 띄울 수 있게 필요한 값만 담는다. 값은 모두 문자열."""
    d = doc["dates"]
    if doc["source"] == "applyhome":
        units = [[u.get("area_m2"), u.get("price_manwon"), u.get("market_manwon"), u.get("gap_manwon"),
                  u.get("count_general"), u.get("count_special")] for u in doc["units"]]
    else:
        units = [[u.get("count"), u.get("deposit_won"), u.get("rent_won"), u.get("sido"), u.get("sidoFull")]
                 for u in doc["units"]]
    data = {"v": "1", "id": doc["id"], "src": doc["source"], "cat": doc["category"], "title": doc["title"],
            "sido": doc.get("sido") or "", "addr": doc.get("address") or "",
            "start": d.get("applyStart") or "", "end": d.get("applyEnd") or "",
            "maxGap": "" if doc.get("maxGapManwon") is None else str(doc["maxGapManwon"]),
            "units": json.dumps(units, ensure_ascii=False, separators=(",", ":"))}
    while len(json.dumps(data, ensure_ascii=False).encode()) > FCM_MAX_BYTES and units:
        units = units[:-1]  # 단지가 아주 많은 공고는 앞쪽만 (전체는 앱이 notices.json에서 읽음)
        data["units"] = json.dumps(units, ensure_ascii=False, separators=(",", ":"))
        data["more"] = "1"
    return data


class ConsoleTopicSender:
    def __init__(self):
        self.sent = []

    def send(self, topic, data):
        self.sent.append((topic, data))
        log.info("[토픽] %s ← %s", topic, data["title"])


class FcmTopicSender:
    def __init__(self, service_account_json):
        import firebase_admin
        from firebase_admin import credentials, messaging
        cred = credentials.Certificate(json.loads(service_account_json))
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        self.m = messaging

    def send(self, topic, data):
        # 데이터 메시지(알림 표시 없음): 앱이 받아서 내 조건으로 거른 뒤 직접 알림을 띄운다
        self.m.send(self.m.Message(topic=topic, data=data, android=self.m.AndroidConfig(
            priority="high", ttl=timedelta(days=2))))
        log.info("[토픽] %s ← %s", topic, data["title"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="site")
    ap.add_argument("--cache", default=".cache/cache.db")
    ap.add_argument("--now", help="(시험) 'YYYY-MM-DD HH:MM'")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    env = collector.load_env()
    now = datetime.strptime(args.now, "%Y-%m-%d %H:%M") if args.now else datetime.now()
    store = GitHubStore(args.site, args.cache)
    sender = FcmTopicSender(env["FCM_SERVICE_ACCOUNT"]) if env.get("FCM_SERVICE_ACCOUNT") else ConsoleTopicSender()

    resend = (env.get("RESEND_ID") or "").strip()
    if resend:  # 푸시 시험: 이미 게시된 공고 하나를 그 토픽으로 다시 보낸다 (수집은 하지 않음)
        doc = store.notices.get(resend)
        if not doc:
            raise SystemExit(f"공고를 찾지 못했습니다: {resend} (notices.json의 id)")
        for topic in topics.topics_for(doc):
            sender.send(topic, topic_data(doc))
        log.info("재발송 완료: %s %s", resend, doc["title"])
        return

    new, status = collector.collect_all(store, env, now)
    sent = failed = 0
    for doc in new:
        data = topic_data(doc)
        for topic in topics.topics_for(doc):
            try:
                sender.send(topic, data)
                sent += 1
            except Exception:
                failed += 1
                log.exception("토픽 발송 실패: %s %s", topic, doc["id"])
    if failed:
        push.admin_alert(env, f"⚠️ 로또청약: FCM 토픽 발송 {failed}건 실패")
    summary = {"lastRun": now.isoformat(timespec="seconds"), "sources": status,
               "newNotices": len(new), "topicMessages": sent}
    published = store.save(now, summary)
    log.info("실행 완료 (게시 %s): %s", "함" if published else "안 함", json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
