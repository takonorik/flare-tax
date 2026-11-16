"""
プロトコル定義とハンドラのレジストリ。

新しいサービスに対応するときは、ここに
  1. 必要ならアドレス/トークンのマッピングを追加
  2. matcher(このtxグループを扱うか判定) と handler(行を書き換える) を書く
  3. PROTOCOL_HANDLERS に (matcher, handler) を登録する
だけでよい。既存ロジックには触れない。

matcher/handler の引数:
  group_rows: 同一tx_hashの全行
  sends:      数量>0 の送付側行 (SELL/SENDFEE)
  receives:   数量>0 の受取側行 (BUY/BONUS)
  ctx:        dict(method, to_name, to_addr, dex_name, new_rows)
"""

from decimal import Decimal

from .helpers import format_decimal, ratio_str

# DEX Router mapping
DEX_ROUTERS = {
    "BlazeSwap": [
        "0xe3a1b355ca63abcbc9589334b5e609583c7baa06",
        "0x3f50f880041521738fa88c46cdf7e0d8eeb11aa2",
    ],
    "SparkDEX": [
        "0x6352a56caadc4f1e25cd6c75970fa768a3304e64",
        "0xc2df11c68f86910b99eaf8aced7f5189915ba24f",
        "0x49be8aa6c684b15e0c5450e8fa0b16bec1435596",
    ],
    "Enosys": [
        "0x17aca82378c2859e40d84a373b49ee0b318020c4",
    ],
}

# 4バイトセレクタ → メソッド名。未検証コントラクト (Kinetic の iso 市場の cToken など) や、
# Blockscout が名前を付けずに返した場合に、判定が名前で行えるようにする
KNOWN_SELECTORS = {
    # 遅延アンステーク (SparkDEX stFLR)
    "0x9ee679e8": "requestWithdrawal",
    "0xf8444436": "claimWithdrawal",
    "0x48e30f54": "claimWithdrawals",
    # sFLR (Sceptre) の Unlock 申請と受け取り
    "0xc9d2ff9d": "requestUnlock",
    "0xbe040fb0": "redeem",
    # Compound 系 (Kinetic)
    "0xa0712d68": "mint",
    "0xdb006a75": "redeem",
    "0x852a12e3": "redeemUnderlying",
    "0xc5ebeaec": "borrow",
    "0x0e752702": "repayBorrow",
    # multicall (BlazeSwap などのルーター)
    "0xac9650d8": "multicall",
    "0x5ae401dc": "multicall",
    # 未検証コントラクトの claim 系 (エクスプローラーの表示名で確認)
    "0x5e318e07": "withdrawFees",  # FAssets CollateralPool (担保プールの手数料分配)
    "0x6e164e23": "claimTokens",   # TokenClaiming (APS)
    # V3 NonfungiblePositionManager (Uniswap V3 互換: Enosys / SparkDEX)
    "0x88316456": "mint",
    "0x219f5d17": "increaseLiquidity",
    "0x0c49ccbe": "decreaseLiquidity",
    "0xfc6f7865": "collect",
    # ブリッジ (LayerZero OFT: USD₮0 など)
    "0xc7c7f5b3": "send",
    # ICHI Vault (SparkDEX のマネージドボールト。預け入れ・引き出しの中継コントラクト)
    "0x5d123e3f": "forwardDepositToICHIVault",
    "0x1a0e8cdf": "forwardWithdrawFromICHIVault",
    # ファーム・報酬の受け取り (未検証コントラクト。関数の署名データベースで確認)
    "0xb88a802f": "claimReward",
    "0x372500ab": "claimRewards",
    "0xef5cfb8c": "claimRewards",
    "0x8c574b48": "claim",
    "0x25181bb0": "claim",
    "0x26a1353b": "claim",
    "0x2e1a7d4d": "withdraw",
    "0x81d04b18": "stakeLpFarmFlr",
    # NFT マーケットでのオファー承諾 (売却)
    "0xc815729d": "acceptOffer",
    # 承認
    "0x095ea7b3": "approve",
    "0xa22cb465": "setApprovalForAll",
}

