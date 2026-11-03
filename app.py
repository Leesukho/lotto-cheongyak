"""로또청약 알리미 설정 기능과 콘솔 메뉴 (개발용: python app.py).

배포용 로또청약.exe는 gui.py(창 + 트레이 아이콘)이며 이 모듈의 설정·자동 실행 기능을 쓴다.
첫 실행: 텔레그램 연결 → 내 조건 → 알림 조건 → 자동 실행 등록
이후 실행: 메뉴 (지금 확인 / 설정 변경 / 자동 실행 등록·해제 / 기록 폴더 열기)
"""
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse
import webbrowser
from pathlib import Path

import main

TASK_NAME = "LottoCheongyakAlert"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "LottoCheongyakTray"
NO_WINDOW = 0x08000000  # 창 없는 exe에서 schtasks를 부를 때 콘솔 창이 깜빡이지 않게
CONFIG_PATH = main.ROOT / "config.json"
ENV_PATH = main.ROOT / ".env"

# 청약홈 공고의 지역명(SUBSCRPT_AREA_CODE_NM) → 주민등록 주소의 시·도 이름
SIDO = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시", "인천": "인천광역시",
    "광주": "전남광주통합특별시", "대전": "대전광역시", "울산": "울산광역시", "세종": "세종특별자치시",
    "경기": "경기도", "강원": "강원특별자치도", "충북": "충청북도", "충남": "충청남도",
    "전북": "전북특별자치도", "전남": "전남광주통합특별시", "경북": "경상북도", "경남": "경상남도",
    "제주": "제주특별자치도",
}

DEFAULT_CONFIG = {
    "regions": [],
    "house_types": ["무순위", "불법행위 재공급"],
    "max_price_manwon": None,
    "min_area_m2": None,
    "max_area_m2": None,
    "include_keywords": [],
    "exclude_keywords": [],
    "min_gap_manwon": None,
    "notify_if_no_market": True,
    "my_profile": {},
    "hide_ineligible": False,
    "daily_report": True,
    "public_housing": {"enabled": True, "types": []},  # 마이홈 공공임대·공공분양 (types 비우면 전체)
}


# ---------- 입력 도우미 ----------

def read(prompt):
    return input(prompt).strip().lstrip("﻿")


def ask(prompt, default=None):
    suffix = f" [{default}]" if default not in (None, "") else ""
    value = read(f"{prompt}{suffix}: ")
    return value or (default if default is not None else "")


def ask_yn(prompt, default=None):
    """예 True / 아니오 False / 모름 None"""
    hint = {True: "Y", False: "N", None: "?"}[default]
    while True:
        v = read(f"{prompt} (y=예 / n=아니오 / ?=모름) [{hint}]: ").lower()
        if not v:
            return default
        if v in ("y", "예", "ㅛ"):
            return True
        if v in ("n", "아니오", "ㅜ"):
            return False
        if v in ("?", "모름"):
            return None


def ask_int(prompt, default=None):
    while True:
        v = ask(prompt, "" if default is None else str(default))
        if v == "":
            return None
        if v.replace(",", "").isdigit():
            return int(v.replace(",", ""))
        print("  숫자만 입력하세요 (비우면 제한 없음)")


def ask_sido(prompt, default=None):
    names = " ".join(SIDO)
    while True:
        v = ask(f"{prompt}\n  ({names})", default)
        if v in SIDO:
            return v
        print("  목록에 있는 이름으로 입력하세요")


# ---------- 텔레그램 연결 ----------

def telegram_link_url(env):
    """(연결 링크, 확인 코드). 사용자가 링크에서 [시작]을 누르면 봇에 '/start 코드'가 온다."""
    token = env.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("봇 토큰이 없습니다. 배포 파일이 올바른지 확인하세요.")
    bot = main.http_json(f"https://api.telegram.org/bot{token}/getMe")["result"]["username"]
    code = secrets.token_hex(4)
    return f"https://t.me/{bot}?start={code}", code


def wait_for_start(env, code, tries=100, cancelled=lambda: False):
    """'/start 코드'를 보낸 대화의 chat id. 시간 초과·취소면 None."""
    token = env["TELEGRAM_BOT_TOKEN"]
    for _ in range(tries):
        if cancelled():
            return None
        updates = main.http_json(f"https://api.telegram.org/bot{token}/getUpdates?limit=100")["result"]
        for u in updates:
            msg = u.get("message") or {}
            if msg.get("text", "").strip() == f"/start {code}":
                chat_id = str(msg["chat"]["id"])
                main.send_telegram({**env, "TELEGRAM_CHAT_ID": chat_id}, "✅ 로또청약 알리미에 연결되었습니다.")
                return chat_id
        time.sleep(3)
    return None


