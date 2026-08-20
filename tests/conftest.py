"""
tests/ 아래 테스트들이 `from agents.xxx import yyy` 형태로 import할 수 있도록
저장소 루트를 sys.path에 추가한다. (프로젝트 루트에서 `pytest` 실행 기준)
"""

import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
