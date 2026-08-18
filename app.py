#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
印刷ステーション（どこでも版 / Cloud）
- 1つのURLを どの端末で開いてもOK（iPad / iPhone / PC）
- 印刷は「その端末のブラウザのプリント画面」で実行 → その端末に登録済みのプリンタ（AirPrint等）で印刷
- Mac不要。校内・自宅・どこでも同じURLで使える

児童: 送信ページ（/）で ねん・くみ・ばん を選んでPDFを送信
先生: 先生ページ（/teacher）で一覧確認 → 「印刷」または「まとめて印刷」
      → PDFが開くので、端末の共有/プリントから自分のプリンタで印刷

Renderなどのクラウドに置いて動かす。ローカルでも `python3 app.py` で動く。
"""

import os
import re
import io
import base64
import json
import time
import uuid
import tempfile
import threading
import http.server
import socketserver
from urllib.parse import unquote, parse_qs, quote

PORT = int(os.environ.get("PORT", "8000"))
UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "print_station_uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
MAX_MB = 50

# 先生ページ（閲覧・PDF）を守るパスワード。Renderの Environment で TEACHER_PASSWORD を設定する。
# 未設定なら、先生側は安全のため停止（児童のデータは誰にも見えない）。
TEACHER_PASSWORD = os.environ.get("TEACHER_PASSWORD", "").strip()
PROTECTED = ("/teacher", "/先生", "/api/state", "/file/", "/merge", "/api/delete")


def needs_auth(path):
    return any(path == p or path.startswith(p) for p in PROTECTED)


SETUP_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>準備中</title>
<style>body{font-family:-apple-system,"Hiragino Sans",sans-serif;background:#fff7f0;color:#333;text-align:center;padding:60px 22px;line-height:1.8}
h2{color:#c0392b}code{background:#eee;padding:2px 6px;border-radius:5px}</style></head>
<body><div style="font-size:56px">🔒</div>
<h2>先生ページは 準備中です</h2>
<p>安全のため、パスワード（<code>TEACHER_PASSWORD</code>）が設定されるまで<br>先生ページと児童のPDFは表示されません。</p>
<p>設定すると、パスワードで守られた状態で使えるようになります。</p></body></html>"""

# PDF結合（まとめて印刷）用。無ければ結合機能だけ無効化。
try:
    from pypdf import PdfWriter, PdfReader
    HAS_PYPDF = True
except Exception:
    HAS_PYPDF = False

_lock = threading.Lock()
items = {}   # id -> {"name": 表示名, "time": "HH:MM", "order": int}
_counter = [0]


def add_item(display_name, data):
    with _lock:
        fid = uuid.uuid4().hex
        with open(os.path.join(UPLOAD_DIR, fid + ".pdf"), "wb") as f:
            f.write(data)
        _counter[0] += 1
        items[fid] = {"name": display_name, "time": time.strftime("%H:%M"), "order": _counter[0]}
        return fid


def path_of(fid):
    p = os.path.join(UPLOAD_DIR, os.path.basename(fid) + ".pdf")
    return p if (fid in items and os.path.isfile(p)) else None


def remove_item(fid):
    with _lock:
        if fid in items:
            items.pop(fid, None)
            p = os.path.join(UPLOAD_DIR, fid + ".pdf")
            try:
                os.remove(p)
            except OSError:
                pass


def queue_list():
    with _lock:
        return [{"id": k, "name": v["name"], "time": v["time"]}
                for k, v in sorted(items.items(), key=lambda kv: kv[1]["order"], reverse=True)]


def merge_pdfs(ids):
    w = PdfWriter()
    for i in ids:
        p = path_of(i)
        if not p:
            continue
        try:
            r = PdfReader(p)
            for pg in r.pages:
                w.add_page(pg)
        except Exception:
            continue
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


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
def opts(nmax, unit):
    return "".join(f'<option value="{i}">{i}{unit}</option>' for i in range(1, nmax + 1))


