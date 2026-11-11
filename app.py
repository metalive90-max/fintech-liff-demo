"""TW Day Trading Decision Center V1.2 — monitoring only; never sends orders."""
import os, json, time, base64, secrets, asyncio, logging, math, sqlite3, re
from pathlib import Path
from urllib.parse import urlparse
from pywebpush import webpush, WebPushException
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from urllib.parse import urlencode
from urllib.request import Request as URLRequest, urlopen
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

log=logging.getLogger("daytrade")
TZ=ZoneInfo("Asia/Taipei")
ROOT=Path(__file__).parent
USERNAME=os.environ.get("APP_USERNAME","trader")
PASSWORD=os.environ.get("APP_PASSWORD","")
FINMIND_TOKEN=os.environ.get("FINMIND_TOKEN","").strip()
TG_TOKEN=os.environ.get("TELEGRAM_BOT_TOKEN","").strip()
TG_CHAT=os.environ.get("TELEGRAM_CHAT_ID","").strip()
DB_PATH=os.environ.get("DB_PATH","/tmp/daytrade-v12.sqlite3")
VAPID_PRIVATE_B64=os.environ.get("VAPID_PRIVATE_KEY_B64","").strip()
VAPID_PUBLIC=os.environ.get("VAPID_PUBLIC_KEY","").strip()
VAPID_SUBJECT=os.environ.get("VAPID_SUBJECT","https://tw-daytrade-mobile-2026.onrender.com")
POLL_SECONDS=max(20,int(os.environ.get("POLL_SECONDS","45")))
MAX_AGE=90
CAPITAL=800000
DAILY_LOSS=10000
MAX_LOTS=2
COMMISSION=.001425
SLIPPAGE=.002
WATCH={
"2449":("京元電","封測"),"3711":("日月光投控","封測"),
"6173":("信昌電","被動元件"),"2327":("國巨","被動元件"),
"3026":("禾伸堂","被動元件"),"1303":("南亞","CCL／材料"),
"8358":("金居","銅箔／PCB"),"1802":("台玻","玻纖"),
"1815":("富喬","玻纖"),"8039":("台虹","電子材料"),
"3037":("欣興","PCB"),"2368":("金像電","PCB"),
"2492":("華新科","被動元件"),"5340":("建榮","玻纖")}
SECTORS={"封測","被動元件","CCL／材料","銅箔／PCB","玻纖","電子材料","PCB"}
cache={"quotes":{},"last_fetch":None,"error":"尚未啟用即時行情","last_alerts":[]}
history=defaultdict(lambda:deque(maxlen=240))
sent_alerts={}
task=None
DATA_URL="https://api.finmindtrade.com/api/v4/taiwan_stock_tick_snapshot"
def now():return datetime.now(TZ)
def money(v):return round(float(v),2)
def local_date():return now().date().isoformat()
def db():
 c=sqlite3.connect(DB_PATH,timeout=3)
 c.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, price REAL NOT NULL, shares INT NOT NULL, fee REAL NOT NULL, created_at TEXT NOT NULL)")
 c.execute("CREATE TABLE IF NOT EXISTS push_subscriptions (endpoint TEXT PRIMARY KEY, subscription_json TEXT NOT NULL, created_at TEXT NOT NULL)")
 c.commit()
 return c
def pnl():
 with db() as c:
  rows=c.execute("SELECT side,price,shares,fee FROM trades WHERE day=? ORDER BY id",(local_date(),)).fetchall()
 gross=sum(((1 if side=="SELL" else -1)*price*shares) for side,price,shares,fee in rows)
 fees=sum(r[3] for r in rows)
 # Gross cash flow is NOT realized P&L when inventory remains; require flat positions for realized daily loss.
 positions=defaultdict(int)
 for side,price,shares,fee in rows: positions["cash"]+=shares*(1 if side=="BUY" else -1)
 # Conservative mark at cost: calculate realized FIFO from closed trades only.
 return gross-fees,len(rows)
