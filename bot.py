
import os, io, gzip, json, time, math, logging, threading
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import requests


# Minimal .env loader so local Termux works without python-dotenv.
def load_dotenv_file(path=".env"):
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        log.warning("Could not read .env: %s", e)

load_dotenv_file()

# ---------------- CONFIG ----------------
UPSTOX_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SCAN_TIMES = [x.strip() for x in os.getenv("SCAN_TIMES", "09:20,12:30,15:15").split(",") if x.strip()]
DEEP_SCAN_LIMIT = int(os.getenv("DEEP_SCAN_LIMIT", "2000"))  # 0 = live universe only
TOP_ALERTS = int(os.getenv("TOP_ALERTS", "10"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "70"))
NEWS_ENABLED = os.getenv("NEWS_ENABLED", "1") == "1"
NEWS_HOURS = int(os.getenv("NEWS_HOURS", "48"))
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
TZ_OFFSET = int(os.getenv("TZ_OFFSET", "5")) * 3600 + int(os.getenv("TZ_MINUTES", "30")) * 60

BASE = "https://api.upstox.com"
INSTRUMENT_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("upstox-bot")

session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "Authorization": f"Bearer {UPSTOX_TOKEN}",
})

# ---------------- HELPERS ----------------
def ist_now():
    return datetime.now(timezone.utc) + timedelta(seconds=TZ_OFFSET)

def tg(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials missing; message skipped.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True
        }, timeout=20)
        if not r.ok:
            log.error("Telegram error %s: %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        log.error("Telegram exception: %s", e)
        return False

def api_get(path, params=None, timeout=30):
    for attempt in range(4):
        try:
            r = session.get(BASE + path, params=params, timeout=timeout)
            if r.status_code == 429:
                wait = min(30, 2 ** attempt)
                log.warning("Rate limited; sleeping %ss", wait)
                time.sleep(wait)
                continue
            if not r.ok:
                log.warning("API %s -> %s %s", path, r.status_code, r.text[:300])
                return None
            return r.json()
        except Exception as e:
            log.warning("API error %s: %s", path, e)
            time.sleep(1.5 * (attempt + 1))
    return None

def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]

# ---------------- INSTRUMENTS ----------------
def load_nse_equities():
    log.info("Downloading NSE instrument master...")
    r = requests.get(INSTRUMENT_URL, timeout=60)
    r.raise_for_status()
    raw = gzip.decompress(r.content)
    data = json.loads(raw.decode("utf-8"))
    out = []
    seen = set()
    for x in data:
        if x.get("segment") != "NSE_EQ": continue
        if x.get("instrument_type") != "EQ": continue
        if x.get("security_type") not in (None, "", "NORMAL"): continue
        sym = (x.get("trading_symbol") or "").strip()
        key = x.get("instrument_key")
        if not sym or not key or sym in seen: continue
        seen.add(sym)
        out.append({
            "symbol": sym,
            "name": x.get("short_name") or x.get("name") or sym,
            "key": key,
            "isin": x.get("isin", ""),
        })
    out.sort(key=lambda z: z["symbol"])
    log.info("NSE cash universe: %d equities", len(out))
    return out

# ---------------- MARKET DATA ----------------
def market_quotes(instruments):
    result = {}
    keys = [x["key"] for x in instruments]
    for group in chunked(keys, 500):
        data = api_get("/v3/market-quote/quotes", {"instrument_key": ",".join(group)})
        if not data: continue
        result.update(data.get("data", {}))
        time.sleep(0.15)
    return result

def candles(key, days=400):
    end = ist_now().date()
    start = end - timedelta(days=days)
    path = f"/v3/historical-candle/{quote(key, safe='')}/days/1/{end.isoformat()}/{start.isoformat()}"
    data = api_get(path, timeout=45)
    if not data: return []
    rows = data.get("data", {}).get("candles", [])
    rows = sorted(rows, key=lambda x: x[0])
    # [timestamp, open, high, low, close, volume, oi]
    return rows

# ---------------- TECHNICALS (stdlib only) ----------------
def closes(rows): return [float(x[4]) for x in rows if len(x) >= 6 and x[4] is not None]
def highs(rows): return [float(x[2]) for x in rows]
def lows(rows): return [float(x[3]) for x in rows]
def vols(rows): return [float(x[5] or 0) for x in rows]

def sma(v, n):
    if len(v) < n: return None
    return sum(v[-n:]) / n