CHILD_PAGE = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
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
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>そうしん かんりょう</title>
<style>
  body { font-family:-apple-system,"Hiragino Sans",sans-serif; margin:0; background:#eef6ff; color:#1c1c1e; }
  .done { text-align:center; padding:70px 20px; }
  .big { font-size:72px; } h2 { font-size:28px; color:#1a7f37; margin:10px 0; } p { font-size:18px; color:#41546b; }
  .again { display:inline-block; margin-top:24px; font-size:19px; font-weight:700; color:#0a84ff; background:#fff; padding:16px 30px; border-radius:14px; text-decoration:none; box-shadow:0 2px 10px rgba(0,60,120,.1); }
</style></head>
<body><div class="done"><div class="big">%ICON%</div><h2>%TITLE%</h2><p>%MSG%</p><a class="again" href="/">%LINK%</a></div></body></html>"""


def child_page():
    return CHILD_PAGE.replace("%NEN%", opts(6, "ねん")).replace("%KUMI%", opts(8, "くみ")).replace("%BAN%", opts(40, "ばん"))


def child_done(icon, title, msg, link="もう1まい おくる"):
    return CHILD_DONE.replace("%ICON%", icon).replace("%TITLE%", title).replace("%MSG%", msg).replace("%LINK%", link)


# ────────────────────────────── 先生 コンソール ──────────────────────────────
TEACHER_PAGE = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>印刷ステーション</title>
<style>
  * { box-sizing:border-box; }
  body { font-family:-apple-system,"Hiragino Sans",sans-serif; margin:0; background:#f2f4f7; color:#1c1c1e; }
  .wrap { max-width:640px; margin:0 auto; padding:18px 14px 60px; }
  h1 { font-size:22px; text-align:center; margin:10px 0 2px; }
  .hint { text-align:center; color:#7a8699; font-size:13px; margin-bottom:14px; }
  .linkcard { background:#fff; border-radius:14px; padding:14px; box-shadow:0 2px 10px rgba(0,0,0,.05); margin-bottom:16px; }
  .linkcard .t { font-size:13px; font-weight:700; color:#555; margin-bottom:6px; }
  .linkrow { display:flex; gap:8px; }
  .linkrow input { flex:1; font-size:14px; padding:10px; border:2px solid #d0d5dd; border-radius:10px; background:#f7f9fc; }
  .copy { font-size:14px; font-weight:700; padding:10px 14px; border:none; border-radius:10px; background:#0a84ff; color:#fff; }
  h2 { font-size:16px; color:#444; margin:18px 4px 10px; display:flex; align-items:center; gap:8px; }
  .badge { background:#0a84ff; color:#fff; border-radius:20px; padding:2px 12px; font-size:14px; }
  .allbtn { margin-left:auto; font-size:15px; font-weight:800; padding:10px 18px; border:none; border-radius:12px; background:#1a7f37; color:#fff; }
  .allbtn:disabled { background:#b8c2cc; }
  .item { background:#fff; border-radius:16px; padding:16px; box-shadow:0 2px 10px rgba(0,0,0,.06); margin-bottom:12px; }
  .top { display:flex; align-items:center; gap:12px; }
  .chk { width:30px; height:30px; flex:0 0 auto; accent-color:#0a84ff; margin:0; }
  .fn { flex:1; color:#0a84ff; font-size:18px; font-weight:800; text-decoration:none; word-break:break-all; }
  .meta { color:#8a94a2; font-size:13px; margin-top:3px; margin-left:42px; }
  .row { display:flex; gap:10px; align-items:center; margin-top:14px; }
  .print { flex:1; font-size:20px; font-weight:800; padding:16px 8px; border:none; border-radius:14px; background:#0a84ff; color:#fff; white-space:nowrap; }
  .del { font-size:20px; padding:14px 16px; border:2px solid #e6bcbc; border-radius:12px; background:#fff; color:#c0392b; }
  .empty { text-align:center; color:#98a2b3; padding:40px 0; font-size:15px; line-height:1.7; }
  .toast { position:fixed; left:50%; bottom:26px; transform:translateX(-50%); background:#1c1c1e; color:#fff; padding:14px 24px; border-radius:30px; font-size:16px; font-weight:700; opacity:0; transition:.25s; z-index:9; }
  .toast.show { opacity:1; }
</style></head>
<body><div class="wrap">
  <h1>🖨️ 印刷ステーション</h1>
  <div class="hint">届いたPDFをタップ → 端末のプリントで、自分のプリンタに印刷</div>

  <div class="linkcard">
    <div class="t">👦 児童用リンク（これを配る）</div>
    <div class="linkrow">
      <input id="childurl" readonly>
      <button class="copy" onclick="copyLink()">📋 コピー</button>
    </div>
  </div>

  <h2>📥 印刷まち <span class="badge" id="cnt">0</span>
    <button class="allbtn" id="allbtn" onclick="printChecked()" disabled>🖨️ まとめて印刷</button></h2>
  <div id="list"><div class="empty">読み込み中…</div></div>
</div>
<div class="toast" id="toast"></div>
<script>
let currentQueue=[], lastSig=null;
const checkState={};
const MERGE=%MERGE%;
document.getElementById('childurl').value = location.origin + '/';
function copyLink(){ const el=document.getElementById('childurl'); el.select(); try{document.execCommand('copy');}catch(e){} if(navigator.clipboard){navigator.clipboard.writeText(el.value);} toast('📋 児童用リンクを コピーしました'); }
function chkChange(enc){ checkState[enc]=document.getElementById('chk_'+enc).checked; }
function toast(t){ const el=document.getElementById('toast'); el.textContent=t; el.classList.add('show'); setTimeout(()=>el.classList.remove('show'),2200); }
function esc(s){ return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function renderList(queue){
  const list=document.getElementById('list');
  if(queue.length===0){ list.innerHTML='<div class="empty">いまは 印刷まちは ありません<br>児童用リンクからPDFが届くと ここに並びます</div>'; return; }
  list.innerHTML='';
  queue.forEach(it=>{
    const enc=encodeURIComponent(it.id);
    const chk=(checkState[enc]===false)?'':'checked';
    const div=document.createElement('div'); div.className='item';
    div.innerHTML=
      '<div class="top"><input type="checkbox" class="chk" id="chk_'+enc+'" '+chk+' onchange="chkChange(\\''+enc+'\\')"><a class="fn" href="/file/'+enc+'" target="_blank">📄 '+esc(it.name)+'</a></div>'+
      '<div class="meta">'+esc(it.time)+' に届いた</div>'+
      '<div class="row">'+
        '<button class="print" onclick="printOne(\\''+enc+'\\')">🖨️ 印刷（ひらく）</button>'+
        '<button class="del" onclick="doDelete(\\''+enc+'\\')">🗑</button>'+
      '</div>';
    list.appendChild(div);
  });
}
async function refresh(){
  try{
    const r=await fetch('/api/state'); const d=await r.json();
    currentQueue=d.queue;
    document.getElementById('cnt').textContent=d.queue.length;
    document.getElementById('allbtn').disabled=(d.queue.length===0 || !MERGE);
    const sig=d.queue.map(it=>it.id).join('|');
    if(sig!==lastSig){ lastSig=sig; renderList(d.queue); }
  }catch(e){}
}
function printOne(enc){ window.open('/file/'+enc, '_blank'); }
function printChecked(){
  const ids=currentQueue.map(it=>it.id).filter(id=>{ const c=document.getElementById('chk_'+encodeURIComponent(id)); return c && c.checked; });
  if(ids.length===0){ toast('チェックされた PDFが ありません'); return; }
  window.open('/merge?ids='+ids.map(encodeURIComponent).join(','), '_blank');
}
async function doDelete(enc){
  if(!confirm('このPDFを 削除しますか？')) return;
  await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'id='+enc});
  refresh();
}
refresh(); setInterval(refresh,3000);
</script>
</body></html>"""


def teacher_page():
    return TEACHER_PAGE.replace("%MERGE%", "true" if HAS_PYPDF else "false")


# ────────────────────────────── HTTP ──────────────────────────────
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

    def _auth_gate(self, path):
        """守るべきパスなら認証を要求。OKならTrue、そうでなければ応答を返してFalse。"""
        if not needs_auth(path):
            return True
        if not TEACHER_PASSWORD:
            self._send(SETUP_HTML, code=503)   # 未設定 → データを見せない
            return False
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                dec = base64.b64decode(hdr[6:]).decode("utf-8", "ignore")
                if dec.partition(":")[2] == TEACHER_PASSWORD:
                    return True
            except Exception:
                pass
        body = "🔒 先生用パスワードが必要です".encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Teacher"')
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_GET(self):
        path = unquote(self.path.split("?")[0])
        if not self._auth_gate(path):
            return
        query = parse_qs(self.path.split("?")[1]) if "?" in self.path else {}
        if path == "/":
            self._send(child_page())
        elif path in ("/teacher", "/先生"):
            self._send(teacher_page())
        elif path == "/api/state":
            self._json({"queue": queue_list()})
        elif path.startswith("/file/"):
            fid = unquote(path[len("/file/"):])
            p = path_of(fid)
            if p:
                with open(p, "rb") as f:
                    name = items.get(fid, {}).get("name", "print") + ".pdf"
                    self._send(f.read(), "application/pdf",
                               extra={"Content-Disposition": "inline; filename*=UTF-8''" + quote(name)})
            else:
                self._send("見つかりません", code=404)
        elif path == "/merge":
            if not HAS_PYPDF:
                self._send("まとめて印刷は利用できません", code=503)
                return
            ids = [i for i in unquote(query.get("ids", [""])[0]).split(",") if i]
            data = merge_pdfs(ids)
            self._send(data, "application/pdf",
                       extra={"Content-Disposition": "inline; filename*=UTF-8''" + quote("まとめ印刷.pdf")})
        elif path in ("/health", "/healthz"):
            self._json({"ok": True})
        else:
            self._send("見つかりません", code=404)

    def do_POST(self):
        path = unquote(self.path.split("?")[0])
        if not self._auth_gate(path):
            return
        length = int(self.headers.get("Content-Length", 0))

        if path == "/submit":
            if length > MAX_MB * 1024 * 1024:
                self._send(child_done("⚠️", "ファイルが 大きすぎます", f"{MAX_MB}MB までにしてね"), code=413)
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
            stem = os.path.splitext(os.path.basename(fname))[0]
            label = (f"{nen}ねん{kumi}くみ{ban}ばん_" if (nen and kumi and ban) else "") + stem
            label = re.sub(r"[/\\:*?\"<>|]", "_", label)[:80]
            add_item(label, fdata)
            self._send(child_done("✅", "せんせいに おくりました！", "せんせいが かくにんして いんさつします。"))
            return

        if path == "/api/delete":
            body = self.rfile.read(length).decode("utf-8", "ignore")
            form = {k: v[0] for k, v in parse_qs(body).items()}
            remove_item(form.get("id", ""))
            self._json({"ok": True})
            return

        self._send("見つかりません", code=404)

    def log_message(self, *a):
        pass


class ReuseServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print("=" * 54)
    print("  🖨️  印刷ステーション（どこでも版）起動")
    print(f"  児童用: http://localhost:{PORT}/")
    print(f"  先生用: http://localhost:{PORT}/teacher")
    print(f"  まとめて印刷(PDF結合): {'有効' if HAS_PYPDF else '無効（pypdf未導入）'}")
    print("=" * 54)
    with ReuseServer(("0.0.0.0", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n停止しました。")