def ledger():
 with db() as c:
  rows=c.execute("SELECT id,day,symbol,side,price,shares,fee,created_at FROM trades WHERE day=? ORDER BY id",(local_date(),)).fetchall()
 purchases=defaultdict(deque)
 realized=0.0;spent=0.;costs=0.;unmatched=False
 out=[]
 for id,day,sym,side,price,shares,fee,timestamp in rows:
  out.append({"id":id,"day":day,"symbol":sym,"side":side,"price":price,"shares":shares,"fee":fee,"created_at":timestamp})
  costs+=fee
  if side=="BUY":
   purchases[sym].append([shares,price]);spent+=shares*price
  else:
   remaining=shares
   while remaining and purchases[sym]:
    p=purchases[sym][0]
    n=min(remaining,p[0]);realized+=n*(price-p[1]);p[0]-=n;remaining-=n
    if not p[0]:purchases[sym].popleft()
   if remaining:unmatched=True
 inventory={k:sum(x[0] for x in v) for k,v in purchases.items() if v}
 # Subtract all costs conservatively from realized P&L, including open buys.
 net=realized-costs
 return {"date":local_date(),"realized_net_estimate":money(net),"net_trading_not_verified":True,
 "open_shares":inventory,"unmatched_sells":unmatched,"orders_logged":len(rows),"trades":out,
 "risk_budget_remaining":money(max(0,DAILY_LOSS+min(0,net))),"persistent":DB_PATH.startswith("/var/data/")}
def risk_values(entry,stop,target,lots,overnight=False):
 shares=int(lots)*1000
 tax=.003 if overnight else .0015
 purchase_fee=entry*shares*COMMISSION
 sale_fee=stop*shares*(COMMISSION+tax)
 slippage=stop*shares*SLIPPAGE
 risk=(entry-stop)*shares+purchase_fee+sale_fee+slippage
 gain=(target-entry)*shares-entry*shares*COMMISSION-target*shares*(COMMISSION+tax)
 record=ledger()
 permitted=entry>stop>0 and target>entry and lots<=MAX_LOTS and entry*shares<=CAPITAL and risk>0 and risk<=record["risk_budget_remaining"] and not record["unmatched_sells"]
 return {"passes":bool(permitted),"capital":round(entry*shares),"risk_including_buffer":round(risk),
 "profit_target_estimate":round(gain),"risk_reward":round(gain/risk,2) if risk>0 else None,
 "remaining_daily_risk":round(record["risk_budget_remaining"]),
 "message":"通過估算篩選，仍需人工檢查行情和券商實際持倉" if permitted else "超出張數、資金或可承受風險；不建議進場"}
def parse_time(value):
 try:
  s=str(value).replace("Z","+00:00")
  dt=datetime.fromisoformat(s)
  return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
 except Exception:return None
def num(r,k):
 try:
  v=float(r.get(k));return v if math.isfinite(v) else None
 except Exception:return None
def market_open():
 n=now()
 return n.weekday()<5 and dtime(9,0)<=n.time()<dtime(13,30)
def fresh(q):
 t=parse_time(q.get("date"))
 if not t:return False
 age=(now()-t).total_seconds()
 return t.date()==now().date() and 0<=age<=MAX_AGE
def market_quotes():
 pairs=[q for q in cache["quotes"].values() if fresh(q)]
 return pairs
def sector_rank():
 grouped=defaultdict(list)
 for q in market_quotes():
  cr=num(q,"change_rate")
  if cr is not None: grouped[WATCH[q["stock_id"]][1]].append(cr)
 return sorted([{"sector":name,"average_change_rate":round(sum(v)/len(v),2),"stocks":len(v),"coverage":sum(1 for sid in WATCH if WATCH[sid][1]==name)} for name,v in grouped.items()],key=lambda x:x["average_change_rate"],reverse=True)
