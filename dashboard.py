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

log = logging.getLogger("dash")

# Shared snapshot; Maker overwrites this each loop.
STATS = {
    "status": "starting",
    "halted": False,
    "halt_reason": "",
    "balance_cents": None,
    "exposure_cents": 0,
    "equity_cents": None,
    "today_realized_cents": 0,
    "total_realized_cents": 0,
    "open_quotes": [],      # [{ticker, side, price, count, fair}]
    "recent_fills": [],     # [{ticker, side, price, count, at}]
    "recent_settles": [],   # [{ticker, pnl_cents, at}]
    "n_markets_quoted": 0,
    "fair_games": 0,
    "updated": "",
}

_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi Maker</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=Barlow+Condensed:wght@700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#07090F;--s1:#0D1117;--s2:#131923;--bd:#1C2535;--g:#00D68F;--g2:#003D24;
--r:#FF4D4D;--r2:#3D0000;--b:#4D9FFF;--a:#FFB84D;--t:#E8EDF5;--m:#4A5568;--l:#8892A4}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--t);font-family:'Inter',sans-serif;font-size:13px}
header{background:var(--s1);border-bottom:1px solid var(--bd);padding:0 20px;height:52px;
display:flex;align-items:center;justify-content:space-between;position:sticky;top:0}
.logo{font-family:'Barlow Condensed',sans-serif;font-size:19px;font-weight:800;letter-spacing:2px}
.logo span{color:var(--g)}
#st{padding:4px 12px;border-radius:20px;font-size:11px;font-weight:700;text-transform:uppercase}
.run{background:var(--g2);color:var(--g)}.halt{background:var(--r2);color:var(--r)}
main{padding:14px 16px;max-width:1100px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px}
@media(max-width:700px){.grid{grid-template-columns:1fr 1fr}}
.sc{background:var(--s1);border:1px solid var(--bd);border-radius:12px;padding:16px 18px;
position:relative;overflow:hidden}
.sc::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--b)}
.lbl{font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--m);margin-bottom:7px}
.val{font-family:'Barlow Condensed',sans-serif;font-size:32px;font-weight:700;line-height:1}
.sub{font-size:11px;color:var(--l);margin-top:5px}
.pos{color:var(--g)!important}.neg{color:var(--r)!important}.neu{color:var(--l)!important}
.card{background:var(--s1);border:1px solid var(--bd);border-radius:12px;overflow:hidden;margin-bottom:12px}
.hd{padding:12px 16px;border-bottom:1px solid var(--bd);display:flex;justify-content:space-between;align-items:center}
.ct{font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--m)}
table{width:100%;border-collapse:collapse}
th{padding:8px 14px;text-align:left;font-size:10px;font-weight:700;letter-spacing:.08em;
text-transform:uppercase;color:var(--m);background:var(--s2);border-bottom:1px solid var(--bd)}
td{padding:9px 14px;border-bottom:1px solid var(--bd);font-size:12px}
tr:last-child td{border-bottom:none}
.tag{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:700;text-transform:uppercase}
.tyes{background:#0A2540;color:var(--b)}.tno{background:#2A1F00;color:var(--a)}
.empty{padding:24px;text-align:center;color:var(--m)}
.foot{text-align:center;color:var(--m);font-size:11px;padding:12px}
</style></head><body>
<header><div class="logo">KALSHI<span>MAKER</span></div><div id="st" class="run">—</div></header>
<main>
<div class="grid">
<div class="sc"><div class="lbl">Balance</div><div class="val" id="bal">—</div><div class="sub" id="bal-sub">cash on exchange</div></div>
<div class="sc"><div class="lbl">Working</div><div class="val" id="exp">—</div><div class="sub" id="exp-sub">resting + positions</div></div>
<div class="sc"><div class="lbl">Today P&amp;L</div><div class="val" id="tp">—</div><div class="sub">realized, settles tonight</div></div>
<div class="sc"><div class="lbl">Total P&amp;L</div><div class="val" id="ap">—</div><div class="sub" id="ap-sub">all time realized</div></div>
</div>
<div class="card"><div class="hd"><span class="ct">Resting Quotes</span><span class="ct" id="qn"></span></div>
<div id="quotes"></div></div>
<div class="card"><div class="hd"><span class="ct">Recent Fills</span></div><div id="fills"></div></div>
<div class="card"><div class="hd"><span class="ct">Recent Settlements</span></div><div id="settles"></div></div>
<div class="foot" id="upd"></div>
</main>
<script>
const $=id=>document.getElementById(id);
const money=c=>c==null?'—':(c<0?'-':'')+'$'+Math.abs(c/100).toFixed(2);
const signed=c=>c==null?'—':(c>=0?'+':'-')+'$'+Math.abs(c/100).toFixed(2);
function cls(el,c){el.className='val '+(c>0?'pos':c<0?'neg':'neu')}
async function load(){
 try{
  const s=await fetch('/api/stats').then(r=>r.json());
  const st=$('st');
  st.textContent=s.halted?('HALTED — '+s.halt_reason):'running';
  st.className=s.halted?'halt':'run';
  $('bal').textContent=money(s.balance_cents);
  $('exp').textContent=money(s.exposure_cents);
  $('exp-sub').textContent=s.n_markets_quoted+' markets quoted · '+s.fair_games+' games priced';
  $('tp').textContent=signed(s.today_realized_cents);cls($('tp'),s.today_realized_cents);
  $('ap').textContent=signed(s.total_realized_cents);cls($('ap'),s.total_realized_cents);
  $('qn').textContent=s.open_quotes.length+' resting';
  $('quotes').innerHTML=s.open_quotes.length?('<table><tr><th>Market</th><th>Side</th><th>Price</th><th>Size</th><th>Fair</th></tr>'+
   s.open_quotes.map(q=>`<tr><td>${q.ticker}</td><td><span class="tag ${q.side==='yes'?'tyes':'tno'}">${q.side}</span></td><td>${q.price}¢</td><td>${q.count}</td><td>${q.fair!=null?q.fair.toFixed(1)+'¢':'—'}</td></tr>`).join('')+'</table>')
   :'<div class="empty">no quotes resting</div>';
  $('fills').innerHTML=s.recent_fills.length?('<table><tr><th>Time</th><th>Market</th><th>Side</th><th>Price</th><th>Size</th></tr>'+
   s.recent_fills.map(f=>`<tr><td style="color:var(--l)">${f.at}</td><td>${f.ticker}</td><td><span class="tag ${f.side==='yes'?'tyes':'tno'}">${f.side}</span></td><td>${f.price}¢</td><td>${f.count}</td></tr>`).join('')+'</table>')
   :'<div class="empty">no fills yet — patience is the strategy</div>';
  $('settles').innerHTML=s.recent_settles.length?('<table><tr><th>Time</th><th>Market</th><th>P&amp;L</th></tr>'+
   s.recent_settles.map(x=>`<tr><td style="color:var(--l)">${x.at}</td><td>${x.ticker}</td><td class="${x.pnl_cents>=0?'pos':'neg'}" style="font-weight:600">${signed(x.pnl_cents)}</td></tr>`).join('')+'</table>')
   :'<div class="empty">nothing settled yet</div>';
  $('upd').textContent='updated '+s.updated+' · refreshes every 15s';
 }catch(e){$('st').textContent='unreachable';$('st').className='halt';}
}
load();setInterval(load,15000);
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence default request logging
        pass

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