def link_telegram(env):
    try:
        url, code = telegram_link_url(env)
    except RuntimeError as ex:
        print(ex)
        return None
    print("\n[1/4] 텔레그램 연결")
    print("  텔레그램이 설치된 기기에서 아래 링크를 열고 [시작]을 누르세요.")
    print(f"  {url}")
    webbrowser.open(url)
    print("  기다리는 중... (최대 5분)")
    chat_id = wait_for_start(env, code)
    print("  연결 완료! 텔레그램에 확인 메시지를 보냈습니다." if chat_id else "  시간 초과. 다시 시도하세요.")
    return chat_id


# ---------- 설정 ----------

def setup_profile(cfg):
    p = cfg.get("my_profile") or {}
    print("\n[2/4] 내 조건 (신청 가능 여부 자가점검용, 모르면 ? 입력)")
    region = ask_sido("주민등록상 거주 시·도", p.get("region"))
    address = SIDO[region]
    if address.endswith("도"):
        old = (p.get("address") or "").split()[1:2]
        city = ask("  시·군 이름 (예: 오산시, 양평군)", old[0] if old else None)
        address = f"{address} {city}".strip()
    p.update({
        "region": region,
        "address": address,
        "householder": ask_yn("주민등록등본상 세대주인가요?", p.get("householder")),
        "homeless": ask_yn("세대원 전원이 무주택인가요? (분양권 포함)", p.get("homeless")),
        "special_eligible": ask_yn("특별공급(신혼부부·생애최초·다자녀·신생아·노부모) 자격이 있나요?",
                                   p.get("special_eligible")),
        "abroad_90d": ask_yn("현재 90일 넘게 계속 해외에 체류 중인가요?", p.get("abroad_90d", False)),
        "restricted": ask_yn("재당첨제한·부적격 당첨 제한 기간 중인가요? (청약홈 '청약제한사항 확인')",
                             p.get("restricted")),
    })
    cfg["my_profile"] = p


def setup_filters(cfg):
    print("\n[3/4] 알림 조건 (Enter = 기본값)")
    current = ",".join(cfg.get("regions") or [])
    v = ask("알림 받을 지역 (쉼표로 구분, 예: 서울,경기,인천 / 비우면 전국)", current)
    cfg["regions"] = [r.strip() for r in v.split(",") if r.strip() in SIDO]
    cfg["min_gap_manwon"] = ask_int("예상 차익 최소 금액(만원, 예: 10000 = 1억 / 비우면 제한 없음)",
                                    cfg.get("min_gap_manwon"))
    cfg["max_price_manwon"] = ask_int("최고 분양가 상한(만원, 비우면 제한 없음)", cfg.get("max_price_manwon"))
    cfg["hide_ineligible"] = bool(ask_yn("신청 불가(❌)로 판정된 공고는 숨길까요?", cfg.get("hide_ineligible", False)))
    cfg["daily_report"] = ask_yn("매일 아침 '정상 동작 중' 상태와 접수 일정을 받을까요?",
                                 cfg.get("daily_report", True)) is not False


def save(cfg, chat_id=None):
    main.ROOT.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    if chat_id:
        ENV_PATH.write_text(f"TELEGRAM_CHAT_ID={chat_id}\n", encoding="utf-8")


def load_config():
    if CONFIG_PATH.exists():
        return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}
    return json.loads(json.dumps(DEFAULT_CONFIG))


# ---------- 자동 실행 (작업 스케줄러) ----------

def background_command():
    if main.FROZEN:
        return str(Path(sys.executable).with_name("로또청약_백그라운드.exe")), ""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return str(pythonw), f'"{Path(__file__).with_name("main.py")}"'


TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>로또청약 알리미: 15분마다 신규 공고 확인</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Repetition><Interval>PT15M</Interval></Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{command}</Command><Arguments>{args}</Arguments><WorkingDirectory>{workdir}</WorkingDirectory></Exec>
  </Actions>
</Task>
"""


def tray_command():
    if main.FROZEN:
        return f'"{sys.executable}" --tray'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw}" "{Path(__file__).with_name("gui.py")}" --tray'


def set_tray_autostart(on):
    """로그인할 때 작업 표시줄 트레이 아이콘을 띄운다 (HKCU Run, 관리자 권한 불필요)."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, tray_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


def register_task():
    """(성공 여부, 메시지). 15분 확인 작업과 트레이 자동 표시를 함께 등록한다."""
    from xml.sax.saxutils import escape
    command, args = background_command()
    xml = TASK_XML.format(start=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + 60)),
                          command=escape(command), args=escape(args), workdir=escape(str(Path(command).parent)))
    path = Path(tempfile.gettempdir()) / "lotto_task.xml"
    path.write_text(xml, encoding="utf-16")
    r = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(path), "/F"],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    path.unlink(missing_ok=True)
    if r.returncode != 0:
        return False, f"등록 실패: {(r.stderr or r.stdout).strip()}"
    set_tray_autostart(True)
    return True, f"자동 실행 등록 완료: 15분마다 확인합니다.\n({command})"