def signal(q):
 sid=q["stock_id"]
 close=num(q,"close");avg=num(q,"average_price");high=num(q,"high")
 lo=num(q,"low");ratio=num(q,"volume_ratio")
 h=list(history[sid])
 if not fresh(q):return "資料過期","行情時間戳已超過90秒",None
 if not market_open():return "非交易時段","盤後行情僅供參考",None
 if not all(v and v>0 for v in (close,avg,high,lo)):return "觀察","價格資料不完整",None
 if not h or len(h)<4:return "觀察","累積有效快照不足，無法判斷動能",None
 earlier=h[-4][1]
 # Not a guaranteed fill or technical 5-minute bar; only snapshot based heuristic.
 momentum=(close-earlier)/earlier if earlier else 0
 if close<avg*.992:
  return "轉弱警戒","現價低於日內成交均價0.8%以上",None
 if close>avg*1.002 and momentum>.002 and ratio is not None and ratio>=.55:
  stop=min(avg,close*.989)
  target=close+2.0*(close-stop)
  rv=risk_values(close,stop,target,1)
  if rv["passes"] and rv["risk_reward"]>=1.5:
   return "候選訊號","價量快照轉強；尚須人工確認盤中K線、當沖資格及委託風險",{"entry":round(close,2),"stop":round(stop,2),"target":round(target,2),"lots":1,"risk":rv["risk_including_buffer"]}
 return "觀察","尚未符合價量快照條件",None
def fetch_snapshot_sync():
 if not FINMIND_TOKEN:return [],"尚未設定 FinMind Sponsor Token"
 q=urlencode({"data_id":list(WATCH.keys())+["001"]},doseq=True)
 req=URLRequest(DATA_URL+"?"+q,headers={"Authorization":"Bearer "+FINMIND_TOKEN,"User-Agent":"TWDaytradeV1/1.2"})
 try:
  with urlopen(req,timeout=15) as rsp:
   data=json.loads(rsp.read(1024*1024).decode())
  if not isinstance(data,dict) or not isinstance(data.get("data"),list):
   return [],"FinMind 回應格式不符或授權不足"
  records=data["data"]
  if not records:return [],"行情回傳空白，請檢查 Sponsor 權限"
  return records,None
 except Exception as e:
  return [],"無法取得行情："+type(e).__name__
