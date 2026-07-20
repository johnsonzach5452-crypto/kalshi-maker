"""Tiny built-in dashboard server. No dependencies beyond stdlib.

Serves:
  /            -> HTML dashboard (auto-refreshing)
  /api/stats   -> JSON snapshot the Maker updates each loop
"""
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

log = logging.getLogger("dash")

# Shared snapshot; Maker overwrites this each loop.
STATS = {
    "status": "starting",
    "mode": "live",
    "halted": False,
    "halt_reason": "",
    "balance_cents": None,
    "exposure_cents": 0,
    "equity_cents": None,
    "today_realized_cents": 0,
    "total_realized_cents": 0,
    "open_quotes": [],      # [{ticker, side, price, count, fair}]
    "recent_fills": [],     # [{at, ticker, side, price, count, fair, edge, clv}]
    "recent_settles": [],   # [{ticker, pnl_cents, at}]
    "inventory": [],        # [{ticker, net, cost_cents}]
    "pnl_history": [],      # [{day, realized_cents}]
    "n_markets_quoted": 0,
    "fair_games": 0,
    "edge": {},          # rolling 7d edge health
    "targets": [],       # priced markets w/ fair, uncertainty, state
    "quota_remaining": None,
    "loop_secs": 30,
    "caps": {},
    "updated": "",
}

