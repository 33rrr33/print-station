#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
印刷ステーション  ~ Print Station ~
「フォルダに置くだけ」方式のPDF印刷システム。アカウント不要・校内で完結。

 児童など: デスクトップの「印刷ボックス」フォルダにPDFを入れる
 先生     : http://<MacのIP>:8000/ を開く → 一覧から確認 → 1タップで印刷
            印刷したPDFは「印刷ずみ」フォルダへ自動で移動する

標準ライブラリのみ。macOSの `lp` コマンドで印刷する。
"""

import http.server
import socketserver
import subprocess
import os
import re
import json
import time
import socket
from urllib.parse import unquote, parse_qs, quote

PORT = 8000
WATCH = os.path.expanduser("~/Desktop/印刷ボックス")       # ここにPDFを入れる
DONE = os.path.join(WATCH, "印刷ずみ")                     # 印刷後はここへ移動
MAX_COPIES = 100
DEFAULT_COPIES = 1
os.makedirs(WATCH, exist_ok=True)
os.makedirs(DONE, exist_ok=True)

history = []   # {filename, copies, time, status}


# ────────────────────────────── プリンタ ──────────────────────────────
IP_NAME = re.compile(r"^_?\d{1,3}(_\d{1,3}){3}$")  # IPアドレスがそのまま名前の一時プリンタは隠す


def get_printers():
    printers, default = [], None
    try:
        out = subprocess.run(["lpstat", "-e"], capture_output=True, text=True).stdout
        printers = [ln.strip() for ln in out.splitlines()
                    if ln.strip() and not IP_NAME.match(ln.strip())]
    except Exception:
        pass
    try:
        out = subprocess.run(["lpstat", "-d"], capture_output=True, text=True).stdout
        m = re.search(r"(\S+)\s*$", out.strip())
        if m and m.group(1) in printers:
            default = m.group(1)
    except Exception:
        pass
    if not default and printers:
        default = printers[0]
    return printers, default


def do_print(filepath, printer, copies, sides):
    cmd = ["lp"]
    if printer:
        cmd += ["-d", printer]
    cmd += ["-n", str(copies)]
    cmd += ["-o", "sides=two-sided-long-edge"] if sides == "two-sided" else ["-o", "sides=one-sided"]
    cmd += [filepath]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True, (r.stdout.strip() or "送信しました")
        return False, (r.stderr.strip() or "印刷に失敗しました")
    except Exception as e:
        return False, f"エラー: {e}"


# ────────────────────────────── フォルダ監視 ──────────────────────────────
def scan_queue():
    """印刷ボックス直下のPDFを新しい順で返す。"""
    items = []
    try:
        for name in os.listdir(WATCH):
            if name.startswith(".") or name == "印刷ずみ":
                continue
            p = os.path.join(WATCH, name)
            if os.path.isfile(p) and name.lower().endswith(".pdf"):
                mt = os.path.getmtime(p)
                items.append({"id": name, "filename": name, "path": p, "mtime": mt,
                              "time": time.strftime("%H:%M", time.localtime(mt))})
    except Exception:
        pass
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def safe_path(fid):
    """id（=ファイル名）を印刷ボックス直下の実在PDFパスに変換。ディレクトリ抜け防止。"""
    name = os.path.basename(fid)
    p = os.path.join(WATCH, name)
    if os.path.isfile(p) and name.lower().endswith(".pdf"):
        return p, name
    return None, name


def move_done(path):
    base = os.path.basename(path)
    dest = os.path.join(DONE, base)
    if os.path.exists(dest):
        root, ext = os.path.splitext(base)
        dest = os.path.join(DONE, f"{root}_{int(time.time())}{ext}")
    try:
        os.rename(path, dest)
    except OSError:
        pass


def save_upload(name, orig_filename, data):
    """児童がWebで送ったPDFを印刷ボックスに保存。ファイル名に送信者名を付ける。"""
    stem = os.path.splitext(os.path.basename(orig_filename))[0]
    label = (name.strip() + "_" if name.strip() else "") + stem
    label = re.sub(r"[/\\:*?\"<>|]", "_", label)[:60]  # 危険な文字だけ除去
    dest = os.path.join(WATCH, label + ".pdf")
    if os.path.exists(dest):
        dest = os.path.join(WATCH, f"{label}_{int(time.time())}.pdf")
    with open(dest, "wb") as fp:
        fp.write(data)
    return dest


# ────────────────────────────── multipart ──────────────────────────────
def parse_multipart(body, boundary):
    fields, fname, fdata = {}, None, None
    for part in body.split(b"--" + boundary):
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        he = part.find(b"\r\n\r\n")
        if he == -1:
            continue
        headers = part[:he].decode("utf-8", "ignore")
        content = part[he + 4:]
        nm = re.search(r'name="([^"]*)"', headers)
        if not nm:
            continue
        fm = re.search(r'filename="([^"]*)"', headers)
        if fm:
            fname, fdata = fm.group(1), content
        else:
            fields[nm.group(1)] = content.decode("utf-8", "ignore").strip()
    return fields, fname, fdata


# ────────────────────────────── 児童 送信ページ ──────────────────────────────
CHILD_PAGE = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>プリント いんさつ おねがい</title>
<style>
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body { font-family:-apple-system,"Hiragino Sans",sans-serif; margin:0; background:#eef6ff; color:#1c1c1e; }
  .wrap { max-width:520px; margin:0 auto; padding:24px 18px 60px; }
  h1 { font-size:28px; text-align:center; margin:16px 0 6px; }
  .sub { text-align:center; color:#5a6b80; font-size:15px; margin-bottom:22px; }
  .card { background:#fff; border-radius:20px; padding:22px; box-shadow:0 3px 16px rgba(0,60,120,.08); }
  label { font-weight:700; font-size:17px; display:block; margin:16px 0 8px; }
  .sel3 { display:flex; gap:10px; }
  .sel3 select { flex:1; font-size:22px; padding:15px 8px; border:2px solid #cdd8e6; border-radius:14px; background:#fff; text-align:center; }
  .drop { border:3px dashed #9dc0ea; border-radius:16px; padding:30px; text-align:center; color:#3d6da8; font-size:18px; font-weight:600; }
  .drop.has { border-color:#1a7f37; color:#1a7f37; background:#f0faf3; }
  .send { width:100%; font-size:26px; font-weight:800; padding:24px; border:none; border-radius:18px; background:#0a84ff; color:#fff; margin-top:26px; }
  .send:disabled { background:#b0c0d4; }
</style></head>
<body><div class="wrap">
  <h1>🖨️ いんさつ おねがい</h1>
  <div class="sub">ねん・くみ・ばんを えらんで、PDFを えらんで、そうしんボタンを おそう</div>
  <form class="card" method="POST" action="/submit" enctype="multipart/form-data" id="f">
    <label>ねん・くみ・ばん</label>
    <div class="sel3">
      <select name="nen" id="nen"><option value="">ねん</option>%NEN%</select>
      <select name="kumi" id="kumi"><option value="">くみ</option>%KUMI%</select>
      <select name="ban" id="ban"><option value="">ばん</option>%BAN%</select>
    </div>
    <label>いんさつする PDF</label>
    <div class="drop" id="drop">タップして PDFを えらぶ</div>
    <input type="file" id="file" name="file" accept="application/pdf" style="display:none">
    <button type="submit" class="send" id="btn" disabled>えらんでね</button>
  </form>
</div>
<script>
  const fi=document.getElementById('file'),drop=document.getElementById('drop'),btn=document.getElementById('btn');
  const nen=document.getElementById('nen'),kumi=document.getElementById('kumi'),ban=document.getElementById('ban');
  function ready(){ return fi.files.length && nen.value && kumi.value && ban.value; }
  function upd(){ const r=ready(); btn.disabled=!r; btn.textContent = r ? '📨 せんせいに そうしん' : 'えらんでね'; }
  drop.onclick=()=>fi.click();
  fi.onchange=()=>{ if(fi.files.length){ drop.textContent='📄 '+fi.files[0].name; drop.classList.add('has'); } upd(); };
  nen.onchange=upd; kumi.onchange=upd; ban.onchange=upd;
  document.getElementById('f').onsubmit=()=>{ btn.disabled=true; btn.textContent='そうしんちゅう…'; };
</script>
</body></html>"""