def telegram_sync(message):
 if not TG_TOKEN or not TG_CHAT:return False
 payload=json.dumps({"chat_id":TG_CHAT,"text":message,"disable_web_page_preview":True}).encode()
 req=URLRequest(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",data=payload,headers={"Content-Type":"application/json"},method="POST")
 try:
  with urlopen(req,timeout=8) as rsp:return bool(json.loads(rsp.read()).get("ok"))
 except Exception:return False
async def monitor_once():
 records,err=await asyncio.to_thread(fetch_snapshot_sync)
 cache["last_fetch"]=now().isoformat()
 if err:
  cache["error"]=err
  return
 cache["error"]=None
 for item in records:
  sid=str(item.get("stock_id",""))
  if sid not in WATCH:continue
  t=parse_time(item.get("date"))
  if not t:continue
  prev=cache["quotes"].get(sid)
  if prev and str(prev.get("date"))==str(item.get("date")):continue
  cache["quotes"][sid]=item
  close=num(item,"close")
  if close and fresh(item):history[sid].append((t,close))
  state,reason,setup=signal(item)
  if state in ("候選訊號","轉弱警戒") and market_open():
   key=f"{local_date()}:{sid}:{state}"
   prev_alert=sent_alerts.get(key,0)
   if time.time()-prev_alert>1800:
    sent_alerts[key]=time.time()
    msg=f"【{WATCH[sid][0]} {sid}】{state}\n{reason}\n成交價:{close} 日均價:{item.get('average_price')}\n行情時間:{item.get('date')} (台灣)\n"
    if setup:msg+=f"候選參考:{setup['entry']}｜1張｜停損:{setup['stop']}｜最大估算風險:{setup['risk']}元\n"
    msg+="請以券商即時報價確認，非自動下單。"
    cache["last_alerts"].insert(0,{"time":now().isoformat(),"symbol":sid,"state":state,"message":msg,"sent":False})
    cache["last_alerts"]=cache["last_alerts"][:30]
    if TG_TOKEN and TG_CHAT:
     cache["last_alerts"][0]["sent"]=await asyncio.to_thread(telegram_sync,msg)
    if VAPID_PRIVATE_B64 and VAPID_PUBLIC:
     cache["last_alerts"][0]["push"]=await asyncio.to_thread(push_to_subscribers,state, WATCH[sid][0]+" "+sid, reason+" · 請用券商報價確認。")
async def worker():
 while True:
  try:
   if FINMIND_TOKEN and market_open():await monitor_once()
  except Exception:log.exception("Monitoring error")
  await asyncio.sleep(POLL_SECONDS)
@asynccontextmanager
async def lifespan(app):
 if not PASSWORD:raise RuntimeError("APP_PASSWORD must be set")
 db().close()
 t=asyncio.create_task(worker())
 try:yield
 finally:t.cancel()
app=FastAPI(title="TW Daytrade Decision Center",lifespan=lifespan)
@app.middleware("http")
async def auth(request:Request,call_next):
 if request.url.path in ("/health","/sw.js","/manifest.webmanifest","/push-icon.svg"):return await call_next(request)
 auth=request.headers.get("authorization","")
 good=False
 if auth.startswith("Basic "):
  try:
   u,p=base64.b64decode(auth[6:],validate=True).decode().split(":",1)
   good=secrets.compare_digest(u,USERNAME) and secrets.compare_digest(p,PASSWORD)
  except Exception:pass
 if not good:return JSONResponse({"error":"login required"},status_code=401,headers={"WWW-Authenticate":'Basic realm="TW Daytrade"', "Cache-Control":"no-store"})
 rsp=await call_next(request)
 rsp.headers["Cache-Control"]="no-store"
 rsp.headers["X-Frame-Options"]="DENY"
 rsp.headers["X-Content-Type-Options"]="nosniff"
 return rsp
@app.get("/health")
def health():return {"ok":True,"auto_order":False,"market_data_connected":bool(FINMIND_TOKEN and cache["quotes"])}
@app.get("/",response_class=HTMLResponse)
def home():return (ROOT/"daytrade-dashboard.html").read_text(encoding="utf-8")
@app.get("/api/status")
def status():
 book=ledger();groups=sector_rank()
 entries=[]
 for sid,(name,sec) in WATCH.items():
  q=cache["quotes"].get(sid)
  if not q:
   entries.append({"symbol":sid,"name":name,"sector":sec,"status":"資料未連線","price":None,"average_price":None,"timestamp":None})
   continue
  label,reason,setup=signal(q)
  entries.append({"symbol":sid,"name":name,"sector":sec,"price":num(q,"close"),"average_price":num(q,"average_price"),"high":num(q,"high"),"low":num(q,"low"),"change_rate":num(q,"change_rate"),"total_volume":num(q,"total_volume"),"volume_ratio":num(q,"volume_ratio"),"timestamp":q.get("date"),"fresh":fresh(q),"status":label,"reason":reason,"setup":setup})
 return {"now":now().isoformat(),"market_data":"CONNECTED" if FINMIND_TOKEN and market_quotes() else "DISCONNECTED",
 "provider":"FinMind Sponsor snapshot" if FINMIND_TOKEN else "未設定行情授權",
 "provider_vwap_type":"供應商 average_price（非完整逐筆 VWAP）",
 "market_message":cache["error"] or ("盤中行情已連線" if market_quotes() else "尚無當日新鮮快照；禁止買進提示"),
 "last_fetch":cache["last_fetch"],"auto_order":False,"capital_limit":CAPITAL,"daily_loss_limit":DAILY_LOSS,"max_lots":MAX_LOTS,
 "telegram_configured":bool(TG_TOKEN and TG_CHAT),"webpush_configured":bool(VAPID_PRIVATE_B64 and VAPID_PUBLIC),"persistence":book["persistent"],
 "ledger":{k:v for k,v in book.items() if k!="trades"},"sectors":groups,"symbols":entries,"alerts":cache["last_alerts"][:10]}
class RiskRequest(BaseModel):
 entry:float=Field(gt=0)
 stop:float=Field(gt=0)
 target:float=Field(gt=0)
 lots:int=Field(ge=1,le=100)
 overnight:bool=False
@app.post("/api/risk")
def risk(r:RiskRequest):return risk_values(r.entry,r.stop,r.target,r.lots,r.overnight)
class TradeRequest(BaseModel):
 symbol:str=Field(pattern=r"^[0-9]{4}$")
 side:str=Field(pattern=r"^(BUY|SELL)$")
 price:float=Field(gt=0)
 shares:int=Field(ge=1,le=1000000)
 overnight:bool=False
@app.post("/api/trades")
def add_trade(v:TradeRequest):
 if v.symbol not in WATCH:raise HTTPException(status_code=400,detail="not in watchlist")
 # Manual register only, not brokerage orders.
 fee=v.price*v.shares*(COMMISSION+(.003 if v.side=="SELL" and v.overnight else .0015 if v.side=="SELL" else 0))
 with db() as c:c.execute("INSERT INTO trades(day,symbol,side,price,shares,fee,created_at) VALUES(?,?,?,?,?,?,?)",(local_date(),v.symbol,v.side,v.price,v.shares,fee,now().isoformat()))
 return {"ok":True,"message":"僅新增手動交易紀錄；未送出任何券商委託","ledger":ledger()}
@app.get("/api/trades")
def trades():return ledger()
@app.post("/api/notifications/test")
async def notify_test():
 if not TG_TOKEN or not TG_CHAT:return {"ok":False,"message":"請先於 Render 設定 Telegram Bot Token 與 Chat ID"}
 ok=await asyncio.to_thread(telegram_sync,"台股當沖決策中心通知測試。系統不會自動下單。")
 return {"ok":ok,"message":"已傳送測試通知" if ok else "發送失敗，請檢查 Bot Token / Chat ID"}


# iPhone Home Screen PWA & standards-based Web Push. No broker orders.
@app.get("/manifest.webmanifest")
def manifest():
 return JSONResponse({"name":"台股當沖決策中心","short_name":"台股監控","start_url":"/","scope":"/","display":"standalone",
 "background_color":"#0b1422","theme_color":"#0b1422",
 "icons":[{"src":"/push-icon.svg","sizes":"any","type":"image/svg+xml","purpose":"any maskable"}]},
 headers={"Cache-Control":"public, max-age=3600"})

@app.get("/push-icon.svg")
def push_icon():
 from fastapi.responses import Response
 svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192"><rect width="192" height="192" rx="40" fill="#13263d"/><path d="M25 126L68 91L97 109L145 48L167 69" fill="none" stroke="#66ddb6" stroke-width="13" stroke-linecap="round" stroke-linejoin="round"/><path d="M26 151H167" stroke="#6787ad" stroke-width="7"/></svg>'
 return Response(svg, media_type="image/svg+xml",headers={"Cache-Control":"public, max-age=86400"})

@app.get("/sw.js")
def service_worker():
 from fastapi.responses import Response
 return Response((ROOT/"daytrade-sw.js").read_text(encoding="utf-8"),media_type="application/javascript",headers={"Cache-Control":"no-cache","Service-Worker-Allowed":"/"})

def push_enabled():return bool(VAPID_PRIVATE_B64 and VAPID_PUBLIC)

def push_record_count():
 with db() as c:return c.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0]

