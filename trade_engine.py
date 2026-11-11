"""Licensed quote polling and explainable, read-only candidate signals.

FinMind's snapshot provides average_price: provider's cumulative average price,
NOT a reconstructed tick-by-tick VWAP. Public quote display disabled by default.
"""
import os, time, threading, collections, math, json, urllib.request, urllib.parse, logging
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

TZ=ZoneInfo("Asia/Taipei")
SYMBOLS={
"2449":("京元電","封測"),"3711":("日月光投控","封測"),
"6173":("信昌電","被動元件"),"2327":("國巨","被動元件"),
"3026":("禾伸堂","被動元件"),"2492":("華新科","被動元件"),
"1303":("南亞","CCL"),"8358":("金居","銅箔"),
"1802":("台玻","玻纖"),"1815":("富喬","玻纖"),
"8039":("台虹","電子材料"),"3037":("欣興","PCB"),
"2368":("金像電","PCB"),"5340":("建榮","玻纖")}
CAPITAL=800000
DAILY_LOSS=10000
LOT_LIMIT=2
STALE_SECONDS=int(os.getenv("STALE_SECONDS","75"))
POLL_SECONDS=max(15,int(os.getenv("POLL_SECONDS","30")))
TOKEN=os.getenv("FINMIND_TOKEN","")
ALLOW_DISPLAY=os.getenv("QUOTE_DISPLAY_ALLOWED","false").lower()=="true"
BOT=os.getenv("TELEGRAM_BOT_TOKEN","")
CHAT=os.getenv("TELEGRAM_CHAT_ID","")
log=logging.getLogger("market")
_lock=threading.RLock()
rows={}
history=collections.defaultdict(lambda:collections.deque(maxlen=18))
alerts=collections.deque(maxlen=35)
sector_rank=[]
error=""
last_fetch=0
last_success=0
last_alert={}
_started=False

def now(): return datetime.now(TZ)
def market_open(at=None):
 at=at or now()
 return at.weekday()<5 and dtime(9,0)<=at.time()<dtime(13,30)
def f(x,default=None):
 try:
  n=float(x)
  if not math.isfinite(n):return default
  return n
 except (ValueError,TypeError): return default
def timestamp(x):
 try:
  d=datetime.fromisoformat(str(x).replace("Z","+00:00"))
  return d.replace(tzinfo=TZ) if d.tzinfo is None else d.astimezone(TZ)
 except Exception:return None

def profit_risk(entry,stop,target,lots,overnight=False,realized=0,buffer_bps=20):
 shares=int(lots)*1000
 tax=.003 if overnight else .0015
 buy_fee=.001425*entry*shares
 stop_fee=(.001425+tax)*stop*shares
 slip=max(stop,0)*shares*(buffer_bps/10000)
 loss=(entry-stop)*shares+buy_fee+stop_fee+slip
 gain=(target-entry)*shares-buy_fee-(.001425+tax)*target*shares
 daily_headroom=max(0,DAILY_LOSS+min(0,float(realized)))
 valid=bool(entry>stop>0 and target>entry and 1<=lots<=LOT_LIMIT and
            entry*shares<=CAPITAL and 0<loss<=daily_headroom and daily_headroom>0)
 return {"passes":valid,"capital":round(entry*shares),"risk_including_buffer":round(loss),
 "profit_target_estimate":round(gain),"risk_reward":round(gain/loss,2) if loss>0 else None,
 "remaining_loss_budget":round(daily_headroom),"fee_and_slippage_included":True,
 "message":("可納入觀察；不代表已觸發行情買點" if valid
     else "超出資金、張數或風險上限；不可作為買進依據")}

def _send_telegram(message):
 if not BOT or not CHAT:return
 try:
  payload=urllib.parse.urlencode({"chat_id":CHAT,"text":message,"disable_web_page_preview":"true"}).encode()
  req=urllib.request.Request("https://api.telegram.org/bot"+BOT+"/sendMessage",data=payload,method="POST")
  with urllib.request.urlopen(req,timeout=7) as r:r.read(100)
 except Exception as e:log.warning("Telegram delivery failed: %s",type(e).__name__)

def _poll():
 global rows,sector_rank,error,last_fetch,last_success
 t=now()
 if not TOKEN:return
 query=urllib.parse.urlencode({"data_id":list(SYMBOLS)+["001"]},doseq=True)
 req=urllib.request.Request("https://api.finmindtrade.com/api/v4/taiwan_stock_tick_snapshot?"+query,
 headers={"Authorization":"Bearer "+TOKEN,"Accept":"application/json","User-Agent":"private-trading-dashboard/1.1"})
 try:
  with urllib.request.urlopen(req,timeout=12) as res:
   raw=json.load(res)
  if raw.get("status") not in (None,200):
   raise ValueError("provider rejected request")
  data=raw.get("data")
  if not isinstance(data,list) or not data:raise ValueError("empty quotes")
  parsed={}
  for p in data:
   symbol=str(p.get("stock_id",""))
   if symbol not in SYMBOLS and symbol!="001":continue
   at=timestamp(p.get("date"))
   close=f(p.get("close"))
   avg=f(p.get("average_price"))
   vol=f(p.get("total_volume"))
   if not at or not close or not avg or not vol or close<=0 or avg<=0:continue
   parsed[symbol]={
    "symbol":symbol,"price":close,"average":avg,"volume":vol,
    "change":f(p.get("change_rate")),"high":f(p.get("high")),
    "low":f(p.get("low")),"timestamp":at.isoformat(),"fresh":(t-at).total_seconds()<=STALE_SECONDS
     and (t-at).total_seconds()>=-20 and at.date()==t.date()}
  with _lock:
   last_fetch=time.time()
   if not parsed:raise ValueError("no valid snapshots")
   market_day=t.date().isoformat()
   for s,p in parsed.items():
    rows[s]=p
    if p["fresh"] and s in SYMBOLS:
     h=history[s]
     if not h or h[-1]["timestamp"]!=p["timestamp"]:
      h.append(p)
   if parsed: last_success=time.time()
   error=""
   _rank_sectors()
   _signals()
 except Exception as e:
  with _lock:
   last_fetch=time.time()
   error=f"{type(e).__name__}: data provider unavailable"
  log.warning("poll failed: %s",type(e).__name__)

