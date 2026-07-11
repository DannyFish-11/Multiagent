"""内置浏览器聊天界面(M21 产品化):打开 http://localhost:8002 就能聊,无需命令行。

单页、自包含(内联 CSS/JS,零外部依赖),由 L3 API 直接托管。给完全不懂命令行的人用:
`make run-api` 后浏览器打开首页即对话,并能看到每轮命中的记忆。
"""

from __future__ import annotations

CHAT_HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>记忆助手</title>
<style>
  :root { --bg:#f7f7f8; --panel:#fff; --ink:#1f2328; --muted:#6b7280;
          --user:#2563eb; --userink:#fff; --bot:#eceef1; --line:#e5e7eb; --accent:#10a37f; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0f1115; --panel:#171a21; --ink:#e6e6e6; --muted:#9aa2ad;
            --user:#2563eb; --userink:#fff; --bot:#232833; --line:#2a2f3a; --accent:#19c39c; }
  }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft Yahei",sans-serif;
         background:var(--bg); color:var(--ink); height:100vh; display:flex; flex-direction:column; }
  header { padding:12px 16px; background:var(--panel); border-bottom:1px solid var(--line);
           display:flex; align-items:center; gap:10px; }
  header h1 { font-size:16px; margin:0; font-weight:600; }
  #dot { width:9px; height:9px; border-radius:50%; background:#9ca3af; }
  #dot.ok { background:var(--accent); } #dot.bad { background:#ef4444; }
  header .sp { flex:1; }
  button { font:inherit; cursor:pointer; border:1px solid var(--line); background:var(--panel);
           color:var(--ink); border-radius:8px; padding:6px 12px; }
  button:hover { border-color:var(--accent); }
  #log { flex:1; overflow-y:auto; padding:18px 16px; display:flex; flex-direction:column; gap:12px;
         max-width:820px; width:100%; margin:0 auto; }
  .row { display:flex; }
  .row.user { justify-content:flex-end; }
  .bubble { max-width:78%; padding:9px 13px; border-radius:14px; white-space:pre-wrap; word-break:break-word; }
  .user .bubble { background:var(--user); color:var(--userink); border-bottom-right-radius:4px; }
  .bot .bubble { background:var(--bot); border-bottom-left-radius:4px; }
  .mem { font-size:12px; color:var(--muted); margin-top:5px; cursor:pointer; }
  .mem ul { margin:4px 0 0; padding-left:16px; }
  .sys { text-align:center; color:var(--muted); font-size:13px; }
  footer { border-top:1px solid var(--line); background:var(--panel); padding:10px 16px; }
  .inp { max-width:820px; margin:0 auto; display:flex; gap:8px; align-items:flex-end; }
  textarea { flex:1; resize:none; font:inherit; color:var(--ink); background:var(--bg);
             border:1px solid var(--line); border-radius:10px; padding:9px 12px; max-height:140px; }
  textarea:focus { outline:none; border-color:var(--accent); }
  .send { background:var(--accent); color:#fff; border:none; padding:9px 18px; }
  .hint { text-align:center; color:var(--muted); font-size:12px; margin-top:6px; }
</style>
</head>
<body>
<header>
  <span id="dot" title="依赖健康"></span>
  <h1>🧠 记忆助手</h1>
  <span class="sp"></span>
  <button id="new">新会话</button>
</header>
<div id="log">
  <div class="sys">跟我说点什么,我会记住它,下次自动想起来。</div>
</div>
<footer>
  <div class="inp">
    <textarea id="box" rows="1" placeholder="输入消息,Enter 发送(Shift+Enter 换行)"></textarea>
    <button class="send" id="send">发送</button>
  </div>
  <div class="hint" id="hint"></div>
</footer>
<script>
  const log = document.getElementById('log'), box = document.getElementById('box'),
        dot = document.getElementById('dot'), hint = document.getElementById('hint');
  let session = 'web-' + Math.random().toString(36).slice(2, 8);
  let busy = false;

  function esc(s){ const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
  function scroll(){ log.scrollTop = log.scrollHeight; }

  function add(role, text){
    const row = document.createElement('div'); row.className = 'row ' + role;
    const b = document.createElement('div'); b.className = 'bubble'; b.textContent = text;
    row.appendChild(b); log.appendChild(row); scroll(); return b;
  }
  function sys(text){
    const d = document.createElement('div'); d.className='sys'; d.textContent=text;
    log.appendChild(d); scroll();
  }
  function memNote(bubble, mems){
    if(!mems || !mems.length) return;
    const m = document.createElement('div'); m.className='mem';
    m.textContent = '🔎 用到 ' + mems.length + ' 条记忆(点击展开)';
    const ul = document.createElement('ul'); ul.style.display='none';
    mems.forEach(x => { const li=document.createElement('li'); li.textContent = x.content || ''; ul.appendChild(li); });
    m.onclick = () => { ul.style.display = ul.style.display==='none' ? 'block':'none'; };
    bubble.parentNode.appendChild(m); bubble.parentNode.appendChild(ul); scroll();
  }

  async function health(){
    try { const r = await fetch('./healthz'); const j = await r.json();
      dot.className = j.status==='ok' ? 'ok' : 'bad';
      dot.title = '依赖:' + JSON.stringify(j.layers);
    } catch(e){ dot.className='bad'; dot.title='服务不可达'; }
  }

  async function send(){
    const msg = box.value.trim();
    if(!msg || busy) return;
    busy = true; box.value=''; box.style.height='auto';
    add('user', msg);
    const thinking = add('bot', '…');
    try {
      // M26 流式:逐 token 增量渲染(SSE);tools/多 agent 档整段一次性也走同一通道
      const r = await fetch('./chat/stream', { method:'POST', headers:{'content-type':'application/json'},
        body: JSON.stringify({ message: msg, session_id: session }) });
      if(!r.ok || !r.body){ thinking.textContent = '⚠️ 出错 HTTP ' + r.status; busy=false; return; }
      const reader = r.body.getReader(), dec = new TextDecoder();
      let buf='', text='', mems=null, started=false, idx;
      while(true){
        const {done, value} = await reader.read();
        if(done) break;
        buf += dec.decode(value, {stream:true});
        while((idx = buf.indexOf('\n\n')) >= 0){
          const line = buf.slice(0, idx).trim(); buf = buf.slice(idx+2);
          if(!line.startsWith('data:')) continue;
          let ev; try { ev = JSON.parse(line.slice(5).trim()); } catch(_){ continue; }
          if(ev.type==='token'){ if(!started){ text=''; started=true; } text += ev.text; thinking.textContent = text; }
          else if(ev.type==='meta'){ mems = ev.memories_used; }
          else if(ev.type==='error'){ thinking.textContent = '⚠️ ' + ev.message; }
        }
      }
      if(!started) thinking.textContent = thinking.textContent==='…' ? '(空回复)' : thinking.textContent;
      memNote(thinking, mems);
    } catch(e){ thinking.textContent = '⚠️ 请求失败:' + e; }
    busy = false; box.focus();
  }

  document.getElementById('send').onclick = send;
  document.getElementById('new').onclick = () => {
    session = 'web-' + Math.random().toString(36).slice(2,8);
    log.innerHTML=''; sys('已开新会话(' + session + ')。');
  };
  box.addEventListener('keydown', e => {
    if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); send(); }
  });
  box.addEventListener('input', () => { box.style.height='auto'; box.style.height = box.scrollHeight+'px'; });
  health();
</script>
</body>
</html>
"""
