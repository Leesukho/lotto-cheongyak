"""청약홈 API 응답 구조 확인용 스크립트 (키는 출력하지 않음)."""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"
ENDPOINTS = {
    "apt": "getAPTLttotPblancDetail",          # APT 일반 분양
    "remndr": "getRemndrLttotPblancDetail",    # 무순위/잔여세대/취소후재공급
    "remndr_mdl": "getRemndrLttotPblancMdl",   # 무순위 주택형별
}


def load_env():
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def fetch(op, key, per_page=20):
    qs = urllib.parse.urlencode({"page": 1, "perPage": per_page, "serviceKey": key})
    with urllib.request.urlopen(f"{BASE}/{op}?{qs}", timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    key = load_env().get("DATA_GO_KR_KEY", "")
    if not key:
        sys.exit(".env 파일의 DATA_GO_KR_KEY가 비어 있습니다.")
    out = ROOT / "data"
    out.mkdir(exist_ok=True)
    for name, op in ENDPOINTS.items():
        try:
            res = fetch(op, key)
        except urllib.error.HTTPError as e:
            print(f"[{name}] HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
            continue
        (out / f"sample_{name}.json").write_text(
            json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        items = res.get("data", [])
        print(f"[{name}] totalCount={res.get('totalCount')} 받은건수={len(items)}")
        if items:
            print("  필드:", ", ".join(items[0].keys()))


if __name__ == "__main__":
    main()
