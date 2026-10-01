# -*- coding: utf-8 -*-
"""极简 pytest 垫片：仅实现本项目测试用到的 raises 与 fixture 注入。
当环境装有真正的 pytest 时优先使用 pytest；否则用本文件运行。"""
from __future__ import annotations

import importlib
import inspect
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path


class _Raises:
    def __init__(self, expected, match: str | None = None):
        self.expected = expected
        self.match = match

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            raise AssertionError(f"未抛出预期异常 {self.expected.__name__}")
        if not issubclass(exc_type, self.expected):
            return False
        if self.match is not None and not re.search(self.match, str(exc)):
            raise AssertionError(f"异常信息 {str(exc)!r} 不匹配 {self.match!r}")
        return True


def raises(expected, match: str | None = None):
    return _Raises(expected, match)


def _run_all(path: str) -> int:
    root = Path(path).resolve()
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tests"))

    # 注入 pytest 垫片（仅 raises）；真有 pytest 时优先使用真的
    try:
        import pytest  # noqa: F401
    except ImportError:
        import types
        shim = types.ModuleType("pytest")
        shim.raises = raises

        def _fixture(*a, **k):
            def deco(fn):
                fn._pytest_fixture = True
                return fn
            return deco
        shim.fixture = _fixture
        sys.modules["pytest"] = shim

    conftest = importlib.import_module("conftest")
    fixtures = {"tmp_path": None}
    for name, fn in vars(conftest).items():
        if getattr(fn, "_pytest_fixture", False):
            fixtures[name] = fn

    passed = failed = 0
    failures: list[str] = []
    test_modules = {}
    for mod_path in sorted((root / "tests").glob("test_*.py")):
        mod = importlib.import_module(f"tests.{mod_path.stem}")
        test_modules[mod_path.stem] = mod
        for name, fn in list(vars(mod).items()):
            if getattr(fn, "_pytest_fixture", False):
                fixtures[name] = fn
        for attr_name in sorted(vars(mod)):
            obj = getattr(mod, attr_name)
            if attr_name.startswith("Test") and inspect.isclass(obj):
                members = sorted(
                    (n, fn) for n, fn in vars(obj).items()
                    if n.startswith("test_") and callable(fn)
                )
                for name, fn in members:
                    instance = obj()
                    _run_one(f"{attr_name}.{name}", fn, instance, fixtures,
                             passed_box=[passed], record=(failures,))
                    if failures and len(failures) > failed:
                        failed = len(failures)
                    else:
                        passed += 1
            elif attr_name.startswith("test_") and callable(obj):
                _run_one(attr_name, obj, None, fixtures, [], record=(failures,))
                if failures and len(failures) > failed:
                    failed = len(failures)
                else:
                    passed += 1

    print("\n" + "=" * 60)
    for f in failures:
        print(f)
    print(f"通过 {passed} 项，失败 {failed} 项")
    return 1 if failures else 0


def _run_one(label, fn, instance, fixtures, passed_box, record):
    tmp = Path(tempfile.mkdtemp(prefix="chessroom_test_"))
    cache: dict[str, object] = {"tmp_path": tmp}
    generators = []

    def build(name: str):
        if name in cache:
            return cache[name]
        maker = fixtures[name]
        sig = inspect.signature(maker)
        kwargs = {p: build(p) for p in sig.parameters}
        result = maker(**kwargs)
        if inspect.isgenerator(result):
            value = next(result)
            generators.append(result)
            cache[name] = value
        else:
            cache[name] = result
        return cache[name]

    try:
        sig = inspect.signature(fn)
        kwargs = {p: build(p) for p in sig.parameters if p != "self"}
        fn(instance, **kwargs) if instance else fn(**kwargs)
        print(f"  PASS  {label}")
    except Exception:  # noqa: BLE001
        record[0].append(f"  FAIL  {label}\n{traceback.format_exc()}")
        print(f"  FAIL  {label}")
    finally:
        for gen in reversed(generators):
            try:
                next(gen)
            except StopIteration:
                pass


if __name__ == "__main__":
    sys.exit(_run_all(os.path.dirname(os.path.abspath(__file__))))
