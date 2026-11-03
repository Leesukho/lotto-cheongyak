"""로또청약 알리미 GUI (배포용 로또청약.exe 진입점).

- 작업 표시줄 오른쪽 아래(트레이)에 동작 상태 아이콘을 띄운다.
  초록=정상 · 노랑=확인 지연/점검 필요 · 빨강=확인 실패 · 회색=설정 전/자동 실행 꺼짐 · 파랑=확인 중
- 아이콘을 더블클릭하면 설정 창이 열린다. 창을 닫아도 트레이에 남는다.
- 15분마다 하는 실제 확인은 작업 스케줄러의 로또청약_백그라운드.exe가 맡는다.
  트레이를 종료해도 자동 확인은 계속된다.

  로또청약.exe          트레이 + 설정 창
  로또청약.exe --tray   트레이만 (로그인할 때 자동 실행)
"""
import base64
import ctypes
import io
import os
import queue
import socket
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import pystray
from PIL import Image, ImageDraw

import app
import main
import myhome

APP_NAME = "로또청약 알리미"
INSTANCE_PORT = 47815   # 두 번째 실행은 이 포트로 첫 실행에 '창 열기'를 요청하고 끝낸다
POLL_MS = 30_000        # 상태 갱신 주기
STALE_MIN = 35          # 마지막 확인이 이보다 오래되면 '확인 지연' (15분 주기 + 여유)
FONT = ("Malgun Gothic", 10)

COLORS = {"green": "#22a55b", "yellow": "#e9a800", "red": "#e0453a", "gray": "#8a8f98", "blue": "#3b82f6"}
YN = {True: "예", False: "아니오", None: "모름"}
PROFILE_QUESTIONS = [
    ("householder", "주민등록등본상 세대주인가요?"),
    ("homeless", "세대원 전원이 무주택인가요? (분양권 포함)"),
    ("special_eligible", "특별공급(신혼·생애최초·다자녀·신생아·노부모) 자격이 있나요?"),
    ("abroad_90d", "현재 90일 넘게 계속 해외에 체류 중인가요?"),
    ("restricted", "재당첨제한·부적격 당첨 제한 기간 중인가요?"),
]


# ---------- 상태 ----------

def read_status():
    env = main.load_env()
    db = main.open_db()
    try:
        return {
            "configured": app.CONFIG_PATH.exists() and bool(env.get("TELEGRAM_CHAT_ID")),
            "task": app.task_command(),
            "last_check": main.meta_get(db, "last_check"),
            "fails": int(main.meta_get(db, "fail_count", 0)),
            "total_sent": int(main.meta_get(db, "total_sent", 0)),
            "public": main.meta_get(db, "public_status", "아직 확인 전"),
        }
    finally:
        db.close()


def evaluate(s, busy):
    """(색, 한 줄 설명)"""
    if busy:
        return "blue", "공고 확인 중..."
    if not s["configured"]:
        return "gray", "설정이 필요합니다"
    if s["task"] is None:
        return "gray", "자동 실행 꺼짐"
    if not Path(s["task"]).exists():
        return "red", "자동 실행 파일을 찾을 수 없음 (자동 실행 다시 켜기)"
    if main.FROZEN and Path(s["task"]).parent != Path(sys.executable).parent:
        return "yellow", "다른 폴더의 프로그램으로 자동 실행 중 (자동 실행 다시 켜기)"
    if s["fails"]:
        return "red", f"확인 실패 {s['fails']}회 연속 (인터넷 연결 확인)"
    if not s["last_check"]:
        return "yellow", "아직 확인 기록이 없습니다"
    minutes = (datetime.now() - datetime.strptime(s["last_check"], "%Y-%m-%d %H:%M")).total_seconds() / 60
    if minutes > STALE_MIN:
        return "yellow", f"확인 지연: 마지막 확인 {s['last_check'][5:]}"
    return "green", "정상 동작 중"