_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi Maker</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=Barlow+Condensed:wght@700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#06080D;--s1:#0C1017;--s2:#121826;--bd:#1B2436;--bd2:#243048;
--g:#00D68F;--gd:#003A26;--r:#FF5C5C;--rd:#3A0F0F;--b:#5AA2FF;--bdk:#0A2036;
--a:#FFB84D;--ad:#3A2A08;--p:#B58CFF;--t:#EAF0F9;--m:#4C586E;--l:#93A0B8}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--t);font-family:'Inter',sans-serif;font-size:13px;
background-image:radial-gradient(1200px 400px at 50% -100px,#0E1524 0%,transparent 70%)}
.mono{font-family:'JetBrains Mono',monospace}
header{background:rgba(12,16,23,.85);backdrop-filter:blur(8px);border-bottom:1px solid var(--bd);
padding:0 18px;height:54px;display:flex;align-items:center;justify-content:space-between;
position:sticky;top:0;z-index:5}
.logo{font-family:'Barlow Condensed',sans-serif;font-size:20px;font-weight:800;letter-spacing:2px}
.logo span{color:var(--g)}
.chips{display:flex;gap:7px;align-items:center;flex-wrap:wrap}
.chip{padding:4px 11px;border-radius:20px;font-size:10px;font-weight:700;
text-transform:uppercase;letter-spacing:.06em;border:1px solid transparent}
.c-run{background:var(--gd);color:var(--g)}.c-halt{background:var(--rd);color:var(--r)}
.c-paper{background:var(--ad);color:var(--a)}.c-live{background:var(--bdk);color:var(--b)}
.c-dim{background:var(--s2);color:var(--l);border-color:var(--bd)}
main{padding:14px;max-width:1160px;margin:0 auto}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:10px}
@media(max-width:760px){.g4{grid-template-columns:1fr 1fr}}
.sc{background:linear-gradient(180deg,var(--s1),#0A0E15);border:1px solid var(--bd);
border-radius:14px;padding:14px 16px;position:relative;overflow:hidden}
.sc.hero::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
background:linear-gradient(90deg,var(--b),var(--p))}
.lbl{font-size:9.5px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--m);margin-bottom:6px}
.val{font-family:'Barlow Condensed',sans-serif;font-size:30px;font-weight:700;line-height:1}
.sub{font-size:10.5px;color:var(--l);margin-top:5px}
.pos{color:var(--g)!important}.neg{color:var(--r)!important}.neu{color:var(--l)!important}
.meter{height:5px;background:var(--s2);border-radius:3px;margin-top:9px;overflow:hidden}
.meter i{display:block;height:100%;border-radius:3px;background:var(--b);transition:width .6s}
.card{background:var(--s1);border:1px solid var(--bd);border-radius:14px;overflow:hidden;margin-bottom:10px}
.hd{padding:11px 15px;border-bottom:1px solid var(--bd);display:flex;justify-content:space-between;align-items:center;gap:8px}
.ct{font-size:10px;font-weight:700;letter-spacing:.11em;text-transform:uppercase;color:var(--m)}
.hint{font-size:10px;color:var(--m)}
table{width:100%;border-collapse:collapse}
th{padding:7px 13px;text-align:left;font-size:9.5px;font-weight:700;letter-spacing:.08em;
text-transform:uppercase;color:var(--m);background:var(--s2);border-bottom:1px solid var(--bd);white-space:nowrap}
td{padding:8px 13px;border-bottom:1px solid var(--bd);font-size:12px;white-space:nowrap}
tr:last-child td{border-bottom:none}
.scroll{overflow-x:auto}
.tag{display:inline-block;padding:2px 8px;border-radius:20px;font-size:9.5px;font-weight:700;text-transform:uppercase}
.tyes{background:var(--bdk);color:var(--b)}.tno{background:var(--ad);color:var(--a)}
.tq{background:var(--gd);color:var(--g)}.ts{background:var(--rd);color:var(--r)}
.tp{background:var(--s2);color:var(--l)}.tu{background:#2A1A3A;color:var(--p)}
.empty{padding:22px;text-align:center;color:var(--m)}
.chart{padding:12px 14px 6px;position:relative}
.bars{display:flex;align-items:flex-end;gap:4px;height:88px;position:relative}
.bwrap{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:3px;min-width:0;height:100%}
.bar{width:100%;border-radius:3px 3px 0 0;min-height:2px}
.bday{font-size:8.5px;color:var(--m)}
svg.cum{position:absolute;inset:12px 14px 20px;width:calc(100% - 28px);height:calc(100% - 32px);pointer-events:none}
.legend{display:flex;gap:14px;padding:0 14px 10px;font-size:10px;color:var(--l)}
.dot{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.risk{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:13px 15px}
@media(max-width:600px){.risk{grid-template-columns:1fr}}
.rrow .lbl{margin-bottom:5px}
.rtxt{display:flex;justify-content:space-between;font-size:11px;color:var(--l);margin-top:4px}
.foot{text-align:center;color:var(--m);font-size:10.5px;padding:10px}
</style></head><body>
<header><div class="logo">KALSHI<span>MAKER</span></div>
<div class="chips"><button id="pw" class="chip c-dim" style="cursor:pointer;border:1px solid var(--bd2);background:var(--s2)" onclick="toggle()">…</button>
<span id="quota" class="chip c-dim">quota —</span>
<span id="cad" class="chip c-dim">30s</span>
<span id="md" class="chip c-live">—</span><span id="st" class="chip c-run">—</span></div></header>
<main>
<div class="g4">
<div class="sc hero"><div class="lbl">Balance</div><div class="val" id="bal">—</div><div class="sub">cash on exchange</div></div>
<div class="sc hero"><div class="lbl">Working Capital</div><div class="val" id="exp">—</div>
<div class="meter"><i id="expm"></i></div><div class="sub" id="exp-sub">of cap</div></div>
<div class="sc hero"><div class="lbl">Today P&amp;L</div><div class="val" id="tp">—</div><div class="sub" id="tp-sub">realized</div></div>
<div class="sc hero"><div class="lbl">Total P&amp;L</div><div class="val" id="ap">—</div><div class="sub">all time, real money</div></div>
</div>
<div class="g4">
<div class="sc"><div class="lbl">Fills Today</div><div class="val" id="ef">—</div><div class="sub" id="ef-sub">fees $—</div></div>
<div class="sc"><div class="lbl">Edge @ Fill · 7d</div><div class="val" id="ee">—</div><div class="sub">avg vs fair when filled</div></div>
<div class="sc"><div class="lbl">CLV vs Close · 7d</div><div class="val" id="ec">—</div><div class="sub" id="ec-sub">the number that matters</div></div>
<div class="sc"><div class="lbl">Markets Live</div><div class="val" id="mq">—</div><div class="sub" id="mq-sub">quoted · priced</div></div>
</div>
<div class="card"><div class="hd"><span class="ct">Risk Limits</span><span class="hint" id="halted"></span></div>
<div class="risk">
<div class="rrow"><div class="lbl">Daily Loss vs Stop</div><div class="meter"><i id="dlm"></i></div><div class="rtxt"><span id="dll">$0</span><span id="dlr">limit $—</span></div></div>
<div class="rrow"><div class="lbl">Drawdown vs Kill</div><div class="meter"><i id="ddm"></i></div><div class="rtxt"><span id="ddl">$0</span><span id="ddr">limit $—</span></div></div>
</div></div>
<div class="card"><div class="hd"><span class="ct">Daily P&amp;L — 14 days</span></div>
<div class="chart"><div class="bars" id="pnlbars"></div><svg class="cum" id="cumsvg" preserveAspectRatio="none"></svg></div>
<div class="legend"><span><span class="dot" style="background:var(--g)"></span>daily +</span>
<span><span class="dot" style="background:var(--r)"></span>daily −</span>
<span><span class="dot" style="background:var(--p)"></span>cumulative</span></div></div>
<div class="card"><div class="hd"><span class="ct">Priced Markets</span><span class="hint">fair from sharp consensus · unc = book disagreement</span></div>
<div class="scroll" id="targets"></div></div>
<div class="card"><div class="hd"><span class="ct">Resting Quotes</span><span class="hint" id="qn"></span></div>
<div class="scroll" id="quotes"></div></div>
<div class="card"><div class="hd"><span class="ct">Inventory</span></div><div class="scroll" id="inv"></div></div>
<div class="card"><div class="hd"><span class="ct">Fill History</span><span class="hint">edge = fair − price at fill · vs close = maker CLV</span></div>
<div class="scroll" id="fills"></div></div>
<div class="card"><div class="hd"><span class="ct">Settlements</span></div><div class="scroll" id="settles"></div></div>
<div class="foot" id="upd"></div>
</main>
<script>
const $=id=>document.getElementById(id);
async function toggle(){
 let t=localStorage.getItem('dtok')||prompt('control token');
 if(!t)return;localStorage.setItem('dtok',t);
 const r=await fetch('/api/pause?token='+encodeURIComponent(t),{method:'POST'});
 const j=await r.json();
 if(j.error){alert(j.error);if(j.error==='bad token')localStorage.removeItem('dtok');return}
 $('pw').textContent=j.paused?'▶ RESUME':'⏸ PAUSE';load();}
const money=c=>c==null?'—':(c<0?'-':'')+'$'+Math.abs(c/100).toFixed(2);
const signed=c=>c==null?'—':(c>=0?'+':'-')+'$'+Math.abs(c/100).toFixed(2);
const cents=v=>(v==null||isNaN(v))?'—':(v>=0?'+':'')+Number(v).toFixed(1)+'¢';
function vcls(el,c){el.className='val '+(c>0?'pos':c<0?'neg':'neu')}
function meter(el,frac,warn){el.style.width=Math.min(100,frac*100)+'%';
 el.style.background=frac>=1?'var(--r)':frac>=warn?'var(--a)':'var(--b)'}
async function load(){
 try{
  const s=await fetch('/api/stats').then(r=>r.json());
  const e=s.edge||{},caps=s.caps||{};
  $('st').textContent=s.halted?'HALTED':s.status;
  $('st').className='chip '+((s.halted||s.status==='degraded')?'c-halt':'c-run');
  $('halted').textContent=s.halt_reason||'';
  $('md').textContent=s.mode==='paper'?'PAPER':'LIVE';
  $('md').className='chip '+(s.mode==='paper'?'c-paper':'c-live');
  $('quota').textContent='odds quota '+(s.quota_remaining!=null?Math.round(s.quota_remaining).toLocaleString():'—');
  $('cad').textContent='loop '+s.loop_secs+'s';
  $('bal').textContent=money(s.balance_cents);
  $('exp').textContent=money(s.exposure_cents);
  if(caps.total){meter($('expm'),(s.exposure_cents||0)/caps.total,.8);
   $('exp-sub').textContent=Math.round((s.exposure_cents||0)/caps.total*100)+'% of '+money(caps.total)+' cap'}
  $('tp').textContent=signed(s.today_realized_cents);vcls($('tp'),s.today_realized_cents);
  $('tp-sub').textContent=s.mode==='paper'?'simulated':'realized';
  $('ap').textContent=signed(s.total_realized_cents);vcls($('ap'),s.total_realized_cents);
  $('ef').textContent=e.fills_today!=null?e.fills_today:'—';
  $('ef-sub').textContent='fees '+money(e.fees_today_cents||0)+' today';
  $('ee').textContent=cents(e.avg_edge_at_fill);vcls($('ee'),e.avg_edge_at_fill||0);
  $('ec').textContent=cents(e.avg_clv);vcls($('ec'),e.avg_clv||0);
  $('ec-sub').textContent=(e.clv_scored||0)+' fills scored · 7d';
  $('mq').textContent=s.n_markets_quoted;
  $('mq-sub').textContent=(s.targets||[]).length+' priced · '+s.fair_games+' games';
  // risk meters
  const dl=Math.max(0,-(s.today_realized_cents||0));
  if(caps.daily_loss){meter($('dlm'),dl/caps.daily_loss,.6);
   $('dll').textContent=money(dl)+' lost';$('dlr').textContent='stop '+money(caps.daily_loss)}
  const dd=Math.max(0,(caps.start_bankroll||0)-(s.equity_cents||caps.start_bankroll||0));
  if(caps.drawdown){meter($('ddm'),dd/caps.drawdown,.6);
   $('ddl').textContent=money(dd)+' down';$('ddr').textContent='kill '+money(caps.drawdown)}
  // pnl bars + cumulative line
  const ph=s.pnl_history||[];
  const mx=Math.max(1,...ph.map(d=>Math.abs(d.realized_cents)));
  $('pnlbars').innerHTML=ph.length?ph.map(d=>{
    const h=Math.max(3,Math.abs(d.realized_cents)/mx*62);
    const c=d.realized_cents>=0?'var(--g)':'var(--r)';
    return '<div class="bwrap" title="'+d.day+': '+signed(d.realized_cents)+'">'+
      '<div class="bar" style="height:'+h+'px;background:'+c+'"></div>'+
      '<div class="bday">'+d.day.slice(5)+'</div></div>'}).join('')
   :'<div class="empty" style="width:100%">no history yet</div>';
  const svg=$('cumsvg');
  if(ph.length>1){let cum=0;const pts=ph.map(d=>cum+=d.realized_cents);
   const lo=Math.min(0,...pts),hi=Math.max(1,...pts),rng=hi-lo||1;
   const W=100,H=100;
   const path=pts.map((v,i)=>((i/(pts.length-1))*W).toFixed(1)+','+((H-8)-((v-lo)/rng*(H-16))).toFixed(1)).join(' ');
   svg.setAttribute('viewBox','0 0 100 100');
   svg.innerHTML='<polyline points="'+path+'" fill="none" stroke="var(--p)" stroke-width="1.6" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>';
  } else svg.innerHTML='';
  // priced markets
  const stag={quoting:'<span class="tag tq">quoting</span>',steam:'<span class="tag ts">steam</span>',
   pulled:'<span class="tag tp">pulled</span>','skip-unc':'<span class="tag tu">unc skip</span>'};
  $('targets').innerHTML=(s.targets&&s.targets.length)?('<table><tr><th>Market</th><th>Fair (YES)</th><th>Unc</th><th>Books</th><th>Starts In</th><th>State</th></tr>'+
   s.targets.map(t=>{const u=t.uncertainty;const uc=u>=3?'neg':u>=1.5?'':'pos';
    const mins=t.mins>=60?Math.floor(t.mins/60)+'h '+(t.mins%60)+'m':t.mins+'m';
    return '<tr><td class="mono">'+t.ticker+'</td><td class="mono">'+t.fair.toFixed(1)+'¢</td>'+
    '<td class="'+uc+'">'+(u!=null?u.toFixed(1)+'¢':'—')+'</td><td>'+t.n_books+'</td><td>'+mins+'</td><td>'+(stag[t.state]||t.state)+'</td></tr>'}).join('')+'</table>')
   :'<div class="empty">no markets matched — waiting on slate</div>';
  $('qn').textContent=s.open_quotes.length+' resting';
  $('quotes').innerHTML=s.open_quotes.length?('<table><tr><th>Market</th><th>Side</th><th>Price</th><th>Size</th><th>Fair</th><th>Edge</th></tr>'+
   s.open_quotes.map(q=>{const ed=q.fair!=null?q.fair-q.price:null;
   return '<tr><td class="mono">'+q.ticker+'</td><td><span class="tag '+(q.side==='yes'?'tyes':'tno')+'">'+q.side+'</span></td><td class="mono">'+q.price+'¢</td><td>'+q.count+'</td><td class="mono">'+(q.fair!=null?q.fair.toFixed(1)+'¢':'—')+'</td><td class="'+(ed>0?'pos':'neg')+'">'+cents(ed)+'</td></tr>'}).join('')+'</table>')
   :'<div class="empty">no quotes resting</div>';
  $('inv').innerHTML=(s.inventory&&s.inventory.length)?('<table><tr><th>Market</th><th>Net</th><th>Cost Basis</th><th>% of Cap</th></tr>'+
   s.inventory.map(i=>{const pct=caps.per_market?Math.round(i.cost_cents/caps.per_market*100):null;
   return '<tr><td class="mono">'+i.ticker+'</td><td class="'+(i.net>0?'pos':i.net<0?'neg':'')+'">'+(i.net>0?'+':'')+i.net+' '+(i.net>0?'YES':i.net<0?'NO':'')+'</td><td>'+money(i.cost_cents)+'</td><td>'+(pct!=null?pct+'%':'—')+'</td></tr>'}).join('')+'</table>')
   :'<div class="empty">flat — no open positions</div>';
  $('fills').innerHTML=s.recent_fills.length?('<table><tr><th>Time</th><th>Market</th><th>Side</th><th>Price</th><th>Size</th><th>Fair@Fill</th><th>Edge</th><th>vs Close</th></tr>'+
   s.recent_fills.map(f=>'<tr><td style="color:var(--l)">'+f.at+'</td><td class="mono">'+f.ticker+'</td><td><span class="tag '+(f.side==='yes'?'tyes':'tno')+'">'+f.side+'</span></td><td class="mono">'+f.price+'¢</td><td>'+f.count+'</td><td class="mono">'+(f.fair!=null?f.fair.toFixed(1)+'¢':'—')+'</td><td class="'+(f.edge>0?'pos':f.edge<0?'neg':'')+'">'+cents(f.edge)+'</td><td class="'+(f.clv>0?'pos':f.clv<0?'neg':'')+'">'+cents(f.clv)+'</td></tr>').join('')+'</table>')
   :'<div class="empty">no fills yet — patience is the strategy</div>';
  $('settles').innerHTML=s.recent_settles.length?('<table><tr><th>Time</th><th>Market</th><th>P&amp;L</th></tr>'+
   s.recent_settles.map(x=>'<tr><td style="color:var(--l)">'+x.at+'</td><td class="mono">'+x.ticker+'</td><td class="'+(x.pnl_cents>=0?'pos':'neg')+'" style="font-weight:600">'+signed(x.pnl_cents)+'</td></tr>').join('')+'</table>')
   :'<div class="empty">nothing settled yet</div>';
  $('upd').textContent='updated '+s.updated+' · auto-refresh 10s';
 }catch(err){$('st').textContent='unreachable';$('st').className='chip c-halt';}
}
load();setInterval(load,10000);
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence default request logging
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/pause":
            return self._json(404, {"error": "not found"})
        import config as C
        if not C.DASH_TOKEN:
            return self._json(403, {"error": "set DASH_TOKEN to enable"})
        token = parse_qs(u.query).get("token", [""])[0]
        if token != C.DASH_TOKEN:
            return self._json(403, {"error": "bad token"})
        import store
        paused = store.meta_get("paused") == "1"
        store.meta_set("paused", "0" if paused else "1")
        return self._json(200, {"paused": not paused})

    def do_GET(self):
        if self.path.startswith("/api/stats"):
            body = json.dumps(STATS).encode()
            ctype = "application/json"
        else:
            body = _HTML.encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def start_dashboard():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info(f"dashboard serving on :{port}")
