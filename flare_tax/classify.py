"""1件単位の基本分類: ネイティブFLRトランザクション / ERC-20転送。"""

from typing import Optional

from .helpers import (
    FXRP_ADDRESS,
    RFLR_ADDRESS,
    WFLR_ADDRESS,
    ZERO_ADDRESS,
    calc_gas_fee,
    format_units,
    short_addr,
    to_jst_str,
    wei_to_flr,
)

# 送受金行には counterparty (相手先アドレス) を付与する。
# 名前付け(自分のウォレット/取引所等)はフロントエンドのアドレス帳
# (localStorage) が行う — 個人情報をコードに持たないため。


def classify_flr_transaction(tx: dict, wallet: str) -> Optional[dict]:
    """
    Classify a native FLR transaction.
    Rules:
      0. Failed tx → gas-only DEFIFEE (own tx) or None (incoming)
      1. Self-transfer (from == to == wallet) → SENDFEE / skip
      2. FLR receive (to == wallet, value > 0) → BUY / export
      3. FLR send (from == wallet, value > 0) → SELL / review
      4. Contract call (from == wallet, value == 0) → DEFIFEE / skip
    """
    from_addr = (tx.get("from", {}).get("hash", "") or "").lower()
    to_addr = (tx.get("to", {}).get("hash", "") or "").lower()
    wallet_lower = wallet.lower()
    value = int(tx.get("value", "0"))
    gas_fee = calc_gas_fee(tx)
    timestamp = to_jst_str(tx.get("timestamp", ""))
    tx_hash = tx.get("hash", "")

    base_row = {
        "timestamp": timestamp,
        "source": "Flare_Wallet",
        "price": "",
        "counter": "JPY",
        "counter_volume": "",
        "fee_ccy": "FLR",
        "tx_hash": tx_hash,
        "block": tx.get("block_number") or tx.get("block"),
    }

    # Rule 0: Failed transaction — assets did not move
    if tx.get("status") != "ok":
        if from_addr == wallet_lower:
            # Gas was still consumed by the sender
            return {
                **base_row,
                "action": "DEFIFEE",
                "base": "FLR",
                "volume": "0",
                "fee": gas_fee,
                "comment": "[Failed] Transaction Failed (gas only)",
                "status": "skip",
            }
        return None

    # Rule 4: Self-transfer
    if from_addr == wallet_lower and to_addr == wallet_lower:
        return {
            **base_row,
            "action": "SENDFEE",
            "base": "FLR",
            "volume": "0",
            "fee": gas_fee,
            "comment": "[Transfer] Self Transfer FLR",
            "status": "skip",
        }

    # Rule 1: FLR receive
    if to_addr == wallet_lower and value > 0:
        return {
            **base_row,
            "action": "BUY",
            "base": "FLR",
            "volume": wei_to_flr(str(value)),
            "fee": gas_fee if from_addr == wallet_lower else "0",
            "comment": "[Transfer] Receive FLR",
            "status": "review",
            "counterparty": from_addr,
        }

    # Rule 2: FLR send
    if from_addr == wallet_lower and value > 0:
        return {
            **base_row,
            "action": "SELL",
            "base": "FLR",
            "volume": wei_to_flr(str(value)),
            "fee": gas_fee,
            "comment": "[Transfer] Send FLR",
            "status": "review",
            "counterparty": to_addr,
        }

    # Rule 3: Contract operation (from == wallet, value == 0)
    if from_addr == wallet_lower and value == 0:
        decoded_input = tx.get("decoded_input") or {}
        # decoded_input が無い (エクスプローラーが記号で返した) ときは、名前に直した method を使う
        method = decoded_input.get("method_call", "") or tx.get("method") or ""
        to_name = tx.get("to", {})
        if isinstance(to_name, dict):
            to_name = to_name.get("name", "") or ""
        else:
            to_name = ""

        if "castVote" in method:
            comment = "[FIP] Cast Vote"
        elif "batchDelegate" in method or "delegate" in method:
            comment = "[Delegate] Delegate to FTSO"
        elif "approve" in method:
            comment = "[GasFee] Approve Token"
        elif "ask" in method and "TWAP" in to_name:
            comment = "[GasFee] TWAP Order Setup"
        else:
            comment = "[GasFee] Smart Contract"

        return {
            **base_row,
            "action": "DEFIFEE",
            "base": "FLR",
            "volume": "0",
            "fee": gas_fee,
            "comment": comment,
            "status": "skip",
        }

    # Fallback: other patterns (e.g., to == wallet, value == 0 — internal tx?)
    return {
        **base_row,
        "action": "BUY",
        "base": "FLR",
        "volume": wei_to_flr(str(value)) if value > 0 else "0",
        "fee": "0",
        "comment": f"その他の取引 (from={short_addr(from_addr)}, to={short_addr(to_addr)})",
        "status": "review",
    }