# 実際の履歴で、エクスプローラーが名前付きで返した実績のあるセレクタ。
# Blockscout は同じ取引でも、名前 ("claim") で返すときと記号 ("0x8e33aba5") で返すときがある。
# 記号で返ってきても同じ判定になるように、ここで名前に直せるようにしておく (上の表が優先)。
_SEEN_SELECTORS = {
    "0x3e07cb2b": "addLiquidity",
    "0xe8e33700": "addLiquidity",
    "0x9943d8f3": "addLiquidityFLR",
    "0x1c75e369": "allocate",
    "0x87517c45": "approve",
    "0xc360ed1c": "approveUsage",
    "0xa8efb998": "ask",
    "0xdc4fcda7": "batchDelegate",
    "0x50d8cd4b": "borrow",
    "0xef706adf": "cancelOffer",
    "0x0412da4b": "cancelRedeem",
    "0x56781388": "castVote",
    "0x379607f5": "claim",
    "0x71ee95c0": "claim",
    "0x8e33aba5": "claim",
    "0xb2c12192": "claim",
    "0x5767bba5": "claimAll",
    "0x0952c563": "claimReward",
    "0xb2af870a": "claimReward",
    "0x0f1d1e31": "claimRewards",
    "0x48c54b9d": "claimTokens",
    "0xdf8de3e7": "claimTokens",
    "0x5aa6d461": "closeTrove",
    "0xa3908e1b": "convert",
    "0x026e402b": "delegate",
    "0x5c19a95c": "delegate",
    "0x0efe6a8b": "deposit",
    "0x6e553f65": "deposit",
    "0xd0e30db0": "deposit",
    "0xdfc39d95": "deposit",
    "0x2394deb1": "disableDelegationAccount",
    "0xf0977215": "enableDelegationAccount",
    "0xc2998238": "enterMarkets",
    "0x24856bc3": "execute",
    "0xaff6cbf1": "finalizeRedeem",
    "0x28ea7ee1": "harvestAllDividends",
    "0x6d319d27": "harvestDividends",
    "0xfe3f3be7": "mint",
    "0xd123b4d8": "mintCube",
    "0x92642744": "mintNFT",
    "0x9cb90ba6": "openTrove",
    "0xaeb4b970": "provideToSP",
    "0x7cbc2373": "redeem",
    "0x01e261f6": "redeemAmount",
    "0x0f7e2048": "redeemOverdueShares",
    "0x5b75d79c": "registerAddresses",
    "0xbaa2abde": "removeLiquidity",
    "0x20d88f9a": "removeLiquidityFLR",
    "0x2195995c": "removeLiquidityWithPermit",
    "0x20b76e81": "repay",
    "0x6bb3b476": "requestOnChainWithdraw",
    "0x275a7bfc": "reserveCollateral",
    "0x31c864e8": "safeMint",
    "0xe72dcdbb": "setAutoClaiming",
    "0x9119c494": "setClaimExecutors",
    "0xa694fc3a": "stake",
    "0xff52a2a4": "submitWrapped",
    "0x238d6579": "supplyCollateral",
    "0x90411a32": "swap",
    "0x94b918de": "swap",
    "0xbc10490c": "swapExactFLRForTokens",
    "0xf16cab23": "swapExactNATForTokens",
    "0x2b3b0f2b": "swapExactTokensForNAT",
    "0x38ed1739": "swapExactTokensForTokens",
    "0xe8cf74bb": "swapNATForExactTokens",
    "0x8803dbee": "swapTokensForExactTokens",
    "0xaad7c93e": "swapTokensForTokens",
    "0xa9059cbb": "transfer",
    "0xa50d5f3d": "withdraw",
    "0xb460af94": "withdraw",
    "0x8720316d": "withdrawCollateral",
    "0xcfddf5f5": "withdrawFromSP",
}
for _sel, _name in _SEEN_SELECTORS.items():
    KNOWN_SELECTORS.setdefault(_sel, _name)


def normalize_method(tx: dict) -> None:
    """
    tx の method が空か記号 (0x…) のとき、分かる範囲で名前に直す (tx を書き換える)。
    エクスプローラーの返し方の揺れで判定が変わらないようにするため、分類の入口で一度だけ行う。
    """
    m = tx.get("method") or ""
    if m and not m.startswith("0x"):
        return
    sel = (tx.get("raw_input") or "")[:10].lower()
    name = KNOWN_SELECTORS.get(sel if len(sel) == 10 else m.lower())
    if name:
        tx["method"] = name

ROUTER_TO_DEX = {}
for _dex, _addrs in DEX_ROUTERS.items():
    for _addr in _addrs:
        ROUTER_TO_DEX[_addr.lower()] = _dex

# LST token mapping: {LST_token: ((underlying_tokens,...), category_tag)}
LST_MAP = {
    "sFLR": (("FLR", "WFLR"), "[sFLR Staking]"),
    "stFLR": (("FLR", "WFLR"), "[stFLR Staking]"),
    "stXRP": (("FXRP",), "[stXRP Staking]"),
    "flrETH": (("WETH",), "[flrETH Staking]"),  # Dinero
}

# 遅延アンステークのあるLST (コントラクト = トークン) → シンボル。申請時に Swap として計上する
DELAYED_UNSTAKE_LST = {
    "0x0988c6ba244a90c07a917ebe609eb3264be716ff": "stFLR",  # SparkDEX stFLR (SparkDEXStakedFLR)
}
WFLR_ADDRESS = "0x1d80c49bbbcd1c0911346656b529df9e5c2f783d"

# 1:1 のトークン変換 (移行・ステーキング用トークンへの交換) → REBRAND (損益なしの付け替え)
# (送ったトークン, 受け取ったトークン) → コメント
REBRANDS = {}
# 同一トークンとして扱う 1:1 の変換 (WFLR→FLR と同じ考え方) → スキップ
# KII は JOULE をステーキングするためのトークンで、JOULE と 1:1 で交換・解除できる
SAME_TOKEN = {
    ("JOULE", "KII"): "[Kii] Stake JOULE → KII（KIIはJOULEと同一扱い）",
}
# DaoFlare: 毎月、転送の記録なしで付与される DFLR を claim で焼却し、HLN を受け取る = HLN の報酬
DAOFLARE_CLAIM = ("DFLR", "HLN")
KII_CONTRACT = "0xd38220cff996a73e9110aaca64e02d581b83a0cd"

# Enosys Loans Stability Pool Mapping
ENOSYS_SP_MAP = {
    "0x0dd6daab4cb9a0ba6707cf59dbfbc28cc33ca24a": "WFLR",
    "0x2c817f7159c08d94f09764086330c96bb3265a2f": "FXRP",
}

# V3 NonfungiblePositionManager (プロキシ契約で名前が取れないものはアドレスで判定)
NPM_ADDRESSES = {
    "0xd9770b1c7a6ccd33c75b5bcb1c0078f46be46657": "Enosys",  # Enosys V3 Position Manager (proxy)
}



def _dex_label(ctx) -> str:
    """DEX名を決める: ルーターマップ → コントラクト名から推定 → DEX。"""
    if ctx["dex_name"]:
        return ctx["dex_name"]
    name = ctx["to_name"]
    if name and "Router" in name:
        return name.replace("DexRouter", "").replace("Router", "")
    return "DEX"


# ---------------------------------------------------------------------------
# Enosys Loans Stability Pool
# ---------------------------------------------------------------------------

def match_enosys_sp(group_rows, sends, receives, ctx) -> bool:
    method = ctx["method"]
    return "provideToSP" in method or "withdrawFromSP" in method


