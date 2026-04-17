"""Виджет чата для сайтов — JS файл + WebSocket."""
import os, json, logging
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from sqlalchemy.orm import Session

from server.routes.deps import get_db
from server.models import ChatBot
from server.chatbot_engine import handle_message

log = logging.getLogger("widget")
router = APIRouter(tags=["widget"])

APP_URL = os.getenv("APP_URL", "https://aiche.ru")


@router.get("/widget/{bot_id}.js")
def widget_js(bot_id: int, db: Session = Depends(get_db)):
    """Отдаёт JS-файл виджета. Встраивается на сайт клиента."""
    bot = db.query(ChatBot).filter_by(id=bot_id).first()
    if not bot or not bot.widget_enabled:
        return Response("// Widget disabled", media_type="application/javascript")

    ws_url = APP_URL.replace("https://", "wss://").replace("http://", "ws://")

    js = f"""
(function(){{
  if(window.__AICHE_LOADED__)return;window.__AICHE_LOADED__=true;
  var BOT_ID={bot_id};
  var WS_URL="{ws_url}/ws/widget/{bot_id}";
  var API_URL="{APP_URL}";

  // Стили
  var st=document.createElement("style");
  st.textContent=`
    #aiche-widget-btn{{position:fixed;bottom:20px;right:20px;width:56px;height:56px;border-radius:50%;background:linear-gradient(135deg,#ff8c42,#ffb347);cursor:pointer;z-index:99999;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 20px rgba(255,140,66,0.4);transition:transform .2s}}
    #aiche-widget-btn:hover{{transform:scale(1.08)}}
    #aiche-widget-btn svg{{width:26px;height:26px;fill:white}}
    #aiche-chat{{position:fixed;bottom:88px;right:20px;width:370px;max-height:520px;background:#1e1a14;border:1px solid rgba(74,63,47,0.4);border-radius:16px;z-index:99999;display:none;flex-direction:column;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,0.5);font-family:Inter,system-ui,sans-serif}}
    #aiche-chat.open{{display:flex}}
    #aiche-chat-hdr{{padding:14px 16px;background:linear-gradient(135deg,#ff8c42,#ffb347);display:flex;align-items:center;justify-content:space-between}}
    #aiche-chat-hdr span{{color:#1e1a14;font-weight:700;font-size:14px}}
    #aiche-chat-hdr button{{background:none;border:none;color:#1e1a14;font-size:20px;cursor:pointer;line-height:1}}
    #aiche-msgs{{flex:1;overflow-y:auto;padding:12px;min-height:300px;max-height:380px}}
    .aiche-m{{margin-bottom:10px;max-width:85%}}.aiche-m.u{{margin-left:auto}}.aiche-m.b{{margin-right:auto}}
    .aiche-m p{{padding:10px 14px;border-radius:12px;font-size:13px;line-height:1.5;color:#f0e6d8;word-wrap:break-word}}
    .aiche-m.u p{{background:#272018;border-bottom-right-radius:4px}}
    .aiche-m.b p{{background:rgba(255,140,66,0.08);border:1px solid rgba(255,140,66,0.15);border-bottom-left-radius:4px}}
    #aiche-inp-wrap{{padding:10px;border-top:1px solid rgba(74,63,47,0.3);display:flex;gap:8px}}
    #aiche-inp{{flex:1;padding:9px 13px;border-radius:10px;background:#272018;color:#f0e6d8;border:1px solid rgba(74,63,47,0.4);outline:none;font-size:13px;font-family:inherit}}
    #aiche-inp:focus{{border-color:#ff8c42}}
    #aiche-inp::placeholder{{color:#a89880}}
    #aiche-send{{width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,#ff8c42,#ffb347);border:none;cursor:pointer;display:flex;align-items:center;justify-content:center}}
    #aiche-send:disabled{{opacity:.4;cursor:not-allowed}}
    #aiche-send svg{{width:16px;height:16px;fill:#1e1a14}}
    .aiche-typing{{display:flex;gap:4px;padding:10px 14px}}.aiche-typing span{{width:6px;height:6px;border-radius:50%;background:#ff8c42;animation:aiche-blink 1.2s infinite}}.aiche-typing span:nth-child(2){{animation-delay:.2s}}.aiche-typing span:nth-child(3){{animation-delay:.4s}}
    @keyframes aiche-blink{{0%,100%{{opacity:.3}}50%{{opacity:1}}}}
  `;
  document.head.appendChild(st);

  // Кнопка
  var btn=document.createElement("div");btn.id="aiche-widget-btn";
  btn.innerHTML='<svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-1.99.9-1.99 2L2 22l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-2 12H6v-2h12v2zm0-3H6V9h12v2zm0-3H6V6h12v2z"/></svg>';
  document.body.appendChild(btn);

  // Чат окно
  var chat=document.createElement("div");chat.id="aiche-chat";
  chat.innerHTML='<div id="aiche-chat-hdr"><span>{esc_name}</span><button onclick="document.getElementById(\\'aiche-chat\\').classList.remove(\\'open\\')">\\u2715</button></div><div id="aiche-msgs"></div><div id="aiche-inp-wrap"><input id="aiche-inp" placeholder="Напишите сообщение..." autocomplete="off"/><button id="aiche-send" disabled><svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg></button></div>';
  document.body.appendChild(chat);

  btn.onclick=function(){{chat.classList.toggle("open");if(chat.classList.contains("open"))document.getElementById("aiche-inp").focus();}};

  var msgs=document.getElementById("aiche-msgs");
  var inp=document.getElementById("aiche-inp");
  var sendBtn=document.getElementById("aiche-send");
  var ws=null;var sid=localStorage.getItem("aiche_sid_"+BOT_ID)||("s_"+Date.now()+"_"+Math.random().toString(36).substr(2,6));
  localStorage.setItem("aiche_sid_"+BOT_ID,sid);

  function addMsg(text,role){{
    var d=document.createElement("div");d.className="aiche-m "+(role==="user"?"u":"b");
    var p=document.createElement("p");p.textContent=text;d.appendChild(p);msgs.appendChild(d);
    msgs.scrollTop=msgs.scrollHeight;
  }}
  function showTyping(){{var d=document.createElement("div");d.id="aiche-typing";d.className="aiche-m b";d.innerHTML='<div class="aiche-typing"><span></span><span></span><span></span></div>';msgs.appendChild(d);msgs.scrollTop=msgs.scrollHeight;}}
  function hideTyping(){{var t=document.getElementById("aiche-typing");if(t)t.remove();}}

  function connect(){{
    ws=new WebSocket(WS_URL+"?sid="+sid);
    ws.onmessage=function(e){{hideTyping();var d=JSON.parse(e.data);if(d.type==="answer")addMsg(d.text,"bot");if(d.type==="welcome")addMsg(d.text,"bot");}};
    ws.onclose=function(){{setTimeout(connect,3000);}};
    ws.onerror=function(){{}};
    ws.onopen=function(){{sendBtn.disabled=false;}};
  }}

  function send(){{
    var t=inp.value.trim();if(!t||!ws||ws.readyState!==1)return;
    addMsg(t,"user");inp.value="";sendBtn.disabled=true;showTyping();
    ws.send(JSON.stringify({{type:"message",text:t,sid:sid}}));
    setTimeout(function(){{sendBtn.disabled=(ws.readyState!==1);}},500);
  }}

  inp.addEventListener("keydown",function(e){{if(e.key==="Enter"&&!e.shiftKey){{e.preventDefault();send();}}}});
  sendBtn.onclick=send;

  connect();
  addMsg("Здравствуйте! Чем могу помочь?","bot");
}})();
""".replace("{esc_name}", bot.name.replace("'", "\\'").replace('"', '\\"'))

    return Response(js, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})


@router.websocket("/ws/widget/{bot_id}")
async def widget_ws(websocket: WebSocket, bot_id: int):
    """WebSocket для виджета чата на сайте."""
    db = next(get_db())
    try:
        bot = db.query(ChatBot).filter_by(id=bot_id).first()
        if not bot or not bot.widget_enabled or bot.status != "active":
            await websocket.close(code=4001, reason="Bot not available")
            return
    finally:
        db.close()

    await websocket.accept()

    sid = websocket.query_params.get("sid", "anon")
    chat_id = f"widget_{sid}"

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                continue

            if data.get("type") != "message":
                continue

            text = data.get("text", "").strip()
            if not text:
                continue

            # Перечитываем бота из БД (статус мог измениться)
            db = next(get_db())
            try:
                bot = db.query(ChatBot).filter_by(id=bot_id).first()
                if not bot or bot.status != "active":
                    await websocket.send_json({"type": "answer", "text": "Бот временно недоступен."})
                    continue
            finally:
                db.close()

            answer = await handle_message(bot, chat_id, text, "widget", sid)
            await websocket.send_json({
                "type": "answer",
                "text": answer or "Не удалось получить ответ.",
            })

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error(f"[Widget WS] error: {e}")