def ema(v, n):
    if len(v) < n: return None
    k = 2 / (n + 1)
    e = sum(v[:n]) / n
    for x in v[n:]: e = x * k + e * (1-k)
    return e

def rsi(v, n=14):
    if len(v) < n + 1: return None
    gains=[]; losses=[]
    for a,b in zip(v[-(n+1):-1], v[-n:]):
        d=b-a
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains)/n; al=sum(losses)/n
    if al == 0: return 100.0
    return 100 - 100/(1 + ag/al)

def atr(rows, n=14):
    if len(rows) < n+1: return None
    tr=[]
    for i in range(1,len(rows)):
        h=float(rows[i][2]); l=float(rows[i][3]); pc=float(rows[i-1][4])
        tr.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(tr[-n:])/n

def pct(a,b):
    return ((a/b)-1)*100 if b else 0

# ---------------- NEWS ----------------
POS = ("upgrade","beat","strong results","profit rises","order win","contract win",
       "growth","buyback","dividend","approval","expansion","record profit","outperform")
NEG = ("downgrade","miss","weak results","profit falls","fraud","penalty","probe",
       "resign","default","loss","cut guidance","investigation","ban")

def news_score(symbol):
    if not NEWS_ENABLED: return 0, []
    url = "https://news.google.com/rss/search"
    try:
        r = requests.get(url, params={"q": f"{symbol} NSE stock", "hl":"en-IN", "gl":"IN", "ceid":"IN:en"}, timeout=15)
        import xml.etree.ElementTree as ET
        root = ET.fromstring(r.content)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_HOURS)
        score=0; titles=[]
        for item in root.findall(".//item")[:20]:
            title=(item.findtext("title") or "").strip()
            pub=(item.findtext("pubDate") or "").strip()
            try:
                from email.utils import parsedate_to_datetime
                d=parsedate_to_datetime(pub).astimezone(timezone.utc)
            except Exception:
                d=cutoff
            if d < cutoff: continue
            t=title.lower()
            p=sum(1 for k in POS if k in t); n=sum(1 for k in NEG if k in t)
            score += min(3, p) - min(3, n)
            if p or n: titles.append(title[:150])
        return max(-10,min(10,score)), titles[:3]
    except Exception as e:
        log.warning("News %s: %s", symbol, e)
        return 0, []

# ---------------- SCORING ----------------
def analyse(inst, row, quote_data, deep=True):
    q = quote_data or {}
    ohlc = q.get("ohlc") or {}
    ltp = float(q.get("last_price") or ohlc.get("close") or 0)
    prev = float(q.get("prev_close_price") or 0)
    if not ltp: return None

    result = {
        "symbol": inst["symbol"], "name": inst["name"], "key": inst["key"],
        "ltp": ltp, "prev": prev, "day_pct": pct(ltp,prev) if prev else 0,
        "score": 0, "reasons": [], "action": "WAIT", "entry": ltp,
        "stop": None, "t1": None, "t2": None, "news": 0, "news_titles": []
    }

    if not deep:
        # Live-only pre-ranking: useful to decide which symbols deserve historical calls.
        s=0
        if result["day_pct"] > 1: s += 20
        elif result["day_pct"] > 0.3: s += 10
        elif result["day_pct"] < -1: s -= 20
        elif result["day_pct"] < -0.3: s -= 10
        result["score"]=50+s
        return result

    rows=candles(inst["key"], 400)
    if len(rows) < 220:
        return None
    c=closes(rows); h=highs(rows); l=lows(rows); v=vols(rows)
    e20=ema(c,20); e50=ema(c,50); e200=ema(c,200); r=rsi(c); a=atr(rows)
    macd=ema(c,12)-ema(c,26) if len(c)>=26 else 0
    signal=ema([ema(c[:i],12)-ema(c[:i],26) for i in range(26,len(c)+1)],9) if len(c)>=35 else 0
    s=50
    if e20 and ltp > e20: s+=7; result["reasons"].append("above EMA20")
    if e50 and ltp > e50: s+=8; result["reasons"].append("above EMA50")
    if e200 and ltp > e200: s+=10; result["reasons"].append("above EMA200")
    if e20 and e50 and e20 > e50: s+=7; result["reasons"].append("EMA20>EMA50")
    if e50 and e200 and e50 > e200: s+=8; result["reasons"].append("EMA50>EMA200")
    if r is not None:
        if 50 <= r <= 68: s+=8; result["reasons"].append(f"RSI {r:.0f}")
        elif 68 < r <= 75: s+=3; result["reasons"].append(f"RSI {r:.0f} (hot)")
        elif r < 35: s-=8; result["reasons"].append(f"RSI {r:.0f} (weak)")
        elif r > 78: s-=10; result["reasons"].append(f"RSI {r:.0f} (overbought)")
    if macd > signal: s+=7; result["reasons"].append("MACD bullish")
    else: s-=4
    av=sma(v,20)
    if av and v[-1] > av*1.3: s+=7; result["reasons"].append("volume expansion")
    elif av and v[-1] < av*0.6: s-=2
    hi20=max(h[-20:]); lo20=min(l[-20:])
    if ltp >= hi20*0.995: s+=5; result["reasons"].append("near 20D high")
    if ltp <= lo20*1.005: s-=5
    d30=pct(ltp,c[-31]) if len(c)>31 else 0
    if d30>5: s+=4
    elif d30<-5: s-=5

    ns,titles=news_score(inst["symbol"])
    result["news"]=ns; result["news_titles"]=titles
    s += max(-8,min(8,ns*1.5))
    if ns>0: result["reasons"].append("positive news")
    if ns<0: result["reasons"].append("negative news")

    s=max(0,min(100,s))
    result["score"]=round(s,1)
    if a and a>0:
        result["stop"]=round(ltp-1.5*a,2)
        risk=ltp-result["stop"]
        result["t1"]=round(ltp+2*risk,2)
        result["t2"]=round(ltp+3*risk,2)
    if s >= MIN_SCORE and ns >= -3:
        result["action"]="BUY WATCH"
    elif s < 40 or ns <= -6:
        result["action"]="AVOID"
    else:
        result["action"]="WAIT"
    return result

