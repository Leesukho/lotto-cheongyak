"""지원금·지원사업 수집: 행정안전부 공공서비스(혜택) 정보(보조금24) → site/benefits/ 에 지역별 JSON 게시.

청약 수집(10분)과 달리 지원사업은 천천히 바뀌므로 하루 한 번만 돈다. github_run과 같은 실행 안에서
site/benefits/index.json 의 마지막 실행 시각을 보고, 20시간이 안 지났으면 건너뛴다.

  python -m server.benefits --site site [--force]

게시 파일 (site/benefits/)
  index.json     갱신 시각, 전체 건수, 파일별 건수
  national.json  중앙행정기관·공공기관 사업 (전국 대상)
  {sido}.json    시도별 사업: 광역·기초 지자체, 교육청, 지방공기업·출연기관 (sido는 topics.SIDO의 영문 코드)

앱은 전국 파일 + 내 지역 파일만 받아 내 조건으로 직접 거른다 (사용자 정보는 서버로 보내지 않는다).
목록의 긴 글은 잘라서 담고, 전문은 url(보조금24 상세 페이지)로 연결한다.
조건 코드(cond)는 supportConditions의 값을 그대로 담는다. 코드 뜻은 공식 코드표로 확인한 뒤에 매칭에 쓴다.

푸시 (github_run이 sender를 넘겨 줄 때만): 지원사업은 전국·시도 단위 토픽 'gv_{시도코드}' / 'gv_national'로
  - 새로 등록된 사업            kind=new
  - 신청 마감이 7일 안으로 다가온 사업  kind=d7 (2~7일 남음), kind=d1 (오늘·내일 마감)  ※ 마감일이 날짜로 있는 사업만
데이터 메시지만 보낸다. 앱이 내 조건(나이·소득·대상 등)으로 걸러서 맞을 때만 알림을 띄운다 (내 조건은 서버로 보내지 않는다).
처음 실행(state.json 없음)에는 지금 있는 사업을 '이미 본 것'으로만 기록하고 알리지 않는다.

환경 변수: DATA_GO_KR_KEY (보조금24 활용신청이 승인된 키)
"""
import argparse
import json
import logging
import re
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

from server import collector, topics

log = logging.getLogger("benefits")
API_BASE = "https://api.odcloud.kr/api/gov24/v3"
PER_PAGE = 1000
RUN_EVERY_HOURS = 20
KEEP_CLOSED_DAYS = 30
MAX_EVENTS = 400   # 한 번 실행에 보낼 푸시 상한 (이상 동작으로 쏟아지는 것을 막는 안전장치)
FCM_MAX_BYTES = 3800
SCHEMA = 2   # 게시 형식이 바뀌면 올린다 (주기가 안 지났어도 다시 수집해 새 형식으로 게시)
CUT = {"summary": 200, "target": 300, "content": 300}

# 시도 이름 앞부분 → topics.SIDO의 짧은 이름. 전남광주통합특별시는 광주·전남 양쪽에 담는다.
SIDO_PREFIX = [
    ("전남광주", "전남광주"), ("서울", "서울"), ("부산", "부산"), ("대구", "대구"), ("인천", "인천"),
    ("광주", "광주"), ("대전", "대전"), ("울산", "울산"), ("세종", "세종"), ("경기", "경기"),
    ("강원", "강원"), ("충청북", "충북"), ("충북", "충북"), ("충청남", "충남"), ("충남", "충남"),
    ("전북", "전북"), ("전라북", "전북"), ("전라남", "전남"), ("전남", "전남"),
    ("경상북", "경북"), ("경북", "경북"), ("경상남", "경남"), ("경남", "경남"), ("제주", "제주"),
]
LOCAL_TYPES = ("광역시도", "시군구", "교육청", "지방공기업", "지방출자_출연기관")


def fetch_all(op, key):
    rows, page = [], 1
    while True:
        qs = urllib.parse.urlencode({"page": page, "perPage": PER_PAGE, "serviceKey": key})
        res = collector.http_json(f"{API_BASE}/{op}?{qs}")
        data = res.get("data") or []
        rows += data
        if not data or len(rows) >= res.get("totalCount", 0):
            return rows
        page += 1


def sido_of(name):
    name = (name or "").strip()
    return next((s for p, s in SIDO_PREFIX if name.startswith(p)), None)


def region_table(rows):
    """기관코드 → (시도, 시군구). 이름에 지역이 없는 공기업·재단은 같은 코드의 시군구 행에서 지역을 가져온다."""
    table = {}
    for r in rows:
        if r["소관기관유형"] not in ("광역시도", "시군구"):
            continue
        parts = r["소관기관명"].split()
        sido = sido_of(parts[0])
        if sido:
            table.setdefault(r["소관기관코드"], (sido, parts[1] if len(parts) > 1 and r["소관기관유형"] == "시군구" else None))
    return table


