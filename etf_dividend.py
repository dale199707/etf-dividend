#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETF 配息自動追蹤
- 讀取 holdings.json（你的持股 + 均價）
- 從 TWSE 抓取除權息資料
- 計算本月實際配息金額、殖利率
- 推送 Telegram：本月配息明細 + 未來除息日提醒
"""

import json
import os
import sys
import datetime as dt
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode

BASE = Path(__file__).resolve().parent
HOLDINGS_FILE = BASE / "holdings.json"
UA = {"User-Agent": "Mozilla/5.0"}

# ---------- 共用 ----------
def fetch_json(url):
    req = Request(url, headers=UA)
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def to_float(s):
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0

def roc_to_ad(roc_date):
    """民國日期 '114/06/13' 或 '1140613' -> date"""
    s = str(roc_date).replace("/", "").strip()
    if len(s) < 7:
        return None
    try:
        y = int(s[:3]) + 1911
        m = int(s[3:5])
        d = int(s[5:7])
        return dt.date(y, m, d)
    except ValueError:
        return None

# ---------- 資料來源：TWSE 上市除權息 ----------
def _months_back(n):
    """回傳最近 n 個月 + 下個月的 (year, month) 集合"""
    today = dt.date.today()
    months = set()
    cur = today.replace(day=1)
    for _ in range(n):
        months.add((cur.year, cur.month))
        cur = (cur - dt.timedelta(days=1)).replace(day=1)
    nxt = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    months.add((nxt.year, nxt.month))
    return months

def _merge_div(result, code, ex_date, cash, name):
    """同一代號保留所有除息紀錄（list），之後再挑本月/未來/最近"""
    if not code or not ex_date:
        return
    result.setdefault(code, {"name": name, "records": []})
    if name and not result[code]["name"]:
        result[code]["name"] = name
    result[code]["records"].append({"ex_date": ex_date, "cash": cash})

def fetch_twse_dividends(result, months):
    """TWSE 除權除息計算結果表（TWT49U），上市 ETF/個股"""
    for y, m in months:
        url = ("https://www.twse.com.tw/rwd/zh/exRight/TWT49U?"
               + urlencode({"startDate": f"{y}{m:02d}01",
                            "endDate": f"{y}{m:02d}31", "response": "json"}))
        try:
            data = fetch_json(url)
        except Exception as e:
            print(f"[warn] TWSE {y}/{m} 失敗: {e}", file=sys.stderr)
            continue
        if data.get("stat") != "OK":
            continue
        idx = {name: i for i, name in enumerate(data.get("fields", []))}
        def col(row, *keys):
            for k in keys:
                for fname, i in idx.items():
                    if k in fname:
                        return row[i]
            return ""
        for row in data.get("data", []):
            code = str(col(row, "股票代號", "代號")).strip()
            ex_date = roc_to_ad(col(row, "除權息日期", "資料日期", "除息日期"))
            cash = to_float(col(row, "現金股利", "權值+息值", "息值"))
            name = str(col(row, "股票名稱", "名稱")).strip()
            _merge_div(result, code, ex_date, cash, name)

def fetch_tpex_dividends(result, months):
    """TPEx 櫃買除權息，債券 ETF（代號結尾 B）在此掛牌
    端點：上櫃股票除權除息預告表 / 計算結果"""
    for y, m in months:
        roc_y = y - 1911
        # 櫃買除權息 API（民國年月）
        url = ("https://www.tpex.org.tw/www/zh-tw/bond/exDayAndStib?"
               + urlencode({"date": f"{roc_y}/{m:02d}", "response": "json"}))
        data = None
        try:
            data = fetch_json(url)
        except Exception as e:
            print(f"[warn] TPEx {y}/{m} 失敗: {e}", file=sys.stderr)
        if not data:
            continue
        rows = data.get("aaData") or data.get("data") or []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                continue
            code = str(row[0]).strip()
            ex_date = roc_to_ad(row[1]) if len(row) > 1 else None
            cash = to_float(row[2]) if len(row) > 2 else 0.0
            _merge_div(result, code, ex_date, cash, "")

def fetch_dividends():
    """合併 TWSE + TPEx，回傳 code -> {name, records:[{ex_date,cash}]}"""
    months = _months_back(6)
    result = {}
    fetch_twse_dividends(result, months)
    fetch_tpex_dividends(result, months)
    # 每檔的 records 依日期排序
    for code in result:
        result[code]["records"].sort(key=lambda r: r["ex_date"])
    return result

# ---------- 計算 ----------
def build_report(holdings, div_map):
    today = dt.date.today()
    cur_y, cur_m = today.year, today.month

    this_month = []   # 本月已除息
    upcoming = []     # 未來即將除息
    recent = []       # 每檔最近一次除息（不限本月）

    for h in holdings:
        code = str(h["code"])
        d = div_map.get(code)
        if not d or not d["records"]:
            continue
        name = h.get("name") or d.get("name", "")
        recs = d["records"]

        # 最近一次「已發生」的除息
        past = [r for r in recs if r["ex_date"] <= today]
        if past:
            last = past[-1]
            recent.append({
                "code": code, "name": name,
                "ex_date": last["ex_date"],
                "cash_per_share": last["cash"],
                "total": last["cash"] * h["shares"],
            })

        for r in recs:
            ex, cash = r["ex_date"], r["cash"]
            total = cash * h["shares"]
            sy = (cash / h["avg_cost"] * 100) if h["avg_cost"] else 0
            rec = {"code": code, "name": name, "ex_date": ex,
                   "cash_per_share": cash, "shares": h["shares"],
                   "total": total, "single_yield": sy}
            if ex.year == cur_y and ex.month == cur_m and ex <= today:
                this_month.append(rec)
            elif ex > today:
                upcoming.append(rec)

    this_month.sort(key=lambda x: x["ex_date"])
    upcoming.sort(key=lambda x: x["ex_date"])
    recent.sort(key=lambda x: x["code"])
    return this_month, upcoming, recent

def format_message(this_month, upcoming, recent, holdings):
    today = dt.date.today()
    lines = [f"📅 *ETF 配息追蹤* ({today:%Y/%m/%d})", ""]

    if this_month:
        total_sum = sum(r["total"] for r in this_month)
        lines.append("💰 *本月已配息*")
        for r in this_month:
            lines.append(
                f"  {r['code']} {r['name']}\n"
                f"    除息 {r['ex_date']:%m/%d}｜每股 {r['cash_per_share']:.3f} 元"
                f"｜{r['shares']:,} 股\n"
                f"    領取 *{r['total']:,.0f} 元*（單次殖利率 {r['single_yield']:.2f}%）"
            )
        lines.append(f"\n  本月合計：*{total_sum:,.0f} 元*")
    else:
        lines.append("💰 本月無 ETF 除息")

    lines.append("")
    if upcoming:
        lines.append("🔔 *即將除息*")
        for r in upcoming:
            days = (r["ex_date"] - today).days
            est = r["cash_per_share"] * r["shares"]
            lines.append(
                f"  {r['code']} {r['name']}｜{r['ex_date']:%m/%d}"
                f"（剩 {days} 天）\n"
                f"    預估每股 {r['cash_per_share']:.3f} 元 → 約 {est:,.0f} 元"
            )
    else:
        lines.append("🔔 近期無預告除息")

    # 每檔最近一次除息
    recent_map = {r["code"]: r for r in recent}
    if recent:
        lines.append("")
        lines.append("🗓 *最近一次除息*")
        for r in recent:
            lines.append(
                f"  {r['code']} {r['name']}｜{r['ex_date']:%Y/%m/%d}"
                f"｜每股 {r['cash_per_share']:.3f} 元 → {r['total']:,.0f} 元"
            )

    # 持股清單（無論有無配息都附上）
    if holdings:
        lines.append("")
        lines.append("📦 *目前持股*")
        total_cost = 0
        for h in holdings:
            cost = h["shares"] * h.get("avg_cost", 0)
            total_cost += cost
            nm = h.get("name", "")
            lines.append(
                f"  {h['code']} {nm}｜{h['shares']:,} 股"
                f"（均價 {h.get('avg_cost',0):.2f}）"
            )
        lines.append(f"\n  總成本：{total_cost:,.0f} 元")

    return "\n".join(lines)

# ---------- Telegram ----------
def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[error] 缺少 TELEGRAM_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
        print("\n--- 訊息預覽 ---\n" + text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id, "text": text, "parse_mode": "Markdown",
    }).encode("utf-8")
    req = Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode("utf-8"))
        print("Telegram:", "OK" if resp.get("ok") else resp)

# ---------- main ----------
def write_result(this_month, upcoming, recent):
    """寫回 last_result.json 供網頁顯示"""
    today = dt.date.today()
    out = {
        "generated_at": f"{today:%Y/%m/%d}",
        "month_total": sum(r["total"] for r in this_month),
        "this_month": [
            {"code": r["code"], "name": r["name"],
             "ex_date": f"{r['ex_date']:%m/%d}",
             "cash_per_share": round(r["cash_per_share"], 3),
             "shares": r["shares"], "total": round(r["total"]),
             "single_yield": round(r["single_yield"], 2)}
            for r in this_month
        ],
        "upcoming": [
            {"code": r["code"], "name": r["name"],
             "ex_date": f"{r['ex_date']:%m/%d}",
             "days_left": (r["ex_date"] - today).days,
             "est": round(r["cash_per_share"] * r["shares"])}
            for r in upcoming
        ],
        "recent": [
            {"code": r["code"], "name": r["name"],
             "ex_date": f"{r['ex_date']:%Y/%m/%d}",
             "cash_per_share": round(r["cash_per_share"], 3),
             "total": round(r["total"])}
            for r in recent
        ],
    }
    (BASE / "last_result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已寫回 last_result.json")

def main():
    holdings = json.loads(HOLDINGS_FILE.read_text(encoding="utf-8")).get("holdings", [])
    if not holdings:
        print("holdings.json 無持股資料"); return
    div_map = fetch_dividends()
    print(f"取得除權息資料 {len(div_map)} 檔")
    this_month, upcoming, recent = build_report(holdings, div_map)
    msg = format_message(this_month, upcoming, recent, holdings)
    send_telegram(msg)
    write_result(this_month, upcoming, recent)

if __name__ == "__main__":
    main()