def holdings():
    data=api_get("/v2/portfolio/long-term-holdings", timeout=30)
    return data.get("data", []) if data else []

def sell_watch_for_holdings(hs):
    # Use holdings average price and current LTP. This is a risk-management WATCH, not an order.
    out=[]
    if not hs: return out
    inst_map={}
    try:
        universe=load_nse_equities()
        inst_map={x["symbol"]:x for x in universe}
    except Exception: pass
    q=market_quotes([inst_map[h.get("trading_symbol","")] for h in hs if h.get("trading_symbol") in inst_map])
    for h in hs:
        sym=h.get("trading_symbol","")
        inst=inst_map.get(sym)
        if not inst: continue
        keyname=f"NSE_EQ:{sym}"
        qd=q.get(keyname,{})
        ltp=float(h.get("last_price") or qd.get("last_price") or 0)
        avg=float(h.get("average_price") or 0)
        pnl=float(h.get("pnl") or 0)
        if not ltp: continue
        change=pct(ltp,avg) if avg else 0
        # Conservative watch rules: large drawdown or strong deterioration.
        if change <= -8 or pnl < 0 and change <= -5:
            out.append((sym, ltp, avg, change, pnl))
    return out

# ---------------- SCAN ----------------
def run_scan():
    if not UPSTOX_TOKEN:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN is missing")
    started=time.time()
    universe=load_nse_equities()
    quotes=market_quotes(universe)
    quick=[]
    for inst in universe:
        key=f"NSE_EQ:{inst['symbol']}"
        q=quotes.get(key)
        if q: quick.append(analyse(inst,q,deep=False))
    quick=[x for x in quick if x]
    quick.sort(key=lambda x:x["score"], reverse=True)

    # Deep scan defaults to the entire NSE equity universe. If API load is a concern,
    # set DEEP_SCAN_LIMIT to a smaller number; candidates are always selected from the full live universe.
    selected=quick if DEEP_SCAN_LIMIT <= 0 else quick[:DEEP_SCAN_LIMIT]
    log.info("Deep technical scan: %d / %d live equities", len(selected), len(quick))

    results=[]
    for i,x in enumerate(selected,1):
        inst=next((z for z in universe if z["symbol"]==x["symbol"]),None)
        if not inst: continue
        try:
            a=analyse(inst,x and quotes.get(f"NSE_EQ:{x['symbol']}"),deep=True)
            if a: results.append(a)
        except Exception as e:
            log.warning("Analyse %s: %s", x["symbol"], e)
        if i % 25 == 0: log.info("Deep progress %d/%d", i, len(selected))
        time.sleep(0.13)  # stay under standard 500/minute API limit

    buys=sorted([x for x in results if x["action"]=="BUY WATCH"], key=lambda x:x["score"], reverse=True)[:TOP_ALERTS]
    avoids=sorted([x for x in results if x["action"]=="AVOID"], key=lambda x:x["score"])[:5]
    hs=holdings()
    sells=sell_watch_for_holdings(hs)

    lines=[f"📊 NSE STOCK SCAN | {ist_now().strftime('%d-%m-%Y %H:%M IST')}",
           f"Universe: {len(universe)} NSE cash equities",
           f"Deep analysed: {len(results)} | Alerts: {len(buys)}",
           ""]
    if buys:
        lines.append("🟢 BUY WATCH (manual)")
        for x in buys:
            lines.append(f"{x['symbol']} | Score {x['score']:.0f} | ₹{x['ltp']:.2f} | SL ₹{x['stop']:.2f} | T1 ₹{x['t1']:.2f} | T2 ₹{x['t2']:.2f}")
            lines.append("  " + ", ".join(x["reasons"][:4]))
            if x["news_titles"]: lines.append("  📰 " + x["news_titles"][0])
    else:
        lines.append("🟡 No BUY WATCH setup above threshold. WAIT.")
    if sells:
        lines += ["","🔴 HOLDINGS — SELL/EXIT WATCH (manual)"]
        for sym,ltp,avg,ch,pnl in sells:
            lines.append(f"{sym} | ₹{ltp:.2f} vs avg ₹{avg:.2f} | {ch:.1f}% | P&L ₹{pnl:.2f}")
    if avoids:
        lines += ["","⚠️ Weak/negative setups"]
        lines += [f"{x['symbol']} {x['score']:.0f} | {x['action']}" for x in avoids]
    lines += ["","⚠️ Analysis only. Bot does NOT place orders."]
    msg="\n".join(lines)
    if DRY_RUN: print(msg)
    else: tg(msg)
    log.info("Scan completed in %.1fs", time.time()-started)
    return results


