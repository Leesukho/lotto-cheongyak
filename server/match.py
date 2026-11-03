"""사용자 조건(prefs)과 공고 문서 매칭, 사용자별 자격 점검.

prefs와 profile은 PC 프로그램 config.json / my_profile과 같은 키를 쓴다.
그래서 PC 설정 파일을 그대로 서버 테스트 사용자로 넣을 수 있다.
"""
import eligibility
import myhome


def _text_ok(prefs, doc):
    text = f"{doc.get('title', '')} {doc.get('address', '')}"
    if prefs.get("include_keywords") and not any(w in text for w in prefs["include_keywords"]):
        return False
    return not any(w in text for w in prefs.get("exclude_keywords") or [])


def _unit_ok(prefs, u):
    price, area = u.get("price_manwon"), u.get("area_m2")
    if prefs.get("max_price_manwon") is not None and price and price > prefs["max_price_manwon"]:
        return False
    if prefs.get("min_area_m2") is not None and area is not None and area < prefs["min_area_m2"]:
        return False
    if prefs.get("max_area_m2") is not None and area is not None and area > prefs["max_area_m2"]:
        return False
    return True


def _gap_ok(prefs, u):
    threshold = prefs.get("min_gap_manwon")
    if threshold is None:
        return True
    if u.get("gap_manwon") is not None:
        return u["gap_manwon"] >= threshold
    return prefs.get("notify_if_no_market", True)


def prefs_match(prefs, doc):
    """(맞음 여부, 맞는 주택형·단지 목록)"""
    prefs = prefs or {}
    regions = prefs.get("regions") or []
    if doc["source"] == "applyhome":
        if prefs.get("lotto_enabled") is False:
            return False, []
        if regions and doc.get("sido") not in regions:
            return False, []
        if prefs.get("house_types") and doc.get("category") not in prefs["house_types"]:
            return False, []
        if not _text_ok(prefs, doc):
            return False, []
        units = doc.get("units") or []
        if not units:  # 주택형 정보가 없으면 PC처럼 그대로 알린다 (차익 조건은 시세 없음과 같게)
            return prefs.get("min_gap_manwon") is None or prefs.get("notify_if_no_market", True), []
        units = [u for u in units if _unit_ok(prefs, u) and _gap_ok(prefs, u)]
        return bool(units), units

    ph = prefs.get("public_housing") or {}
    if ph.get("enabled") is False:
        return False, []
    if ph.get("types") and doc.get("category") not in ph["types"]:
        return False, []
    if not _text_ok(prefs, doc):
        return False, []
    units = [u for u in doc.get("units") or [] if myhome.region_ok(regions, u.get("sidoFull") or u.get("sido"))]
    return bool(units), units


def check(profile, doc, units):
    """(등급 ok/warn/no 또는 None, 메시지 목록)"""
    if not profile:
        return None, []
    if doc["source"] == "applyhome":
        n = {"HOUSE_SECD_NM": doc.get("category"), "SUBSCRPT_AREA_CODE_NM": doc.get("sido"),
             "HSSPLY_ADRES": doc.get("address")}
        models = [{"SUPLY_HSHLDCO": u.get("count_general"), "SPSPLY_HSHLDCO": u.get("count_special")}
                  for u in units] or None
        return eligibility.self_check(profile, n, models)

    fails = []
    if profile.get("homeless") is False:
        fails.append("❌ 유주택 세대: 공공임대·공공분양은 무주택 요건 필요")
    if profile.get("abroad_90d"):
        fails.append("❌ 90일 초과 해외 체류 중: 거주자로 인정되지 않음")
    if doc["source"] == "myhome_sale" and profile.get("restricted"):
        fails.append("❌ 재당첨제한·부적격 제한 기간 중")
    if fails:
        return "no", fails
    return "warn", ["⚠️ 소득·자산 기준과 대상 계층(청년·신혼·고령자 등)은 공고문 확인"]
