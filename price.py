"""공고 주소 기준 주변 시세(국토부 실거래가)를 조회한다.

시세 우선순위 (전용면적 ±AREA_TOL㎡):
  1. 동일 단지 실거래 (매매 또는 분양권)
  2. 같은 법정동 → 같은 시군구의 신축(NEW_YEARS년 이내) 유사평형 매매
  3. 같은 법정동 → 같은 시군구의 전체 유사평형 매매
"""
import json
import re
import statistics
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

REGION_URL = "https://apis.data.go.kr/1741000/StanReginCd/getStanReginCdList"
APT_TRADE_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
SILV_TRADE_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcSilvTrade/getRTMSDataSvcSilvTrade"
MONTHS = 6       # 최근 몇 개월 거래를 볼지
AREA_TOL = 5.0   # 유사평형 기준 (전용면적 차이 ㎡)
MIN_COUNT = 3    # 법정동 거래가 이보다 적으면 시군구로 넓힌다
NEW_YEARS = 10   # 준공 몇 년 이내를 '신축'으로 보고 우선 비교할지


class SqliteCache:
    """PC 프로그램용 캐시 (seen.db의 cache 테이블). 서버는 같은 get/set을 가진 Firestore 캐시를 쓴다."""

    def __init__(self, db):
        self.db = db
        db.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT, at TEXT)")

    def get(self, k):
        """(값, 저장 시각) 또는 None"""
        row = self.db.execute("SELECT v, at FROM cache WHERE k=?", (k,)).fetchone()
        return (json.loads(row[0]), datetime.fromisoformat(row[1])) if row else None

    def set(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                        (k, json.dumps(v, ensure_ascii=False), datetime.now().isoformat()))
        self.db.commit()


