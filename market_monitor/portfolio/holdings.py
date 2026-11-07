"""두 계좌 보유 종목의 합산 평가액과 비중.

평가액은 각 증권사 잔고 응답의 평가금액(키움 evlt_amt, LS FcurrEvalAmt)을 쓴다. 수량×현재가와 대조해 어긋나면
해당 포지션을 미평가로 두고 전체 계산을 불완전으로 표시한다.

비중 분모는 주식 평가액과 현금의 합(total_assets)이다. 현금은 스냅샷의 settled_cash, 즉 알려진 미결제 매매가 모두
결제된 뒤의 예수금이다. 잔고 수량에는 미결제 매매가 이미 들어 있어 결제 전 예수금과 짝지으면 막 산 종목이 주식과
현금에 두 번 잡힌다. 원화는 ECOS 매매기준율로 USD 환산한다. 한 계좌라도 현금을 확인·환산하지 못하면 주식 평가액만
분모로 쓰고(stocks_only) 사유를 issues에 남긴다. 금액은 Decimal로 계산한다. 금액 필드는 반올림 전 값이고 *_display만
센트로 반올림한다.
"""
from decimal import ROUND_HALF_EVEN, Decimal

from ..market.sectors import ASSET_CLASSES, asset_class


BROKER_LABELS = {"kiwoom": "키움", "ls": "LS"}
PCT = Decimal("0.01")
CONCENTRATION_LIMIT_PCT = Decimal(30)  # 기능 구성도: 확신 종목도 두 계좌 합산 최대 30%
CENT = Decimal("0.01")


def _decimal(value):
    if value is None:
        return None
    result = Decimal(value)
    return result if result.is_finite() else None


def _valued(position):
    """(평가액, 사유). 평가액은 USD 포지션에서 응답 평가금액이 수량×현재가와 일치할 때만 돌려준다."""
    if position.get("currency") != "USD":
        return None, "non_usd"
    quantity, price, value = (_decimal(position.get(k)) for k in ("quantity", "price", "market_value"))
    if value is None or price is None:
        return None, "missing_valuation"
    if quantity is None or quantity < 0 or price < 0 or value < 0:
        return None, "invalid_valuation"
    # 증권사 평가금액은 반올림된 값이다. 1센트 또는 0.05% 중 큰 범위 안이면 같은 값으로 본다.
    if abs(quantity * price - value) > max(CENT, value * Decimal("0.0005")):
        return None, "valuation_mismatch"
    return value, None


def _quantity(value):
    """표시용 수량. 98.000000 → 98, 0.50 → 0.5. 값은 바꾸지 않는다."""
    value = Decimal(value)
    return format(value.normalize() if value != 0 else Decimal(0), "f")


def _pct(part, total):
    return format((part / total * 100).quantize(PCT, ROUND_HALF_EVEN), "f")


def _settled_cash(broker, snapshot, rate):
    """(계좌 현금, 사유). 계좌 현금은 USD·KRW 합계와 근거 필드."""
    entries = snapshot.get("settled_cash")
    if entries is None:
        issue = snapshot.get("settled_cash_issue")
        return None, issue if issue in {"inconsistent_cash", "invalid_cash"} else "cash_unavailable"
    totals = {"USD": Decimal(0), "KRW": Decimal(0)}
    for entry in entries:
        value = _decimal(entry.get("amount"))
        if value is None:
            return None, "invalid_cash"
        if entry.get("currency") in totals:
            totals[entry["currency"]] += value
        elif value != 0:
            return None, "unconverted_cash"
    if totals["KRW"] != 0 and rate is None:
        return None, "krw_cash_without_fx"
    dates = [e["settles_on"] for e in entries if e.get("settles_on") and e.get("currency") in totals]
    return {"broker": broker, "usd": totals["USD"], "krw": totals["KRW"], "settles_on": max(dates) if dates else None,
            "sources": [f'{e.get("source")}.{e.get("source_field")}' for e in entries if e.get("currency") in totals]}, None


def _money(value):
    return format(value, "f"), format(value.quantize(CENT, ROUND_HALF_EVEN), "f")


