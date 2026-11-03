"""공고별 청약 자격 요약, 내 조건 자가점검, 모집공고문 PDF 링크.

근거와 불확실 항목은 docs/청약자격_조사_및_설문설계.md 참고.
- 무순위(2025-06-10~): 성년 무주택세대구성원, 통장 불필요,
  거주요건은 지자체가 시·도 / 광역권 / 없음 중 공고별 지정 (시·군 단위 없음)
- 계약취소 재공급(제47조의3): 일반분은 해당 주택건설지역(시·군) 거주 무주택세대주,
  특별분은 해당 지역 거주 + 원 특별공급 유형 자격. 통장 불필요
판정은 참고용이며 세부 요건(거주요건·재당첨제한·원 특공 유형)은 모집공고문에만 있다.
"""
import re
import urllib.request

PDF_BASE = "https://static.applyhome.co.kr/ai/aia/"
CAPITAL_AREA = {"서울", "인천", "경기"}  # 무순위 '광역권' 요건 중 확인된 범위는 수도권뿐

SUMMARY = {
    "무순위": "성년 무주택세대구성원 · 청약통장 불필요 · 거주요건(시·도/광역권/전국)은 공고별 지정",
    "불법행위 재공급": "계약취소주택: 해당 시·군 거주 무주택세대주(일반분) · 청약통장 불필요 · 재당첨제한은 원 주택 기준",
}


def pdf_link(n):
    """청약홈 상세 페이지에서 '모집공고문 보기' PDF 주소를 찾는다. 실패하면 None."""
    url = n.get("PBLANC_URL")
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            page = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    m = re.search(r"(?:https?://static\.applyhome\.co\.kr/ai/aia/)?(getAtchmnfl\.do\?[^\"'\s<>]+)", page)
    return PDF_BASE + m.group(1).replace("&amp;", "&") if m else None


def supply_counts(models):
    """(일반공급 세대수, 특별공급 세대수)"""
    models = models or []
    return (sum(int(m.get("SUPLY_HSHLDCO") or 0) for m in models),
            sum(int(m.get("SPSPLY_HSHLDCO") or 0) for m in models))


def construction_area(address):
    """주택건설지역: 특별시·광역시·특별자치시는 시 전체, 도는 시·군. 판단 불가면 None."""
    tokens = (address or "").split()
    if not tokens:
        return None
    sido = tokens[0]
    if "통합특별시" in sido:  # 2026 신설 통합특별시는 범위 규정 미확인
        return None
    if sido.endswith(("특별시", "광역시", "특별자치시")):
        return sido
    if len(tokens) > 1 and tokens[1].endswith(("시", "군")):
        return f"{sido} {tokens[1]}"
    return None


def summary(n, models):
    """자격 요약 줄 목록"""
    kind = n.get("HOUSE_SECD_NM")
    lines = [SUMMARY.get(kind, "자격 요건은 공고문 확인")]
    general, special = supply_counts(models)
    if special and not general:
        lines.append(f"⚠️ 특별공급 전용 {special}세대: 원 특별공급(신혼부부·생애최초·다자녀·신생아 등) 자격 필요")
    elif special:
        lines.append(f"일반 {general}세대 + 특별 {special}세대 (특별공급은 원 특공 유형 자격 필요)")
    return lines


def self_check(profile, n, models):
    """내 조건(profile) 기준 점검. (등급, 메시지 목록) 등급: ok / warn / no"""
    if not profile:
        return None, []
    fails, warns = [], []

    # 공통 결격
    if profile.get("homeless") is False:
        fails.append("❌ 유주택 세대: 무주택 요건 미충족 (소형·저가주택 예외는 공고문 확인)")
    if profile.get("abroad_90d"):
        fails.append("❌ 90일 초과 해외 체류 중: 거주자로 인정되지 않음")
    if profile.get("restricted"):
        fails.append("❌ 재당첨제한·부적격 제한 기간 중")
    elif profile.get("restricted") is None:
        warns.append("⚠️ 재당첨제한·부적격 기간 여부 미입력 (청약홈 '청약제한사항 확인')")

    kind = n.get("HOUSE_SECD_NM")
    my_sido = profile.get("region")
    notice_sido = n.get("SUBSCRPT_AREA_CODE_NM")
    general, special = supply_counts(models)

    if kind == "무순위":
        if my_sido and my_sido != notice_sido:
            if my_sido in CAPITAL_AREA and notice_sido in CAPITAL_AREA:
                warns.append(f"⚠️ 거주지({my_sido})≠공급지역({notice_sido}): '시·도' 요건이면 불가, '수도권'·'전국'이면 가능")
            else:
                warns.append(f"⚠️ 거주지({my_sido})≠공급지역({notice_sido}): '전국' 요건일 때만 가능")

    elif kind == "불법행위 재공급":
        target = construction_area(n.get("HSSPLY_ADRES"))
        mine = construction_area(profile.get("address"))
        if not target or not mine:
            warns.append("⚠️ 주택건설지역 거주 여부 판단 불가: 공고문 확인")
        elif target != mine:
            fails.append(f"❌ 해당 주택건설지역({target}) 거주자만 신청 가능 (최근 공고 기준)")
        # 일반분은 세대주, 특별분은 원 특공 자격
        can_general = general and profile.get("householder") is not False
        can_special = special and profile.get("special_eligible") is not False
        if general and profile.get("householder") is False:
            if can_special:
                warns.append("⚠️ 세대원이라 일반공급분은 불가, 특별공급분만 신청 가능")
            else:
                fails.append("❌ 일반공급분은 무주택 '세대주'만 신청 가능")
        if special and not general and profile.get("special_eligible") is False:
            fails.append("❌ 특별공급 전용 공고: 특별공급 자격 없음으로 설정됨")
        if special and (profile.get("special_eligible") or not can_general):
            warns.append("⚠️ 원 특공 유형(공고문)과 내 자격 일치 여부 확인 필요")
        if profile.get("householder") is None and general:
            warns.append("⚠️ 세대주 여부 미입력: 일반공급분은 세대주만 가능")

    if fails:
        return "no", fails
    if warns:
        return "warn", warns
    return "ok", ["✅ 기본 요건 충족 (세부 요건은 공고문 확인)"]