def aggregate_token_transfers(token_transfers: list, wallet: str) -> list:
    """
    トークン転送の前処理:
      1. (tx_hash, log_index) で重複排除 — ページネーションで同一イベントが二重取得されることがある
      2. 同一tx内の同一トークン・同方向の転送を合算 — 分割ルーティングのスワップ等は
         複数のTransferイベントを発行するため、捨てると過少計上になる
    合算結果は代表transferのdictに _raw_sum として保持する。
    """
    wallet_lower = wallet.lower()
    seen_logs = set()
    aggregated: dict = {}
    agg_order = []

    for transfer in token_transfers:
        t_hash = transfer.get("tx_hash") or transfer.get("transaction_hash", "")
        log_index = transfer.get("log_index")
        if log_index is not None:
            log_key = (t_hash, log_index)
            if log_key in seen_logs:
                continue
            seen_logs.add(log_key)

        from_addr = (transfer.get("from", {}).get("hash", "") or "").lower()
        to_addr = (transfer.get("to", {}).get("hash", "") or "").lower()
        token = transfer.get("token", {}) or {}
        token_addr = (token.get("address_hash") or token.get("address") or token.get("symbol") or "").lower()
        if from_addr == wallet_lower and to_addr == wallet_lower:
            direction = "self"
        elif to_addr == wallet_lower:
            direction = "in"
        elif from_addr == wallet_lower:
            direction = "out"
        else:
            direction = "other"
        # Zero-address flags keep mint/burn (wrap/unwrap) distinct from normal moves
        group_key = (t_hash, token_addr, direction, from_addr == ZERO_ADDRESS, to_addr == ZERO_ADDRESS)

        raw_value = int(transfer.get("total", {}).get("value", "0"))
        if group_key in aggregated:
            aggregated[group_key]["_raw_sum"] += raw_value
        else:
            entry = dict(transfer)
            entry["_raw_sum"] = raw_value
            aggregated[group_key] = entry
            agg_order.append(group_key)

    return [aggregated[k] for k in agg_order]


