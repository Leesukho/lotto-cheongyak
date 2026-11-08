"""과거 로또청약(무순위·계약취소 재공급) 전수 조사 → 차익 2억 이상 주택형의 분양가·시세·경쟁률 CSV (일회성)

  python research/lotto_history.py            전체 실행 (시세 조회 때문에 오래 걸림, 중간에 끊겨도 캐시로 이어서)
  python research/lotto_history.py --min-gap 20000

- 공고: 청약홈 무순위/잔여세대·취소후재공급 상세 + 주택형 (ApplyhomeInfoDetailSvc)
- 경쟁률: 청약홈 잔여세대·취소후재공급 경쟁률 (ApplyhomeInfoCmpetRtSvc)
- 시세: 국토부 실거래 6개월 중위값 (price.PriceLookup, 알림 프로그램과 같은 방식)
  당시시세 = 모집공고일 직전 6개월, 현재시세 = 오늘 직전 6개월. 목록은 당시차익으로 거른다.
"""
import argparse
import csv
import json
import os
import sqlite3
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from price import PriceLookup  # noqa: E402
from server import collector  # noqa: E402

OUT = Path(__file__).resolve().parent
CACHE = OUT / "cache"
API = "https://api.odcloud.kr/api"


def area(ty):
    try:
        return round(float(str(ty).strip().rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")), 2)
    except ValueError:
        return None


def num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def fetch_all(key, path, name):
    """전체 목록을 받아 cache/{name}.json에 저장 (있으면 재사용)"""
    f = CACHE / f"{name}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    rows, page = [], 1
    while True:
        qs = urllib.parse.urlencode({"page": page, "perPage": 1000, "serviceKey": key})
        d = collector.http_json(f"{API}/{path}?{qs}")
        rows += d.get("data", [])
        print(f"  {name}: {len(rows)}/{d.get('totalCount')}", flush=True)
        if not d.get("data") or len(rows) >= int(d.get("totalCount") or 0):
            break
        page += 1
    f.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-gap", type=int, default=20000, help="최소 차익(만원)")
    args = ap.parse_args()
    CACHE.mkdir(exist_ok=True)
    key = collector.load_env()["DATA_GO_KR_KEY"].strip()

    print("1) 공고·주택형·경쟁률 목록", flush=True)
    notices = fetch_all(key, "ApplyhomeInfoDetailSvc/v1/getRemndrLttotPblancDetail", "notices")
    models = fetch_all(key, "ApplyhomeInfoDetailSvc/v1/getRemndrLttotPblancMdl", "models")
    cmp_remain = fetch_all(key, "ApplyhomeInfoCmpetRtSvc/v1/getRemndrLttotPblancCmpet", "cmpet_remndr")
    cmp_cancel = fetch_all(key, "ApplyhomeInfoCmpetRtSvc/v1/getCancResplLttotPblancCmpet", "cmpet_cancel")

    by_notice = defaultdict(list)
    for m in models:
        by_notice[(m["HOUSE_MANAGE_NO"], m["PBLANC_NO"])].append(m)

    # 경쟁률: (관리번호, 공고번호, 전용면적) → [신청건수, 공급세대]. 무순위는 차수(유형코드)별 행을 합친다
    comp = defaultdict(lambda: [0.0, 0.0])
    for c in cmp_remain:
        k = (c["HOUSE_MANAGE_NO"], c["PBLANC_NO"], area(c.get("HOUSE_TY")))
        comp[k][0] += num(c.get("REQ_CNT")) or 0
        comp[k][1] += num(c.get("SUPLY_HSHLDCO")) or 0
    for c in cmp_cancel:
        k = (c["HOUSE_MANAGE_NO"], c["PBLANC_NO"], area(c.get("HOUSE_TY")))
        for f, v in c.items():  # 일반·신혼·생애최초·다자녀·노부모·기관추천… 유형별 칸을 모두 더한다
            if f.endswith("_REQ_CNT"):
                comp[k][0] += num(v) or 0
            elif f.endswith("_HSHLDCO"):
                comp[k][1] += num(v) or 0

    print(f"2) 시세 조회: 공고 {len(notices)}건 (캐시: research/cache/price.db)", flush=True)
    db = sqlite3.connect(CACHE / "price.db")
    prices = PriceLookup(key, db)
    rows, failed = [], 0
    started = time.time()
    for i, n in enumerate(notices, 1):
        ms = by_notice.get((n["HOUSE_MANAGE_NO"], n["PBLANC_NO"]), [])
        areas = sorted({a for a in (area(m.get("HOUSE_TY")) for m in ms) if a})
        then, now = {}, {}
        if areas:
            addr, name = n.get("HSSPLY_ADRES") or "", n.get("HOUSE_NM") or ""
            try:
                day = n.get("RCRIT_PBLANC_DE")
                then = prices.market(addr, name, areas, as_of=date.fromisoformat(day)) if day else {}
                now = prices.market(addr, name, areas)
            except Exception as ex:
                failed += 1
                print(f"   시세 실패 {n['HOUSE_NM']}: {str(ex)[:80]}", flush=True)
        for m in ms:
            a, price = area(m.get("HOUSE_TY")), num(m.get("LTTOT_TOP_AMOUNT"))
            mt, mn = then.get(a) or {}, now.get(a) or {}
            req, sup = comp.get((n["HOUSE_MANAGE_NO"], n["PBLANC_NO"], a), (None, None))
            rows.append({
                "모집공고일": n.get("RCRIT_PBLANC_DE"), "유형": n.get("HOUSE_SECD_NM"), "단지명": n.get("HOUSE_NM"),
                "시도": n.get("SUBSCRPT_AREA_CODE_NM"), "주소": n.get("HSSPLY_ADRES"),
                "주택형": str(m.get("HOUSE_TY", "")).strip(), "전용면적(㎡)": a,
                "공급세대": (num(m.get("SUPLY_HSHLDCO")) or 0) + (num(m.get("SPSPLY_HSHLDCO")) or 0),
                "분양가(만원)": int(price) if price else None,
                "당시시세(만원)": mt.get("price"), "당시시세기준": mt.get("basis"), "당시거래건수": mt.get("count"),
                "당시차익(만원)": int(mt["price"] - price) if mt and price else None,
                "현재시세(만원)": mn.get("price"), "현재시세기준": mn.get("basis"),
                "현재차익(만원)": int(mn["price"] - price) if mn and price else None,
                "신청건수": int(req) if req else None,
                "경쟁률": round(req / sup, 2) if req and sup else None,
                "접수시작": n.get("SUBSCRPT_RCEPT_BGNDE"), "당첨발표": n.get("PRZWNER_PRESNATN_DE"),
                "청약홈": n.get("PBLANC_URL"),
            })
        if i % 25 == 0:
            el = time.time() - started
            print(f"   {i}/{len(notices)} ({el/60:.1f}분, 남은 예상 {el/i*(len(notices)-i)/60:.0f}분)", flush=True)

    fields = list(rows[0].keys())
    stamp = datetime.now().strftime("%Y%m%d")
    all_path = OUT / f"로또청약_전체주택형_{stamp}.csv"
    with open(all_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    hits = sorted((r for r in rows if r["당시차익(만원)"] is not None and r["당시차익(만원)"] >= args.min_gap),
                  key=lambda r: -r["당시차익(만원)"])
    hit_path = OUT / f"로또청약_차익{args.min_gap // 10000}억이상_{stamp}.csv"
    with open(hit_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(hits)

    with_price = sum(1 for r in rows if r["분양가(만원)"])
    with_market = sum(1 for r in rows if r["당시시세(만원)"])
    with_comp = sum(1 for r in rows if r["경쟁률"] is not None)
    print(f"완료: 주택형 {len(rows)}개 (분양가 {with_price}, 시세 {with_market}, 경쟁률 {with_comp}), "
          f"차익 {args.min_gap}만원 이상 {len(hits)}개, 시세 실패 공고 {failed}건")
    print(f"공고일 범위: {min(n.get('RCRIT_PBLANC_DE') or '9' for n in notices)} ~ {max(n.get('RCRIT_PBLANC_DE') or '' for n in notices)}")
    print(all_path)
    print(hit_path)


if __name__ == "__main__":
    main()