CHILD_DONE = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>そうしん かんりょう</title>
<style>
  body { font-family:-apple-system,"Hiragino Sans",sans-serif; margin:0; background:#eef6ff; color:#1c1c1e; }
  .done { text-align:center; padding:70px 20px; }
  .big { font-size:72px; }
  h2 { font-size:28px; color:#1a7f37; margin:10px 0; }
  p { font-size:18px; color:#41546b; }
  .again { display:inline-block; margin-top:24px; font-size:19px; font-weight:700; color:#0a84ff; background:#fff; padding:16px 30px; border-radius:14px; text-decoration:none; box-shadow:0 2px 10px rgba(0,60,120,.1); }
</style></head>
<body><div class="done">
  <div class="big">%ICON%</div>
  <h2>%TITLE%</h2>
  <p>%MSG%</p>
  <a class="again" href="/">%LINK%</a>
</div></body></html>"""


def child_done(icon, title, msg, link="もう1まい おくる"):
    return (CHILD_DONE.replace("%ICON%", icon).replace("%TITLE%", title)
            .replace("%MSG%", msg).replace("%LINK%", link))


def _opts(nmax, unit):
    return "".join(f'<option value="{i}">{i}{unit}</option>' for i in range(1, nmax + 1))


def child_page():
    return (CHILD_PAGE
            .replace("%NEN%", _opts(6, "ねん"))
            .replace("%KUMI%", _opts(8, "くみ"))
            .replace("%BAN%", _opts(40, "ばん")))


# ────────────────────────────── 先生ページ ──────────────────────────────
TEACHER_PAGE = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>印刷ステーション</title>
<style>
  * { box-sizing:border-box; }
  body { font-family:-apple-system,"Hiragino Sans",sans-serif; margin:0; background:#f2f4f7; color:#1c1c1e; }
  .wrap { max-width:640px; margin:0 auto; padding:18px 14px 60px; }
  h1 { font-size:22px; text-align:center; margin:10px 0 2px; }
  .hint { text-align:center; color:#7a8699; font-size:13px; margin-bottom:14px; }
  .bar { background:#fff; border-radius:14px; padding:14px; box-shadow:0 2px 10px rgba(0,0,0,.05); margin-bottom:16px; display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .bar label { font-size:13px; font-weight:700; color:#555; }
  select { font-size:16px; padding:10px; border:2px solid #d0d5dd; border-radius:10px; flex:1; min-width:140px; }
  h2 { font-size:16px; color:#444; margin:18px 4px 10px; display:flex; align-items:center; gap:8px; }
  .badge { background:#0a84ff; color:#fff; border-radius:20px; padding:2px 12px; font-size:14px; }
  .item { background:#fff; border-radius:16px; padding:16px; box-shadow:0 2px 10px rgba(0,0,0,.06); margin-bottom:12px; }
  .top { display:flex; align-items:center; gap:12px; }
  .chk { width:30px; height:30px; flex:0 0 auto; accent-color:#0a84ff; margin:0; }
  .fn { flex:1; color:#0a84ff; font-size:18px; font-weight:800; text-decoration:none; word-break:break-all; }
  .meta { color:#8a94a2; font-size:13px; margin-top:3px; margin-left:42px; }
  .row { display:flex; gap:10px; align-items:center; margin-top:14px; }
  .copies { display:flex; height:52px; flex:0 0 auto; }
  .stp { width:42px; height:100%; font-size:26px; font-weight:800; border:2px solid #d0d5dd; background:#f2f6fc; color:#0a84ff; }
  .stp:active { background:#dbe7f7; }
  .stp.minus { border-radius:12px 0 0 12px; border-right:none; }
  .stp.plus { border-radius:0 12px 12px 0; border-left:none; }
  .cnum { width:48px; height:100%; text-align:center; font-size:21px; font-weight:800; border:2px solid #d0d5dd; background:#fff; color:#1c1c1e; padding:0; }
  .print { flex:1; font-size:20px; font-weight:800; padding:16px 8px; border:none; border-radius:14px; background:#0a84ff; color:#fff; white-space:nowrap; }
  .print:active { background:#0060df; }
  .del { font-size:20px; padding:14px 16px; border:2px solid #e6bcbc; border-radius:12px; background:#fff; color:#c0392b; }
  .empty { text-align:center; color:#98a2b3; padding:40px 0; font-size:15px; line-height:1.7; }
  table { width:100%; border-collapse:collapse; font-size:13px; background:#fff; border-radius:12px; overflow:hidden; }
  td { padding:9px 8px; border-bottom:1px solid #eee; }
  .toast { position:fixed; left:50%; bottom:26px; transform:translateX(-50%); background:#1c1c1e; color:#fff; padding:14px 24px; border-radius:30px; font-size:16px; font-weight:700; opacity:0; transition:.25s; z-index:9; }
  .toast.show { opacity:1; }
  .side { display:flex; gap:6px; }
  .sbtn { font-size:13px; font-weight:700; padding:12px 10px; border:2px solid #d0d5dd; border-radius:10px; background:#fff; color:#555; }
  .sbtn.on { border-color:#0a84ff; background:#eaf3ff; color:#0a52c9; }
  .allbtn { margin-left:auto; font-size:15px; font-weight:800; padding:10px 18px; border:none; border-radius:12px; background:#1a7f37; color:#fff; }
  .allbtn:disabled { background:#b8c2cc; }
  .urlcard { background:#fff; border-radius:14px; padding:14px; box-shadow:0 2px 10px rgba(0,0,0,.05); margin-bottom:16px; }
  .urllabel { font-size:13px; font-weight:700; color:#555; margin-bottom:8px; }
  .urlrow { display:flex; gap:8px; }
  #childurl { flex:1; font-size:15px; padding:12px; border:2px solid #d0d5dd; border-radius:10px; background:#f7f9fc; color:#1c1c1e; min-width:0; }
  .copybtn { font-size:15px; font-weight:800; padding:12px 18px; border:none; border-radius:10px; background:#0a84ff; color:#fff; white-space:nowrap; }
  .copybtn:active { background:#0060df; }
</style></head>
<body><div class="wrap">
  <h1>🖨️ 印刷ステーション</h1>
  <div class="hint">「印刷ボックス」フォルダに入れたPDFがここに並びます</div>

  <div class="urlcard">
    <div class="urllabel">👦 児童用リンク（これを児童に配ると、タップで送信ページが開きます）</div>
    <div class="urlrow">
      <input id="childurl" readonly onclick="this.select()">
      <button class="copybtn" onclick="copyUrl()">📋 コピー</button>
    </div>
  </div>

  <h2>📥 印刷まち <span class="badge" id="cnt">0</span>
    <button class="allbtn" id="allbtn" onclick="printAll()" disabled>🖨️ チェックを印刷</button></h2>
  <div id="list"><div class="empty">読み込み中…</div></div>

  <h2>🧾 印刷ずみ（直近10件）</h2>
  <table id="hist"><tbody></tbody></table>
</div>
<div class="toast" id="toast"></div>
<script>
let currentQueue=[], lastSig=null;
const DEFAULT_COPIES=%DEFAULT_COPIES%, MAX_COPIES=%MAX_COPIES%;
const copyState={}, checkState={};
function step(enc,d){ const el=document.getElementById('c_'+enc); let v=(parseInt(el.value)||1)+d; if(v<1)v=1; if(v>MAX_COPIES)v=MAX_COPIES; el.value=v; copyState[enc]=v; }
function chkChange(enc){ checkState[enc]=document.getElementById('chk_'+enc).checked; }
function toast(t){ const el=document.getElementById('toast'); el.textContent=t; el.classList.add('show'); setTimeout(()=>el.classList.remove('show'),2200); }
function esc(s){ return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function renderList(queue){
  const list=document.getElementById('list');
  if(queue.length===0){ list.innerHTML='<div class="empty">いまは 印刷まちは ありません<br>「印刷ボックス」フォルダにPDFを入れてください</div>'; return; }
  list.innerHTML='';
  queue.forEach(it=>{
    const enc=encodeURIComponent(it.id);
    const val=(copyState[enc]!==undefined)?copyState[enc]:DEFAULT_COPIES;
    const chk=(checkState[enc]===false)?'':'checked';
    const div=document.createElement('div'); div.className='item';
    div.innerHTML=
      '<div class="top"><input type="checkbox" class="chk" id="chk_'+enc+'" '+chk+' onchange="chkChange(\\''+enc+'\\')"><a class="fn" href="/file/'+enc+'" target="_blank">📄 '+esc(it.filename)+'</a></div>'+
      '<div class="meta">'+esc(it.time)+' に追加</div>'+
      '<div class="row">'+
        '<div class="copies"><button class="stp minus" onclick="step(\\''+enc+'\\',-1)">−</button>'+
          '<input class="cnum" id="c_'+enc+'" value="'+val+'" readonly>'+
          '<button class="stp plus" onclick="step(\\''+enc+'\\',1)">＋</button></div>'+
        '<button class="print" onclick="doPrint(\\''+enc+'\\')">🖨️ 印刷</button>'+
        '<button class="del" onclick="doReject(\\''+enc+'\\')">🗑</button>'+
      '</div>';
    list.appendChild(div);
  });
}

async function refresh(){
  try{
    const r=await fetch('/api/state'); const d=await r.json();
    currentQueue=d.queue;
    document.getElementById('cnt').textContent=d.queue.length;
    document.getElementById('allbtn').disabled=(d.queue.length===0);
    const sig=d.queue.map(it=>it.id).join('|');
    if(sig!==lastSig){ lastSig=sig; renderList(d.queue); }
    const tb=document.querySelector('#hist tbody'); tb.innerHTML='';
    if(d.history.length===0){ tb.innerHTML='<tr><td style="color:#aaa;text-align:center">まだありません</td></tr>'; }
    d.history.slice().reverse().forEach(h=>{
      const tr=document.createElement('tr');
      tr.innerHTML='<td>'+(h.status==='printed'?'✅':'🗑')+'</td><td>'+esc(h.filename)+'</td><td>'+(h.status==='printed'?h.copies+'部':'さくじょ')+'</td><td>'+esc(h.time)+'</td>';
      tb.appendChild(tr);
    });
  }catch(e){}
}
async function doPrint(enc){
  const copies=document.getElementById('c_'+enc).value;
  const r=await fetch('/api/print',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'id='+enc+'&copies='+copies});
  const d=await r.json();
  toast(d.ok?('🖨️ '+copies+'部 印刷しました'):('⚠️ '+d.msg));
  refresh();
}
async function printAll(){
  const items=currentQueue.filter(it=>{ const c=document.getElementById('chk_'+encodeURIComponent(it.id)); return c && c.checked; });
  if(items.length===0){ toast('チェックされた PDFが ありません'); return; }
  let total=0;
  items.forEach(it=>{ const el=document.getElementById('c_'+encodeURIComponent(it.id)); total+=parseInt((el&&el.value)||DEFAULT_COPIES)||0; });
  if(!confirm('チェックした '+items.length+'件を 合計'+total+'部 印刷します。よろしいですか？')) return;
  const btn=document.getElementById('allbtn'); btn.disabled=true; btn.textContent='印刷中…';
  let ok=0, ng=0;
  for(const it of items){
    const enc=encodeURIComponent(it.id);
    const el=document.getElementById('c_'+enc);
    const copies=(el&&el.value)||DEFAULT_COPIES;
    try{
      const r=await fetch('/api/print',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'id='+enc+'&copies='+copies});
      const d=await r.json(); d.ok?ok++:ng++;
    }catch(e){ ng++; }
  }
  btn.textContent='🖨️ チェックを印刷';
  toast('🖨️ '+ok+'件 印刷しました'+(ng?('／'+ng+'件 失敗'):''));
  refresh();
}
async function doReject(enc){
  if(!confirm('このPDFを 削除しますか？（「印刷ずみ」フォルダへ移動します）')) return;
  await fetch('/api/reject',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'id='+enc});
  refresh();
}
const CHILD_URL = location.origin + '/';
document.getElementById('childurl').value = CHILD_URL;
function copyUrl(){
  const done=()=>toast('📋 コピーしました');
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(CHILD_URL).then(done, fallbackCopy);
  } else { fallbackCopy(); }
}
function fallbackCopy(){
  const el=document.getElementById('childurl'); el.focus(); el.select();
  try{ document.execCommand('copy'); toast('📋 コピーしました'); }
  catch(e){ toast('長押しでコピーしてください'); }
}
refresh(); setInterval(refresh,3000);
</script>
</body></html>"""


# ────────────────────────────── HTTP ハンドラ ──────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype="text/html; charset=utf-8", code=200, extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", code)

    def do_GET(self):
        path = unquote(self.path.split("?")[0])
        if path == "/":
            self._send(child_page())
        elif path in ("/teacher", "/先生"):
            self._send(TEACHER_PAGE
                       .replace("%DEFAULT_COPIES%", str(DEFAULT_COPIES))
                       .replace("%MAX_COPIES%", str(MAX_COPIES)))
        elif path == "/api/printers":
            printers, default = get_printers()
            self._json({"printers": printers, "default": default})
        elif path == "/api/state":
            q = [{"id": it["id"], "filename": it["filename"], "time": it["time"]} for it in scan_queue()]
            self._json({"queue": q, "history": history[-10:]})
        elif path.startswith("/file/"):
            p, name = safe_path(unquote(path[len("/file/"):]))
            if p:
                with open(p, "rb") as fp:
                    self._send(fp.read(), "application/pdf",
                               extra={"Content-Disposition": "inline; filename*=UTF-8''" + quote(name)})
            else:
                self._send("見つかりません", code=404)
        else:
            self._send("見つかりません", code=404)

    def do_POST(self):
        path = unquote(self.path.split("?")[0])
        length = int(self.headers.get("Content-Length", 0))

        # 児童からのPDF送信（multipart）→ 印刷ボックスへ保存
        if path == "/submit":
            if length > 50 * 1024 * 1024:
                self._send(child_done("⚠️", "ファイルが 大きすぎます", "50MB までにしてね"), code=413)
                return
            m = re.search(r"boundary=(.+)", self.headers.get("Content-Type", ""))
            if not m:
                self._send(child_done("⚠️", "おくれませんでした", "もういちど ためしてね"), code=400)
                return
            boundary = m.group(1).strip('"').encode("utf-8")
            fields, fname, fdata = parse_multipart(self.rfile.read(length), boundary)
            if not fdata or not fname or not fname.lower().endswith(".pdf"):
                self._send(child_done("📄", "PDFを えらんでね", "PDFファイルだけ おくれます"))
                return
            nen, kumi, ban = fields.get("nen", ""), fields.get("kumi", ""), fields.get("ban", "")
            label = f"{nen}ねん{kumi}くみ{ban}ばん" if (nen and kumi and ban) else ""
            save_upload(label, fname, fdata)
            self._send(child_done("✅", "せんせいに おくりました！", "せんせいが かくにんして いんさつします。"))
            return

        body = self.rfile.read(length).decode("utf-8", "ignore")
        form = {k: v[0] for k, v in parse_qs(body).items()}

        if path == "/api/print":
            p, name = safe_path(form.get("id", ""))
            if not p:
                self._json({"ok": False, "msg": "見つかりません"})
                return
            try:
                copies = max(1, min(int(form.get("copies", DEFAULT_COPIES)), MAX_COPIES))
            except ValueError:
                copies = DEFAULT_COPIES
            printer = form.get("printer", "").strip()
            if not printer:
                # 使えるプリンタを自動選択（IP名の応答しない一時プリンタは除外済み）
                _, printer = get_printers()
            ok, msg = do_print(p, printer or "", copies, form.get("sides", "one-sided"))
            if ok:
                move_done(p)
                history.append({"filename": name, "copies": copies,
                                "time": time.strftime("%H:%M"), "status": "printed"})
            self._json({"ok": ok, "msg": msg})
            return

        if path == "/api/reject":
            p, name = safe_path(form.get("id", ""))
            if p:
                move_done(p)
                history.append({"filename": name, "copies": 0,
                                "time": time.strftime("%H:%M"), "status": "rejected"})
            self._json({"ok": True})
            return

        self._send("見つかりません", code=404)

    def log_message(self, *args):
        pass


class ReuseServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    ip = "?"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    print("=" * 54)
    print("  🖨️  印刷ステーション 起動中")
    print("=" * 54)
    print(f"  児童用（送信）  : http://{ip}:{PORT}/")
    print(f"  先生用（印刷）  : http://{ip}:{PORT}/teacher")
    print(f"  PDF保存フォルダ : {WATCH}")
    print(f"  停止: Control + C")
    print("=" * 54)
    with ReuseServer(("0.0.0.0", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n停止しました。")
