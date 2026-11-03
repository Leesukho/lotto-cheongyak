"""최근 공고 알림 메시지 미리보기 (발송·CSV 기록 안 함). 예: python preview.py 3"""
import json
import sys

import main

env = main.load_env()
cfg = json.loads((main.ROOT / "config.json").read_text(encoding="utf-8"))
db = main.open_db()
prices = main.PriceLookup(env["DATA_GO_KR_KEY"], db)
count = int(sys.argv[1]) if len(sys.argv) > 1 else 1
for n in main.api_get("getRemndrLttotPblancDetail", env["DATA_GO_KR_KEY"])[:count]:
    models = main.match_models({}, env["DATA_GO_KR_KEY"], n)
    market = main.lookup_market(prices, n, models)
    print(main.build_message(n, models, market, main.build_eligibility(cfg, n, models)))
    print("-" * 40)