def _rank_sectors():
 global sector_rank
 by=collections.defaultdict(list)
 benchmark=rows.get("001",{})
 benchmark_change=benchmark.get("change") if benchmark.get("fresh") else None
 for s,p in rows.items():
  if s not in SYMBOLS or not p.get("fresh") or p.get("change") is None:continue
  by[SYMBOLS[s][1]].append(p["change"])
 ranking=[]
 for sector,changes in by.items():
  if len(changes)<2:continue
  avg=sum(changes)/len(changes)
  ranking.append({"sector":sector,"count":len(changes),"avg_change":round(avg,2),
    "relative_to_index":round(avg-benchmark_change,2) if benchmark_change is not None else None})
 sector_rank=sorted(ranking,key=lambda x:x["relative_to_index"] if x["relative_to_index"] is not None else x["avg_change"],reverse=True)

def _signals():
 t=now()
 if not market_open(t):return
 bench=rows.get("001",{})
 if not bench.get("fresh") or bench.get("change") is None or bench["change"]<-1.0:return
 sectors={r["sector"]:r for r in sector_rank}
 for symbol,(name,sector) in SYMBOLS.items():
  p=rows.get(symbol,{})
  h=list(history[symbol])
  if not p.get("fresh") or len(h)<4 or p.get("change") is None:continue
  latest=h[-1]
  if latest["timestamp"]!=p["timestamp"]:continue
  earlier=h[-4]
  # >=60 seconds of fresh samples to avoid spurious ticks or replays
  d=timestamp(p["timestamp"])-timestamp(earlier["timestamp"])
  if d.total_seconds()<45:continue
  industry=sectors.get(sector)
  if not industry or industry["relative_to_index"] is None or industry["relative_to_index"]<0.5:continue
  # prospective entry: not a promise to buy
  stronger=(p["price"]>p["average"]*1.002 and
   p["price"]>earlier["price"] and p["volume"]>earlier["volume"] and
   sum(x["price"]>x["average"] for x in h[-4:])>=3)
  # Conservative coarse exit candidate, not an automated sell order
  weaker=p["price"]<p["average"]*.992 and p["price"]<earlier["price"]
  if not (stronger or weaker):continue
  state="買點候選（人工確認）" if stronger else "轉弱警示"
  key=symbol+state
  if t.timestamp()-last_alert.get(key,0)<1800:continue
  last_alert[key]=t.timestamp()
  message=f"{name} {symbol}｜{state}｜{p['timestamp']}｜資料商當日均價參考，非完整逐筆VWAP；仍須確認五分鐘K線、資金及當沖資格。沒有任何自動下單。"
  alert={"symbol":symbol,"name":name,"status":state,"time":p["timestamp"],
   "reason":"成交價與累計量連續改善、強於大盤" if stronger else "低於當日均價且短期走弱",
   "message":message}
  alerts.appendleft(alert)
  # no actionable 'buy now': provider licensing and P&L incompleteness
  _send_telegram(message)

def _loop():
 while True:
  if TOKEN and market_open():
   _poll()
  time.sleep(POLL_SECONDS)

def start():
 global _started
 if _started:return
 _started=True
 threading.Thread(target=_loop,daemon=True,name="quote-watch").start()

def snapshot():
 t=now()
 with _lock:
  connected=bool(TOKEN)
  result=[]
  for symbol,(name,sector) in SYMBOLS.items():
   p=rows.get(symbol,{})
   age=(t-timestamp(p["timestamp"])).total_seconds() if p.get("timestamp") else None
   fresh=bool(p.get("fresh") and age is not None and -20<=age<=STALE_SECONDS and market_open(t))
   # Provider terms do not permit direct redistribution of live prices on web UI.
   result.append({"symbol":symbol,"name":name,"sector":sector,
    "state":"行情有效（僅供分析）" if fresh else ("資料過期" if connected else "等待授權"),
    "fresh":fresh,"time":p.get("timestamp") if fresh else None,
    "price":p.get("price") if ALLOW_DISPLAY and fresh else None,
    "avg_price":p.get("average") if ALLOW_DISPLAY and fresh else None,
    "price_display_authorized":ALLOW_DISPLAY})
  return {"now":t.isoformat(),"market_open":market_open(t),
  "provider":"FinMind" if connected else "未連接",
  "data_connection":"已設定授權（待檢驗）" if connected else "尚未設定行情 Token",
  "latest_success":datetime.fromtimestamp(last_success,TZ).isoformat() if last_success else None,
  "last_error":error,"sector_ranking":list(sector_rank) if market_open(t) and connected else [],
  "alerts":list(alerts)[:10],"symbols":result,
  "telegram_configured":bool(BOT and CHAT),"quote_display_authorized":ALLOW_DISPLAY,
  "auto_order":False,"capital_limit":CAPITAL,"daily_loss_limit":DAILY_LOSS,
  "market_note":"行情未驗證前不可依系統操作。平均成交價為資料商欄位，非逐筆計算VWAP。"}