# ---------------- TELEGRAM COMMANDS ----------------
COMMAND_HELP = """🤖 UPSTOX NSE STOCK BOT V2

/scan - Full NSE scan
/top - Top BUY WATCH setups
/buy - BUY WATCH list
/sell - Holdings SELL/EXIT WATCH
/wait - WAIT candidates
/stock SYMBOL - Full stock analysis
/news SYMBOL - Recent news impact
/entry SYMBOL - Entry/SL/Targets
/levels SYMBOL - Price levels
/breakout - Breakout candidates
/reversal - Reversal candidates
/momentum - Momentum candidates
/market - Market snapshot
/holdings - Upstox holdings
/portfolio - Holdings + P&L
/exit - Holdings exit watch
/pnl - Portfolio P&L
/watch SYMBOL - Add symbol to watchlist
/unwatch SYMBOL - Remove from watchlist
/watchlist - Show watchlist
/status - Bot status
/alerts_on - Enable scheduled alerts
/alerts_off - Disable scheduled alerts
/help - Commands

⚠️ Manual trading only. Bot never places orders.
"""
WATCHLIST=set(x.strip().upper() for x in os.getenv("WATCHLIST", "RELIANCE,TCS,HDFCBANK,ICICIBANK,INFY,SBIN,LT").split(",") if x.strip())
ALERTS_ENABLED=os.getenv("ALERTS_ENABLED", "1") == "1"

_universe_cache=None
_universe_cache_ts=0

def universe_cached(force=False):
    global _universe_cache, _universe_cache_ts
    if force or _universe_cache is None or time.time()-_universe_cache_ts > 6*3600:
        _universe_cache=load_nse_equities(); _universe_cache_ts=time.time()
    return _universe_cache

def find_inst(symbol):
    symbol=symbol.upper().strip()
    for x in universe_cached():
        if x["symbol"]==symbol: return x
    return None

def analyse_symbol(symbol):
    inst=find_inst(symbol)
    if not inst: return None
    q=market_quotes([inst]).get(inst["key"].replace("|",":"), {})
    if not q:
        # v3 response keys normally use instrument key with ':' in some examples; support fallback.
        qs=market_quotes([inst])
        q=next(iter(qs.values()), {})
    return analyse(inst,q,deep=True) if q else None

def fmt_analysis(x):
    if not x: return "❌ Stock not found or market data unavailable."
    out=[f"📌 {x['symbol']} — {x['name']}",f"Action: {x['action']}",f"Score: {x['score']:.0f}/100",f"LTP: ₹{x['ltp']:.2f}",f"Day: {x['day_pct']:+.2f}%"]
    if x.get('stop'): out += [f"Entry: ₹{x['entry']:.2f}",f"SL: ₹{x['stop']:.2f}",f"T1: ₹{x['t1']:.2f}",f"T2: ₹{x['t2']:.2f}"]
    if x.get('reasons'): out.append("Reasons: "+", ".join(x['reasons'][:6]))
    if x.get('news_titles'): out.append("📰 "+x['news_titles'][0])
    return "\n".join(out)+"\n\n⚠️ Manual analysis only."