def make_icon(color):
    """색 원 위에 흰 집 모양."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=COLORS[color])
    d.polygon([(32, 13), (51, 30), (13, 30)], fill="white")
    d.rectangle((19, 29, 45, 49), fill="white")
    d.rectangle((28, 37, 36, 49), fill=COLORS[color])
    return img


def run_check():
    try:
        return main.main()
    except SystemExit:
        return None


def last_log_line():
    path = main.DATA / "app.log"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return lines[-1].split(" ", 3)[-1] if lines else "기록 없음"


# ---------- 화면 ----------

class App:
    def __init__(self, show_window):
        self.q = queue.Queue()
        self.busy = False
        self.notified_hide = False
        self.link_cancel = threading.Event()

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("580x600")
        self.root.minsize(540, 560)
        self.root.option_add("*Font", FONT)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.build()

        # 아이콘이 화면에 뜨는 도중에 이미지를 바꾸면 반영되지 않을 수 있어 첫 색을 미리 정한다
        self.status = read_status()
        self.color, self.state_text = evaluate(self.status, False)
        self.set_window_icon()
        self.icon = pystray.Icon("LottoCheongyak", make_icon(self.color), APP_NAME, pystray.Menu(
            pystray.MenuItem(lambda _: self.state_text, None, enabled=False),
            pystray.MenuItem("열기", lambda: self.post(self.show), default=True),
            pystray.MenuItem("지금 확인", lambda: self.post(self.check_now)),
            pystray.MenuItem("텔레그램 테스트 메시지", lambda: self.post(self.send_test)),
            pystray.MenuItem("알림 기록 폴더", lambda: self.post(lambda: os.startfile(main.DATA))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("트레이 종료 (자동 확인은 계속됨)", lambda: self.post(self.quit)),
        ))
        threading.Thread(target=self.icon.run, daemon=True).start()

        self.root.after(100, self.pump)
        self.refresh()
        if show_window or not self.status["configured"]:
            self.show()
        else:
            self.root.withdraw()

    # --- 스레드 간 전달: 트레이·작업 스레드는 큐에 넣고, tk 스레드가 꺼내 실행 ---
    def post(self, fn):
        self.q.put(fn)

    def pump(self):
        while not self.q.empty():
            try:
                self.q.get_nowait()()
            except Exception:
                main.log.exception("GUI 처리 오류")
        self.root.after(100, self.pump)

    def background(self, fn, done):
        def work():
            try:
                result, error = fn(), None
            except Exception as ex:
                result, error = None, ex
            self.post(lambda: done(result, error))
        threading.Thread(target=work, daemon=True).start()

    # --- 구성 ---
    def build(self):
        self.banner = ttk.Label(self.root, foreground="#b45309", wraplength=520, padding=(12, 8, 12, 0))
        self.banner.pack(fill="x")
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=10)
        self.build_status(self.add_tab("상태"))
        self.build_profile(self.add_tab("내 조건"))
        self.build_filters(self.add_tab("알림 조건"))
        self.build_telegram(self.add_tab("텔레그램"))

    def add_tab(self, title):
        frame = ttk.Frame(self.tabs, padding=14)
        self.tabs.add(frame, text=f"  {title}  ")
        return frame

    def build_status(self, f):
        top = ttk.Frame(f)
        top.pack(fill="x")
        self.dot = tk.Canvas(top, width=28, height=28, highlightthickness=0)
        self.dot.pack(side="left")
        self.state_label = ttk.Label(top, font=(FONT[0], 12, "bold"), wraplength=460)
        self.state_label.pack(side="left", padx=8)

        grid = ttk.Frame(f, padding=(0, 14))
        grid.pack(fill="x")
        self.info = {}
        for i, (key, name) in enumerate([("task", "자동 실행"), ("last", "마지막 확인"),
                                         ("sent", "보낸 알림"), ("public", "공공주택"), ("tg", "텔레그램")]):
            ttk.Label(grid, text=name, foreground="#666").grid(row=i, column=0, sticky="nw", pady=3)
            self.info[key] = ttk.Label(grid, wraplength=400)
            self.info[key].grid(row=i, column=1, sticky="w", padx=12, pady=3)

        buttons = ttk.Frame(f)
        buttons.pack(fill="x", pady=4)
        self.check_btn = ttk.Button(buttons, text="지금 확인", command=self.check_now)
        self.check_btn.grid(row=0, column=0, sticky="ew", padx=3, pady=3)
        self.task_btn = ttk.Button(buttons, text="자동 실행 켜기", command=self.toggle_task)
        self.task_btn.grid(row=0, column=1, sticky="ew", padx=3, pady=3)
        ttk.Button(buttons, text="텔레그램 테스트 메시지", command=self.send_test).grid(
            row=1, column=0, sticky="ew", padx=3, pady=3)
        ttk.Button(buttons, text="알림 기록(CSV) 폴더", command=lambda: os.startfile(main.DATA)).grid(
            row=1, column=1, sticky="ew", padx=3, pady=3)
        buttons.columnconfigure((0, 1), weight=1)

        self.result = ttk.Label(f, wraplength=500, padding=(0, 12))
        self.result.pack(fill="x")
        ttk.Label(f, foreground="#666", wraplength=500, text=(
            "작업 표시줄 오른쪽 아래 아이콘 색: 초록 정상 · 노랑 확인 지연 · 빨강 실패 · 회색 꺼짐\n"
            "창을 닫아도 아이콘은 남아 있습니다. PC가 켜져 있는 동안 15분마다 자동 확인합니다.")
                  ).pack(side="bottom", fill="x")

    def build_profile(self, f):
        p = app.load_config().get("my_profile") or {}
        ttk.Label(f, text="신청 가능 여부 자가점검에 씁니다. 모르면 '모름'을 고르세요.",
                  foreground="#666").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ttk.Label(f, text="주민등록상 거주 시·도").grid(row=1, column=0, sticky="w", pady=4)
        self.region = ttk.Combobox(f, values=list(app.SIDO), state="readonly", width=12)
        self.region.set(p.get("region", ""))
        self.region.grid(row=1, column=1, sticky="w", pady=4)
        self.region.bind("<<ComboboxSelected>>", lambda _: self.update_city())
        ttk.Label(f, text="시·군 (도 지역만, 예: 오산시)").grid(row=2, column=0, sticky="w", pady=4)
        self.city = ttk.Entry(f, width=14)
        parts = (p.get("address") or "").split()
        self.city.insert(0, parts[1] if len(parts) > 1 else "")
        self.city.grid(row=2, column=1, sticky="w", pady=4)
        self.answers = {}
        for i, (key, question) in enumerate(PROFILE_QUESTIONS, start=3):
            ttk.Label(f, text=question, wraplength=330).grid(row=i, column=0, sticky="w", pady=4)
            box = ttk.Combobox(f, values=list(YN.values()), state="readonly", width=8)
            box.set(YN[p.get(key, False if key == "abroad_90d" else None)])
            box.grid(row=i, column=1, sticky="w", pady=4)
            self.answers[key] = box
        ttk.Button(f, text="저장", command=self.save_profile).grid(row=20, column=1, sticky="e", pady=14)
        f.columnconfigure(0, weight=1)
        self.update_city()

    def build_filters(self, f):
        cfg = app.load_config()
        ttk.Label(f, text="알림 받을 지역 (아무것도 고르지 않으면 전국)").pack(anchor="w")
        grid = ttk.Frame(f, padding=(0, 6))
        grid.pack(fill="x")
        self.region_vars = {}
        for i, name in enumerate(app.SIDO):
            var = tk.BooleanVar(value=name in (cfg.get("regions") or []))
            ttk.Checkbutton(grid, text=name, variable=var).grid(row=i // 6, column=i % 6, sticky="w", padx=4, pady=2)
            self.region_vars[name] = var

        nums = ttk.Frame(f, padding=(0, 10))
        nums.pack(fill="x")
        self.min_gap = self.number_row(nums, 0, "예상 차익 최소 (만원, 예: 10000 = 1억)", cfg.get("min_gap_manwon"))
        self.max_price = self.number_row(nums, 1, "최고 분양가 상한 (만원)", cfg.get("max_price_manwon"))
        ttk.Label(nums, text="비우면 제한 없음", foreground="#666").grid(row=2, column=0, sticky="w")

        self.hide_var = tk.BooleanVar(value=cfg.get("hide_ineligible", False))
        ttk.Checkbutton(f, text="신청 불가(❌)로 판정된 공고는 알리지 않기", variable=self.hide_var).pack(anchor="w", pady=2)
        self.report_var = tk.BooleanVar(value=cfg.get("daily_report", True))
        ttk.Checkbutton(f, text="매일 아침 '정상 동작 중' 상태와 접수 일정 받기", variable=self.report_var).pack(anchor="w", pady=2)

        ph = cfg.get("public_housing") or {}
        box = ttk.LabelFrame(f, text=" 공공주택 (LH·SH·GH 등, 마이홈포털) ", padding=8)
        box.pack(fill="x", pady=(10, 0))
        self.public_var = tk.BooleanVar(value=ph.get("enabled", True))
        ttk.Checkbutton(box, text="공공임대·공공분양 공고 받기 (위 알림 지역 적용)", variable=self.public_var,
                        command=self.update_public).grid(row=0, column=0, columnspan=4, sticky="w")
        self.public_types = {}
        self.public_checks = []
        for i, name in enumerate(myhome.TYPE_NAMES):
            var = tk.BooleanVar(value=name in (ph.get("types") or []))
            check = ttk.Checkbutton(box, text=name, variable=var)
            check.grid(row=1 + i // 4, column=i % 4, sticky="w", padx=4, pady=2)
            self.public_types[name] = var
            self.public_checks.append(check)
        ttk.Label(box, text="유형을 하나도 고르지 않으면 전체", foreground="#666").grid(
            row=4, column=0, columnspan=4, sticky="w")
        self.update_public()
        ttk.Button(f, text="저장", command=self.save_filters).pack(anchor="e", pady=10)

    def update_public(self):
        for check in self.public_checks:
            check.config(state="normal" if self.public_var.get() else "disabled")

    def number_row(self, parent, row, label, value):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        entry = ttk.Entry(parent, width=12)
        entry.insert(0, "" if value is None else str(value))
        entry.grid(row=row, column=1, sticky="w", padx=10, pady=3)
        return entry

    def build_telegram(self, f):
        self.tg_state = ttk.Label(f, font=(FONT[0], 11, "bold"))
        self.tg_state.pack(anchor="w")
        ttk.Label(f, foreground="#666", wraplength=500, padding=(0, 8), text=(
            "[연결하기]를 누르면 텔레그램 링크가 열립니다. 봇 화면에서 [시작]을 누르면 자동으로 연결됩니다.\n"
            "링크가 열리지 않으면 아래 주소를 복사해 휴대폰 텔레그램에서 여세요.")).pack(fill="x")
        self.tg_url = ttk.Entry(f, state="readonly")
        self.tg_url.pack(fill="x", pady=6)
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.link_btn = ttk.Button(row, text="연결하기", command=self.link)
        self.link_btn.pack(side="left")
        self.cancel_btn = ttk.Button(row, text="취소", command=self.link_cancel.set, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.tg_result = ttk.Label(f, wraplength=500, padding=(0, 10))
        self.tg_result.pack(fill="x")

    # --- 상태 갱신 ---
    def refresh(self, schedule=True):
        try:
            prev = self.status
            self.status = read_status()
            self.apply_status(prev)
        except Exception:
            main.log.exception("상태 읽기 실패")
        if schedule:
            self.root.after(POLL_MS, self.refresh)

    def apply_status(self, prev=None):
        s = self.status
        color, text = evaluate(s, self.busy)
        self.state_text = text
        if color != self.color:
            if color == "red":
                self.icon.notify(text, APP_NAME)
            self.color = color
            self.icon.icon = make_icon(color)
            self.set_window_icon()
        if prev and s["total_sent"] > prev["total_sent"]:
            self.icon.notify(f"새 공고 {s['total_sent'] - prev['total_sent']}건을 텔레그램으로 보냈습니다.", APP_NAME)
        last = s["last_check"] or "없음"
        self.icon.title = f"{APP_NAME}: {text}\n마지막 확인 {last}"[:127]
        self.icon.update_menu()

        self.dot.delete("all")
        self.dot.create_oval(3, 3, 25, 25, fill=COLORS[color], outline="")
        self.state_label.config(text=text)
        self.info["task"].config(text=f"켜짐 · 15분마다 확인\n{s['task']}" if s["task"] else "꺼짐")
        self.info["last"].config(text=last)
        self.info["sent"].config(text=f"{s['total_sent']}건 (v1.2 이후 누적)")
        self.info["public"].config(text=s["public"], foreground="#c0392b" if s["public"].startswith("오류") else "")
        self.info["tg"].config(text="연결됨" if s["configured"] or main.load_env().get("TELEGRAM_CHAT_ID") else "연결 안 됨")
        self.task_btn.config(text="자동 실행 켜기" if not s["task"]
                             else "자동 실행 다시 켜기" if color in ("yellow", "red") else "자동 실행 끄기")
        linked = bool(main.load_env().get("TELEGRAM_CHAT_ID"))
        self.tg_state.config(text="✅ 연결됨" if linked else "연결 안 됨")
        self.link_btn.config(text="다시 연결하기" if linked else "연결하기")
        self.update_banner()

    def set_window_icon(self):
        buf = io.BytesIO()
        make_icon(self.color).save(buf, "PNG")
        self.window_icon = tk.PhotoImage(data=base64.b64encode(buf.getvalue()))
        self.root.iconphoto(False, self.window_icon)

    def update_banner(self):
        s = self.status
        if not main.load_env().get("TELEGRAM_CHAT_ID"):
            msg = "처음 설정: ① 텔레그램 탭에서 연결 → ② 내 조건 저장 → ③ 알림 조건 저장 → ④ 상태 탭에서 자동 실행 켜기"
        elif s["task"] is None:
            msg = "설정을 마친 뒤 상태 탭에서 [자동 실행 켜기]를 눌러야 15분마다 확인합니다."
        else:
            msg = ""
        self.banner.config(text=msg)
        if msg:
            self.banner.pack(fill="x", before=self.tabs)
        else:
            self.banner.pack_forget()

    # --- 동작 ---
    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        if not main.load_env().get("TELEGRAM_CHAT_ID"):
            self.tabs.select(3)

    def hide(self):
        self.root.withdraw()
        if not self.notified_hide:
            self.notified_hide = True
            self.icon.notify("창을 닫아도 오른쪽 아래 아이콘으로 상태를 계속 표시합니다.", APP_NAME)

    def quit(self):
        self.link_cancel.set()
        self.icon.stop()
        self.root.destroy()

    def say(self, text):
        self.result.config(text=text)

    def check_now(self):
        if self.busy:
            return
        if not self.status["configured"]:
            self.show()
            messagebox.showinfo(APP_NAME, "먼저 텔레그램 연결과 조건 설정을 마쳐 주세요.")
            return
        self.busy = True
        self.check_btn.config(state="disabled")
        self.say("공고 확인 중... (처음에는 시세 조회 때문에 1~2분 걸릴 수 있습니다)")
        self.apply_status()
        self.background(run_check, self.check_done)

    def check_done(self, sent, error):
        self.busy = False
        self.check_btn.config(state="normal")
        if sent:
            text = f"새 공고 {sent}건을 텔레그램으로 보냈습니다."
        elif sent == 0:
            text = "정상 확인 완료: 새 공고가 없습니다. (이미 알린 공고는 다시 보내지 않습니다)"
        else:
            text = f"확인 실패: {error or last_log_line()}"
        self.say(text)
        self.icon.notify(text, APP_NAME)
        self.refresh(schedule=False)

    def send_test(self):
        def work():
            env = main.load_env()
            main.send_telegram(env, f"🔔 테스트 메시지: 연결 정상입니다. (마지막 자동 확인 {self.status['last_check'] or '없음'})")

        def done(_, error):
            text = (f"테스트 메시지 전송 실패: {error}" if error
                    else "텔레그램으로 테스트 메시지를 보냈습니다. 도착하지 않으면 텔레그램 탭에서 다시 연결하세요.")
            self.say(text)
            self.icon.notify(text, APP_NAME)

        if not main.load_env().get("TELEGRAM_CHAT_ID"):
            self.show()
            messagebox.showinfo(APP_NAME, "먼저 텔레그램 탭에서 연결해 주세요.")
            return
        self.background(work, done)

    def toggle_task(self):
        healthy = self.status["task"] and self.color not in ("yellow", "red")
        if healthy:
            if not messagebox.askyesno(APP_NAME, "자동 실행을 끄면 새 공고 알림이 오지 않습니다. 끌까요?"):
                return
            ok, msg = app.unregister_task()
        else:
            if not self.status["configured"]:
                messagebox.showinfo(APP_NAME, "먼저 텔레그램 연결과 조건 설정을 마쳐 주세요.")
                return
            ok, msg = app.register_task()
        self.say(msg)
        if not ok:
            messagebox.showerror(APP_NAME, msg)
        self.refresh(schedule=False)

    def update_city(self):
        is_do = app.SIDO.get(self.region.get(), "").endswith("도")
        self.city.config(state="normal" if is_do else "disabled")

    def save_profile(self):
        region = self.region.get()
        if region not in app.SIDO:
            messagebox.showwarning(APP_NAME, "거주 시·도를 골라 주세요.")
            return
        address = app.SIDO[region]
        if address.endswith("도"):
            city = self.city.get().strip()
            if not city:
                messagebox.showwarning(APP_NAME, "시·군 이름을 입력해 주세요. (예: 오산시)")
                return
            address = f"{address} {city}"
        reverse = {v: k for k, v in YN.items()}
        cfg = app.load_config()
        cfg["my_profile"] = {**(cfg.get("my_profile") or {}), "region": region, "address": address,
                             **{k: reverse[box.get()] for k, box in self.answers.items()}}
        app.save(cfg)
        messagebox.showinfo(APP_NAME, "내 조건을 저장했습니다.")
        self.after_save()

    def save_filters(self):
        values = {}
        for key, entry in (("min_gap_manwon", self.min_gap), ("max_price_manwon", self.max_price)):
            raw = entry.get().replace(",", "").strip()
            if raw and not raw.isdigit():
                messagebox.showwarning(APP_NAME, "금액은 숫자(만원)만 입력하세요. 비우면 제한 없음입니다.")
                return
            values[key] = int(raw) if raw else None
        cfg = app.load_config()
        cfg.update(values)
        cfg["regions"] = [name for name, var in self.region_vars.items() if var.get()]
        cfg["hide_ineligible"] = self.hide_var.get()
        cfg["daily_report"] = self.report_var.get()
        cfg["public_housing"] = {"enabled": self.public_var.get(),
                                 "types": [name for name, var in self.public_types.items() if var.get()]}
        app.save(cfg)
        messagebox.showinfo(APP_NAME, "알림 조건을 저장했습니다.")
        self.after_save()

    def after_save(self):
        self.refresh(schedule=False)
        if self.status["configured"] and self.status["task"] is None and messagebox.askyesno(
                APP_NAME, "PC가 켜져 있는 동안 15분마다 자동으로 확인하도록 자동 실행을 켤까요?"):
            ok, msg = app.register_task()
            (messagebox.showinfo if ok else messagebox.showerror)(APP_NAME, msg)
            self.refresh(schedule=False)
            if ok and messagebox.askyesno(APP_NAME, "지금 바로 한 번 확인할까요? (접수 중인 공고를 보내 드립니다)"):
                self.tabs.select(0)
                self.check_now()

    def link(self):
        env = main.load_env()
        self.link_cancel.clear()
        self.link_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.tg_result.config(text="링크 만드는 중...")

        def got_url(result, error):
            if error:
                return finish(None, error)
            url, code = result
            self.tg_url.config(state="normal")
            self.tg_url.delete(0, "end")
            self.tg_url.insert(0, url)
            self.tg_url.config(state="readonly")
            webbrowser.open(url)
            self.tg_result.config(text="텔레그램에서 [시작]을 눌러 주세요. 기다리는 중... (최대 5분)")
            self.background(lambda: app.wait_for_start(env, code, cancelled=self.link_cancel.is_set), finish)

        def finish(chat_id, error):
            self.link_btn.config(state="normal")
            self.cancel_btn.config(state="disabled")
            if chat_id:
                app.save(app.load_config(), chat_id)
                self.tg_result.config(text="연결 완료! 텔레그램에 확인 메시지를 보냈습니다.")
                self.refresh(schedule=False)
                if not (app.load_config().get("my_profile") or {}).get("region"):
                    self.tabs.select(1)
            else:
                self.tg_result.config(text=f"연결 실패: {error}" if error else "연결하지 못했습니다 (시간 초과 또는 취소). 다시 시도하세요.")

        self.background(lambda: app.telegram_link_url(env), got_url)

    def run(self):
        self.root.mainloop()


# ---------- 한 번만 실행 ----------

def already_running():
    """이미 실행 중이면 그쪽에 창 열기를 요청하고 True."""
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1) as s:
            s.sendall(b"show")
        return True
    except OSError:
        return False


def listen(gui):
    server = socket.socket()
    server.bind(("127.0.0.1", INSTANCE_PORT))
    server.listen()

    def loop():
        while True:
            conn, _ = server.accept()
            with conn:
                if conn.recv(16) == b"show":
                    gui.post(gui.show)
    threading.Thread(target=loop, daemon=True).start()


def start():
    if already_running():
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 고해상도 화면에서 글자가 흐리지 않게
    except Exception:
        pass
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LottoCheongyak.Alert")
    gui = App(show_window="--tray" not in sys.argv)
    listen(gui)
    gui.run()


if __name__ == "__main__":
    try:
        start()
    except Exception as ex:
        main.log.exception("GUI 오류")
        messagebox.showerror(APP_NAME, f"오류가 발생했습니다: {ex}")
