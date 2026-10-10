"""Phase 0 占位用例：确保包能正常 import。"""


def test_import_package() -> None:
    import secpt

    assert secpt.__version__ == "0.1.0"
