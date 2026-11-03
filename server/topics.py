"""FCM 토픽 이름: '{시도}_{유형}' (예: gyeonggi_muns, seoul_happy).

토픽 이름에는 영문·숫자·_-.~% 만 쓸 수 있어 코드로 바꾼다.
앱은 내가 고른 지역 × 유형 조합만 구독한다. 이 표는 site/topics.json으로도 게시해 앱과 맞춘다.
"""
SIDO = {
    "서울": "seoul", "부산": "busan", "대구": "daegu", "인천": "incheon", "광주": "gwangju",
    "대전": "daejeon", "울산": "ulsan", "세종": "sejong", "경기": "gyeonggi", "강원": "gangwon",
    "충북": "chungbuk", "충남": "chungnam", "전북": "jeonbuk", "전남": "jeonnam", "경북": "gyeongbuk",
    "경남": "gyeongnam", "제주": "jeju",
}
CATEGORY = {
    "무순위": "muns", "불법행위 재공급": "resupply",
    "행복주택": "happy", "국민임대": "national", "영구임대": "permanent", "통합공공임대": "integrated",
    "장기전세": "jeonse", "5·10년 공공임대": "lease510", "매입·전세임대": "purchase",
    "공공분양": "sale", "기타 공공임대": "other",
}


def sidos_of(doc):
    """공고가 걸친 시·도 (짧은 이름). 전남광주통합특별시는 광주·전남 둘 다."""
    out = []
    fulls = [u.get("sidoFull") for u in doc.get("units") or [] if u.get("sidoFull")] or [doc.get("sidoFull")]
    for s in doc.get("sidos") or [doc.get("sido")]:
        out.append(s)
    if any("전남광주" in (f or "") for f in fulls):
        out += ["광주", "전남"]
    return [s for s in dict.fromkeys(out) if s in SIDO]


def topics_for(doc):
    cat = CATEGORY.get(doc.get("category"), "other")
    return [f"{SIDO[s]}_{cat}" for s in sidos_of(doc)]


def table():
    return {"sido": SIDO, "category": CATEGORY, "format": "{sido}_{category}"}