def endpoint_allowed(endpoint):
 try:
  parsed=urlparse(endpoint)
  host=(parsed.hostname or "").lower()
  return parsed.scheme=="https" and not parsed.username and not parsed.password and parsed.port in (None,443) and len(endpoint)<=2048 and (
  host in ("web.push.apple.com","fcm.googleapis.com","updates.push.services.mozilla.com")
  or host.endswith(".push.apple.com") or host.endswith(".notify.windows.com")
  )
 except Exception:return False

class PushSubscription(BaseModel):
 endpoint:str=Field(min_length=20,max_length=2048)
 keys:dict

def validate_push_subscription(v):
 if not endpoint_allowed(v.endpoint):raise HTTPException(status_code=422, detail="unsupported Web Push endpoint")
 for k in ("auth","p256dh"):
  val=v.keys.get(k)
  if not isinstance(val,str) or not 8<=len(val)<=300 or not re.fullmatch(r"[A-Za-z0-9_-]+",val):
   raise HTTPException(status_code=422,detail="invalid push key")
 return {"endpoint":v.endpoint,"keys":{"auth":v.keys["auth"],"p256dh":v.keys["p256dh"]}}

@app.get("/api/push/config")
def push_config():
 return {"configured":push_enabled(),"public_key":VAPID_PUBLIC if push_enabled() else None,
 "subscriber_count":push_record_count(),"persistent_storage":DB_PATH.startswith("/var/data/"),
 "message":"網頁推播可用，iPhone 請先加入主畫面再允許通知" if push_enabled() else "推播金鑰尚未設定"}

