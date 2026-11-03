"""공고 수집·매칭·푸시 작업 (Cloud Run Job이 10분마다 1회 실행).

  python -m server.collector                       운영: Firestore + FCM
  python -m server.collector --local               로컬 시험: server/local.db + 발송 대신 로그
  python -m server.collector --local --add-pc-user me     PC 프로그램 설정을 시험 사용자로 등록
  python -m server.collector --local --forget 5    최근 공고 5건을 잊게 해 '새 공고' 흐름 시험

환경 변수: DATA_GO_KR_KEY, ADMIN_TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_CHAT_ID, GOOGLE_CLOUD_PROJECT
시간은 한국 시간(컨테이너 TZ=Asia/Seoul) 기준이다.
"""
import argparse
import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import eligibility  # noqa: E402
import myhome  # noqa: E402
from price import PriceLookup  # noqa: E402
from server import match, normalize, push  # noqa: E402
from server.store import FirestoreStore, LocalStore  # noqa: E402

API_BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"
FAIL_ALERT_AFTER = 3
log = logging.getLogger("collector")


# ---------- 외부 API ----------

def http_json(url, data=None, retries=3):
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, data, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            if ex.code in (401, 403):  # 인증 오류는 다시 해도 같다. 원인 파악용으로 응답 본문 일부를 붙인다
                body = ex.read()[:300].decode("utf-8", "replace").replace("\n", " ")
                raise RuntimeError(f"HTTP {ex.code}: {body}") from None
            if i == retries - 1:
                raise
            time.sleep(5 * (i + 1))
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(5 * (i + 1))


def api_get(op, key, **params):
    qs = urllib.parse.urlencode({"page": 1, "perPage": 100, "serviceKey": key, **params})
    return http_json(f"{API_BASE}/{op}?{qs}").get("data", [])


