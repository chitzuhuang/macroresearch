#!/usr/bin/env python3
"""Fetch and normalize the latest Taiwan stock market close snapshot."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import ssl
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8 fallback
    ZoneInfo = None  # type: ignore[assignment]


TWSE_QUOTES_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_INDEX_URL = "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX"
TWSE_T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TPEX_QUOTES_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
TPEX_INDEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_index"
TPEX_QFII_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_qfii_trading"
TAIFEX_CSV_URL = "https://www.taifex.com.tw/cht/3/futDataDown"
# TWSE's daily STOCK_DAY_ALL file can lag a session behind TPEx's OpenAPI (it
# is only finalized well after the close). For watchlist/holdings quotes we
# want same-day prices, so fall back to TWSE's real-time quote endpoint.
MIS_QUOTE_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
# TWSE's openapi.twse.com.tw mirrors (STOCK_DAY_ALL, MI_INDEX, FMTQIK) can lag
# a full session behind. The same data is available same-day on TWSE's own
# website (non-mirrored "rwd" endpoints), so use that for the TAIEX headline.
TWSE_RWD_FMTQIK_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"
# Exchange-listed ETFs and similar products commonly use codes beginning with 0.
# A four-digit code beginning with 1-9 is a conservative common-stock proxy.
COMMON_STOCK_RE = re.compile(r"^[1-9]\d{3}$")

ALIASES = {
    "date": ["date", "日期", "資料日期", "交易日期"],
    "market": ["market", "市場", "交易所"],
    "code": ["code", "stock_id", "stock_code", "證券代號", "股票代號", "代號"],
    "name": ["name", "stock_name", "證券名稱", "股票名稱", "名稱", "公司名稱"],
    "open": ["open", "openingprice", "開盤價", "開市價"],
    "high": ["high", "highestprice", "最高價"],
    "low": ["low", "lowestprice", "最低價"],
    "close": ["close", "closingprice", "收盤價", "收市價"],
    "change": ["change", "漲跌", "漲跌價差"],
    "volume": ["volume", "tradevolume", "tradingshares", "成交股數", "成交量"],
    "value": ["value", "tradevalue", "transactionamount", "成交金額", "成交值"],
    "transactions": ["transactions", "transaction", "transactionnumber", "成交筆數", "筆數"],
}


def _now_taipei() -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo("Asia/Taipei"))
        except Exception:
            pass
    return datetime.now(timezone(timedelta(hours=8), name="Asia/Taipei"))


def _fetch_json(url: str) -> Any:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "taiwan-stock-daily-report/1.0",
        },
    )
    context = ssl.create_default_context()
    strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict_flag:
        context.verify_flags &= ~strict_flag
    with urlopen(request, timeout=30, context=context) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def _fetch_mis_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch same-day real-time/closing quotes for specific codes from TWSE's
    MIS endpoint, trying both the TWSE (tse_) and TPEx (otc_) listings since
    the market isn't known ahead of time. Used to refresh watchlist prices
    when the daily OpenAPI files haven't caught up to the current session."""
    if not codes:
        return {}
    channels = []
    for code in codes:
        channels.append(f"tse_{code}.tw")
        channels.append(f"otc_{code}.tw")
    query = urlencode({"ex_ch": "|".join(channels), "json": "1", "delay": "0"})
    url = f"{MIS_QUOTE_URL}?{query}"
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    context = ssl.create_default_context()
    strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict_flag:
        context.verify_flags &= ~strict_flag
    with urlopen(request, timeout=30, context=context) as response:
        payload = json.loads(response.read().decode("utf-8-sig"))

    quotes: dict[str, dict[str, Any]] = {}
    for item in payload.get("msgArray", []):
        code = item.get("c")
        if not code:
            continue
        close = _number(item.get("z"))
        prev_close = _number(item.get("y"))
        if close is None:
            close = prev_close
        if close is None:
            continue
        change = round(close - prev_close, 4) if prev_close is not None else None
        quotes[code] = {
            "market": "TWSE" if item.get("ex") == "tse" else "TPEX",
            "code": code,
            "name": item.get("n") or "",
            "date": _iso_date(item.get("d")),
            "close": close,
            "change": change,
            "change_pct": _change_pct(close, change),
        }
    return quotes


def _number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("％", "").replace("%", "")
    text = text.replace("＋", "+").replace("－", "-")
    if not text or text in {"--", "---", "N/A", "null", "None"}:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _iso_date(value: Any) -> str | None:
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 7:
        year = int(digits[:3]) + 1911
        return f"{year:04d}-{digits[3:5]}-{digits[5:7]}"
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return str(value).strip() or None