def build(statuses, usd_krw=None, profiles=None):
    """statuses: {broker: BrokerSync.status()}. usd_krw: 시장 지표 항목(available/stale일 때만 원화 환산).
    profiles: {symbol: LS g3104 분류 또는 None}. 없으면 분류 미확인."""
    profiles = profiles or {}
    rate = _decimal(usd_krw.get("value")) if usd_krw and usd_krw.get("state") in {"available", "stale"} else None
    if rate is not None and rate <= 0:
        rate = None
    merged, accounts, issues, cash_accounts, cash_issues = {}, [], [], [], []
    for broker in ("kiwoom", "ls"):
        status = statuses.get(broker) or {}
        snapshot = status.get("snapshot")
        if not snapshot:
            accounts.append({"broker": broker, "state": status.get("state", "not_queried"), "collected_at": None})
            issues.append({"broker": broker, "reason": "no_snapshot"})
            continue
        accounts.append({"broker": broker, "state": status.get("state"), "collected_at": snapshot.get("collected_at"),
                         "stale": status.get("state") != "fetched_unreconciled"})
        if status.get("state") != "fetched_unreconciled":
            issues.append({"broker": broker, "reason": "previous_snapshot"})
        account_cash, reason = _settled_cash(broker, snapshot, rate)
        if reason:
            cash_issues.append({"broker": broker, "reason": reason})
        else:
            cash_accounts.append(account_cash)
        for position in snapshot.get("positions", []):
            value, reason = _valued(position)
            key = (position["symbol"], position["currency"])
            entry = merged.setdefault(key, {"symbol": position["symbol"], "currency": position["currency"], "name": None,
                                            "exchange": None, "quantity": Decimal(0), "value": Decimal(0),
                                            "valued": True, "accounts": []})
            entry["name"] = entry["name"] or position.get("name")
            entry["exchange"] = entry["exchange"] or position.get("exchange")
            entry["quantity"] += Decimal(position["quantity"])
            entry["accounts"].append({"broker": broker, "quantity": _quantity(position["quantity"]),
                                      "market_value": position.get("market_value") if value is not None else None,
                                      "price": position.get("price"), "issue": reason})
            if value is None:
                entry["valued"] = False
                issues.append({"broker": broker, "symbol": position["symbol"], "reason": reason})
            else:
                entry["value"] += value
    total = sum((e["value"] for e in merged.values() if e["valued"]), Decimal(0))
    with_cash = bool(cash_accounts) and not cash_issues
    issues += cash_issues
    if with_cash:
        usd = sum((a["usd"] for a in cash_accounts), Decimal(0))
        krw = sum((a["krw"] for a in cash_accounts), Decimal(0))
        krw_in_usd = krw / rate if krw else Decimal(0)
        cash_total = usd + krw_in_usd
        denominator = total + cash_total
    else:
        denominator = total
    holdings = []
    for entry in merged.values():
        valued = entry["valued"]
        profile = profiles.get(entry["symbol"])
        kind = asset_class(profile)
        holdings.append({
            "symbol": entry["symbol"], "name": entry["name"], "exchange": entry["exchange"], "currency": entry["currency"],
            "quantity": _quantity(entry["quantity"]),
            "market_value": format(entry["value"], "f") if valued else None,
            "market_value_display": format(entry["value"].quantize(CENT, ROUND_HALF_EVEN), "f") if valued else None,
            "weight_pct": _pct(entry["value"], denominator) if valued and denominator > 0 else None,
            "over_concentration_limit": bool(valued and denominator > 0
                                             and entry["value"] / denominator * 100 > CONCENTRATION_LIMIT_PCT),
            "asset_class": kind, "asset_class_label": ASSET_CLASSES[kind],
            "industry": profile.get("industry") if profile else None,
            "leverage": profile.get("leverage") if profile else None,
            "accounts": entry["accounts"]})
    holdings.sort(key=lambda h: (h["market_value"] is None, -Decimal(h["market_value"] or 0), h["symbol"]))
    result = {"basis": "total_assets" if with_cash else "stocks_only", "currency": "USD", "holdings": holdings,
              "total_market_value": format(total, "f") if merged else None,
              "total_market_value_display": format(total.quantize(CENT, ROUND_HALF_EVEN), "f") if merged else None,
              "cash": None, "total_assets": None, "total_assets_display": None,
              "accounts": accounts, "issues": issues, "complete": not issues and bool(merged),
              "note": "비중 분모는 확인된 USD 주식 평가액과 결제 예정분을 반영한 현금(원화는 ECOS 매매기준율로 환산)의 합계입니다."
                      if with_cash else "비중 분모는 확인된 USD 주식 평가액 합계입니다. 현금을 확인하지 못해 제외했습니다."}
    if with_cash:
        result["cash"] = {
            **dict(zip(("usd", "usd_display"), _money(usd))), "krw": format(krw, "f"),
            **dict(zip(("krw_in_usd", "krw_in_usd_display"), _money(krw_in_usd))),
            **dict(zip(("total_usd", "total_usd_display"), _money(cash_total))),
            "weight_pct": _pct(cash_total, denominator) if denominator > 0 else None,
            "accounts": [dict(a, usd=format(a["usd"], "f"), krw=format(a["krw"], "f")) for a in cash_accounts]}
        result["total_assets"], result["total_assets_display"] = _money(denominator)
        if rate:
            result["total_assets_krw"] = format(((total + usd) * rate + krw).quantize(Decimal(1), ROUND_HALF_EVEN), "f")
    if rate and merged:
        result["total_market_value_krw"] = format((total * rate).quantize(Decimal(1), ROUND_HALF_EVEN), "f")
    if rate:
        result["fx"] = {k: usd_krw.get(k) for k in ("value", "as_of_date", "source", "state")}
    return result
