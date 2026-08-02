# tests/conftest.py
# pytest の共通設定。プロジェクトルートを sys.path に追加して
# agents/, tools/, config/ を直接 import できるようにする。

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