def _change_pct(close: float | None, change: float | None) -> float | None:
    if close is None or change is None:
        return None
    previous = close - change
    if previous <= 0:
        return None
    return round(change / previous * 100.0, 4)


def _normalized_row(
    *,
    market: str,
    date: Any,
    code: Any,
    name: Any,
    open_: Any,
    high: Any,
    low: Any,
    close: Any,
    change: Any,
    volume: Any,
    value: Any,
    transactions: Any,
) -> dict[str, Any]:
    close_value = _number(close)
    change_value = _number(change)
    return {
        "market": market.upper() or "UPLOADED",
        "date": _iso_date(date),
        "code": str(code).strip(),
        "name": str(name or "").strip(),
        "open": _number(open_),
        "high": _number(high),
        "low": _number(low),
        "close": close_value,
        "change": change_value,
        "change_pct": _change_pct(close_value, change_value),
        "volume_shares": _integer(volume),
        "value_twd": _integer(value),
        "transactions": _integer(transactions),
    }


def _normalize_twse(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _normalized_row(
            market="TWSE",
            date=row.get("Date"),
            code=row.get("Code"),
            name=row.get("Name"),
            open_=row.get("OpeningPrice"),
            high=row.get("HighestPrice"),
            low=row.get("LowestPrice"),
            close=row.get("ClosingPrice"),
            change=row.get("Change"),
            volume=row.get("TradeVolume"),
            value=row.get("TradeValue"),
            transactions=row.get("Transaction"),
        )
        for row in rows
    ]


def _normalize_tpex(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _normalized_row(
            market="TPEX",
            date=row.get("Date"),
            code=row.get("SecuritiesCompanyCode"),
            name=row.get("CompanyName"),
            open_=row.get("Open"),
            high=row.get("High"),
            low=row.get("Low"),
            close=row.get("Close"),
            change=row.get("Change"),
            volume=row.get("TradingShares"),
            value=row.get("TransactionAmount"),
            transactions=row.get("TransactionNumber"),
        )
        for row in rows
    ]


def _key_map(row: dict[str, Any]) -> dict[str, str]:
    return {re.sub(r"[\s_\-]", "", str(key)).lower(): str(key) for key in row}


def _pick(row: dict[str, Any], logical_name: str) -> Any:
    normalized_keys = _key_map(row)
    for alias in ALIASES[logical_name]:
        key = normalized_keys.get(re.sub(r"[\s_\-]", "", alias).lower())
        if key is not None:
            return row.get(key)
    return None