def handle_enosys_sp(group_rows, sends, receives, ctx) -> None:
    """
    Stability Pool: 預入CDPは清算の肩代わりで減り、代わりに清算担保
    (FXRP/WFLR) を受け取る。CDPの出入りは差分エンジンで元本管理し、
    受け取った担保は清算益(BONUS)として計上する。

    SP は操作のたびに DepositOperation イベントを出す (取得時に tx の "_sp_op" に付く)。
    これがあれば、その操作の時点で
      - 前回の操作から清算で使われた CDP → LOSS (元本から差し引く。差分エンジンの writeoff)
      - 受け取った利回り (CDP) → BONUS (届いた CDP から切り出す)
    を計上する。担保の BONUS と同じ日に対価の CDP が LOSS になるので、年度もずれない。
    イベントが無い場合は従来どおり元本の差分だけで管理する。
    """
    method = ctx["method"]
    sp_token = ENOSYS_SP_MAP.get(ctx["to_addr"], "")
    pool_insert = f" {sp_token}" if sp_token else ""
    pool = f"Enosys Loans SP{pool_insert}:CDP"
    providing = "provideToSP" in method
    op = (ctx.get("tx_info") or {}).get("_sp_op") or {}
    loss = Decimal(op.get("loss") or "0")
    yield_left = Decimal(op.get("yield_claimed") or "0")
    template = group_rows[0] if group_rows else None

    for r in group_rows:
        if float(r.get("volume", "0")) <= 0:
            continue
        if r["base"] == "CDP":
            # CDPの向きで決める: 送った = 預け入れ (LEND)、受け取った = 引き出し (RECOVER)。
            # 預け入れの tx でも、それまでの利回り (CDP) が払い出されて届くことがある
            incoming = r["action"] in ("BUY", "BONUS")
            r["status"] = "skip"
            r["action"] = "RECOVER" if incoming else "LEND"
            r["_lp"] = ("Enosys Loans SP", "recover" if incoming else "lend")
            r["_pool"] = pool
            if not incoming:
                r["comment"] = f"[Enosys Loans] Deposit to{pool_insert} Stability Pool"
            elif providing:
                r["comment"] = f"[Enosys Loans] Withdraw yield from{pool_insert} Stability Pool（預け入れ時に払い出された利回り）"
            else:
                r["comment"] = f"[Enosys Loans] Withdraw from{pool_insert} Stability Pool"
            if incoming and yield_left > 0:
                # 届いた CDP のうち利回りの分は元本の回収ではなく収入
                vol = Decimal(r["volume"])
                y = min(vol, yield_left)
                yield_left -= y
                gain = dict(r)
                gain.pop("_lp", None)
                gain.pop("_pool", None)
                gain.update(action="BONUS", status="export", volume=format_decimal(y), fee="0",
                            comment=f"[Enosys Loans] SP Yield CDP（{sp_token or 'SP'} Stability Pool の利回り）")
                ctx["new_rows"].append(gain)
                rest = vol - y
                if rest > Decimal("1e-12"):
                    r["volume"] = format_decimal(rest)
                else:
                    r["_delete"] = True
        elif r["action"] in ("BUY", "BONUS"):
            # 清算で受け取った担保 = 収入 (対価の CDP は下の LOSS)
            r["action"] = "BONUS"
            r["status"] = "export"
            r["comment"] = f"[Enosys Loans] SP Liquidation Gain {r['base']}"

    if loss > 0 and template is not None:
        # 清算で使われた CDP: 元本から差し引き (writeoff)、損失として出す
        cost = dict(template)
        cost.pop("counterparty", None)
        cost.update(action="LOSS", status="export", base="CDP", volume=format_decimal(loss), price="",
                    counter="JPY", fee="0", _lp=("Enosys Loans SP", "writeoff"), _pool=pool,
                    comment=f"[Enosys Loans] SP Liquidation Cost CDP（清算で使われたCDP・{sp_token or 'SP'} Stability Pool）")
        ctx["new_rows"].append(cost)


# ---------------------------------------------------------------------------
# V3 DEX NonfungiblePositionManager (集中流動性LP)
#
# LPの税務方針は「差分計上」: 預入=LEND / 引出=RECOVER で元本を動かし、
# 引出が元本を超えた分だけ postprocess.post_process_lp_differential が
# BONUS化する。行に付ける "_lp" タグ (pool, role) がその入力になる。
# ---------------------------------------------------------------------------

def match_v3_position_manager(group_rows, sends, receives, ctx) -> bool:
    method = ctx["method"]
    is_npm = "NonfungiblePositionManager" in ctx["to_name"] or ctx["to_addr"] in NPM_ADDRESSES
    return is_npm and (
        "increaseLiquidity" in method
        or "mint" in method
        or "multicall" in method
        or "collect" in method
    )


def handle_v3_position_manager(group_rows, sends, receives, ctx) -> None:
    method = ctx["method"]
    out_dex = ctx["dex_name"] or NPM_ADDRESSES.get(ctx["to_addr"]) or "SparkDEX"
    # ポジション (NFT) が分かれば、元本はポジションごとに管理する。全部解除した tx で差損を出す
    v3 = (ctx.get("tx_info") or {}).get("_v3")
    pos = {"_pool": f"{out_dex} #{v3['id']}", "_v3_closed": v3["closed"]} if v3 else {}

    if "increaseLiquidity" in method or "mint" in method:
        tokens = [r["base"] for r in sends]
        token_str = "-".join(tokens)
        for r in group_rows:
            if r["action"] in ("SELL", "SENDFEE") and float(r.get("volume", "0")) > 0:
                r["status"] = "skip"
                r["action"] = "LEND"
                r["comment"] = f"[{out_dex}] Add LP {token_str}"
                r["_lp"] = (out_dex, "lend")
                r.update(pos)
    elif "multicall" in method:
        tokens = [r["base"] for r in receives]
        token_str = "-".join(tokens)
        for r in group_rows:
            if r["action"] in ("BUY", "BONUS") and float(r.get("volume", "0")) > 0:
                r["status"] = "skip"
                r["action"] = "RECOVER"
                r["comment"] = f"[{out_dex}] Remove LP {token_str}"
                r["_lp"] = (out_dex, "recover")
                r.update(pos)
    elif "collect" in method:
        for r in group_rows:
            if r["action"] in ("BUY", "BONUS") and float(r.get("volume", "0")) > 0:
                r["status"] = "export"
                r["action"] = "BONUS"
                r["comment"] = f"[{out_dex}] Claim LP Rewards"


