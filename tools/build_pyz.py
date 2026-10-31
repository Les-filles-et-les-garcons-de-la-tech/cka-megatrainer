#!/usr/bin/env python3
"""Build dist/cka-megatrainer.pyz: the whole game in one runnable file (Python zipapp).

Runs on any Linux / macOS with Python 3.9+ (tested with 3.9 to 3.15):
    ./cka-megatrainer.pyz        or        python3 cka-megatrainer.pyz
"""
import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["__main__.py", "cka_trainer.py", "retro.py", "questions.json", "LICENSE"]
OUT = ROOT / "dist" / "cka-megatrainer.pyz"


def main():
    sys.path.insert(0, str(ROOT))
    import cka_trainer  # refuse to ship a broken database
    domains, questions = cka_trainer.load_db(cka_trainer.DEFAULT_DB)
    with tempfile.TemporaryDirectory() as tmp:
        for name in FILES:
            shutil.copy2(ROOT / name, Path(tmp) / name)
        OUT.parent.mkdir(exist_ok=True)
        zipapp.create_archive(tmp, OUT, interpreter="/usr/bin/env python3", compressed=True)
    print(f"{OUT.relative_to(ROOT)}: {OUT.stat().st_size // 1024} KB, {len(questions)} questions")


if __name__ == "__main__":
    main()
