"""청약홈·마이홈 응답을 하나의 공고 문서(notices/{id}) 형식으로 바꾼다.

매칭·자격 점검·푸시는 이 형식만 본다. 새 소스를 붙일 때는 여기에 변환 함수만 추가한다.
문서 구조는 docs/모바일앱_설계.md 3장 참고.
"""
import hashlib
import json

import eligibility
import myhome

# 청약홈 SUBSCRPT_AREA_CODE_NM은 이미 짧은 이름(서울, 경기...)이다. 마이홈 광역시도명 → 짧은 이름
SIDO_SHORT = [
    ("서울", "서울"), ("부산", "부산"), ("대구", "대구"), ("인천", "인천"), ("대전", "대전"),
    ("울산", "울산"), ("세종", "세종"), ("경기", "경기"), ("강원", "강원"), ("충청북", "충북"),
    ("충청남", "충남"), ("전북", "전북"), ("전라북", "전북"), ("전라남", "전남"), ("경상북", "경북"),
    ("경상남", "경남"), ("제주", "제주"), ("전남광주", "전남"), ("광주", "광주"),
]


def sido_short(full):
    full = full or ""
    return next((short for prefix, short in SIDO_SHORT if full.startswith(prefix)), full[:2])


def ymd(s):
    """'20261110' / '2026-11-10' / '2026.11.10' → '2026-11-10' (모르면 None)"""
    s = (s or "").replace("-", "").replace(".", "").strip()
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 and s[:8].isdigit() else None


def num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def content_hash(doc):
    """정정공고·일정 변경 감지용. 수집 시각 등 매번 바뀌는 값은 빼고 계산한다."""
    core = {k: doc[k] for k in ("title", "status", "dates", "units", "links") if k in doc}
    return hashlib.sha1(json.dumps(core, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


# ---------- 청약홈 (무순위·계약취소 재공급) ----------

def area_of(model):
    try:
        return float(model["HOUSE_TY"].rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
    except (KeyError, ValueError, AttributeError):
        return None


def applyhome(n, models, market, pdf):
    """n: 공고 상세 1건, models: 주택형 목록(전체), market: {면적: 시세}, pdf: 모집공고문 링크"""
    address = n.get("HSSPLY_ADRES") or ""
    tokens = address.split()
    units = []
    for m in models or []:
        area = area_of(m)
        price = num(m.get("LTTOT_TOP_AMOUNT"))
        mk = market.get(area) or {}
        units.append({
            "name": m.get("HOUSE_TY"), "area_m2": area,
            "count_general": num(m.get("SUPLY_HSHLDCO")) or 0,
            "count_special": num(m.get("SPSPLY_HSHLDCO")) or 0,
            "price_manwon": price,
            "market_manwon": mk.get("price"), "market_basis": mk.get("basis"), "market_count": mk.get("count"),
            "gap_manwon": mk["price"] - price if mk and price else None,
        })
    gaps = [u["gap_manwon"] for u in units if u["gap_manwon"] is not None]
    doc = {
        "id": f"AH_{n['HOUSE_MANAGE_NO']}_{n['PBLANC_NO']}",
        "source": "applyhome",
        "category": n.get("HOUSE_SECD_NM") or "무순위",
        "title": n.get("HOUSE_NM") or "",
        "agency": n.get("BSNS_MBY_NM") or "",
        "status": "",
        "sido": n.get("SUBSCRPT_AREA_CODE_NM") or sido_short(tokens[0] if tokens else ""),
        "sidoFull": tokens[0] if tokens else "",
        "sigungu": tokens[1] if len(tokens) > 1 else "",
        "address": address,
        "dates": {"announce": ymd(n.get("RCRIT_PBLANC_DE")), "applyStart": ymd(n.get("SUBSCRPT_RCEPT_BGNDE")),
                  "applyEnd": ymd(n.get("SUBSCRPT_RCEPT_ENDDE")), "winner": ymd(n.get("PRZWNER_PRESNATN_DE"))},
        "totalUnits": num(n.get("TOT_SUPLY_HSHLDCO")),
        "units": units,
        "maxGapManwon": max(gaps) if gaps else None,
        "eligibility": {"summary": eligibility.summary(n, models)},
        "links": {"pdf": pdf, "applyhome": n.get("PBLANC_URL")},
    }
    doc["hash"] = content_hash(doc)
    return doc


# ---------- 마이홈 (공공임대·공공분양) ----------

def myhome_notice(nid, items):
    """myhome.group()이 묶은 공고 1건(단지 목록) → 문서"""
    first = items[0]
    units = [{
        "name": it.get("hsmpNm") or " ".join(filter(None, [it.get("brtcNm"), it.get("signguNm")])),
        "sido": sido_short(it.get("brtcNm")), "sidoFull": it.get("brtcNm") or "",
        "sigungu": it.get("signguNm") or "",
        "address": it.get("fullAdres") or "",
        "count": num(it.get("sumSuplyCo")) or num(it.get("suplyHoCo")),
        "deposit_won": num(it.get("rentGtn")) or None, "rent_won": num(it.get("mtRntchrg")) or None,
    } for it in items]
    sidos = list(dict.fromkeys(u["sido"] for u in units))
    doc = {
        "id": nid,
        "source": "myhome_sale" if first["_source"] == "공공분양" else "myhome_rent",
        "category": myhome.category(first),
        "title": first.get("pblancNm") or "",
        "agency": first.get("suplyInsttNm") or "",
        "status": first.get("sttusNm") or "",
        "sido": sidos[0] if sidos else "",
        "sidos": sidos,                     # 여러 시·도에 걸친 공고(매입임대 등) 매칭용
        "sidoFull": first.get("brtcNm") or "",
        "sigungu": first.get("signguNm") or "",
        "address": first.get("fullAdres") or "",
        "dates": {"announce": ymd(first.get("rcritPblancDe")), "applyStart": ymd(first.get("beginDe")),
                  "applyEnd": ymd(first.get("endDe")), "winner": ymd(first.get("przwnerPresnatnDe"))},
        "totalUnits": sum(u["count"] or 0 for u in units) or None,
        "units": units,
        "maxGapManwon": None,
        "eligibility": {"summary": ["무주택 세대구성원 + 유형별 소득·자산 기준 (청년·신혼·고령자 등 계층별로 다름)"]},
        "links": {"pdf": first.get("url"), "myhome": first.get("pcUrl")},
    }
    doc["hash"] = content_hash(doc)
    return doc