# ---------------------------------------------------------------------------
# V2 DEX LP (BlazeSwap / Enosys の addLiquidity / removeLiquidity)
# ---------------------------------------------------------------------------

def match_v2_lp(group_rows, sends, receives, ctx) -> bool:
    method = ctx["method"]
    return "addLiquidity" in method or "removeLiquidity" in method


def handle_v2_lp(group_rows, sends, receives, ctx) -> None:
    method = ctx["method"]
    dex = _dex_label(ctx)

    if "addLiquidity" in method:
        underlying, lp_rows = sends, receives
        lend_role, lp_role = "lend", "lp_in"
        lp_comment = "Receive LP Token"
    else:
        underlying, lp_rows = receives, sends
        lend_role, lp_role = "recover", "lp_out"
        lp_comment = "Return LP Token"

    token_str = "-".join(r["base"] for r in underlying)

    for r in underlying:
        r["status"] = "skip"
        r["action"] = "LEND" if lend_role == "lend" else "RECOVER"
        verb = "Provide" if lend_role == "lend" else "Withdraw"
        r["comment"] = f"[{dex}] {verb} LP {token_str}"
        r["_lp"] = (dex, lend_role)
    for r in lp_rows:
        r["action"] = "SENDFEE"
        r["status"] = "skip"
        r["comment"] = f"[{dex}] {lp_comment} {r['base']}"
        r["_lp"] = (dex, lp_role)


# ---------------------------------------------------------------------------
# ICHI Vault (SparkDEXのマネージドボールト)
# ---------------------------------------------------------------------------

def match_ichi_vault(group_rows, sends, receives, ctx) -> bool:
    # 中継コントラクトは未検証で関数名が取れないことがあるので、シェアトークン (IV-…) でも判定する
    return "ICHIVault" in ctx["method"] or any(
        r["base"].startswith("IV-") and float(r.get("volume", "0")) > 0 for r in group_rows)


def handle_ichi_vault(group_rows, sends, receives, ctx) -> None:
    """
    LP と同じ差分計上: 預け入れ=LEND、引き出し=RECOVER。引き出しは預けたトークンと違う組み合わせで
    戻ることがある (片方のトークンで預け、両方で戻る) ので、トークンごとに元本を超えた分が差益、
    シェアを全部返した時に残った元本が差損になる。
    """
    share = next((r["base"] for r in group_rows if r["base"].startswith("IV-")), None)
    for r in group_rows:
        if float(r.get("volume", "0")) <= 0:
            continue
        incoming = r["action"] in ("BUY", "BONUS")
        if r["base"].startswith("IV-"):
            r["action"] = "SENDFEE"
            r["status"] = "skip"
            r["comment"] = f"[ICHI Vault] {'Receive' if incoming else 'Return'} Vault Share {r['base']}"
            r["_lp"] = ("ICHI Vault", "lp_in" if incoming else "lp_out")
        elif not share:
            # シェアの動きが見えない tx は台帳に載せられないので要確認
            r["action"] = "RECOVER" if incoming else "LEND"
            r["status"] = "review"
            r["comment"] = f"[ICHI Vault] {'Withdraw' if incoming else 'Deposit'} {r['base']}"
            continue
        elif incoming:
            r["action"] = "RECOVER"
            r["status"] = "skip"
            r["comment"] = f"[ICHI Vault] Withdraw {r['base']}"
            r["_lp"] = ("ICHI Vault", "recover")
        else:
            r["action"] = "LEND"
            r["status"] = "skip"
            r["comment"] = f"[ICHI Vault] Deposit {r['base']}"
            r["_lp"] = ("ICHI Vault", "lend")
        r["_pool"] = f"ICHI Vault:{share}"


# ---------------------------------------------------------------------------
# Enosys Loans Trove (CDP借入の開閉)
# ---------------------------------------------------------------------------

def match_trove(group_rows, sends, receives, ctx) -> bool:
    method = ctx["method"]
    # Liquity V2 系の BorrowerOperations。担保・借入の出し入れは、行の向きで判定する
    # (関数名は完全一致で見る。Morpho の withdrawCollateral などを巻き込まないため)
    name = method.split("(")[0]
    return ("openTrove" in name or "closeTrove" in name or "adjustTrove" in name or name in (
        "adjustZombieTrove", "addColl", "withdrawColl", "withdrawBold", "repayBold"))


