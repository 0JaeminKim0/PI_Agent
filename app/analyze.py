"""
공통 헬퍼: 데모 데이터 로더.
(파이프라인은 indexer→extractor→validate 로 분리됨)
"""
from __future__ import annotations
import os
import json

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_PATH = os.path.join(HERE, "demo_data.json")


def load_demo_data() -> dict:
    with open(DEMO_PATH, encoding="utf-8") as f:
        return json.load(f)
