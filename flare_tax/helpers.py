"""共通ヘルパー: 数量フォーマット・時刻変換・日付フィルタ。"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal, localcontext
from typing import Optional

JST = timezone(timedelta(hours=9))
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# FAssets の FXRP。発行・焼却は AssetManager からしか起きないので、
# 0x0 からの発行 = ミント、0x0 への焼却 = 償還と判定できる。
# ミント・償還は XRPL Tax for Cryptact 側で REBRAND として計上するため、Flare側では出力しない。
FXRP_ADDRESS = "0xad552a648c74d49e10027ab8a618a3ad4901c5be"
# ラップ・報酬の判定はアドレスで行う (同じシンボルを名乗る偽トークンを巻き込まないため)
WFLR_ADDRESS = "0x1d80c49bbbcd1c0911346656b529df9e5c2f783d"
RFLR_ADDRESS = "0x26d460c3cf931fb2014fa436a49e3af08619810e"


def format_decimal(d: Decimal) -> str:
    """Format a Decimal without scientific notation, stripping trailing zeros."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def ratio_str(num: Decimal, den: Decimal) -> str:
    """num / den を有効数字18桁の文字列で返す (Swap の交換比率。小数8桁などに丸めると数量がずれる)。"""
    with localcontext() as ctx:
        ctx.prec = 40
        q = num / den
        if q == 0:
            return "0"
        return format_decimal(q.quantize(Decimal(1).scaleb(q.adjusted() - 17)))


def format_units(raw: int, decimals: int) -> str:
    """Convert a raw integer token amount to a full-precision decimal string."""
    with localcontext() as ctx:
        ctx.prec = 60
        return format_decimal(Decimal(raw) / (Decimal(10) ** decimals))


def wei_to_flr(wei_str: str) -> str:
    """Convert wei string to FLR (full precision)."""
    return format_units(int(wei_str), 18)


def to_jst_str(iso_timestamp: str) -> str:
    """Convert ISO 8601 timestamp to JST formatted string."""
    dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    jst_dt = dt.astimezone(JST)
    return jst_dt.strftime("%Y/%m/%d %H:%M:%S")


def to_jst_dt(iso_timestamp: str) -> datetime:
    """Convert ISO 8601 timestamp to JST datetime object."""
    dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    return dt.astimezone(JST)


def calc_gas_fee(tx: dict) -> str:
    """Calculate gas fee in FLR from transaction data."""
    gas_used = int(tx.get("gas_used", "0"))
    gas_price = int(tx.get("gas_price", "0"))
    fee_wei = gas_used * gas_price
    return wei_to_flr(str(fee_wei))


def short_addr(addr: str) -> str:
    """Shorten address for display."""
    if addr and len(addr) >= 10:
        return f"{addr[:10]}..."
    return addr or ""


def parse_date_jst(date_str: str, end_of_day: bool = False) -> datetime:
    """Parse YYYY-MM-DD string as JST datetime."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt.replace(tzinfo=JST)


def filter_by_date(
    rows: list,
    start_date: Optional[str],
    end_date: Optional[str],
) -> list:
    """Filter rows by JST date range."""
    if not start_date and not end_date:
        return rows

    start_dt = parse_date_jst(start_date) if start_date else None
    end_dt = parse_date_jst(end_date, end_of_day=True) if end_date else None

    filtered = []
    for row in rows:
        ts = datetime.strptime(row["timestamp"], "%Y/%m/%d %H:%M:%S").replace(
            tzinfo=JST
        )
        if start_dt and ts < start_dt:
            continue
        if end_dt and ts > end_dt:
            continue
        filtered.append(row)
    return filtered