def clip(text, n):
    text = re.sub(r"\s+", " ", (text or "").replace("\r", " ")).strip()
    return text if len(text) <= n else text[:n - 1] + "…"


def dates_of(text):
    """신청기한 문장에서 날짜 두 개를 찾아 (시작, 끝). 하나면 끝만. 날짜 형식이 아니면 (None, None)."""
    found = re.findall(r"(20\d{2})[.\-/ ]*(\d{1,2})[.\-/ ]*(\d{1,2})", text or "")
    days = []
    for y, m, d in found:
        try:
            days.append(datetime(int(y), int(m), int(d)).date().isoformat())
        except ValueError:
            pass
    if len(days) >= 2:
        return days[0], days[1]
    return (None, days[0]) if days else (None, None)


def conditions_of(row):
    """supportConditions 한 행 → {"y": [Y인 코드], "ageMin", "ageMax"}"""
    y = sorted(k for k, v in row.items() if k.startswith("JA") and v == "Y")
    out = {"y": y}
    for k, name in (("JA0110", "ageMin"), ("JA0111", "ageMax")):
        if isinstance(row.get(k), (int, float)) and row[k] is not None:
            out[name] = int(row[k])
    return out


def normalize(row, regions, cond):
    typ, org = row["소관기관유형"], row["소관기관명"]
    parts = org.split()
    sidos, sigungu = [], None
    if typ in LOCAL_TYPES:
        sido = sido_of(parts[0])
        if sido:
            sigungu = parts[1] if typ == "시군구" and len(parts) > 1 else None
        elif row["소관기관코드"] in regions:
            sido, sigungu = regions[row["소관기관코드"]]
            sigungu = sigungu if typ == "시군구" else None
        sidos = ["광주", "전남"] if sido == "전남광주" else ([sido] if sido else [])
    scope = "national" if not sidos else ("sigungu" if sigungu else "sido")
    start, end = dates_of(row.get("신청기한"))
    doc = {"id": "GV_" + row["서비스ID"], "title": row["서비스명"], "summary": clip(row.get("서비스목적요약"), CUT["summary"]),
           "field": row.get("서비스분야"), "supportType": row.get("지원유형"),
           "target": clip(row.get("지원대상"), CUT["target"]), "content": clip(row.get("지원내용"), CUT["content"]),
           "method": row.get("신청방법"), "deadline": row.get("신청기한"), "applyStart": start, "applyEnd": end,
           "org": org, "orgType": typ, "scope": scope, "sidos": sidos, "sigungu": sigungu,
           "phone": row.get("전화문의"), "url": row.get("상세조회URL"), "modified": row.get("수정일시"),
           "cond": cond, "userType": row.get("사용자구분")}
    return {k: v for k, v in doc.items() if v not in (None, "", [])}


def build(key, now):
    services = fetch_all("serviceList", key)
    conds = {c["서비스ID"]: conditions_of(c) for c in fetch_all("supportConditions", key)}
    regions = region_table(services)
    docs = [normalize(r, regions, conds.get(r["서비스ID"])) for r in services]
    # 신청 마감일이 명확하고 한 달 넘게 지난 사업은 게시하지 않는다 (상시·미정 사업은 그대로)
    cutoff = (now - timedelta(days=KEEP_CLOSED_DAYS)).date().isoformat()
    docs = [d for d in docs if (d.get("applyEnd") or "9999") >= cutoff]
    unplaced = [d["id"] for d in docs if d["orgType"] in LOCAL_TYPES and not d.get("sidos")]
    log.info("서비스 %d건 (조건 %d건), 지역을 못 찾은 지자체 계열 %d건", len(docs), len(conds), len(unplaced))
    files = {"national": [d for d in docs if d["scope"] == "national"]}
    for short, code in topics.SIDO.items():
        files[code] = [d for d in docs if short in d.get("sidos", [])]
    return docs, files


# ---------- 푸시 이벤트 ----------

def topics_of(doc):
    """사업이 가야 할 토픽: 전국이면 gv_national, 아니면 걸친 시도마다 gv_{코드}"""
    if doc.get("scope") == "national" or not doc.get("sidos"):
        return ["gv_national"]
    return [f"gv_{topics.SIDO[s]}" for s in doc["sidos"] if s in topics.SIDO]


