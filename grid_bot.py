#!/usr/bin/env python3
"""Grid-trading watchdog: build the ladder, watch the price, push buy/sell alerts.

You place the orders yourself — this only tells you *when* a grid level is hit
and keeps the book of what has been filled so far.

  Config  : grid/config.json   — ladder definition + which levels are held
  Quote   : polling.finance.naver.com (fallback: api.finance.naver.com)
  Journal : data/grid_trades.csv
  Output  : dist/grid.html — grid_template.html with the payload injected

The ladder is geometric: 19 levels between 하단 and 상단 means 18 equal ratio
steps, each level price floored to the KRX tick. Money is spread the other way
round — the bottom level gets `endMultiple` times the top level's order, again
geometrically — so the further the price falls the more it buys.

Standard library only; no pip install required.

Usage:  python3 grid_bot.py [--render-only] [--price 1592000] [--dry-run]
"""

import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "grid", "config.json")
TRADES = os.path.join(ROOT, "data", "grid_trades.csv")
TEMPLATE = os.path.join(ROOT, "grid_template.html")
OUT_HTML = os.path.join(ROOT, "dist", "grid.html")

KST = timezone(timedelta(hours=9))
TRADE_COLUMNS = ["ts", "level", "side", "price", "shares", "amount", "profit"]


# ---------------------------------------------------------------- ladder math
# Kept deliberately simple and duplicated in grid_template.html — the page has to
# recompute the table while you drag the inputs around, so the two must agree.
# Any change here needs the same change in gridLevels() over there.

def tick_size(price):
    """KRX price tick (2023 table). Only the last two bands matter above 500k."""
    for limit, tick in ((2000, 1), (5000, 5), (20000, 10), (50000, 50),
                        (200000, 100), (500000, 500)):
        if price < limit:
            return tick
    return 1000


def snap(price, tick):
    t = tick or tick_size(price)
    return int(math.floor(price / t) * t)


def build_levels(cfg):
    """Return levels bottom-first: [{level, price, amount, shares, target, ...}]."""
    lower, upper = float(cfg["lower"]), float(cfg["upper"])
    n, budget = int(cfg["levels"]), float(cfg["budget"])
    mult, tick = float(cfg["endMultiple"]), int(cfg.get("tick") or 0)
    if n < 2 or lower <= 0 or upper <= lower:
        raise ValueError("grid/config.json: 상단 > 하단 > 0, 레벨 2개 이상이어야 합니다.")

    ratio = (upper / lower) ** (1.0 / (n - 1))
    weights = [mult ** ((n - 1 - k) / (n - 1)) for k in range(n)]
    total_w = sum(weights)
    holdings = {int(k): int(v) for k, v in (cfg.get("holdings") or {}).items()}

    out = []
    for k in range(n):
        price = snap(upper / ratio ** (n - 1 - k), tick)
        amount = budget * weights[k] / total_w
        shares = amount / price
        # The top level sells one ratio step above the ladder — that rung has no
        # level above it, so it gets a virtual one.
        up = snap(upper * ratio, tick) if k == n - 1 else \
            snap(upper / ratio ** (n - 2 - k), tick)
        out.append({
            "level": k + 1,
            "price": price,
            "amount": int(round(amount)),
            "shares": round(shares, 4),
            "plan": int(math.floor(shares)),   # 실매수 권장 = 정수 내림
            "target": up,
            "held": holdings.get(k + 1, 0),
        })
    return out, ratio


def net_profit(cfg, shares, buy, sell):
    """Realised won for `shares` bought at `buy` and sold at `sell`."""
    fee = float(cfg.get("feeRate") or 0) / 100.0
    tax = float(cfg.get("taxRate") or 0) / 100.0
    cost = shares * buy * (1 + fee)
    gain = shares * sell * (1 - fee - tax)
    return int(round(gain - cost))


# --------------------------------------------------------------------- quotes