def _load_uploaded(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            if "data" in data:
                data = data["data"]
            elif "rows" in data:
                data = data["rows"]
            else:
                raise ValueError("JSON object must contain a data or rows array.")
        if not isinstance(data, list):
            raise ValueError("JSON must contain a row array, or an object with data/rows.")
        return [dict(row) for row in data if isinstance(row, dict)]
    if suffix == ".xlsx":
        try:
            import openpyxl  # type: ignore
        except ImportError as exc:
            raise RuntimeError("XLSX requires openpyxl; export the sheet as UTF-8 CSV.") from exc
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sheet = workbook.active
        values = sheet.iter_rows(values_only=True)
        headers = [str(value or "").strip() for value in next(values)]
        return [dict(zip(headers, row)) for row in values]
    raise ValueError("Supported inputs are CSV, JSON, and XLSX.")


def _normalize_uploaded(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for row in rows:
        normalized.append(
            _normalized_row(
                market=str(_pick(row, "market") or "UPLOADED"),
                date=_pick(row, "date"),
                code=_pick(row, "code"),
                name=_pick(row, "name"),
                open_=_pick(row, "open"),
                high=_pick(row, "high"),
                low=_pick(row, "low"),
                close=_pick(row, "close"),
                change=_pick(row, "change"),
                volume=_pick(row, "volume"),
                value=_pick(row, "value"),
                transactions=_pick(row, "transactions"),
            )
        )
    return [row for row in normalized if row["code"] not in {"", "None"}]


def _summarize_market(rows: list[dict[str, Any]]) -> dict[str, Any]:
    common = [row for row in rows if COMMON_STOCK_RE.fullmatch(row["code"])]
    valid_changes = [row for row in common if row["change"] is not None]
    dates = sorted({row["date"] for row in common if row["date"]})

    def public_row(row: dict[str, Any]) -> dict[str, Any]:
        return dict(row)

    turnover = sorted(common, key=lambda row: row["value_twd"] or -1, reverse=True)[:10]
    return {
        "data_date": dates[-1] if dates else None,
        "all_instruments": len(rows),
        "common_stocks": len(common),
        "advancers": sum(1 for row in valid_changes if row["change"] > 0),
        "decliners": sum(1 for row in valid_changes if row["change"] < 0),
        "unchanged": sum(1 for row in valid_changes if row["change"] == 0),
        "missing_change": len(common) - len(valid_changes),
        "total_volume_shares": sum(row["volume_shares"] or 0 for row in common),
        "total_value_twd": sum(row["value_twd"] or 0 for row in common),
        "top_turnover": [public_row(row) for row in turnover],
    }


def _normalize_indices(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    wanted = {"發行量加權股價指數"}
    normalized = []
    for row in rows:
        name = str(row.get("指數", "")).strip()
        if name not in wanted:
            continue
        change = _number(row.get("漲跌點數"))
        sign = str(row.get("漲跌", "")).strip()
        if change is not None and sign == "-":
            change = -abs(change)
        normalized.append(
            {
                "date": _iso_date(row.get("日期")),
                "name": name,
                "close": _number(row.get("收盤指數")),
                "change": change,
                "change_pct": _number(row.get("漲跌百分比")),
            }
        )
    return normalized


def _fetch_twse_taiex_realtime(as_of_date: str | None) -> dict[str, Any] | None:
    """Fetch today's TAIEX close/change plus total market volume and value
    from TWSE's own (non-mirrored) site, which updates same-day even when
    the openapi.twse.com.tw mirrors are still a session behind."""
    query_date = (as_of_date or _now_taipei().strftime("%Y-%m-%d")).replace("-", "")
    url = f"{TWSE_RWD_FMTQIK_URL}?response=json&date={query_date}"
    try:
        payload = _fetch_json(url)
    except Exception:
        return None
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not rows:
        return None
    last = rows[-1]
    if len(last) < 5:
        return None
    close = _number(last[4])
    change = _number(last[5]) if len(last) > 5 else None
    return {
        "date": _iso_date(last[0]),
        "name": "發行量加權股價指數",
        "close": close,
        "change": change,
        "change_pct": _change_pct(close, change),
        "volume_shares": _integer(last[1]),
        "value_twd": _integer(last[2]),
    }


def _fetch_tpex_composite_index(as_of_date: str | None) -> dict[str, Any] | None:
    try:
        rows = _fetch_json(TPEX_INDEX_URL)
    except Exception:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    target = (as_of_date or "").replace("-", "")
    chosen = None
    for row in rows:
        if str(row.get("Date", "")).strip() == target:
            chosen = row
            break
    if chosen is None:
        chosen = rows[-1]
    close = _number(chosen.get("Close"))
    change = _number(chosen.get("Change"))
    return {
        "date": _iso_date(chosen.get("Date")),
        "name": "櫃買指數",
        "close": close,
        "change": change,
        "change_pct": _change_pct(close, change),
    }


def _fetch_taifex_tx(as_of_date: str | None) -> dict[str, Any] | None:
    if not as_of_date:
        return None
    try:
        year, month, day = as_of_date.split("-")
    except ValueError:
        return None
    form_date = f"{year}/{month}/{day}"
    payload = urlencode(
        {
            "queryStartDate": form_date,
            "queryEndDate": form_date,
            "commodity_id": "TX",
            "down_type": "1",
        }
    ).encode("utf-8")
    request = Request(
        TAIFEX_CSV_URL,
        data=payload,
        headers={
            "User-Agent": "taiwan-stock-daily-report/1.0",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    context = ssl.create_default_context()
    try:
        with urlopen(request, timeout=30, context=context) as response:
            raw = response.read()
    except Exception:
        return None
    try:
        text = raw.decode("big5", errors="replace")
    except Exception:
        return None
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        return None
    header = [h.strip() for h in rows[0]]

    def col(name: str) -> int | None:
        try:
            return header.index(name)
        except ValueError:
            return None

    idx_contract = col("契約")
    idx_month = col("到期月份(週別)")
    idx_close = col("收盤價")
    idx_change = col("漲跌價")
    idx_change_pct = col("漲跌%")
    idx_volume = col("成交量")
    idx_session = col("交易時段")
    if None in (idx_contract, idx_month, idx_close, idx_session):
        return None
    candidates = []
    for row in rows[1:]:
        if len(row) <= max(idx_contract, idx_month, idx_close, idx_session):
            continue
        contract = row[idx_contract].strip()
        session = row[idx_session].strip()
        month = row[idx_month].strip()
        if contract != "TX" or session != "一般":
            continue
        if not re.fullmatch(r"\d{6}", month):
            continue
        candidates.append((month, row))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    _, row = candidates[0]
    close = _number(row[idx_close]) if idx_close is not None else None
    change = _number(row[idx_change]) if idx_change is not None else None
    change_pct = (
        _number(row[idx_change_pct]) if idx_change_pct is not None else _change_pct(close, change)
    )
    volume = _integer(row[idx_volume]) if idx_volume is not None else None
    return {
        "date": as_of_date,
        "name": "臺股期貨（大台指期，近月）",
        "close": close,
        "change": change,
        "change_pct": change_pct,
        "volume": volume,
    }


def _fetch_twse_foreign_flows(as_of_date: str | None) -> list[dict[str, Any]]:
    if not as_of_date:
        return []
    date_str = as_of_date.replace("-", "")
    url = f"{TWSE_T86_URL}?date={date_str}&selectType=ALL&response=json"
    try:
        payload = _fetch_json(url)
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    fields = payload.get("fields") or []
    rows = payload.get("data") or []
    try:
        code_idx = fields.index("證券代號")
        name_idx = fields.index("證券名稱")
        net_idx = fields.index("外陸資買賣超股數(不含外資自營商)")
    except ValueError:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if len(row) <= max(code_idx, name_idx, net_idx):
            continue
        code = str(row[code_idx]).strip()
        if not COMMON_STOCK_RE.fullmatch(code):
            continue
        net = _integer(row[net_idx])
        if net is None:
            continue
        out.append(
            {
                "market": "TWSE",
                "code": code,
                "name": str(row[name_idx]).strip(),
                "foreign_net_shares": net,
            }
        )
    return out


def _fetch_tpex_foreign_flows() -> list[dict[str, Any]]:
    try:
        rows = _fetch_json(TPEX_QFII_URL)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        norm = {re.sub(r"\s+", "", str(k)): v for k, v in row.items()}
        code = str(norm.get("SecuritiesCompanyCode", "")).strip()
        if not COMMON_STOCK_RE.fullmatch(code):
            continue
        net = _integer(norm.get("ForeignInvestorsIncludeMainlandAreaInvestors-Difference"))
        if net is None:
            continue
        out.append(
            {
                "market": "TPEX",
                "code": code,
                "name": str(norm.get("CompanyName", "")).strip(),
                "foreign_net_shares": net,
            }
        )
    return out


def _build_foreign_flows(as_of_date: str | None) -> dict[str, Any]:
    rows = _fetch_twse_foreign_flows(as_of_date) + _fetch_tpex_foreign_flows()
    for row in rows:
        row["foreign_net_lots"] = round(row["foreign_net_shares"] / 1000.0, 1)
    top_buy = sorted(rows, key=lambda r: r["foreign_net_shares"], reverse=True)[:10]
    top_sell = sorted(rows, key=lambda r: r["foreign_net_shares"])[:10]
    return {"top_buy": top_buy, "top_sell": top_sell}


def _official_snapshot() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    jobs = {
        "twse_quotes": TWSE_QUOTES_URL,
        "twse_indices": TWSE_INDEX_URL,
        "tpex_quotes": TPEX_QUOTES_URL,
    }
    results: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_fetch_json, url): (name, url) for name, url in jobs.items()}
        for future in as_completed(futures):
            name, url = futures[future]
            try:
                data = future.result()
                if not isinstance(data, list):
                    raise ValueError("Expected a JSON array.")
                results[name] = data
            except Exception as exc:  # retain partial-market results
                errors.append({"source": name, "url": url, "error": str(exc)})
    return (
        _normalize_twse(results.get("twse_quotes", [])),
        _normalize_tpex(results.get("tpex_quotes", [])),
        _normalize_indices(results.get("twse_indices", [])),
        errors,
    )


def build_snapshot(input_path: Path | None, watchlist: list[str]) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    indices: list[dict[str, Any]] = []
    sources: list[dict[str, str]] = []
    foreign_flows: dict[str, Any] = {"top_buy": [], "top_sell": []}

    if input_path is not None:
        raw_rows = _load_uploaded(input_path)
        rows = _normalize_uploaded(raw_rows)
        sources.append({"name": "uploaded_file", "location": str(input_path.resolve())})
    else:
        twse, tpex, indices, errors = _official_snapshot()
        rows = twse + tpex
        sources.extend(
            [
                {"name": "TWSE daily trading data", "location": TWSE_QUOTES_URL},
                {"name": "TWSE daily indices", "location": TWSE_INDEX_URL},
                {"name": "TPEx closing quotes", "location": TPEX_QUOTES_URL},
                {"name": "TWSE 三大法人買賣超 (T86)", "location": TWSE_T86_URL},
                {"name": "TPEx 三大法人買賣超", "location": TPEX_QFII_URL},
                {"name": "TPEx 櫃買指數", "location": TPEX_INDEX_URL},
                {"name": "TAIFEX 臺股期貨每日行情", "location": TAIFEX_CSV_URL},
            ]
        )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["market"], []).append(row)
    markets = {market: _summarize_market(market_rows) for market, market_rows in sorted(grouped.items())}
    market_dates = sorted({summary["data_date"] for summary in markets.values() if summary["data_date"]})
    as_of_date = market_dates[-1] if market_dates else None

    if input_path is None:
        taiex_realtime = _fetch_twse_taiex_realtime(as_of_date)
        if taiex_realtime and taiex_realtime["date"]:
            stale = next((idx for idx in indices if idx["name"] == "發行量加權股價指數"), None)
            if stale is None or not stale.get("date") or taiex_realtime["date"] > stale["date"]:
                indices = [idx for idx in indices if idx["name"] != "發行量加權股價指數"]
                indices.insert(0, taiex_realtime)
                sources.append({"name": "TWSE 大盤成交資訊（即時，非 openapi 鏡像）", "location": TWSE_RWD_FMTQIK_URL})
        tpex_index = _fetch_tpex_composite_index(as_of_date)
        if tpex_index:
            indices.append(tpex_index)
        tx_futures = _fetch_taifex_tx(as_of_date)
        if tx_futures:
            indices.append(tx_futures)
        foreign_flows = _build_foreign_flows(as_of_date)

    matches = [row for row in rows if row["code"] in watchlist]
    unmatched = sorted(set(watchlist) - {row["code"] for row in matches})

    if input_path is None and watchlist:
        try:
            mis_quotes = _fetch_mis_quotes(watchlist)
        except Exception:
            mis_quotes = {}
        for match in matches:
            quote = mis_quotes.get(match["code"])
            if quote and quote["date"] and (not match.get("date") or quote["date"] > match["date"]):
                match["date"] = quote["date"]
                match["close"] = quote["close"]
                match["change"] = quote["change"]
                match["change_pct"] = quote["change_pct"]
        if mis_quotes:
            sources.append({"name": "TWSE MIS real-time quotes (watchlist refresh)", "location": MIS_QUOTE_URL})

    warnings = []
    for market, summary in markets.items():
        if market in {"TWSE", "TPEX"} and summary["common_stocks"] < 200:
            warnings.append(f"{market} has fewer than 200 four-digit common-stock records.")
    if len(market_dates) > 1:
        warnings.append("Market source dates differ; do not aggregate cross-market statistics.")
    if not rows:
        warnings.append("No market rows were available from the selected sources.")
    if input_path is None and not foreign_flows["top_buy"] and not foreign_flows["top_sell"]:
        warnings.append("Foreign-investor net buy/sell data was unavailable for this date.")

    return {
        "schema_version": 2,
        "generated_at": _now_taipei().isoformat(timespec="seconds"),
        "input_mode": "uploaded_file" if input_path else "official_latest",
        "as_of_date": as_of_date,
        "sources": sources,
        "quality": {
            "date_consistent": len(market_dates) <= 1,
            "market_dates": market_dates,
            "warnings": warnings,
        },
        "errors": errors,
        "indices": indices,
        "markets": markets,
        "foreign_flows": foreign_flows,
        "watchlist": {
            "requested": watchlist,
            "matches": matches,
            "unmatched": unmatched,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Optional CSV, JSON, or XLSX file")
    parser.add_argument("--watchlist", default="", help="Comma-separated security codes")
    parser.add_argument("--output", default="-", help="Output JSON path, or - for stdout")
    parser.add_argument("--compact", action="store_true", help="Write compact JSON")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    watchlist = [item.strip() for item in args.watchlist.split(",") if item.strip()]
    try:
        snapshot = build_snapshot(args.input, watchlist)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    text = json.dumps(
        snapshot,
        ensure_ascii=False,
        indent=None if args.compact else 2,
        separators=(",", ":") if args.compact else None,
    )
    if args.output == "-":
        print(text)
    else:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    return 0 if snapshot["markets"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