def classify_token_transfer(transfer: dict, wallet: str) -> dict:
    """
    Classify an ERC-20 token transfer.
    Rules:
      5. Self-transfer (from == to == wallet) → SENDFEE / skip
      6. Token receive (to == wallet) → BUY / export
      7. Token send (from == wallet) → SELL / review
    """
    from_addr = (transfer.get("from", {}).get("hash", "") or "").lower()
    to_addr = (transfer.get("to", {}).get("hash", "") or "").lower()
    wallet_lower = wallet.lower()

    token_info = transfer.get("token", {})
    symbol = token_info.get("symbol", "UNKNOWN")
    decimals = int(token_info.get("decimals", "18"))

    # _raw_sum is set when multiple transfers of the same token/direction
    # within one tx have been aggregated (e.g. split-route swaps)
    raw_value = transfer.get("_raw_sum")
    if raw_value is None:
        raw_value = int(transfer.get("total", {}).get("value", "0"))
    volume_str = format_units(raw_value, decimals)

    # Get tx_hash from the nested transaction object or directly
    tx_data = transfer.get("tx_hash", "")
    if not tx_data:
        tx_data = transfer.get("transaction_hash", "")

    timestamp = to_jst_str(transfer.get("timestamp", ""))
    token_addr = (token_info.get("address_hash") or token_info.get("address") or "").lower()

    base_row = {
        "timestamp": timestamp,
        "source": "Flare_Wallet",
        "price": "",
        "counter": "JPY",
        "counter_volume": "",
        "fee": "0",
        "fee_ccy": "FLR",
        "tx_hash": tx_data,
        "block": transfer.get("block_number"),
        # (シンボル, アドレス)。同じシンボルを名乗る別トークンと区別するため、行に持たせておく
        "_tok": (symbol, token_addr),
    }

    # Rule 7: Self-transfer
    if from_addr == wallet_lower and to_addr == wallet_lower:
        return {
            **base_row,
            "action": "SENDFEE",
            "base": symbol,
            "volume": volume_str,
            "comment": f"[Transfer] Self Transfer {symbol}",
            "status": "skip",
        }

    # FXRP のミント・償還 (XRPL側で REBRAND 計上済み → 出力しない)
    is_sa_mint = bool(transfer.get("_fxrp_mint")) and to_addr == wallet_lower
    if token_addr == FXRP_ADDRESS and (from_addr == ZERO_ADDRESS or to_addr == ZERO_ADDRESS or is_sa_mint):
        minting = from_addr == ZERO_ADDRESS or is_sa_mint
        return {
            **base_row,
            "action": "BUY" if minting else "SENDFEE",
            "base": symbol,
            "volume": volume_str,
            "comment": "[FAssets] Mint FXRP（XRPと同じコインとして扱うので記録しない）" if minting
                       else "[FAssets] Redeem FXRP（XRPと同じコインとして扱うので記録しない）",
            "status": "skip",
            "_locked": True,
        }

    # WFLR Wrap/Unwrap detection (mint from / burn to zero address)
    if token_addr == WFLR_ADDRESS and (from_addr == ZERO_ADDRESS or to_addr == ZERO_ADDRESS):
        if to_addr == ZERO_ADDRESS:
            # WFLR → FLR (Unwrap / Burn)
            return {
                **base_row,
                "action": "SENDFEE",
                "base": symbol,
                "volume": volume_str,
                "comment": "[Wrap] Unwrap WFLR",
                "status": "skip",
            }
        else:
            # FLR → WFLR (Wrap / Mint) — may be reclassified as BONUS in post-processing
            return {
                **base_row,
                "action": "SENDFEE",
                "base": symbol,
                "volume": volume_str,
                "comment": "[Wrap] Wrap FLR",
                "status": "skip",
            }

    # rFLR burn to zero address (rFLR → WFLR withdraw)
    if token_addr == RFLR_ADDRESS and to_addr == ZERO_ADDRESS:
        return {
            **base_row,
            "action": "SENDFEE",
            "base": symbol,
            "volume": volume_str,
            "comment": "[Delegate] rFLR Withdraw",
            "status": "skip",
        }

    # rFLR receive (claim) → skip (税計算はWithdraw時のBONUSで一括計上)
    if token_addr == RFLR_ADDRESS and to_addr == wallet_lower:
        return {
            **base_row,
            "action": "BONUS",
            "base": symbol,
            "volume": volume_str,
            "comment": "[rFLR] Claim",
            "status": "skip",
        }

    # 数量0の転送は資産が動いていない。本物のトークンで「0枚送った」履歴を作り、似たアドレスを
    # 送金先に選ばせる手口 (アドレスポイズニング) にも使われるので、要確認には出さない
    if raw_value == 0:
        return {
            **base_row,
            "action": "BUY" if to_addr == wallet_lower else "SELL",
            "base": symbol,
            "volume": "0",
            "comment": f"[Transfer] {symbol} 数量0の転送（資産の動きなし）",
            "status": "skip",
        }

    # Rule 5: Token receive
    if to_addr == wallet_lower:
        # ゼロアドレスからの発行 = FAssetsミント or ブリッジ着金 (Stargate等のOFT)。
        # 自分由来なら資金移動なので、判断材料としてラベルを分ける。
        # (LPトークン等のミントは後段のプロトコルハンドラがコメントを上書きする)
        if from_addr == ZERO_ADDRESS:
            comment = f"[Bridge/Mint] Receive {symbol} (Stargate等のブリッジ着金なら資金移動)"
        else:
            comment = f"[Transfer] Receive {symbol}"
        return {
            **base_row,
            "action": "BUY",
            "base": symbol,
            "volume": volume_str,
            "comment": comment,
            "status": "review",
            "counterparty": from_addr,
        }

    # Rule 6: Token send
    if from_addr == wallet_lower:
        return {
            **base_row,
            "action": "SELL",
            "base": symbol,
            "volume": volume_str,
            "comment": f"[Transfer] Send {symbol}",
            "status": "review",
            "counterparty": to_addr,
        }

    # Fallback
    return {
        **base_row,
        "action": "BUY",
        "base": symbol,
        "volume": volume_str,
        "comment": f"[Transfer] {symbol} Other",
        "status": "review",
    }