def _get_json(url):
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
        "Referer": "https://finance.naver.com/",
        "Accept": "application/json",
    })
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_quote(code):
    """Current price for a KRX ticker. Raises RuntimeError if every source fails."""
    errors = []
    try:
        d = _get_json("https://polling.finance.naver.com/api/realtime/domestic/stock/%s" % code)
        row = d["result"]["areas"][0]["datas"][0]
        price = int(row["nv"])
        if price > 0:
            return {
                "price": price,
                "name": row.get("nm") or "",
                "prevClose": int(row.get("pcv") or 0),
                "change": int(row.get("cv") or 0),
                "changeRate": float(row.get("cr") or 0),
                "high": int(row.get("hv") or 0),
                "low": int(row.get("lv") or 0),
                "source": "polling.finance.naver.com",
            }
        errors.append("polling: nv=%r" % row.get("nv"))
    except (URLError, OSError, ValueError, KeyError, IndexError, TypeError) as e:
        errors.append("polling: %s" % e)

    try:
        d = _get_json("https://api.finance.naver.com/service/itemSummary.naver?itemcode=%s" % code)
        price = int(d["now"])
        if price > 0:
            prev = int(d.get("lastDay") or 0)
            return {
                "price": price,
                "name": d.get("nm") or "",
                "prevClose": prev,
                "change": price - prev if prev else 0,
                "changeRate": round((price - prev) / prev * 100, 2) if prev else 0.0,
                "high": int(d.get("high") or 0),
                "low": int(d.get("low") or 0),
                "source": "api.finance.naver.com",
            }
        errors.append("itemSummary: now=%r" % d.get("now"))
    except (URLError, OSError, ValueError, KeyError, TypeError) as e:
        errors.append("itemSummary: %s" % e)

    raise RuntimeError("시세를 못 받았습니다 (%s) — %s" % (code, " | ".join(errors)))


# ------------------------------------------------------------------- journal

