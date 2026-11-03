"""청약홈 무순위/취소후재공급 신규 공고를 감지해 텔레그램으로 알린다.

작업 스케줄러가 주기적으로 실행한다. 한 번 실행 = 한 번 조회 후 종료.
  python main.py           신규 공고 확인
  python main.py --reset   기록을 지우고 처음 실행처럼 다시 시작
"""
import csv
import html
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

import eligibility
import myhome
from price import PriceLookup

# exe로 실행되면 사용자별 데이터(설정·기록)는 %LOCALAPPDATA%\LottoCheongyak 에 둔다
FROZEN = getattr(sys, "frozen", False)
ROOT = Path(os.environ["LOCALAPPDATA"]) / "LottoCheongyak" if FROZEN else Path(__file__).parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "seen.db"
CSV_PATH = DATA / "alerts.csv"
PUBLIC_CSV_PATH = DATA / "public_alerts.csv"
API_BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"
FAIL_ALERT_AFTER = 3  # 연속 실패가 이 횟수에 도달하면 텔레그램으로 오류 알림
REPORT_HOUR = 8       # 매일 이 시각 이후 첫 실행 때 상태 보고

logging.basicConfig(
    filename=DATA / "app.log", level=logging.INFO, encoding="utf-8",
    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lotto")


def load_env():
    """공유 키(배포용 _secrets.py, 빌드 때 생성) 위에 사용자 .env 값을 덮어쓴다."""
    env = {}
    try:
        import _secrets
        env.update(_secrets.SECRETS)
    except ImportError:
        pass
    path = ROOT / ".env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                env[k.strip()] = v.strip()
    return env


def http_json(url, data=None, retries=3):
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, data, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(5 * (i + 1))


def api_get(op, key, **params):
    qs = urllib.parse.urlencode({"page": 1, "perPage": 100, "serviceKey": key, **params})
    return http_json(f"{API_BASE}/{op}?{qs}").get("data", [])


def send_telegram(env, text):
    body = urllib.parse.urlencode({
        "chat_id": env["TELEGRAM_CHAT_ID"], "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    http_json(f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage", body)


# ---------- 기록(DB) ----------

def open_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, name TEXT, seen_at TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return db


def meta_get(db, k, default=None):
    row = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def meta_set(db, k, v):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, str(v)))
    db.commit()


# ---------- 조건 매칭 ----------