def unregister_task():
    r = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    set_tray_autostart(False)
    return r.returncode == 0, "자동 실행 해제 완료" if r.returncode == 0 else "등록된 자동 실행이 없습니다."


def task_command():
    """등록된 작업이 실행하는 exe 경로. 등록이 없으면 None."""
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"],
                       capture_output=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        return None
    text = r.stdout.decode("utf-16", errors="ignore") if r.stdout[:2] == b"\xff\xfe" else r.stdout.decode("oem", errors="ignore")
    start = text.find("<Command>")
    return text[start + 9:text.find("</Command>")] if start >= 0 else ""


def task_registered():
    return task_command() is not None


# ---------- 실행 ----------

def run_now():
    print("\n공고 확인 중... (처음에는 시세 조회 때문에 1~2분 걸릴 수 있습니다)")
    try:
        sent = main.main()
    except SystemExit:
        sent = None
    if sent:
        print(f"  새 공고 {sent}건을 텔레그램으로 보냈습니다.")
    elif sent == 0:
        print("  정상 확인 완료: 새 공고가 없습니다.")
        print("  (이미 알린 공고는 다시 보내지 않습니다. 새 공고가 올라오면 자동으로 알려 드립니다.)")
    else:
        lines = (main.DATA / "app.log").read_text(encoding="utf-8").splitlines()
        print("  " + (lines[-1].split(" ", 3)[-1] if lines else "기록 없음"))


def last_check():
    if not main.DB_PATH.exists():
        return "없음"
    return main.meta_get(main.open_db(), "last_check", "없음")


def send_test():
    env = main.load_env()
    main.send_telegram(env, f"🔔 테스트 메시지: 연결 정상입니다. (마지막 자동 확인 {last_check()})")
    print("  텔레그램으로 테스트 메시지를 보냈습니다. 도착하지 않으면 '4. 텔레그램 다시 연결'을 해 주세요.")


def first_setup():
    print("=" * 50)
    print(" 로또청약 알리미 설정을 시작합니다")
    print(" 무순위·계약취소 재공급 공고를 텔레그램으로 알려 드립니다.")
    print("=" * 50)
    env = main.load_env()
    chat_id = link_telegram(env)
    if not chat_id:
        return False
    cfg = load_config()
    setup_profile(cfg)
    setup_filters(cfg)
    save(cfg, chat_id)
    print("\n[4/4] 자동 실행")
    if ask_yn("PC가 켜져 있는 동안 15분마다 자동으로 확인할까요?", True):
        print("  " + register_task()[1])
    if ask_yn("지금 바로 한 번 확인할까요? (접수 중인 공고를 보내 드립니다)", True):
        run_now()
    return True


def menu():
    while True:
        status = "등록됨" if task_registered() else "꺼짐"
        print(f"\n===== 로또청약 알리미 (자동 실행: {status} · 마지막 확인: {last_check()}) =====")
        print(" 1. 지금 공고 확인")
        print(" 2. 내 조건 변경")
        print(" 3. 알림 조건 변경")
        print(" 4. 텔레그램 다시 연결")
        print(" 5. 자동 실행 등록")
        print(" 6. 자동 실행 해제")
        print(" 7. 알림 기록(CSV) 폴더 열기")
        print(" 8. 텔레그램 테스트 메시지 보내기")
        print(" 0. 종료")
        choice = read("선택: ")
        cfg = load_config()
        if choice == "1":
            run_now()
        elif choice == "2":
            setup_profile(cfg)
            save(cfg)
        elif choice == "3":
            setup_filters(cfg)
            save(cfg)
        elif choice == "4":
            chat_id = link_telegram(main.load_env())
            if chat_id:
                save(cfg, chat_id)
        elif choice == "5":
            print("  " + register_task()[1])
        elif choice == "6":
            print("  " + unregister_task()[1])
        elif choice == "7":
            os.startfile(main.DATA)
        elif choice == "8":
            send_test()
        elif choice == "0":
            return


def run():
    configured = CONFIG_PATH.exists() and main.load_env().get("TELEGRAM_CHAT_ID")
    if configured or first_setup():
        menu()
    else:
        input("\n설정을 마치지 못했습니다. Enter를 누르면 종료합니다.")


if __name__ == "__main__":
    try:
        run()
    except (KeyboardInterrupt, EOFError):
        pass
    except Exception as ex:
        main.log.exception("설정 프로그램 오류")
        input(f"\n오류가 발생했습니다: {ex}\nEnter를 누르면 종료합니다.")
