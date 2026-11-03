"""봇에게 보낸 메시지에서 Chat ID를 찾아 .env에 저장하고 테스트 메시지를 보낸다."""
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ENV = Path(__file__).parent / ".env"


def env_value(text, key):
    m = re.search(rf"^{key}=(.*)$", text, re.M)
    return m.group(1).strip() if m else ""


def api(token, method, **params):
    data = urllib.parse.urlencode(params).encode() if params else None
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    text = ENV.read_text(encoding="utf-8")
    token = env_value(text, "TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit(".env 파일의 TELEGRAM_BOT_TOKEN이 비어 있습니다.")
    updates = api(token, "getUpdates").get("result", [])
    chats = [u["message"]["chat"]["id"] for u in updates if "message" in u]
    if not chats:
        sys.exit("봇이 받은 메시지가 없습니다. 봇에게 아무 메시지나 보낸 뒤 다시 실행하세요.")
    chat_id = str(chats[-1])
    text = re.sub(r"^TELEGRAM_CHAT_ID=.*$", f"TELEGRAM_CHAT_ID={chat_id}", text, flags=re.M)
    ENV.write_text(text, encoding="utf-8")
    api(token, "sendMessage", chat_id=chat_id, text="✅ 로또청약 알리미 연결 완료!")
    print("Chat ID 저장 및 테스트 메시지 발송 완료")


if __name__ == "__main__":
    main()