def raw_hash(obj):
    return hashlib.sha1(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


# ---------- 수집 ----------
# 소스마다 처음 실행(seeding) 때는 기존 공고를 기록만 하고 알리지 않는다.

def collect_applyhome(store, key, now):
    raw = api_get("getRemndrLttotPblancDetail", key)
    if not raw:  # 자정 무렵 데이터 갱신 중에는 0건이 온다
        log.info("청약홈 응답 0건: 건너뜀")
        return {"count": 0, "new": [], "skipped": True}
    today = now.date().isoformat()
    seeding = store.get_state("seeded_applyhome") is None
    prices = PriceLookup(key, store)
    new = []
    for n in raw:
        nid = f"AH_{n['HOUSE_MANAGE_NO']}_{n['PBLANC_NO']}"
        h = raw_hash(n)
        old = store.get_notice(nid)
        if old and old.get("rawHash") == h:
            continue
        is_open = (normalize.ymd(n.get("SUBSCRPT_RCEPT_ENDDE")) or "") >= today
        models, market, pdf = [], {}, None
        if is_open or not seeding:  # 지난 공고를 처음 기록할 때는 주택형·시세 조회를 생략
            models = api_get("getRemndrLttotPblancMdl", key, **{"cond[HOUSE_MANAGE_NO::EQ]": n["HOUSE_MANAGE_NO"]})
            models = [m for m in models if m.get("PBLANC_NO") == n["PBLANC_NO"]] or models
            areas = [a for a in map(normalize.area_of, models) if a]
            try:
                market = prices.market(n.get("HSSPLY_ADRES", ""), n.get("HOUSE_NM", ""), areas) if areas else {}
            except Exception:
                log.exception("시세 조회 실패: %s", n.get("HOUSE_NM"))
            pdf = eligibility.pdf_link(n)
        doc = normalize.applyhome(n, models, market, pdf)
        doc.update(rawHash=h, firstSeen=(old or {}).get("firstSeen") or now.isoformat(timespec="minutes"))
        store.put_notice(doc)
        if not old and not seeding:
            new.append(doc)
            log.info("새 공고: %s %s", nid, doc["title"])
    if seeding:
        store.set_state("seeded_applyhome", today)
        log.info("청약홈 첫 기록: %d건", len(raw))
    return {"count": len(raw), "new": new}


def collect_myhome(store, key, now):
    rows = myhome.fetch(key, http_json)
    groups = myhome.group(rows)
    seeding = store.get_state("seeded_myhome") is None
    new = []
    for nid, items in groups.items():
        doc = normalize.myhome_notice(nid, items)
        old = store.get_notice(nid)
        if old and old.get("hash") == doc["hash"]:
            continue
        doc["firstSeen"] = (old or {}).get("firstSeen") or now.isoformat(timespec="minutes")
        store.put_notice(doc)
        if not old and not seeding:
            new.append(doc)
            log.info("새 공고: %s %s", nid, doc["title"])
    if seeding:
        store.set_state("seeded_myhome", now.date().isoformat())
        log.info("마이홈 첫 기록: %d건", len(groups))
    return {"count": len(groups), "new": new}


SOURCES = (("applyhome", collect_applyhome), ("myhome", collect_myhome))


# ---------- 매칭·발송 ----------

def deliver(store, sender, users, docs, now):
    """새 공고를 사용자 조건과 맞춰 알림함에 넣고, 조용한 시간이 아니면 바로 보낸다."""
    ready = {}
    for doc in docs:
        for uid, user in users:
            prefs = user.get("prefs") or {}
            ok, units = match.prefs_match(prefs, doc)
            if not ok or store.inbox_exists(uid, doc["id"]):
                continue
            grade, checks = match.check(user.get("profile"), doc, units)
            if grade == "no" and prefs.get("hide_ineligible"):
                continue
            payload = push.notice_payload(doc, units, grade)
            due = push.quiet_until(prefs, payload["channel"], now)
            store.put_inbox(uid, doc["id"], {
                "noticeId": doc["id"], "title": doc["title"], "category": doc["category"],
                "source": doc["source"], "channel": payload["channel"], "grade": grade, "checks": checks,
                "units": [u.get("name") for u in units], "payload": payload, "read": False,
                "createdAt": now.isoformat(timespec="minutes"),
                "sentAt": None if due else now.isoformat(timespec="minutes"),
                "queuedUntil": due.isoformat(timespec="minutes") if due else None,
            })
            if due:
                store.queue(uid, doc["id"], due)
            else:
                ready.setdefault(uid, []).append((doc, payload))
    send(store, sender, ready)
    return sum(len(v) for v in ready.values())


def send(store, sender, ready):
    """ready: {uid: [(문서, payload)]}. 사용자별로 GROUP_AT건 이상이면 묶어서 1개만 보낸다."""
    for uid, items in ready.items():
        tokens = store.devices(uid)
        payloads = [push.group_payload([d for d, _ in items])] if len(items) >= push.GROUP_AT else [p for _, p in items]
        for p in payloads:
            for token in sender.send(uid, tokens, p):
                store.remove_device(uid, token)
                tokens.remove(token)


def flush_outbox(store, sender, now):
    """조용한 시간이 끝난 대기 알림을 보낸다."""
    ready = {}
    for uid, nid in store.due_outbox(now):
        item = store.get_inbox(uid, nid)
        store.unqueue(uid, nid)
        if not item or item.get("sentAt"):
            continue
        item["sentAt"] = now.isoformat(timespec="minutes")
        store.put_inbox(uid, nid, item)
        ready.setdefault(uid, []).append(({"id": nid, "title": item["title"], "source": item["source"]},
                                          item["payload"]))
    send(store, sender, ready)
    return sum(len(v) for v in ready.values())


def weekly_digest(store, sender, users, now):
    """월요일 8시 이후 첫 실행 때, 이번 주 접수가 시작되는 내 조건의 공고를 요약해 보낸다."""
    today = now.date()
    if today.weekday() != 0 or now.hour < 8 or store.get_state("last_digest") == today.isoformat():
        return 0
    store.set_state("last_digest", today.isoformat())
    week_end = (today + timedelta(days=6)).isoformat()
    docs = [d for d in store.open_notices(today.isoformat())
            if today.isoformat() <= (d["dates"].get("applyStart") or "") <= week_end]
    sent = 0
    for uid, user in users:
        prefs = user.get("prefs") or {}
        if prefs.get("weekly_digest") is False:
            continue
        items = sorted((d["dates"]["applyStart"], d["title"]) for d in docs if match.prefs_match(prefs, d)[0])
        if items:
            for token in sender.send(uid, store.devices(uid), push.digest_payload(items)):
                store.remove_device(uid, token)
            sent += 1
    return sent


# ---------- 실행 ----------

def collect_all(store, env, now):
    """모든 소스 수집 → (새 공고 목록, 소스별 상태). 한 소스가 실패해도 다른 소스는 계속."""
    # 붙여 넣을 때 섞인 공백·줄바꿈, 인코딩된 키(%2B 등)를 모두 원래 키로 맞춘다
    key = urllib.parse.unquote(env["DATA_GO_KR_KEY"].strip())
    new, status = [], {}
    for name, collect in SOURCES:
        try:
            result = collect(store, key, now)
            new += result["new"]
            status[name] = {"ok": True, "count": result["count"], "new": len(result["new"]),
                            "skipped": result.get("skipped", False)}
            store.set_state(f"fail_{name}", 0)
        except Exception as ex:
            log.exception("%s 수집 실패", name)
            fails = int(store.get_state(f"fail_{name}", 0)) + 1
            store.set_state(f"fail_{name}", fails)
            status[name] = {"ok": False, "error": str(ex)[:200], "fails": fails}
            if fails == FAIL_ALERT_AFTER:
                push.admin_alert(env, f"⚠️ 로또청약 서버: {name} {fails}회 연속 실패\n{str(ex)[:300]}")
    return new, status


def run(store, sender, env, now=None):
    """Firebase 서버용: 수집 + 사용자별 매칭·발송"""
    now = now or datetime.now()
    new, status = collect_all(store, env, now)
    users = store.list_users()
    pushed = deliver(store, sender, users, new, now)
    flushed = flush_outbox(store, sender, now)
    digests = weekly_digest(store, sender, users, now)
    summary = {"lastRun": now.isoformat(timespec="seconds"), "sources": status, "newNotices": len(new),
               "users": len(users), "pushed": pushed, "flushed": flushed, "digests": digests}
    store.set_state("collector", summary)
    log.info("실행 완료: %s", json.dumps(summary, ensure_ascii=False))
    return summary


# ---------- 명령줄 ----------

def load_env():
    env = dict(os.environ)
    path = ROOT / ".env"  # 로컬 시험 때만 있음 (운영은 Secret Manager → 환경 변수)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


def pc_user():
    """PC 프로그램 config.json → {profile, prefs}"""
    for path in (Path(os.environ.get("LOCALAPPDATA", "")) / "LottoCheongyak" / "config.json", ROOT / "config.json"):
        if path.exists():
            cfg = json.loads(path.read_text(encoding="utf-8"))
            prefs = {k: v for k, v in cfg.items() if k not in ("my_profile",) and not k.startswith("_")}
            return {"profile": cfg.get("my_profile") or {}, "prefs": prefs, "source": str(path)}
    raise SystemExit("PC 프로그램 설정(config.json)을 찾지 못했습니다.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", nargs="?", const=str(ROOT / "server" / "local.db"), help="로컬 sqlite 경로")
    ap.add_argument("--fcm", action="store_true", help="로컬에서도 실제 FCM 발송")
    ap.add_argument("--add-pc-user", metavar="UID", help="PC 설정을 사용자로 등록하고 끝냄")
    ap.add_argument("--forget", type=int, metavar="N", help="(로컬 시험) 접수 중인 최근 공고 N건을 지움")
    ap.add_argument("--now", help="(시험) 현재 시각 'YYYY-MM-DD HH:MM'")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    env = load_env()
    store = LocalStore(args.local) if args.local else FirestoreStore(env.get("GOOGLE_CLOUD_PROJECT"))
    if args.add_pc_user:
        user = pc_user()
        store.put_user(args.add_pc_user, {"profile": user["profile"], "prefs": user["prefs"]})
        store.put_device(args.add_pc_user, f"local-{args.add_pc_user}", {"platform": "console"})
        print(f"등록: {args.add_pc_user} ← {user['source']}")
        return
    if args.forget:
        if not args.local:
            raise SystemExit("--forget은 로컬 시험에서만 쓸 수 있습니다.")
        docs = sorted(store.open_notices(datetime.now().date().isoformat()),
                      key=lambda d: d.get("dates", {}).get("announce") or "", reverse=True)[:args.forget]
        for d in docs:
            store.delete_notice(d["id"])
            print("잊음:", d["id"], d["title"])
        return
    sender = push.FcmSender() if (args.fcm or not args.local) else push.ConsoleSender()
    now = datetime.strptime(args.now, "%Y-%m-%d %H:%M") if args.now else None
    run(store, sender, env, now)


if __name__ == "__main__":
    main()