def command_scan_subset(mode="top"):
    uni=universe_cached()
    qs=market_quotes(uni)
    quick=[]
    for inst in uni:
        q=qs.get(inst["key"].replace("|",":"), {})
        if q:
            a=analyse(inst,q,deep=False)
            if a: quick.append(a)
    if mode=="momentum": quick.sort(key=lambda x:x["day_pct"], reverse=True)
    elif mode=="reversal": quick.sort(key=lambda x:x["day_pct"])
    elif mode=="breakout": quick.sort(key=lambda x:x["day_pct"], reverse=True)
    else: quick.sort(key=lambda x:x["score"], reverse=True)
    selected=quick[:10]
    deep=[]
    for a in selected:
        x=analyse_symbol(a["symbol"])
        if x: deep.append(x)
    if mode=="reversal": deep.sort(key=lambda x:x["score"], reverse=True)
    elif mode in ("momentum","breakout"): deep.sort(key=lambda x:x["score"], reverse=True)
    else: deep.sort(key=lambda x:x["score"], reverse=True)
    return deep

def holdings_text():
    hs=holdings()
    if not hs: return "💼 No long-term holdings returned by Upstox."
    lines=["💼 HOLDINGS"]
    total=0
    for h in hs[:40]:
        sym=h.get("trading_symbol","?"); qty=h.get("quantity",0); avg=float(h.get("average_price") or 0); ltp=float(h.get("last_price") or 0); pnl=float(h.get("pnl") or 0); total+=pnl
        lines.append(f"{sym} | Qty {qty} | Avg ₹{avg:.2f} | LTP ₹{ltp:.2f} | P&L ₹{pnl:.2f}")
    lines.append(f"\nTotal reported P&L: ₹{total:.2f}")
    return "\n".join(lines)

def market_text():
    # NIFTY 50 index is used only as a market-regime reference.
    for sym in ("NIFTY 50", "NIFTY"):
        inst=next((x for x in universe_cached() if x["symbol"]==sym),None)
        if inst:
            q=market_quotes([inst]); qd=next(iter(q.values()),{})
            ltp=float(qd.get("last_price") or 0); prev=float(qd.get("prev_close_price") or 0)
            return f"📈 MARKET\nNIFTY: ₹{ltp:.2f}\nDay: {pct(ltp,prev):+.2f}%"
    return "📈 Market data unavailable."

