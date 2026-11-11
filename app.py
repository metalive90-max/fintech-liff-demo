import os, time, secrets, base64
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from pathlib import Path

app=FastAPI(title="TW Daytrade Mobile V1")
ROOT=Path(__file__).parent
PASSWORD=os.environ.get("APP_PASSWORD","")
USERNAME=os.environ.get("APP_USERNAME","trader")
if not PASSWORD: raise RuntimeError("APP_PASSWORD must be set")
CAPITAL=800000
DAILY_RISK=10000
WATCH={
"2449":["京元電","封測"],"3711":["日月光投控","封測"],
"6173":["信昌電","被動元件"],"2327":["國巨","被動元件"],
"3026":["禾伸堂","被動元件"],"1303":["南亞","CCL"],
"8358":["金居","銅箔"],"1802":["台玻","玻纖"],
"1815":["富喬","玻纖"],"8039":["台虹","電子材料"]
}
@app.middleware("http")
async def auth(request:Request,call_next):
 if request.url.path=="/health": return await call_next(request)
 header=request.headers.get("authorization","")
 ok=False
 if header.startswith("Basic "):
  try:
   u,p=base64.b64decode(header[6:],validate=True).decode().split(":",1)
   ok=secrets.compare_digest(u,USERNAME) and secrets.compare_digest(p,PASSWORD)
  except Exception: pass
 if not ok:
  return JSONResponse({"error":"login required"},status_code=401,headers={"WWW-Authenticate":'Basic realm="TW Trade Mobile"',"Cache-Control":"no-store"})
 response=await call_next(request)
 response.headers["Cache-Control"]="no-store"
 response.headers["X-Frame-Options"]="DENY"
 return response

@app.get("/health")
def health():return {"ok":True,"service":"tw-daytrade-mobile","trading":False}

@app.get("/api/status")
def status():
 return {
 "now":datetime.now(ZoneInfo("Asia/Taipei")).isoformat(),
 "market_data":"DISCONNECTED",
 "market_message":"尚未取得官方行情授權。禁止依此系統產生買進訊號。",
 "provider":"尚未連接",
 "auto_order":False,
 "capital_limit":CAPITAL,"daily_loss_limit":DAILY_RISK,
 "symbols":[{"symbol":k,"name":v[0],"sector":v[1],"status":"未連線","price":None,"vwap":None} for k,v in WATCH.items()]
 }

class RiskRequest(BaseModel):
 entry:float=Field(gt=0)
 stop:float=Field(gt=0)
 target:float=Field(gt=0)
 lots:int=Field(ge=1,le=100)
 overnight:bool=False

@app.post("/api/risk")
def risk(r:RiskRequest):
 shares=r.lots*1000
 tax=.003 if r.overnight else .0015
 fees=r.entry*shares*.001425+r.stop*shares*(.001425+tax)
 slip=r.stop*shares*.002
 estimated_loss=(r.entry-r.stop)*shares+fees+slip
 target_gain=(r.target-r.entry)*shares-r.entry*shares*.001425-r.target*shares*(.001425+tax)
 ok=r.stop<r.entry<r.target and r.entry*shares<=CAPITAL and estimated_loss<=DAILY_RISK and r.lots<=2
 return {"passes":ok,"capital":round(r.entry*shares),"risk_including_buffer":round(estimated_loss),
 "profit_target_estimate":round(target_gain),"risk_reward":round(target_gain/estimated_loss,2) if estimated_loss>0 else None,
 "message":"符合預設單筆限制，但不代表實際停損保證成交" if ok else "超出風險、資金或張數限制；不建議交易"}

@app.get("/",response_class=HTMLResponse)
def home():
 return (ROOT/"daytrade-dashboard.html").read_text(encoding="utf-8")
