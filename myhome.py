"""마이홈포털 공공주택 모집공고 (국토교통부, data.go.kr 15108420).

LH·SH·GH 등 공공기관이 내는 공공임대(행복주택·국민임대·영구임대 등)와 공공분양 공고를 받는다.
한 공고(pblancId)에 단지(houseSn)가 여러 개 들어 있어 공고 단위로 묶어 알린다.
"""
import csv
import html
import urllib.parse
from datetime import date, datetime

API = "https://apis.data.go.kr/1613000/HWSPR02"
OPS = {"rsdtRcritNtcList": "공공임대", "ltRsdtRcritNtcList": "공공분양"}

# 설정 화면의 유형 이름 → 공급유형/주택유형/공고명에서 찾을 낱말
CATEGORIES = [
    ("행복주택", ["행복주택"]),
    ("국민임대", ["국민임대"]),
    ("영구임대", ["영구임대"]),
    ("통합공공임대", ["통합공공"]),
    ("장기전세", ["장기전세"]),
    ("5·10년 공공임대", ["10년임대", "5년임대", "50년임대", "분양전환"]),
    ("매입·전세임대", ["매입임대", "전세임대", "매입"]),
    ("공공분양", ["공공분양", "분양"]),
]
OTHER = "기타 공공임대"
TYPE_NAMES = [name for name, _ in CATEGORIES] + [OTHER]

# 알림 지역(짧은 이름) → 광역시도명에 들어 있는 낱말
REGION_WORDS = {"경남": ["경상남", "경남"], "경북": ["경상북", "경북"], "충남": ["충청남", "충남"],
                "충북": ["충청북", "충북"], "전남": ["전라남", "전남"], "전북": ["전라북", "전북"]}


def category(item):
    if item["_source"] == "공공분양":
        return "공공분양"
    # 공급유형을 먼저 보고, 비어 있거나 모르는 값일 때만 공고명으로 짐작한다
    for text in (item.get("suplyTyNm") or "", item.get("pblancNm") or ""):
        for name, words in CATEGORIES[:-1]:
            if any(w in text for w in words):
                return name
    return OTHER


def region_ok(regions, brtc):
    return not regions or any(w in (brtc or "") for r in regions for w in REGION_WORDS.get(r, [r]))


def _items(data):
    """응답 모양이 {response:{body}} / {body} 둘 다 올 수 있고, item이 1건이면 dict로 온다."""
    body = (data.get("response") or data).get("body") or {}
    items = body.get("item")
    if items is None and isinstance(body.get("items"), dict):
        items = body["items"].get("item")
    elif items is None:
        items = body.get("items")
    if isinstance(items, dict):
        items = [items]
    return items or [], int(body.get("totalCount") or 0)


def fetch(key, http_json, months=2):
    """최근 months개월 모집공고의 단지 목록 (공공임대 + 공공분양)."""
    today = date.today()
    y, m = today.year, today.month - (months - 1)
    while m < 1:
        y, m = y - 1, m + 12
    rows = []
    for op, source in OPS.items():
        page = 1
        while True:
            qs = urllib.parse.urlencode({"serviceKey": key, "numOfRows": 100, "pageNo": page,
                                         "yearMtBegin": f"{y}{m:02d}", "yearMtEnd": today.strftime("%Y%m")})
            items, total = _items(http_json(f"{API}/{op}?{qs}"))
            rows += [{**it, "_source": source} for it in items]
            if not items or page * 100 >= total:
                break
            page += 1
    return rows


def group(rows):
    """공고 ID별로 단지를 묶는다 → [{id, items}]"""
    notices = {}
    for r in rows:
        nid = f"MH_{r['_source']}_{r.get('pblancId')}"
        notices.setdefault(nid, []).append(r)
    return notices


def notice_ok(cfg, items):
    """설정의 공공주택 조건(유형·지역)에 맞는 단지만 돌려준다."""
    ph = cfg.get("public_housing") or {}
    types = ph.get("types") or []
    if types and category(items[0]) not in types:
        return []
    return [it for it in items if region_ok(cfg.get("regions") or [], it.get("brtcNm"))]


def dday(s):
    """'20261110' / '2026-11-10' → '11/10'"""
    s = (s or "").replace("-", "").replace(".", "")
    return f"{s[4:6]}/{s[6:8]}" if len(s) >= 8 else "-"


