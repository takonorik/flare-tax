"""エクスプローラーがメソッド名を記号 (0x…) で返しても、同じ名前に直せることの確認 (通信なし)。"""
from flare_tax.protocols import KNOWN_SELECTORS, normalize_method


def test_hex_method_becomes_name():
    # 0xa694fc3a = stake(uint256)。同じ取引でも名前で返るときと記号で返るときがある
    tx = {"method": "0xa694fc3a", "raw_input": "0xa694fc3a" + "0" * 64, "decoded_input": None}
    normalize_method(tx)
    assert tx["method"] == "stake"


def test_empty_method_uses_raw_input():
    tx = {"method": None, "raw_input": "0x095ea7b3" + "0" * 128}
    normalize_method(tx)
    assert tx["method"] == "approve"


def test_named_method_is_kept():
    tx = {"method": "claim", "raw_input": "0xa694fc3a" + "0" * 64}
    normalize_method(tx)
    assert tx["method"] == "claim"


def test_unknown_selector_is_left_alone():
    tx = {"method": "0xdeadbeef", "raw_input": "0xdeadbeef"}
    normalize_method(tx)
    assert tx["method"] == "0xdeadbeef"
    tx = {"method": "", "raw_input": "0x"}  # ただの送金
    normalize_method(tx)
    assert tx["method"] == ""


def test_table_keys_are_selectors():
    for sel, name in KNOWN_SELECTORS.items():
        assert sel.startswith("0x") and len(sel) == 10 and sel == sel.lower(), sel
        assert name and not name.startswith("0x"), sel
