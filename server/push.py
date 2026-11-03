"""푸시 알림 내용 만들기와 발송.

채널(안드로이드 Notification Channel ID, 앱과 같아야 함):
  lotto 🎯 로또 청약 · public 🏠 공공주택 · digest ☀️ 주간 요약
  (schedule 📅 일정 알림은 앱이 기기에서 직접 예약하므로 서버는 보내지 않는다)
"""
import logging
import urllib.parse
import urllib.request
from datetime import timedelta

log = logging.getLogger("collector")

GROUP_AT = 3  # 한 번에 이만큼 이상이면 묶음 알림 1개로 보낸다


def money(manwon):
    if manwon is None:
        return "-"
    eok, man = divmod(abs(int(manwon)), 10000)
    s = f"{eok}.{man // 1000}억" if eok and man >= 1000 else f"{eok}억" if eok else f"{man:,}만"
    return ("-" if manwon < 0 else "") + s


def won(v):
    if not v:
        return None
    return f"{v / 10000:.1f}".rstrip("0").rstrip(".") + "만" if v < 1_000_000 else f"{round(v / 10000):,}만"


def md(d):
    return f"{int(d[5:7])}/{int(d[8:10])}" if d else "-"


def channel(doc):
    return "lotto" if doc["source"] == "applyhome" else "public"


GRADE = {"ok": "✅ 기본 요건 충족", "warn": "⚠️ 공고문 확인 필요", "no": "❌ 신청 불가"}


def notice_payload(doc, units, grade):
    """공고 1건 알림 {title, body, data}"""
    d = doc["dates"]
    when = f"접수 {md(d.get('applyStart'))}~{md(d.get('applyEnd'))}"
    if doc["source"] == "applyhome":
        gaps = [u["gap_manwon"] for u in units if u.get("gap_manwon") is not None]
        prices = [u["price_manwon"] for u in units if u.get("price_manwon")]
        count = sum((u.get("count_general") or 0) + (u.get("count_special") or 0) for u in units)
        head = f"🎯 [{doc['category']}] {doc['title']}"
        if count:
            head += f" {count}세대"
        if gaps:
            head += f" · 차익 {'+' if max(gaps) >= 0 else ''}{money(max(gaps))}"
        body = [when]
        if prices:
            body.append(f"분양가 {money(min(prices))}" + ("~" if len(set(prices)) > 1 else ""))
        best = max((u for u in units if u.get("market_manwon")), key=lambda u: u.get("gap_manwon") or 0, default=None)
        if best:
            body.append(f"시세 {money(best['market_manwon'])}")
    else:
        count = sum(u.get("count") or 0 for u in units)
        head = f"🏠 [{doc['category']}] {doc['title']}"
        if count:
            head += f" · {count:,}호"
        body = [when]
        deposits = [u["deposit_won"] for u in units if u.get("deposit_won")]
        rents = [u["rent_won"] for u in units if u.get("rent_won")]
        if deposits:
            body.append(f"보증금 {won(min(deposits))}원~")
        if rents:
            body.append(f"월 {won(min(rents))}원~")
    if grade:
        body.append(GRADE[grade])
    return {"title": head[:120], "body": " · ".join(body)[:200],
            "data": {"noticeId": doc["id"], "channel": channel(doc)}, "channel": channel(doc)}


def group_payload(docs):
    lotto = sum(1 for d in docs if d["source"] == "applyhome")
    titles = ", ".join(d["title"] for d in docs[:3])
    return {"title": f"🔔 새 공고 {len(docs)}건" + (f" (로또 {lotto}건)" if lotto else ""),
            "body": titles + (" 외" if len(docs) > 3 else ""),
            "data": {"screen": "inbox"}, "channel": "lotto" if lotto else "public"}


def digest_payload(items):
    """items: [(접수시작, 제목)]"""
    body = ", ".join(f"{md(s)} {t}" for s, t in items[:3]) + (f" 외 {len(items) - 3}건" if len(items) > 3 else "")
    return {"title": f"☀️ 이번 주 접수 일정 {len(items)}건", "body": body[:200],
            "data": {"screen": "home"}, "channel": "digest"}


# ---------- 조용한 시간 ----------

def quiet_until(prefs, payload_channel, now):
    """지금이 조용한 시간이면 발송 시각(datetime), 아니면 None"""
    q = {"enabled": True, "start": "22:00", "end": "08:00", "lotto_bypass": True, **(prefs.get("quiet_hours") or {})}
    if not q["enabled"] or (payload_channel == "lotto" and q["lotto_bypass"]):
        return None
    sh, sm = map(int, q["start"].split(":"))
    eh, em = map(int, q["end"].split(":"))
    start, end = now.replace(hour=sh, minute=sm, second=0, microsecond=0), now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if start <= end:  # 같은 날 안의 구간 (예: 13:00~14:00)
        return end if start <= now < end else None
    if now >= start:
        return end + timedelta(days=1)
    return end if now < end else None


# ---------- 발송 ----------

class ConsoleSender:
    """로컬 시험용: 발송하지 않고 기록만 한다."""

    def __init__(self):
        self.sent = []

    def send(self, uid, tokens, payload):
        self.sent.append((uid, payload))
        log.info("[푸시] %s → %s | %s", uid, payload["title"], payload["body"])
        return []


class FcmSender:
    """운영용. firebase-admin 필요. 반환: 더 이상 유효하지 않은 토큰 목록"""

    def __init__(self):
        import firebase_admin
        from firebase_admin import messaging
        if not firebase_admin._apps:
            firebase_admin.initialize_app()
        self.m = messaging

    def send(self, uid, tokens, payload):
        if not tokens:
            return []
        msg = self.m.MulticastMessage(
            tokens=tokens,
            notification=self.m.Notification(title=payload["title"], body=payload["body"]),
            data={k: str(v) for k, v in payload["data"].items()},
            android=self.m.AndroidConfig(
                priority="high" if payload["channel"] == "lotto" else "normal",
                notification=self.m.AndroidNotification(channel_id=payload["channel"])),
            apns=self.m.APNSConfig(payload=self.m.APNSPayload(aps=self.m.Aps(sound="default"))),
        )
        res = self.m.send_each_for_multicast(msg)
        dead = []
        for token, r in zip(tokens, res.responses):
            if not r.success:
                code = getattr(r.exception, "code", "")
                log.warning("FCM 실패 %s: %s", uid, r.exception)
                if code in ("NOT_FOUND", "UNREGISTERED", "INVALID_ARGUMENT") or "registration" in str(r.exception).lower():
                    dead.append(token)
        return dead


def admin_alert(env, text):
    """운영자 텔레그램 (수집 실패 등). 설정이 없으면 로그만."""
    token, chat = env.get("ADMIN_TELEGRAM_BOT_TOKEN"), env.get("ADMIN_TELEGRAM_CHAT_ID")
    if not (token and chat):
        log.warning("[운영자 알림] %s", text)
        return
    body = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=20).read()
    except Exception:
        log.exception("운영자 알림 실패")