@app.post("/api/push/subscribe")
def push_subscribe(v:PushSubscription):
 if not push_enabled():raise HTTPException(status_code=503,detail="Web Push not configured")
 value=validate_push_subscription(v)
 with db() as c:
  c.execute("INSERT INTO push_subscriptions(endpoint,subscription_json,created_at) VALUES(?,?,?) ON CONFLICT(endpoint) DO UPDATE SET subscription_json=excluded.subscription_json",
  (v.endpoint,json.dumps(value),now().isoformat()))
 return {"ok":True,"message":"已訂閱網頁推播","subscriber_count":push_record_count()}

@app.post("/api/push/unsubscribe")
def push_unsubscribe(v:PushSubscription):
 value=validate_push_subscription(v)
 with db() as c:c.execute("DELETE FROM push_subscriptions WHERE endpoint=?",(value["endpoint"],))
 return {"ok":True}

def push_to_subscribers(event,title,body):
 if not push_enabled():return {"sent":0,"failed":0}
 with db() as c:
  rows=c.execute("SELECT endpoint,subscription_json FROM push_subscriptions LIMIT 50").fetchall()
 try:private=base64.b64decode(VAPID_PRIVATE_B64).decode()
 except Exception:return {"sent":0,"failed":len(rows),"error":"VAPID config"}
 sent=failed=0
 for endpoint,raw in rows:
  try:
   webpush(subscription_info=json.loads(raw),data=json.dumps({"title":"台股監控｜"+title,"body":body,
   "url":"/","tag":"daytrade-"+event+"-"+title},ensure_ascii=False),vapid_private_key=private,
   vapid_claims={"sub":VAPID_SUBJECT},ttl=120,timeout=8)
   sent+=1
  except WebPushException as exc:
   failed+=1
   if getattr(getattr(exc,"response",None),"status_code",0) in (404,410):
    with db() as c:c.execute("DELETE FROM push_subscriptions WHERE endpoint=?",(endpoint,))
   log.warning("Web Push delivery error: %s",type(exc).__name__)
  except Exception as exc:
   failed+=1;log.warning("Web Push delivery error: %s",type(exc).__name__)
 return {"sent":sent,"failed":failed,"subscribers":len(rows)}

@app.post("/api/push/test")
async def push_test():
 if not push_enabled():return {"ok":False,"message":"網頁推播尚未完成設定"}
 res=await asyncio.to_thread(push_to_subscribers,"測試","通知測試","這是測試通知，不是買入訊號。")
 return {"ok":res["sent"]>0,"message":f"推播服務回報已接受 {res['sent']} 筆、失敗 {res['failed']} 筆。是否實際顯示仍受裝置通知設定影響。"}