class PriceLookup:
    def __init__(self, key, cache):
        """cache: get/set을 가진 캐시 객체, 또는 sqlite 연결(SqliteCache로 감싼다)"""
        self.key = key
        self.cache = cache if hasattr(cache, "get") else SqliteCache(cache)

    # ---------- 공통 ----------

    def _get(self, url, params, retries=3):
        qs = urllib.parse.urlencode({"serviceKey": self.key, **params})
        for i in range(retries):  # 국토부 API는 가끔 503을 낸다
            try:
                with urllib.request.urlopen(f"{url}?{qs}", timeout=30) as r:
                    return r.read().decode("utf-8")
            except Exception:
                if i == retries - 1:
                    raise
                time.sleep(3 * (i + 1))

    def _cached(self, k, max_age_days, fetch):
        hit = self.cache.get(k)
        if hit and datetime.now() - hit[1] < timedelta(days=max_age_days):
            return hit[0]
        v = fetch()
        self.cache.set(k, v)
        return v

    # ---------- 주소 → 시군구 코드, 법정동 ----------

    def region(self, address):
        """(시군구코드 5자리, 시군구명, 법정동명 또는 None)"""
        tokens = address.split()
        query = " ".join(tokens[:3] if len(tokens) > 2 and tokens[2].endswith("구") else tokens[:2])
        rows = self._cached(f"region:{query}", 90, lambda: self._region_rows(query))
        sgg = [r for r in rows if r["umd_cd"] == "000" and r["ri_cd"] == "00"
               and address.startswith(r["locatadd_nm"])]
        if not sgg:
            return None
        best = max(sgg, key=lambda r: len(r["locatadd_nm"]))
        rest = address[len(best["locatadd_nm"]):]
        dongs = {r["locatadd_nm"].split()[-1] for r in rows
                 if r["region_cd"][:5] == best["region_cd"][:5] and r["umd_cd"] != "000"}
        m = re.search(r"\((\S+?[동읍면가리])\)", rest)  # 도로명 주소의 (동패동)
        cands = [m.group(1)] if m else []
        cands += re.findall(r"\S+?[동읍면가]", rest.split("번지")[0])
        umd = next((c for c in cands if c in dongs), None)
        return best["region_cd"][:5], best["locatadd_nm"].split()[-1], umd

    def _region_rows(self, query):
        res = json.loads(self._get(REGION_URL, {
            "type": "json", "pageNo": 1, "numOfRows": 1000, "locatadd_nm": query}))
        rows = []
        for part in res.get("StanReginCd", []):
            rows += part.get("row", [])
        return [{k: r[k] for k in ("region_cd", "umd_cd", "ri_cd", "locatadd_nm")} for r in rows]

    # ---------- 실거래 ----------

    def _trades(self, url, lawd, ym):
        def fetch_items():
            items, page = [], 1
            while True:
                root = ET.fromstring(self._get(url, {"LAWD_CD": lawd, "DEAL_YMD": ym,
                                                     "numOfRows": 1000, "pageNo": page}))
                code = root.findtext(".//resultCode")
                if code not in ("00", "000"):
                    raise RuntimeError(f"실거래가 API 오류 {code}: {root.findtext('.//resultMsg')}")
                items += root.iter("item")
                if len(items) >= int(root.findtext(".//totalCount") or 0) or page >= 20:
                    return items
                page += 1

        def fetch():
            out = []
            for it in fetch_items():
                g = lambda t: (it.findtext(t) or "").strip()
                if g("cdealType"):  # 해제된 거래 제외
                    continue
                try:
                    out.append({"name": g("aptNm"), "umd": g("umdNm"), "area": float(g("excluUseAr")),
                                "built": int(g("buildYear") or 0),
                                "price": int(g("dealAmount").replace(",", "")),
                                "date": f"{g('dealYear')}-{int(g('dealMonth')):02d}-{int(g('dealDay')):02d}"})
                except ValueError:
                    continue
            return out
        current = ym == date.today().strftime("%Y%m")
        return self._cached(f"{url.rsplit('/', 1)[-1]}:{lawd}:{ym}", 1 if current else 7, fetch)

    def _recent(self, url, lawd, as_of=None):
        """as_of(기준일)부터 거슬러 MONTHS개월 거래. 기본은 오늘."""
        base = as_of or date.today()
        y, m = base.year, base.month
        out = []
        for _ in range(MONTHS):
            out += self._trades(url, lawd, f"{y}{m:02d}")
            y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        return out

    # ---------- 시세 추정 ----------

    def market(self, address, house_name, areas, as_of=None):
        """전용면적별 시세 dict: {area: {"price", "count", "basis"}}
        as_of를 주면 그 날짜 기준 직전 MONTHS개월 거래로 계산한다 (과거 공고 당시 시세)."""
        reg = self.region(address)
        if not reg:
            return {}
        lawd, sgg_name, umd = reg
        apt = self._recent(APT_TRADE_URL, lawd, as_of)
        try:
            silv = self._recent(SILV_TRADE_URL, lawd, as_of)
        except Exception:
            silv = []
        if as_of:  # 기준일 이후 거래는 뺀다 (기준 달의 나머지 날짜)
            cut = as_of.isoformat()
            apt = [t for t in apt if t["date"] <= cut]
            silv = [t for t in silv if t["date"] <= cut]
        target = _norm(house_name)
        same = [t for t in apt + silv if _same_complex(_norm(t["name"]), target)]

        new_year = (as_of or date.today()).year - NEW_YEARS
        dong = [t for t in apt if umd and t["umd"] == umd]
        newer = lambda ts: [t for t in ts if t["built"] >= new_year]
        result = {}
        for area in areas:
            near = lambda ts: [t for t in ts if abs(t["area"] - area) <= AREA_TOL]
            for basis, ts in (("동일단지", near(same)),
                              (f"{umd} 신축 유사평형", near(newer(dong))),
                              (f"{sgg_name} 신축 유사평형", near(newer(apt))),
                              (f"{umd} 유사평형", near(dong)),
                              (f"{sgg_name} 유사평형", near(apt))):
                if (basis == "동일단지" and ts) or len(ts) >= MIN_COUNT:
                    result[area] = {"price": int(statistics.median(t["price"] for t in ts)),
                                    "count": len(ts), "basis": basis}
                    break
        return result


def _norm(name):
    return re.sub(r"\(.*?\)|\s|아파트|[0-9]+차$", "", name or "")


def _same_complex(a, b):
    return len(a) >= 4 and len(b) >= 4 and (a in b or b in a)
