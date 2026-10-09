"""分层 import 守卫的 pytest 入口：直接调用 tests/layer_guard.main() 并断言退出码为 0。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from layer_guard import main as layer_guard_main  # noqa: E402


def test_layered_import_guard():
    """禁止新增越层 import（基线见 tests/layer_guard_baseline.txt）。"""
    with pytest.raises(SystemExit) as exc:
        layer_guard_main()
    assert exc.value.code == 0, "发现新增越层 import，需修复或更新 tests/layer_guard_baseline.txt"