def handle_trove(group_rows, sends, receives, ctx) -> None:
    method = ctx["method"]
    opening = "openTrove" in method
    for r in group_rows:
        if float(r.get("volume", "0")) <= 0:
            continue
        if r["base"] == "WFLR":
            # Liquity系のガス補償デポジット (返却されるので損益なし)
            r["action"] = "SENDFEE"
            r["status"] = "skip"
            r["comment"] = "[Enosys Loans] Gas Compensation Deposit" if opening else "[Enosys Loans] Gas Compensation Refund"
        elif r["base"] == "CDP":
            if r["action"] in ("BUY", "BONUS"):
                r["action"] = "BORROW"
                r["status"] = "export"
                r["comment"] = "[Enosys Loans] Borrow CDP"
                r["_lp"] = ("Enosys Loans", "borrow")
            else:
                # 借入超過分(手数料)は差分エンジンがLOSS行に分離する
                r["action"] = "RETURN"
                r["status"] = "export"
                r["comment"] = "[Enosys Loans] Repay CDP"
                r["_lp"] = ("Enosys Loans", "return")
        else:
            # 担保 (FXRP等) — 元本移動なのでCSVには出さない
            if r["action"] in ("SELL", "SENDFEE"):
                r["action"] = "LEND"
                r["status"] = "skip"
                r["comment"] = f"[Enosys Loans] Deposit Collateral {r['base']}"
                r["_lp"] = ("Enosys Loans", "lend")
            else:
                r["action"] = "RECOVER"
                r["status"] = "skip"
                r["comment"] = f"[Enosys Loans] Withdraw Collateral {r['base']}"
                r["_lp"] = ("Enosys Loans", "recover")


# ---------------------------------------------------------------------------
# FAssets (FXRPミント予約手数料 / 担保プール)
#
# 担保プール: enter でFLRを預けFCPT-XRP-*を受領、exit でFCPTを返しFLRが
# internal txで戻る。LP同様の差分計上 (デリゲート報酬分が差益になる)。
# ---------------------------------------------------------------------------

def _is_fassets_redeem(ctx) -> bool:
    return "AssetManager" in ctx["to_name"] and "redeem" in ctx["method"]


def match_fassets_reserve(group_rows, sends, receives, ctx) -> bool:
    return "reserveCollateral" in ctx["method"] or _is_fassets_redeem(ctx)


def handle_fassets_reserve(group_rows, sends, receives, ctx) -> None:
    redeem = _is_fassets_redeem(ctx)
    for r in group_rows:
        if r["base"] == "FLR" and float(r.get("volume", "0")) > 0:
            # ミント予約手数料 (エージェントに支払われ戻らない) / 償還時のエグゼキューター手数料 = 経費。
            # XRPL Tax for Cryptact 側のミント手数料と揃えて DEFIFEE。
            # (Smart Account でオペレーターが立て替えた分は XRPL 側で計上済みで、ここには現れない)
            r["action"] = "DEFIFEE"
            r["status"] = "export"
            r["comment"] = "[FAssets] Redemption Executor Fee" if redeem else "[FAssets] Collateral Reservation Fee"


# sFLR (Sceptre) のアンステーク: requestUnlock で申請 → 約14.5日後に redeem で WFLR を受け取る
SCEPTRE_SFLR = "0x12e605bc104e93b45e1ad99f9e555f659051c2bb"


def match_sflr_unlock(group_rows, sends, receives, ctx) -> bool:
    return ctx["to_addr"] == SCEPTRE_SFLR and any(
        k in ctx["method"] for k in ("requestUnlock", "redeem", "cancelUnlock", "cancelPendingUnlock", "cancelRedeemableUnlock"))


def handle_sflr_unlock(group_rows, sends, receives, ctx) -> None:
    """
    申請した sFLR は Transfer イベントなしで減るので、申請・取り消しの tx には資産の動きが出ない (ガス代だけ)。
    受け取りの量は redeem の時点で決まる (Redeem イベント、取得時に tx の "_sflr_redeem" に付く) ので、
    redeem を sFLR → 受け取ったトークン の Swap として出す。数量が取れない場合は要確認。
    """
    method = ctx["method"]
    tag = LST_MAP["sFLR"][1]
    if "redeem" not in method:
        label = "Request Unlock sFLR（受け取り時に Swap として計上）" if "requestUnlock" in method else "Cancel Unlock sFLR"
        for r in group_rows:
            r["comment"] = f"{tag} {label}"
        return
    info = (ctx.get("tx_info") or {}).get("_sflr_redeem")
    for r in receives:
        if r["base"] not in LST_MAP["sFLR"][0]:
            continue
        if info and Decimal(info["shares"]) > 0:
            r.update(action="SELL", status="export", base="sFLR", _tok=("sFLR", SCEPTRE_SFLR),
                     volume=info["shares"], counter=r["base"], _ctok=r.get("_tok"),
                     counter_volume=r["volume"],
                     price=ratio_str(Decimal(r["volume"]), Decimal(info["shares"])),
                     comment=f"{tag} Unstake sFLR（Unlock 後の受け取り）")
        else:
            r["status"] = "review"
            r["comment"] = f"{tag} Redeem sFLR（返した sFLR の量を取得できませんでした）"


# 報酬の自動クレーム (ClaimSetupManager): エグゼキューターの登録時に払う手数料
def match_claim_setup(group_rows, sends, receives, ctx) -> bool:
    return ctx["to_name"] == "ClaimSetupManager"


def handle_claim_setup(group_rows, sends, receives, ctx) -> None:
    for r in group_rows:
        if r["base"] == "FLR" and r["action"] == "SELL" and float(r.get("volume", "0")) > 0:
            # setClaimExecutors / setAutoClaiming に付けて送る FLR = エグゼキューターへの登録料 (戻らない) = 経費
            r["action"] = "DEFIFEE"
            r["status"] = "export"
            r["comment"] = "[Auto-Claim] Executor Registration Fee"


def _is_fassets_pool_token(symbol: str) -> bool:
    return symbol.startswith("FCPT-")


def match_fassets_pool(group_rows, sends, receives, ctx) -> bool:
    return any(_is_fassets_pool_token(r["base"]) for r in group_rows)


