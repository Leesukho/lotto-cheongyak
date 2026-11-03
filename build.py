"""배포용 exe 빌드: python build.py

- .env의 공유 키(공공데이터 API 키, 텔레그램 봇 토큰)만 _secrets.py로 만들어 exe에 포함한다.
  개인 Chat ID와 프로필(config.json)은 포함하지 않는다.
- 결과: release/로또청약_v{VERSION}/ 폴더와 같은 이름의 zip
"""
import shutil
from pathlib import Path

import PyInstaller.__main__

import gui
import main

VERSION = "1.3"
ROOT = Path(__file__).parent
SHARED_KEYS = ("DATA_GO_KR_KEY", "TELEGRAM_BOT_TOKEN")
OUT = ROOT / "release" / f"로또청약_v{VERSION}"
ICON = ROOT / "build" / "icon.ico"


def build(script, name, *extra):
    PyInstaller.__main__.run([
        str(ROOT / script), "--onefile", "--clean", "--noconfirm", "--noconsole", f"--name={name}",
        f"--icon={ICON}", f"--distpath={ROOT / 'dist'}", f"--workpath={ROOT / 'build'}",
        f"--specpath={ROOT / 'build'}", *extra,
    ])


def make_ico():
    ICON.parent.mkdir(exist_ok=True)
    gui.make_icon("green").save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])


def main_build():
    env = main.load_env()
    missing = [k for k in SHARED_KEYS if not env.get(k)]
    if missing:
        raise SystemExit(f".env에 {missing} 값이 없습니다.")
    secrets_py = ROOT / "_secrets.py"
    secrets_py.write_text("SECRETS = " + repr({k: env[k] for k in SHARED_KEYS}) + "\n", encoding="utf-8")
    make_ico()
    try:
        build("gui.py", "로또청약", "--hidden-import=pystray._win32")
        build("main.py", "로또청약_백그라운드")
    finally:
        secrets_py.unlink(missing_ok=True)

    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    for name in ("로또청약.exe", "로또청약_백그라운드.exe"):
        shutil.copy2(ROOT / "dist" / name, OUT / name)
    shutil.copy2(ROOT / "사용설명서.txt", OUT / "사용설명서.txt")
    shutil.make_archive(str(OUT), "zip", OUT.parent, OUT.name)
    print(f"\n완료: {OUT}.zip")


if __name__ == "__main__":
    main_build()