def event_data(doc, kind):
    """FCM 데이터 메시지(값은 모두 문자열). 앱이 조건 코드로 직접 자격을 점검할 수 있게 조건을 함께 담는다."""
    cond = doc.get("cond") or {}
    data = {"t": "gv", "kind": kind, "id": doc["id"], "title": doc["title"][:80], "org": doc.get("org", ""),
            "field": (doc.get("field") or "").split("||")[0], "scope": doc.get("scope", ""),
            "sigungu": doc.get("sigungu") or "", "end": doc.get("applyEnd") or "",
            "ut": doc.get("userType") or "", "y": ",".join(cond.get("y", [])),
            "a0": "" if cond.get("ageMin") is None else str(cond["ageMin"]),
            "a1": "" if cond.get("ageMax") is None else str(cond["ageMax"])}
    while len(json.dumps(data, ensure_ascii=False).encode()) > FCM_MAX_BYTES and data["y"]:
        data["y"] = data["y"][:-7]   # 조건 코드가 너무 길면 뒤에서부터 줄인다 (앱은 모르는 코드는 무시)
    return data


def find_events(docs, state, now):
    """새 사업과 마감 임박 사업을 찾는다. state를 갱신하고 [(문서, kind)]를 돌려준다. 처음 실행이면 기록만 한다."""
    ids = {d["id"] for d in docs}
    seen = set(state.get("seen", []))
    sent = set(state.get("sent", []))
    first = not state.get("seeded")
    today = now.date()
    events = []
    for d in docs:
        if not first and d["id"] not in seen:
            events.append((d, "new"))
        end = d.get("applyEnd")
        if first or not end:
            continue
        left = (datetime.fromisoformat(end).date() - today).days
        for kind, lo, hi in (("d7", 2, 7), ("d1", 0, 1)):
            key = f"{d['id']}|{kind}"
            if lo <= left <= hi and key not in sent:
                events.append((d, kind))
                sent.add(key)
                break   # 오래 쉬었다 켜졌을 때 같은 사업에 두 번 보내지 않는다
    state["seeded"] = True
    state["seen"] = sorted(ids)
    state["sent"] = sorted(k for k in sent if k.split("|")[0] in ids)   # 사라진 사업의 기록은 정리
    return events


def send_events(events, sender):
    sent = failed = 0
    for doc, kind in events[:MAX_EVENTS]:
        data = event_data(doc, kind)
        for topic in topics_of(doc):
            try:
                sender.send(topic, data)
                sent += 1
            except Exception:
                failed += 1
                log.exception("지원사업 푸시 실패: %s %s", topic, doc["id"])
    if len(events) > MAX_EVENTS:
        log.warning("지원사업 푸시 %d건 중 %d건만 보냄 (상한)", len(events), MAX_EVENTS)
    return sent, failed


def write_if_changed(path, data):
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def due(out_dir, now):
    idx = out_dir / "index.json"
    if not idx.exists():
        return True
    meta = json.loads(idx.read_text(encoding="utf-8"))
    last = meta.get("updated")
    return meta.get("schema") != SCHEMA or not last or now - datetime.fromisoformat(last) >= timedelta(hours=RUN_EVERY_HOURS)


def run(site, key, now, force=False, sender=None):
    out_dir = Path(site) / "benefits"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not force and not due(out_dir, now):
        log.info("지원사업 수집: %d시간이 안 지나 건너뜀", RUN_EVERY_HOURS)
        return None
    docs, files = build(key, now)
    changed = 0
    for name, items in files.items():
        items.sort(key=lambda d: d["id"])
        changed += write_if_changed(out_dir / f"{name}.json", items)
    index = {"updated": now.isoformat(timespec="seconds"), "schema": SCHEMA, "total": len(docs),
             "files": {name: len(items) for name, items in files.items()}}
    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    log.info("지원사업 게시: 파일 %d개 중 %d개 변경", len(files), changed)
    state_path = out_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    events = find_events(docs, state, now)
    result = {"events": len(events), "sent": 0, "failed": 0}
    if sender is not None and events:
        result["sent"], result["failed"] = send_events(events, sender)
    elif events:
        log.info("지원사업 푸시 대상 %d건 (보내는 곳이 없어 건너뜀)", len(events))
    write_if_changed(state_path, state)
    log.info("지원사업 알림: %s", result)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="site")
    ap.add_argument("--force", action="store_true", help="주기와 상관없이 지금 수집")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    env = collector.load_env()
    key = urllib.parse.unquote((env.get("DATA_GO_KR_KEY") or "").strip())  # collector와 같게 인코딩된 키도 허용
    if not key:
        raise SystemExit("DATA_GO_KR_KEY가 없습니다.")
    run(args.site, key, datetime.now(), args.force)


if __name__ == "__main__":
    main()