def handle_fassets_pool(group_rows, sends, receives, ctx) -> None:
    proto = "FAssets Pool"
    for r in group_rows:
        if float(r.get("volume", "0")) <= 0:
            continue
        if _is_fassets_pool_token(r["base"]):
            entering = r["action"] in ("BUY", "BONUS")
            r["action"] = "SENDFEE"
            r["status"] = "skip"
            r["comment"] = f"[FAssets] {'Receive' if entering else 'Return'} Pool Token {r['base']}"
            r["_lp"] = (proto, "lp_in" if entering else "lp_out")
        elif r["action"] in ("SELL", "SENDFEE"):
            # FLR等を預入
            r["action"] = "LEND"
            r["status"] = "skip"
            r["comment"] = f"[FAssets] Enter Collateral Pool ({r['base']})"
            r["_lp"] = (proto, "lend")
        else:
            r["action"] = "RECOVER"
            r["status"] = "skip"
            r["comment"] = f"[FAssets] Exit Collateral Pool ({r['base']})"
            r["_lp"] = (proto, "recover")


# ---------------------------------------------------------------------------
# ボールト預入 (Upshift earnXRP / MXRPY / More Markets 等)
# レンディング扱い: 預入=LEND、引出=RECOVER、増加分は差分計上でBONUS化
# ---------------------------------------------------------------------------

# シェアトークン → (プロトコル名, 原資産, 原資産をやり取りするvault側のアドレス)
# 引き出しが「シェアを返す」と「後日 原資産が届く」の別txになる (キュー方式) ことがあるので、
# 同じtx内での対応付けではなく、シェアトークンと vault のアドレスで判定する。
VAULTS = {
    "earnXRP": ("Upshift", "FXRP", {"0x373d7d201c8134d4a2f7b5c63560da217e3dea28"}),
    "MXRPY": ("MXRPY", "FXRP", {"0x2439d4bb753a0f3777d4c9011afacc475ba6b951"}),
    "MoreFXRP+": ("More Markets", "FXRP", {
        "0x65ea525d8bb35f96a4e60958e86c86a849ad6506",  # 引き出しキュー (シェアの返却先)
        "0x2f09e1ee4b04e0b9fbc0a662cd49fc87442c23cf",  # 引き出しの支払い元
    }),
}
VAULT_BY_ADDRESS = {a: share for share, (_, _, addrs) in VAULTS.items() for a in addrs}


def _vault_of(row, shares_in_tx=()):
    """
    この行がどのvaultのものか。
      - シェアトークンそのもの
      - 同じtx内でシェアが動いている場合の、そのvaultの原資産 (預入: 送り先は vault 本体なので住所を問わない)
      - vault 側のアドレスとの原資産のやり取り (キュー方式の後日払いなど)
    """
    if row["base"] in VAULTS:
        return row["base"]
    for share in shares_in_tx:
        if row["base"] == VAULTS[share][1]:
            return share
    share = VAULT_BY_ADDRESS.get(row.get("counterparty", ""))
    if share and row["base"] == VAULTS[share][1]:
        return share
    return None


def match_vault(group_rows, sends, receives, ctx) -> bool:
    return any(_vault_of(r) and float(r.get("volume", "0")) > 0 for r in group_rows)


def handle_vault(group_rows, sends, receives, ctx) -> None:
    """
    レンディング扱い: 預入=LEND、引出=RECOVER、増加分は差分計上でBONUS化。
    キュー方式ではシェアを返した時点ではまだ原資産が届いていないので、
    LPトークンの残高が0になっても差損は自動計上しない (_async)。
    """
    shares_in_tx = [r["base"] for r in group_rows if r["base"] in VAULTS and float(r.get("volume", "0")) > 0]
    for r in group_rows:
        if float(r.get("volume", "0")) <= 0:
            continue
        share = _vault_of(r, shares_in_tx)
        if not share:
            continue
        proto, underlying, _ = VAULTS[share]
        incoming = r["action"] in ("BUY", "BONUS")
        r["status"] = "skip"
        r["_pool"] = f"{proto}:{underlying}"
        r["_async"] = True
        if r["base"] == share:
            r["action"] = "SENDFEE"
            r["comment"] = f"[{proto}] {'Receive' if incoming else 'Return'} Vault Share {share}"
            r["_lp"] = (proto, "lp_in" if incoming else "lp_out")
        elif incoming:
            r["action"] = "RECOVER"
            r["comment"] = f"[{proto}] Withdraw {underlying}"
            r["_lp"] = (proto, "recover")
        else:
            r["action"] = "LEND"
            r["comment"] = f"[{proto}] Deposit {underlying}"
            r["_lp"] = (proto, "lend")


# ---------------------------------------------------------------------------
# ファームステーキング (Enosys farm等のstake呼び出し)
# ---------------------------------------------------------------------------

def match_farm_stake(group_rows, sends, receives, ctx) -> bool:
    method = ctx["method"]
    return (method == "stake" or method.startswith("stake(")) and len(sends) >= 1 and len(receives) == 0


def handle_farm_stake(group_rows, sends, receives, ctx) -> None:
    from .postprocess import _is_lp_token
    for r in sends:
        if _is_lp_token(r["base"]):
            # LP元本はProvide時に計上済み — ファームへの移動は非イベント
            r["action"] = "SENDFEE"
            r["status"] = "skip"
            r["comment"] = f"[Farm] Send LP Token {r['base']}"
        else:
            # 単体トークンのステーキング (HLN等)。解除側の自動検出が
            # できないため元本トラッキングはせず、要確認に留める
            r["comment"] = f"[Farm] Stake {r['base']}"
            r["status"] = "review"


# ---------------------------------------------------------------------------
# Spectra (PT = Principal Token / 固定利回り)
#
# stXRP等を割引価格のPTに換え、満期に額面で償還する「割引債」型。
# 経済実態はレンディングなので差分計上: 預入=LEND、償還/売却=RECOVER、
# 額面との差額(固定利回り)は差分エンジンがBONUS化する。
# PTトークン自体はCSVに出さない (Cryptact未対応のため好都合)。
# ---------------------------------------------------------------------------

