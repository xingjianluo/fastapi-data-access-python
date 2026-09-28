"""测试公共配置：打印每个用例的输入与判定依据。"""

from __future__ import annotations

import logging
import sys

import pytest

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("tests")


def report(case: str, inputs: object, verdict: str, extra: object = None) -> None:
    """统一打印：用例名、输入、判定依据、可选细节。"""
    log.info("CASE=%s | INPUT=%s | VERDICT=%s%s",
             case, inputs, verdict, f" | DETAIL={extra}" if extra else "")


@pytest.fixture
def log_case():
    def _log(case: str, inputs: object, verdict: str, extra: object = None) -> None:
        report(case, inputs, verdict, extra)
    return _log