def area_of(model):
    try:
        return float(model["HOUSE_TY"].rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
    except (KeyError, ValueError):
        return None


def model_ok(cfg, m):
    price, area = m.get("LTTOT_TOP_AMOUNT"), area_of(m)
    if cfg.get("max_price_manwon") is not None and price and int(price) > cfg["max_price_manwon"]:
        return False
    if cfg.get("min_area_m2") is not None and area is not None and area < cfg["min_area_m2"]:
        return False
    if cfg.get("max_area_m2") is not None and area is not None and area > cfg["max_area_m2"]:
        return False
    return True


def notice_ok(cfg, n):
    if cfg.get("regions") and n.get("SUBSCRPT_AREA_CODE_NM") not in cfg["regions"]:
        return False
    if cfg.get("house_types") and n.get("HOUSE_SECD_NM") not in cfg["house_types"]:
        return False
    text = f"{n.get('HOUSE_NM', '')} {n.get('HSSPLY_ADRES', '')}"
    if cfg.get("include_keywords") and not any(w in text for w in cfg["include_keywords"]):
        return False
    if any(w in text for w in cfg.get("exclude_keywords", [])):
        return False
    return True


def match_models(cfg, key, n):
    """조건에 맞는 주택형 목록. 주택형 정보가 없으면 그대로 알린다(None)."""
    models = api_get("getRemndrLttotPblancMdl", key,
                     **{"cond[HOUSE_MANAGE_NO::EQ]": n["HOUSE_MANAGE_NO"]})
    models = [m for m in models if m.get("PBLANC_NO") == n["PBLANC_NO"]] or models
    if not models:
        return None
    return [m for m in models if model_ok(cfg, m)]


# ---------- 메시지 ----------

def fmt_price(manwon):
    try:
        v = int(manwon)
    except (TypeError, ValueError):
        return "-"
    eok, man = divmod(v, 10000)
    return " ".join(filter(None, [f"{eok}억" if eok else "", f"{man:,}만" if man else ""])) + "원"


def fmt_gap(manwon):
    return ("+" if manwon >= 0 else "-") + fmt_price(abs(manwon))


def model_price(m):
    return int(m["LTTOT_TOP_AMOUNT"]) if str(m.get("LTTOT_TOP_AMOUNT", "")).isdigit() else None


def model_line(m, market):
    e = html.escape
    area = area_of(m)
    price = model_price(m)
    cnt = int(m.get("SUPLY_HSHLDCO") or 0) + int(m.get("SPSPLY_HSHLDCO") or 0)
    head = (f"• 전용 {area:.2f}㎡ / {cnt}세대" if area else f"• {e(m.get('HOUSE_TY', ''))}")
    lines = [head, f"   분양가 {fmt_price(price)}"]
    mk = market.get(area)
    if mk:
        lines.append(f"   시세 {fmt_price(mk['price'])} ({e(mk['basis'])} {mk['count']}건 중위)")
        if price:
            lines.append(f"   👉 차익 {fmt_gap(mk['price'] - price)}")
    else:
        lines.append("   시세 정보 없음")
    return "\n".join(lines)


def build_message(n, models, market, elig):
    e = html.escape
    lines = [
        f"🔔 <b>[{e(n.get('HOUSE_SECD_NM', ''))}] {e(n.get('HOUSE_NM', ''))}</b>",
        f"📍 {e(n.get('SUBSCRPT_AREA_CODE_NM', ''))} · {e(n.get('HSSPLY_ADRES', ''))}",
        f"📅 모집공고 {e(n.get('RCRIT_PBLANC_DE', ''))}",
        f"📝 청약접수 {e(n.get('SUBSCRPT_RCEPT_BGNDE', ''))} ~ {e(n.get('SUBSCRPT_RCEPT_ENDDE', ''))}",
        f"🏆 당첨발표 {e(n.get('PRZWNER_PRESNATN_DE', ''))}",
        f"🏠 공급 {e(str(n.get('TOT_SUPLY_HSHLDCO', '')))}세대",
        "",
        f"📋 <b>자격</b> {html.escape(elig['summary'][0], quote=False)}",
        *(html.escape(s, quote=False) for s in elig["summary"][1:] + elig["check"]),
    ]
    if models:
        lines.append("")
        for m in models[:10]:
            lines.append(model_line(m, market))
        if len(models) > 10:
            lines.append(f"  외 {len(models) - 10}개 주택형")
        if market:
            lines.append(f"\n<i>시세: 최근 실거래 중위값 기준 참고용 추정치</i>")
    links = []
    if elig["pdf"]:
        links.append(f"<a href=\"{e(elig['pdf'])}\">📄 모집공고문(PDF)</a>")
    if n.get("PBLANC_URL"):
        links.append(f"<a href=\"{e(n['PBLANC_URL'])}\">청약홈 공고 보기</a>")
    if links:
        lines.append("\n" + " | ".join(links))
    return "\n".join(lines)


CSV_FIELDS = ["알림일시", "모집공고일", "유형", "단지명", "지역", "주소", "청약접수시작", "청약접수종료",
              "당첨발표", "전용면적", "일반세대", "특별세대", "분양가(만원)", "시세(만원)", "시세기준",
              "차익(만원)", "자격요약", "자가점검", "모집공고문", "청약홈"]


def append_csv(n, models, market, elig):
    """주택형별로 한 줄씩 data/alerts.csv에 누적 (엑셀에서 바로 열리도록 UTF-8 BOM)."""
    base = {
        "알림일시": datetime.now().strftime("%Y-%m-%d %H:%M"), "모집공고일": n.get("RCRIT_PBLANC_DE"),
        "유형": n.get("HOUSE_SECD_NM"), "단지명": n.get("HOUSE_NM"), "지역": n.get("SUBSCRPT_AREA_CODE_NM"),
        "주소": n.get("HSSPLY_ADRES"), "청약접수시작": n.get("SUBSCRPT_RCEPT_BGNDE"),
        "청약접수종료": n.get("SUBSCRPT_RCEPT_ENDDE"), "당첨발표": n.get("PRZWNER_PRESNATN_DE"),
        "자격요약": " / ".join(elig["summary"]), "자가점검": " / ".join(elig["check"]),
        "모집공고문": elig["pdf"] or "", "청약홈": n.get("PBLANC_URL"),
    }
    rows = []
    for m in models or [None]:
        row = dict(base)
        if m:
            area, price = area_of(m), model_price(m)
            mk = market.get(area) or {}
            row.update({"전용면적": area, "일반세대": m.get("SUPLY_HSHLDCO"), "특별세대": m.get("SPSPLY_HSHLDCO"),
                        "분양가(만원)": price, "시세(만원)": mk.get("price"), "시세기준": mk.get("basis"),
                        "차익(만원)": mk["price"] - price if mk and price else None})
        rows.append(row)
    new_file = not CSV_PATH.exists()
    with open(CSV_PATH, "a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)


# ---------- 실행 ----------

def lookup_market(prices, n, models):
    if not models:
        return {}
    try:
        return prices.market(n.get("HSSPLY_ADRES", ""), n.get("HOUSE_NM", ""),
                             [a for a in map(area_of, models) if a])
    except Exception:
        log.exception("시세 조회 실패: %s", n.get("HOUSE_NM"))
        return {}


def gap_filter(cfg, models, market):
    """차익 조건(min_gap_manwon)을 만족하는 주택형만 남긴다. 시세가 없으면 notify_if_no_market에 따른다."""
    threshold = cfg.get("min_gap_manwon")
    keep_unknown = cfg.get("notify_if_no_market", True)
    if threshold is None:
        return models
    if models is None:
        return None if keep_unknown else []
    out = []
    for m in models:
        mk = market.get(area_of(m))
        price = model_price(m)
        if mk and price:
            if mk["price"] - price >= threshold:
                out.append(m)
        elif keep_unknown:
            out.append(m)
    return out


def build_eligibility(cfg, n, models):
    grade, check = eligibility.self_check(cfg.get("my_profile"), n, models)
    return {"summary": eligibility.summary(n, models), "grade": grade, "check": check,
            "pdf": eligibility.pdf_link(n)}


def run_public(env, cfg, db):
    """마이홈 공공주택(LH·SH 등) 공고 → (보낸 건수, 조회한 단지 목록).
    실패해도 청약홈 확인은 계속하도록 예외를 삼키고 상태만 meta에 남긴다."""
    if not (cfg.get("public_housing") or {}).get("enabled", True):
        meta_set(db, "public_status", "꺼짐")
        return 0, []
    now = datetime.now().strftime("%m-%d %H:%M")
    try:
        rows = myhome.fetch(env["DATA_GO_KR_KEY"], http_json)
    except Exception as ex:
        reason = "API 활용신청 필요" if "403" in str(ex) else str(ex)[:80]
        meta_set(db, "public_status", f"오류 ({now}): {reason}")
        log.warning("마이홈 조회 실패: %s", ex)
        return 0, []
    first_run = not db.execute("SELECT 1 FROM seen WHERE id LIKE 'MH!_%' ESCAPE '!'").fetchone()
    today = date.today().isoformat()
    sent, opened = 0, []
    notices = myhome.group(rows)
    for nid, items in sorted(notices.items(), key=lambda kv: kv[1][0].get("rcritPblancDe") or ""):
        if db.execute("SELECT 1 FROM seen WHERE id=?", (nid,)).fetchone():
            continue
        matched = myhome.notice_ok(cfg, items)
        if matched and first_run:
            if myhome.date_key(items[0].get("endDe")) >= today:
                opened.append(matched[0])
        elif matched:
            send_telegram(env, myhome.build_message(matched, len(items)))
            myhome.append_csv(PUBLIC_CSV_PATH, matched)
            sent += 1
            log.info("공공주택 알림 발송: %s %s", nid, items[0].get("pblancNm"))
        db.execute("INSERT INTO seen VALUES (?, ?, datetime('now', 'localtime'))", (nid, items[0].get("pblancNm")))
        db.commit()
    if first_run and notices:
        # 처음에는 공고가 많아 하나씩 보내지 않고 접수 중·예정 목록만 한 번에 보낸다
        lines = [f"✅ <b>공공주택(LH·SH 등) 감시 시작</b>",
                 f"최근 2개월 공고 {len(notices)}건 기록 · 내 조건의 접수 중·예정 {len(opened)}건", ""]
        for it in sorted(opened, key=lambda it: it.get("beginDe") or "")[:20]:
            lines.append(f"• {myhome.dday(it.get('beginDe'))}~{myhome.dday(it.get('endDe'))} "
                         f"[{myhome.category(it)}] {html.escape(it.get('pblancNm', ''))}")
        lines.append("\n이제부터 새 공고가 올라오면 하나씩 알려 드립니다.")
        send_telegram(env, "\n".join(lines))
    meta_set(db, "public_status", f"정상 ({now}) · 최근 2개월 공고 {len(notices)}건")
    log.info("공공주택 조회 완료: %d건 중 신규 알림 %d건", len(notices), sent)
    return sent, rows


def run(env, cfg, db):
    key = env["DATA_GO_KR_KEY"]
    prices = PriceLookup(key, db)
    public_sent, public_rows = run_public(env, cfg, db)
    notices = api_get("getRemndrLttotPblancDetail", key)
    if not notices:  # 청약홈 데이터 갱신 중(자정 무렵 등)에는 0건이 온다
        log.info("조회 완료: 청약홈 응답 0건 (데이터 갱신 중으로 보고 건너뜀)")
        meta_set(db, "last_check", datetime.now().strftime("%Y-%m-%d %H:%M"))
        meta_set(db, "total_sent", int(meta_get(db, "total_sent", 0)) + public_sent)
        return public_sent
    first_run = not db.execute("SELECT 1 FROM seen WHERE id NOT LIKE 'MH!_%' ESCAPE '!'").fetchone()
    today = date.today().isoformat()
    sent = public_sent
    for n in reversed(notices):  # 오래된 공고부터 알림
        nid = f"{n['HOUSE_MANAGE_NO']}_{n['PBLANC_NO']}"
        if db.execute("SELECT 1 FROM seen WHERE id=?", (nid,)).fetchone():
            continue
        # 첫 실행 때는 아직 접수가 끝나지 않은 공고만 알린다
        should_check = not first_run or (n.get("SUBSCRPT_RCEPT_ENDDE") or "") >= today
        if should_check and notice_ok(cfg, n):
            models = match_models(cfg, key, n)
            if models is None or models:
                market = lookup_market(prices, n, models)
                models = gap_filter(cfg, models, market)
            if models is None or models:
                elig = build_eligibility(cfg, n, models)
                if elig["grade"] == "no" and cfg.get("hide_ineligible"):
                    log.info("자격 미달로 알림 생략: %s %s", nid, n.get("HOUSE_NM"))
                else:
                    send_telegram(env, build_message(n, models, market, elig))
                    append_csv(n, models, market, elig)
                    sent += 1
                    log.info("알림 발송: %s %s", nid, n.get("HOUSE_NM"))
        db.execute("INSERT INTO seen VALUES (?, ?, datetime('now', 'localtime'))", (nid, n.get("HOUSE_NM")))
        db.commit()
    if first_run:
        send_telegram(env, f"✅ 감시 시작: 기존 공고 {len(notices)}건 기록, 접수 중인 공고 {sent - public_sent}건 알림")
        meta_set(db, "last_report", today)  # 첫날은 감시 시작 메시지로 상태 보고를 대신한다
    log.info("조회 완료: %d건 중 신규 알림 %d건", len(notices), sent)
    meta_set(db, "last_check", datetime.now().strftime("%Y-%m-%d %H:%M"))
    meta_set(db, "checks_since_report", int(meta_get(db, "checks_since_report", 0)) + 1)
    meta_set(db, "sent_since_report", int(meta_get(db, "sent_since_report", 0)) + sent)
    meta_set(db, "total_sent", int(meta_get(db, "total_sent", 0)) + sent)  # 트레이 알림용
    daily_report(env, cfg, db, notices, public_rows)
    return sent


def daily_report(env, cfg, db, notices, public_rows=()):
    """하루 한 번(REPORT_HOUR시 이후 첫 실행) 정상 동작 여부와 접수 일정을 보낸다."""
    now = datetime.now()
    today = now.date().isoformat()
    if not cfg.get("daily_report", True) or now.hour < REPORT_HOUR or meta_get(db, "last_report") == today:
        return
    upcoming = sorted((n for n in notices
                       if (n.get("SUBSCRPT_RCEPT_ENDDE") or "") >= today and notice_ok(cfg, n)),
                      key=lambda n: n.get("SUBSCRPT_RCEPT_BGNDE") or "")
    lines = ["☀️ <b>로또청약 알리미 정상 동작 중</b>",
             f"지난 보고 이후 {meta_get(db, 'checks_since_report', 0)}회 확인 · "
             f"새 알림 {meta_get(db, 'sent_since_report', 0)}건",
             "", "📅 <b>접수 예정·진행 중</b> (내 알림 조건 기준)"]
    for n in upcoming[:10]:
        begin = (n.get("SUBSCRPT_RCEPT_BGNDE") or "")[5:].replace("-", "/")
        lines.append(f"• {begin} {html.escape(n.get('HOUSE_NM', ''))} [{html.escape(n.get('HOUSE_SECD_NM', ''))}]")
    if not upcoming:
        lines.append("• 없음")
    public = myhome.upcoming(cfg, public_rows, today)
    if public:
        lines += ["", "🏠 <b>공공주택 접수 예정·진행 중</b>"]
        for begin, name, cat in public[:10]:
            lines.append(f"• {begin[5:].replace('-', '/')} [{html.escape(cat)}] {html.escape(name)}")
        if len(public) > 10:
            lines.append(f"  외 {len(public) - 10}건")
    status = meta_get(db, "public_status", "")
    if status.startswith("오류"):
        lines.append(f"\n⚠️ 공공주택 조회 {html.escape(status)}")
    send_telegram(env, "\n".join(lines))
    meta_set(db, "last_report", today)
    meta_set(db, "checks_since_report", 0)
    meta_set(db, "sent_since_report", 0)
    log.info("상태 보고 발송")


def main():
    env = load_env()
    if not (ROOT / "config.json").exists() or not env.get("TELEGRAM_CHAT_ID"):
        log.warning("설정이 끝나지 않아 실행하지 않음 (로또청약.exe로 설정 필요)")
        return
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    if "--reset" in sys.argv and DB_PATH.exists():
        DB_PATH.unlink()
    db = open_db()
    try:
        sent = run(env, cfg, db)
        meta_set(db, "fail_count", 0)
        return sent
    except Exception as ex:
        fails = int(meta_get(db, "fail_count", 0)) + 1
        meta_set(db, "fail_count", fails)
        log.exception("실행 실패 (%d회 연속)", fails)
        if fails == FAIL_ALERT_AFTER:
            try:
                send_telegram(env, f"⚠️ 로또청약 알리미 {fails}회 연속 실패: {html.escape(str(ex))[:300]}")
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
