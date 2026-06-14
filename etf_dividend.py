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

# ---------- 資料來源 ----------
def _merge_div(result, code, ex_date, cash, cash_pending, name):
    if not code or not ex_date:
        return
    result.setdefault(code, {"name": name, "records": []})
    if name and not result[code]["name"]:
        result[code]["name"] = name
    result[code]["records"].append(
        {"ex_date": ex_date, "cash": cash, "pending": cash_pending})

def fetch_twse_dividends(result):
    """TWSE 除權除息預告表（TWT48U）。嘗試多個端點與完整標頭，
    並在抓不到時印出 raw 回傳以利診斷。"""
    endpoints = [
        "https://www.twse.com.tw/rwd/zh/exRight/TWT48U?response=json",
        "https://www.twse.com.tw/exchangeReport/TWT48U?response=json",
    ]
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.twse.com.tw/zh/announcement/ex-right/twt48u.html",
        "Accept-Language": "zh-TW,zh;q=0.9",
    }
    data = None
    for url in endpoints:
        try:
            req = Request(url, headers=headers)
            raw = urlopen(req, timeout=30).read().decode("utf-8")
            data = json.loads(raw)
            if data.get("stat") == "OK" and data.get("data"):
                print(f"[info] TWSE 端點成功: {url}", file=sys.stderr)
                break
            else:
                print(f"[info] TWSE {url} stat={data.get('stat')} "
                      f"data筆數={len(data.get('data',[]))} "
                      f"raw前200={raw[:200]}", file=sys.stderr)
        except Exception as e:
            print(f"[warn] TWSE {url} 失敗: {e}", file=sys.stderr)
    if not data or not data.get("data"):
        print("[warn] TWSE 所有端點皆無資料", file=sys.stderr)
        return

    fields = data.get("fields", [])
    print(f"[info] TWSE fields: {fields}", file=sys.stderr)
    idx = {name: i for i, name in enumerate(fields)}
    def col(row, *keys):
        for k in keys:
            for fname, i in idx.items():
                if k in fname:
                    return row[i]
        return ""
    cnt = 0
    for row in data.get("data", []):
        code = str(col(row, "股票代號", "證券代號", "代號")).strip()
        ex_date = roc_to_ad(col(row, "除權除息日期", "除權息日期", "除息日期"))
        raw_cash = str(col(row, "現金股利")).strip()
        cash = to_float(raw_cash)
        pending = cash == 0 and "待公告" in raw_cash
        name = str(col(row, "名稱", "股票名稱", "證券名稱")).strip()
        if code and ex_date:
            _merge_div(result, code, ex_date, cash, pending, name)
            cnt += 1
    print(f"[info] TWSE 預告表: {cnt} 筆", file=sys.stderr)

def fetch_dividends():
    result = {}
    fetch_twse_dividends(result)
    for code in result:
        result[code]["records"].sort(key=lambda r: r["ex_date"])
    return result

# ---------- 計算 ----------
def build_report(holdings, div_map):
    today = dt.date.today()
    cur_y, cur_m = today.year, today.month

    this_month = []   # 本月已除息（有金額）
    upcoming = []     # 即將除息（未來）
    matched = []      # 持股中有對應到預告的（不分過去未來）

    for h in holdings:
        code = str(h["code"])
        d = div_map.get(code)
        if not d or not d["records"]:
            continue
        name = h.get("name") or d.get("name", "")
        for r in d["records"]:
            ex, cash, pending = r["ex_date"], r["cash"], r.get("pending", False)
            total = cash * h["shares"]
            sy = (cash / h["avg_cost"] * 100) if h["avg_cost"] else 0
            rec = {"code": code, "name": name, "ex_date": ex,
                   "cash_per_share": cash, "pending": pending,
                   "shares": h["shares"], "total": total, "single_yield": sy}
            matched.append(rec)
            if ex.year == cur_y and ex.month == cur_m and ex <= today and not pending:
                this_month.append(rec)
            elif ex >= today:
                upcoming.append(rec)

    this_month.sort(key=lambda x: x["ex_date"])
    upcoming.sort(key=lambda x: x["ex_date"])
    matched.sort(key=lambda x: x["ex_date"])
    return this_month, upcoming, matched

def format_message(this_month, upcoming, matched, holdings):
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
        lines.append("💰 本月無已公告配息")

    lines.append("")
    if upcoming:
        lines.append("🔔 *即將除息*")
        for r in upcoming:
            days = (r["ex_date"] - today).days
            day_str = "今天" if days == 0 else f"剩 {days} 天"
            if r["pending"]:
                lines.append(
                    f"  {r['code']} {r['name']}｜{r['ex_date']:%m/%d}"
                    f"（{day_str}）\n    金額待公告"
                )
            else:
                est = r["cash_per_share"] * r["shares"]
                lines.append(
                    f"  {r['code']} {r['name']}｜{r['ex_date']:%m/%d}"
                    f"（{day_str}）\n"
                    f"    每股 {r['cash_per_share']:.3f} 元 → 約 {est:,.0f} 元"
                )
    else:
        lines.append("🔔 近期無預告除息")

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
def write_result(this_month, upcoming):
    """寫回 last_result.json"""
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
             "pending": r["pending"],
             "est": round(r["cash_per_share"] * r["shares"])}
            for r in upcoming
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
    this_month, upcoming, matched = build_report(holdings, div_map)
    msg = format_message(this_month, upcoming, matched, holdings)
    send_telegram(msg)
    write_result(this_month, upcoming)

if __name__ == "__main__":
    main()