def date_key(s):
    s = (s or "").replace("-", "").replace(".", "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 else ""


def fmt_won(v):
    """원 단위 금액 → '1,234만원'. 0·없음은 None."""
    try:
        v = int(float(v))
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v < 1_000_000:  # 월 임대료처럼 작은 금액은 반올림하면 차이가 커서 소수 한 자리까지
        return f"{v / 10000:.1f}".rstrip("0").rstrip(".") + "만원"
    man = round(v / 10000)
    eok, man = divmod(man, 10000)
    return " ".join(filter(None, [f"{eok}억" if eok else "", f"{man:,}만" if man else ""])) + "원"


def item_line(it):
    e = html.escape
    name = it.get("hsmpNm")
    place = " ".join(filter(None, [it.get("brtcNm"), it.get("signguNm")]))
    # 매입임대처럼 단지명이 없는 공고는 지역만 표시
    head = f"• {e(name)}" + (f" ({e(place)})" if place else "") if name else f"• {e(place or '-')}"
    count = it.get("sumSuplyCo") or it.get("suplyHoCo")
    money = [f"{count}호" if count else "",
             f"보증금 {fmt_won(it.get('rentGtn'))}~" if fmt_won(it.get("rentGtn")) else "",
             f"월 {fmt_won(it.get('mtRntchrg'))}~" if fmt_won(it.get("mtRntchrg")) else ""]
    money = " · ".join(filter(None, money))
    return head + (f"\n   {money}" if money else "")


def build_message(items, total_items):
    e = html.escape
    first = items[0]
    cat = category(first)
    icon = "🏢" if cat == "공공분양" else "🏠"
    kinds = " · ".join(dict.fromkeys(filter(None, [first.get("suplyInsttNm"), first.get("suplyTyNm"),
                                                   first.get("houseTyNm")])))
    lines = [f"{icon} <b>[{e(cat)}] {e(first.get('pblancNm', ''))}</b>"]
    if kinds:
        lines.append(f"🏛 {e(kinds)}")
    lines += [f"📅 모집공고 {dday(first.get('rcritPblancDe'))} · 접수 {dday(first.get('beginDe'))} ~ "
              f"{dday(first.get('endDe'))}" + (f" · 발표 {dday(first.get('przwnerPresnatnDe'))}"
                                               if first.get("przwnerPresnatnDe") else ""),
              f"📌 상태 {e(first.get('sttusNm') or '-')}", ""]
    for it in items[:8]:
        lines.append(item_line(it))
    rest = total_items - min(len(items), 8)
    if rest > 0:
        lines.append(f"  외 {rest}개 단지")
    lines += ["", "📋 <b>자격</b> 무주택 세대구성원 + 유형별 소득·자산 기준 (청년·신혼·고령자 등 계층별로 다름)",
              "⚠️ 최종 자격과 임대 조건은 모집공고문에서 확인하세요."]
    links = []
    if first.get("url"):
        links.append(f"<a href=\"{e(first['url'])}\">📄 모집공고</a>")
    if first.get("pcUrl"):
        links.append(f"<a href=\"{e(first['pcUrl'])}\">마이홈 보기</a>")
    if links:
        lines.append("\n" + " | ".join(links))
    return "\n".join(lines)


CSV_FIELDS = ["알림일시", "유형", "공고명", "공급기관", "공급유형", "단지명", "시도", "시군구", "주소",
              "모집공고일", "접수시작", "접수종료", "당첨발표", "공급호수", "최소보증금(원)", "최소월임대료(원)",
              "상태", "모집공고", "마이홈"]


def append_csv(path, items):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = [{"알림일시": now, "유형": category(it), "공고명": it.get("pblancNm"), "공급기관": it.get("suplyInsttNm"),
             "공급유형": it.get("suplyTyNm"), "단지명": it.get("hsmpNm"), "시도": it.get("brtcNm"),
             "시군구": it.get("signguNm"), "주소": it.get("fullAdres"), "모집공고일": date_key(it.get("rcritPblancDe")),
             "접수시작": date_key(it.get("beginDe")), "접수종료": date_key(it.get("endDe")),
             "당첨발표": date_key(it.get("przwnerPresnatnDe")), "공급호수": it.get("sumSuplyCo"),
             "최소보증금(원)": it.get("rentGtn"), "최소월임대료(원)": it.get("mtRntchrg"),
             "상태": it.get("sttusNm"), "모집공고": it.get("url"), "마이홈": it.get("pcUrl")} for it in items]
    new_file = not path.exists()
    with open(path, "a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)


def upcoming(cfg, rows, today):
    """접수가 끝나지 않은 조건 맞는 공고 [(접수시작, 공고명, 유형)] — 아침 상태 보고용."""
    out = []
    for items in group(rows).values():
        items = notice_ok(cfg, items)
        if items and date_key(items[0].get("endDe")) >= today:
            out.append((date_key(items[0].get("beginDe")), items[0].get("pblancNm", ""), category(items[0])))
    return sorted(out)