def _is_pt_token(symbol: str) -> bool:
    return symbol.startswith("PT-")


def match_spectra(group_rows, sends, receives, ctx) -> bool:
    return any(_is_pt_token(r["base"]) and float(r.get("volume", "0")) > 0 for r in group_rows)


def _pt_tokens(pt_symbol: str):
    """'PT-stXRP(FXRP)-2026/08/27' → ('stXRP', 'FXRP') = (利回りの付くトークン IBT, その原資産)。"""
    try:
        body = pt_symbol[3:]
        ibt, rest = body.split("(", 1)
        return ibt, rest.split(")", 1)[0]
    except ValueError:
        return None, None


def handle_spectra(group_rows, sends, receives, ctx) -> None:
    """
    PT の満期償還は IBT (stXRP など) で払われる。原資産 (FXRP) で PT を買った場合は、
    Spectra の中で FXRP → stXRP に変換されてから PT になるので、
    「FXRP → stXRP の変換 (LST のステークと同じく Swap)」+「stXRP の預け入れ」として記録する。
    そうしないと、FXRP で預けた元本が満期時に丸ごと差益として計上されてしまう。
    変換レートは 1:1 とみなす (同じ日の PT 比率で、stXRP と FXRP の差は 0.01% 程度)。
    """
    proto = "Spectra"
    pt_row = next((r for r in group_rows if _is_pt_token(r["base"])), None)
    ibt, underlying = _pt_tokens(pt_row["base"]) if pt_row else (None, None)
    for r in group_rows:
        if float(r.get("volume", "0")) <= 0:
            continue
        if (ibt and underlying and ibt != underlying and r["base"] == underlying
                and r["action"] in ("SELL", "SENDFEE")):
            conv = dict(r)
            conv.pop("_lp", None)
            conv.pop("counterparty", None)
            conv["action"] = "SELL"
            conv["status"] = "export"
            conv["counter"] = ibt
            conv["counter_volume"] = conv["volume"]
            conv["price"] = "1"
            conv["comment"] = f"[Spectra] {underlying} → {ibt} 変換（PT購入時。1:1とみなす）"
            ctx["new_rows"].append(conv)
            r["base"] = ibt
            r["fee"] = "0"   # ガス代は変換の行 (出力対象) に残す
        if _is_pt_token(r["base"]):
            incoming = r["action"] in ("BUY", "BONUS")
            r["action"] = "SENDFEE"
            r["status"] = "skip"
            r["comment"] = f"[{proto}] {'Receive' if incoming else 'Redeem/Sell'} PT {r['base']}"
            r["_lp"] = (proto, "lp_in" if incoming else "lp_out")
        elif r["action"] in ("SELL", "SENDFEE"):
            r["action"] = "LEND"
            r["status"] = "skip"
            r["comment"] = f"[{proto}] Deposit {r['base']} (Fixed Yield)"
            r["_lp"] = (proto, "lend")
        elif r["action"] in ("BUY", "BONUS"):
            r["action"] = "RECOVER"
            r["status"] = "skip"
            r["comment"] = f"[{proto}] Withdraw {r['base']}"
            r["_lp"] = (proto, "recover")


# ---------------------------------------------------------------------------
# ブリッジ (Stargate / OFT send)
# ---------------------------------------------------------------------------

def match_bridge(group_rows, sends, receives, ctx) -> bool:
    return "Stargate" in ctx["to_name"] or ctx["method"] == "send" or ctx["method"].startswith("send(")


def handle_bridge(group_rows, sends, receives, ctx) -> None:
    for r in group_rows:
        if float(r.get("volume", "0")) <= 0:
            continue
        if r["base"] == "FLR" and r["action"] in ("SELL", "SENDFEE"):
            # 同梱のネイティブFLRはブリッジ手数料
            r["action"] = "SENDFEE"
            r["status"] = "export"
            r["comment"] = "[Bridge] Bridge Fee"
        elif r["action"] in ("SELL", "SENDFEE"):
            r["status"] = "review"
            r["comment"] = f"[Bridge] Send {r['base']} to another chain (自分宛なら出力不要)"


# ---------------------------------------------------------------------------
# Lending / Borrowing (Kinetic / Mystic)
# ---------------------------------------------------------------------------

def match_lending(group_rows, sends, receives, ctx) -> bool:
    to_name = ctx["to_name"]
    return (
        "CErc20Delegat" in to_name
        or "Lending " in to_name
        or "VaultV2" in to_name
        or "MorphoMarketV1AdapterV2" in to_name
        or "Morpho" in to_name
    )


def handle_lending(group_rows, sends, receives, ctx) -> None:
    method = ctx["method"]
    to_name = ctx["to_name"]
    if "VaultV2" in to_name or "Morpho" in to_name:
        lending_protocol = "Mystic"
    else:
        lending_protocol = "Kinetic"

    # 元本移動(LEND/RECOVER)は損益ゼロなのでCSVに出さない(skip)。
    # 利息 = 引出超過分は差分エンジンがBONUS行として自動生成する。
    # 借入(BORROW/RETURN)は在庫整合のためexport。返済超過分(利息)は
    # 差分エンジンがLOSS行として自動生成する。
    if "mint" in method or "deposit" in method or "supply" in method:  # Morpho: supply / supplyCollateral
        _relabel_principal(
            group_rows,
            principal_actions=("SELL", "SENDFEE"),
            drop_actions=("BUY", "BONUS"),
            action="LEND",
            comment_fmt=f"[{lending_protocol}] Supply {{token}}",
            status="skip",
            lp_tag=(lending_protocol, "lend"),
        )
    elif "redeem" in method or "withdraw" in method:
        _relabel_principal(
            group_rows,
            principal_actions=("BUY", "BONUS"),
            drop_actions=("SELL", "SENDFEE"),
            action="RECOVER",
            comment_fmt=f"[{lending_protocol}] Withdraw {{token}}",
            status="skip",
            lp_tag=(lending_protocol, "recover"),
        )
    elif "borrow" in method:
        _relabel_principal(
            group_rows,
            principal_actions=("BUY", "BONUS"),
            drop_actions=("SELL", "SENDFEE"),
            action="BORROW",
            comment_fmt=f"[{lending_protocol}] Borrow {{token}}",
            status="export",
            lp_tag=(lending_protocol, "borrow"),
        )
    elif "repayBorrow" in method or method.startswith("repay"):  # Kinetic: repayBorrow / Mystic (Morpho): repay
        _relabel_principal(
            group_rows,
            principal_actions=("SELL", "SENDFEE"),
            drop_actions=("BUY", "BONUS"),
            action="RETURN",
            comment_fmt=f"[{lending_protocol}] Repay {{token}}",
            status="export",
            lp_tag=(lending_protocol, "return"),
        )