def handle_command(chat_id,text):
    global ALERTS_ENABLED
    parts=text.strip().split()
    cmd=parts[0].split("@")[0].lower() if parts else ""
    arg=parts[1].upper() if len(parts)>1 else ""
    try:
        if cmd in ("/start","/help"): msg=COMMAND_HELP
        elif cmd=="/scan":
            run_scan(); msg="✅ Full scan sent to Telegram."
        elif cmd in ("/top","/buy"):
            xs=command_scan_subset("top"); buys=[x for x in xs if x["action"]=="BUY WATCH"][:10]
            msg="🟢 TOP BUY WATCH\n"+"\n".join(f"{x['symbol']} | {x['score']:.0f} | ₹{x['ltp']:.2f} | SL ₹{x['stop']:.2f} | T1 ₹{x['t1']:.2f}" for x in buys) if buys else "🟡 No strong BUY WATCH setup right now."
        elif cmd=="/wait":
            xs=command_scan_subset("top"); ws=[x for x in xs if x["action"]=="WAIT"][:10]
            msg="🟡 WAIT\n"+"\n".join(f"{x['symbol']} | {x['score']:.0f} | ₹{x['ltp']:.2f}" for x in ws) if ws else "No WAIT candidates in top scan."
        elif cmd in ("/stock","/entry","/levels"):
            if not arg: msg="Use: /stock RELIANCE"
            else: msg=fmt_analysis(analyse_symbol(arg))
        elif cmd=="/news":
            if not arg: msg="Use: /news RELIANCE"
            else:
                ns,titles=news_score(arg); msg=f"📰 {arg}\nNews score: {ns:+d}\n"+"\n".join("• "+t for t in titles) if titles else f"📰 {arg}\nNo recent keyword-scored news found."
        elif cmd in ("/breakout","/momentum","/reversal"):
            mode=cmd[1:]; xs=command_scan_subset(mode)
            msg=f"📊 {mode.upper()}\n"+"\n".join(f"{x['symbol']} | {x['score']:.0f} | {x['day_pct']:+.2f}% | {x['action']}" for x in xs[:10])
        elif cmd=="/market": msg=market_text()
        elif cmd in ("/holdings","/portfolio"):
            msg=holdings_text()
        elif cmd in ("/sell","/exit"):
            ss=sell_watch_for_holdings(holdings())
            msg="🔴 SELL/EXIT WATCH\n"+"\n".join(f"{s} | {ch:+.1f}% | P&L ₹{p:.2f}" for s,ltp,avg,ch,p in ss) if ss else "🟢 No holdings currently meet the conservative EXIT-WATCH rules."
        elif cmd=="/pnl":
            hs=holdings(); total=sum(float(h.get("pnl") or 0) for h in hs); msg=f"💰 Reported holdings P&L: ₹{total:.2f}"
        elif cmd=="/watch":
            if not arg or not find_inst(arg): msg="Use a valid NSE symbol: /watch RELIANCE"
            else: WATCHLIST.add(arg); msg=f"👀 Added {arg} to watchlist."
        elif cmd=="/unwatch":
            WATCHLIST.discard(arg); msg=f"👀 Removed {arg} from watchlist."
        elif cmd=="/watchlist": msg="👀 WATCHLIST\n"+", ".join(sorted(WATCHLIST))
        elif cmd=="/alerts_on": ALERTS_ENABLED=True; msg="🔔 Scheduled alerts ON"
        elif cmd=="/alerts_off": ALERTS_ENABLED=False; msg="🔕 Scheduled alerts OFF"
        elif cmd=="/status": msg=f"🤖 Bot OK\nUniverse: {len(universe_cached())} NSE equities\nScheduled alerts: {'ON' if ALERTS_ENABLED else 'OFF'}\nF&O: DISABLED\nOrders: NEVER"
        else: msg="Unknown command. Use /help"
    except Exception as e:
        log.exception("Command failed")
        msg=f"❌ Command error: {e}"
    send_to_chat(chat_id,msg)

def send_to_chat(chat_id,text):
    if not TELEGRAM_TOKEN: return False
    try:
        r=requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",json={"chat_id":chat_id,"text":text,"disable_web_page_preview":True},timeout=20)
        return r.ok
    except Exception as e:
        log.warning("Telegram send: %s",e); return False

def command_poller():
    offset=0
    while True:
        if not TELEGRAM_TOKEN:
            time.sleep(30); continue
        try:
            r=requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",params={"timeout":25,"offset":offset},timeout=35)
            if not r.ok: time.sleep(5); continue
            data=r.json()
            for u in data.get("result",[]):
                offset=u["update_id"]+1
                m=u.get("message") or u.get("edited_message")
                if not m or "text" not in m: continue
                handle_command(str(m["chat"]["id"]),m["text"])
        except Exception as e:
            log.warning("Command poller: %s",e); time.sleep(5)


def scheduler():
    sent_today=set()
    last_day=None
    while True:
        now=ist_now()
        if last_day != now.date():
            sent_today=set(); last_day=now.date()
        hhmm=now.strftime("%H:%M")
        if ALERTS_ENABLED and hhmm in SCAN_TIMES and hhmm not in sent_today:
            sent_today.add(hhmm)
            try: run_scan()
            except Exception as e:
                log.exception("Scan failed")
                tg(f"❌ Bot scan failed: {e}")
        time.sleep(20)

# Lightweight health endpoint for Render Web Service / Railway.
def health_server():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body=b"UPSTOX STOCK BOT OK\n"
            self.send_response(200); self.send_header("Content-Type","text/plain")
            self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self,*args): pass
    port=int(os.getenv("PORT","8080"))
    HTTPServer(("0.0.0.0",port),H).serve_forever()

if __name__=="__main__":
    if not UPSTOX_TOKEN:
        raise SystemExit("UPSTOX_ACCESS_TOKEN missing")
    if DRY_RUN:
        run_scan()
    else:
        tg("🤖 Upstox NSE Stock Bot started.\nStocks only • NSE cash • Manual BUY/SELL • No F&O")
        threading.Thread(target=health_server,daemon=True).start()
        threading.Thread(target=command_poller,daemon=True).start()
        scheduler()