def load_trades():
    rows = []
    if os.path.exists(TRADES):
        with open(TRADES, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if not r.get("ts"):
                    continue
                r["level"] = int(r["level"])
                for c in ("price", "shares", "amount", "profit"):
                    r[c] = int(float(r[c] or 0))
                rows.append(r)
    return rows


def save_trades(rows):
    os.makedirs(os.path.dirname(TRADES), exist_ok=True)
    with open(TRADES, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows([{c: r[c] for c in TRADE_COLUMNS} for r in rows])


# -------------------------------------------------------------------- signals

def detect(cfg, levels, price, last):
    """Levels crossed since the previous poll, as alert dicts.

    Only *crossings* fire. Without the `last` bound, the first poll of a session
    would announce every level the price already sits below, all at once.
    """
    signals = []
    for lv in levels:
        if lv["held"] <= 0 and lv["plan"] > 0 and price <= lv["price"] < last:
            signals.append({
                "side": "buy", "level": lv["level"], "price": lv["price"],
                "shares": lv["plan"], "amount": lv["plan"] * lv["price"],
                "profit": 0,
            })
        elif lv["held"] > 0 and price >= lv["target"] > last:
            signals.append({
                "side": "sell", "level": lv["level"], "price": lv["target"],
                "shares": lv["held"], "amount": lv["held"] * lv["target"],
                "profit": net_profit(cfg, lv["held"], lv["price"], lv["target"]),
            })
    signals.sort(key=lambda s: (s["side"] == "buy", -s["price"]))
    return signals


def alert_text(cfg, quote, signals, levels):
    """Telegram body. One message per poll, however many levels were crossed."""
    def won(v):
        return "₩{:,}".format(int(v))

    head = "%s %s · 현재가 %s (%+.2f%%)" % (
        cfg.get("name") or cfg["code"], datetime.now(KST).strftime("%m/%d %H:%M"),
        won(quote["price"]), quote.get("changeRate") or 0)

    lines = [head, ""]
    for s in signals:
        if s["side"] == "buy":
            lines.append("🟢 #%d 매수  %s × %d주 = %s"
                         % (s["level"], won(s["price"]), s["shares"], won(s["amount"])))
        else:
            lines.append("🔴 #%d 매도  %s × %d주 = %s  (실현 %s%s)"
                         % (s["level"], won(s["price"]), s["shares"], won(s["amount"]),
                            "+" if s["profit"] >= 0 else "-", won(abs(s["profit"]))))

    held = [lv for lv in levels if lv["held"] > 0]
    shares = sum(lv["held"] for lv in held)
    cost = sum(lv["held"] * lv["price"] for lv in held)
    lines.append("")
    if shares:
        avg = cost // shares
        pnl = (quote["price"] - avg) / avg * 100
        lines.append("보유 %d주 · 평단 %s · 평가 %+.2f%%" % (shares, won(avg), pnl))
    else:
        lines.append("보유 없음 (전량 현금)")
    return "\n".join(lines)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없습니다.", file=sys.stderr)
        return False
    body = json.dumps({"chat_id": chat, "text": text,
                       "disable_web_page_preview": True}).encode("utf-8")
    req = Request("https://api.telegram.org/bot%s/sendMessage" % token, data=body,
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=20) as resp:
            answer = json.loads(resp.read().decode("utf-8", "replace"))
    except (URLError, OSError, ValueError) as e:
        print("ERROR: 텔레그램 전송 실패: %s" % e, file=sys.stderr)
        return False
    # Telegram answers 200 with {"ok":false} on a bad token or chat id.
    if not answer.get("ok"):
        print("ERROR: 텔레그램이 거부했습니다: %s" % answer, file=sys.stderr)
        return False
    return True


# --------------------------------------------------------------------- render

def render(cfg, levels, ratio, quote, trades):
    os.makedirs(os.path.dirname(OUT_HTML), exist_ok=True)
    payload = {
        "generatedAt": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "config": cfg,
        "quote": quote,
        "trades": trades,
    }
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("/*__DATA__*/null",
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print("Wrote %s (%d levels, %d trades, 간격 %.2f%%)"
          % (OUT_HTML, len(levels), len(trades), (ratio - 1) * 100))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--render-only", action="store_true",
                    help="네트워크 없이 커밋된 설정·기록만으로 페이지를 다시 만듭니다")
    ap.add_argument("--price", type=int,
                    help="시세 대신 이 값을 쓴 것처럼 계산합니다 (테스트용)")
    ap.add_argument("--dry-run", action="store_true",
                    help="신호는 계산하되 설정·기록을 고치지 않고 알림도 보내지 않습니다")
    args = ap.parse_args()

    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    trades = load_trades()

    try:
        levels, ratio = build_levels(cfg)
    except ValueError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1

    if args.render_only:
        render(cfg, levels, ratio, cfg.get("quote"), trades)
        return 0

    if args.price:
        quote = {"price": args.price, "name": cfg.get("name") or "", "prevClose": 0,
                 "change": 0, "changeRate": 0.0, "high": 0, "low": 0, "source": "--price"}
    else:
        if not cfg.get("code"):
            print("grid/config.json 의 code 가 비어 있습니다 — 감시할 종목을 넣기 전까지 "
                  "시세도 알림도 없습니다. 페이지만 다시 그립니다.")
            render(cfg, levels, ratio, cfg.get("quote"), trades)
            return 0
        try:
            quote = fetch_quote(cfg["code"])
        except RuntimeError as e:
            print("ERROR: %s" % e, file=sys.stderr)
            return 1

    now = datetime.now(KST)
    quote["at"] = now.strftime("%Y-%m-%d %H:%M")
    price = quote["price"]
    last = cfg.get("lastPrice")

    if last is None:
        # First poll: adopt the price silently. Announcing every level the price
        # already sits below would be a wall of alerts for trades long past.
        print("첫 폴링 — 기준가만 %d 로 잡고 알림은 보내지 않습니다." % price)
        signals = []
    else:
        signals = detect(cfg, levels, price, float(last))

    for s in signals:
        holdings = cfg.setdefault("holdings", {})
        key = str(s["level"])
        if s["side"] == "buy":
            holdings[key] = s["shares"]
        else:
            holdings.pop(key, None)
        trades.append({
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"), "level": s["level"],
            "side": s["side"], "price": s["price"], "shares": s["shares"],
            "amount": s["amount"], "profit": s["profit"],
        })
        print("SIGNAL %s #%d %d x %d주" % (s["side"], s["level"], s["price"], s["shares"]))

    cfg["lastPrice"] = price
    cfg["lastPriceAt"] = quote["at"]
    cfg["quote"] = quote
    if quote.get("name") and not cfg.get("name"):
        cfg["name"] = quote["name"]

    # Rebuild so the page and the alert see the holdings this poll just changed.
    levels, ratio = build_levels(cfg)

    if args.dry_run:
        print("--dry-run: 설정·기록을 쓰지 않고 알림도 보내지 않습니다 (신호 %d건)."
              % len(signals))
        if signals:
            print("ALERT\n%s" % alert_text(cfg, quote, signals, levels))
        render(cfg, levels, ratio, quote, trades)
        return 0

    # The alert goes out *before* the state is written. If Telegram is down and
    # the fill were already recorded, the next poll would see nothing to report
    # and you would never learn about it; leaving the state untouched makes the
    # next poll retry the very same signal.
    if signals:
        text = alert_text(cfg, quote, signals, levels)
        print("ALERT\n%s" % text)
        if not send_telegram(text):
            print("ERROR: 알림을 못 보내서 체결 상태를 기록하지 않았습니다 — "
                  "다음 폴링에서 같은 신호를 다시 시도합니다.", file=sys.stderr)
            return 1
        print("텔레그램 전송 완료 (%d건)." % len(signals))
    else:
        print("신호 없음 — 현재가 %d, 기준 %s." % (price, last))

    # Only a state change is written back. Persisting every poll would put a
    # commit on the repository every five minutes all session long, and the
    # reference price is *supposed* to be the price at the last fill: that is
    # what makes a gap between polls fire every level it jumped over.
    if signals or last is None:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        save_trades(trades)
    render(cfg, levels, ratio, quote, trades)
    return 0


if __name__ == "__main__":
    sys.exit(main())