def _relabel_principal(group_rows, principal_actions, drop_actions, action, comment_fmt, status="skip", lp_tag=None) -> None:
    """元本トークンの行を貸借アクションに書き換え、対になる受領トークン行を削除する。"""
    target_token_row = None
    for r in group_rows:
        if r["action"] in principal_actions and float(r.get("volume", "0")) > 0:
            target_token_row = r
            break
    if not target_token_row:
        return
    target_token = target_token_row["base"]
    for r in group_rows:
        if r is target_token_row:
            r["status"] = status
            r["action"] = action
            r["comment"] = comment_fmt.format(token=target_token)
            if lp_tag:
                r["_lp"] = lp_tag
        elif float(r.get("volume", "0")) > 0 and r["action"] in drop_actions:
            r["_delete"] = True


# ---------------------------------------------------------------------------
# Delayed Unstaking (requestWithdrawal / claimWithdrawal)
# ---------------------------------------------------------------------------

def match_delayed_unstake(group_rows, sends, receives, ctx) -> bool:
    method = ctx["method"]
    return "requestWithdrawal" in method or "claimWithdrawal" in method


def handle_delayed_unstake(group_rows, sends, receives, ctx) -> None:
    """
    stFLR (SparkDEX) の遅延アンステーク。コントラクト上、申請 (requestWithdrawal) の時点で
    受け取る FLR の量が決まり (WithdrawalRequested イベント、取得時に tx の "_withdraw_req" に付く)、
    確定後の claim ではその量の WFLR が届くだけ。
    → 申請を stFLR→WFLR の Swap として出し、claim は計上済みの受け取りとしてスキップする。
    申請の受取量が取れない場合は従来どおり要確認。
    """
    method = ctx["method"]
    dex_tag = f"[{ctx['dex_name']}]" if ctx["dex_name"] else "[SparkDEX]"
    req = (ctx.get("tx_info") or {}).get("_withdraw_req")
    if "requestWithdrawal" in method:
        for r in group_rows:
            if r["action"] in ("SELL", "SENDFEE") and float(r.get("volume", "0")) > 0:
                r["action"] = "SELL"
                if req and r["base"] in LST_MAP and Decimal(req["shares"]) > 0:
                    lst_tag = LST_MAP[r["base"]][1]
                    sent, shares = Decimal(r["volume"]), Decimal(req["shares"])
                    r["status"] = "export"
                    r["volume"] = req["shares"]
                    r["counter"] = "WFLR"
                    r["_ctok"] = ("WFLR", WFLR_ADDRESS)
                    r["counter_volume"] = req["flr"]
                    r["price"] = ratio_str(Decimal(req["flr"]), shares)
                    r["comment"] = f"{lst_tag} Unstake {r['base']}（申請時に受取量が確定・後日WFLRで受け取り）"
                    if sent - shares > Decimal("0.000001"):
                        # 申請の手数料 (stFLR で差し引かれる分)
                        ctx["new_rows"].append(dict(
                            r, action="LOSS", volume=format_decimal(sent - shares), price="", counter="JPY",
                            counter_volume="", _ctok=None, fee="0", comment=f"{lst_tag} Unstake Fee {r['base']}"))
                else:
                    r["status"] = "review"
                    r["comment"] = f"{dex_tag} Request Unstake {r['base']}"
    elif "claimWithdrawal" in method:
        for r in group_rows:
            if r["action"] in ("BUY", "BONUS") and float(r.get("volume", "0")) > 0:
                r["action"] = "BUY"
                if ctx["to_addr"] in DELAYED_UNSTAKE_LST:
                    lst = DELAYED_UNSTAKE_LST[ctx["to_addr"]]
                    r["status"] = "skip"
                    r["comment"] = f"{LST_MAP[lst][1]} Claim Unstaked {r['base']}（申請時に {lst} の Unstake として計上済み）"
                else:
                    r["status"] = "review"
                    r["comment"] = f"{dex_tag} Claim Unstaked {r['base']}"


# ---------------------------------------------------------------------------
# レジストリ本体 — 上から順に評価し、最初にマッチしたものだけ実行される
# ---------------------------------------------------------------------------

PROTOCOL_HANDLERS = [
    (match_enosys_sp, handle_enosys_sp),
    (match_trove, handle_trove),
    (match_fassets_reserve, handle_fassets_reserve),
    (match_fassets_pool, handle_fassets_pool),
    (match_claim_setup, handle_claim_setup),
    (match_sflr_unlock, handle_sflr_unlock),
    (match_spectra, handle_spectra),
    (match_vault, handle_vault),
    (match_v3_position_manager, handle_v3_position_manager),
    (match_ichi_vault, handle_ichi_vault),
    (match_v2_lp, handle_v2_lp),
    (match_lending, handle_lending),
    (match_delayed_unstake, handle_delayed_unstake),
    (match_farm_stake, handle_farm_stake),
    (match_bridge, handle_bridge),
]
