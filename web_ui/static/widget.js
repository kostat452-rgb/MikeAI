/**
 * Mike AI Chat Widget
 *
 * Usage: add to client's website:
 *   <script src="https://YOUR_DOMAIN/static/widget.js"
 *           data-api="https://YOUR_DOMAIN"
 *           data-key="API_KEY_HERE"
 *           data-color="#2563eb"
 *           data-title="Онлайн-помощник"
 *           data-greeting="Привет! Чем могу помочь?">
 *   </script>
 */
(function() {
  var script = document.currentScript;
  var API_URL = script.getAttribute('data-api') || '';
  var API_KEY = script.getAttribute('data-key') || '';
  var COLOR = script.getAttribute('data-color') || '#2563eb';
  var TITLE = script.getAttribute('data-title') || '\u041e\u043d\u043b\u0430\u0439\u043d-\u043f\u043e\u043c\u043e\u0449\u043d\u0438\u043a';
  var GREETING = script.getAttribute('data-greeting') || '\u041f\u0440\u0438\u0432\u0435\u0442! \u0427\u0435\u043c \u043c\u043e\u0433\u0443 \u043f\u043e\u043c\u043e\u0447\u044c?';

  var isOpen = false;

  // Styles
  var css = document.createElement('style');
  css.textContent = '\
    #mike-widget-btn{position:fixed;bottom:24px;right:24px;width:60px;height:60px;border-radius:50%;background:' + COLOR + ';color:#fff;border:none;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.2);z-index:99999;display:flex;align-items:center;justify-content:center;transition:transform .2s}\
    #mike-widget-btn:hover{transform:scale(1.1)}\
    #mike-widget-btn svg{width:28px;height:28px}\
    #mike-widget-box{position:fixed;bottom:96px;right:24px;width:380px;max-width:calc(100vw - 32px);height:520px;max-height:calc(100vh - 120px);background:#fff;border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,.18);z-index:99999;display:none;flex-direction:column;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}\
    #mike-widget-box.open{display:flex}\
    #mike-widget-header{background:' + COLOR + ';color:#fff;padding:16px 20px;font-size:16px;font-weight:600;display:flex;align-items:center;justify-content:space-between}\
    #mike-widget-close{background:none;border:none;color:#fff;cursor:pointer;font-size:20px;padding:0 4px}\
    #mike-widget-messages{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}\
    .mike-msg{max-width:85%;padding:10px 14px;border-radius:14px;font-size:14px;line-height:1.5;word-wrap:break-word}\
    .mike-msg.bot{background:#f0f2f5;color:#1a1a1a;align-self:flex-start;border-bottom-left-radius:4px}\
    .mike-msg.user{background:' + COLOR + ';color:#fff;align-self:flex-end;border-bottom-right-radius:4px}\
    .mike-msg.typing{background:#f0f2f5;align-self:flex-start;border-bottom-left-radius:4px;color:#999}\
    #mike-widget-input-wrap{display:flex;padding:12px 16px;border-top:1px solid #e5e7eb;gap:8px}\
    #mike-widget-input{flex:1;border:1px solid #d1d5db;border-radius:24px;padding:10px 16px;font-size:14px;outline:none;resize:none}\
    #mike-widget-input:focus{border-color:' + COLOR + '}\
    #mike-widget-send{background:' + COLOR + ';color:#fff;border:none;border-radius:50%;width:40px;height:40px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0}\
    #mike-widget-send:disabled{opacity:.5;cursor:default}\
    #mike-widget-send svg{width:18px;height:18px}\
    @media(max-width:480px){#mike-widget-box{bottom:0;right:0;width:100vw;height:100vh;max-height:100vh;border-radius:0}#mike-widget-btn{bottom:16px;right:16px}}\
  ';
  document.head.appendChild(css);

  // Button
  var btn = document.createElement('button');
  btn.id = 'mike-widget-btn';
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>';
  btn.onclick = function() {
    isOpen = !isOpen;
    box.classList.toggle('open', isOpen);
    if (isOpen && msgs.children.length === 0) {
      addMsg(GREETING, 'bot');
    }
  };
  document.body.appendChild(btn);

  // Chat box
  var box = document.createElement('div');
  box.id = 'mike-widget-box';
  box.innerHTML = '\
    <div id="mike-widget-header">\
      <span>' + TITLE + '</span>\
      <button id="mike-widget-close">\u00d7</button>\
    </div>\
    <div id="mike-widget-messages"></div>\
    <div id="mike-widget-input-wrap">\
      <input id="mike-widget-input" placeholder="\u0412\u0430\u0448 \u0432\u043e\u043f\u0440\u043e\u0441..." />\
      <button id="mike-widget-send" disabled>\
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>\
      </button>\
    </div>\
  ';
  document.body.appendChild(box);

  var msgs = document.getElementById('mike-widget-messages');
  var input = document.getElementById('mike-widget-input');
  var sendBtn = document.getElementById('mike-widget-send');

  document.getElementById('mike-widget-close').onclick = function() {
    isOpen = false;
    box.classList.remove('open');
  };

  input.oninput = function() {
    sendBtn.disabled = !input.value.trim();
  };

  input.onkeydown = function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  sendBtn.onclick = send;

  function addMsg(text, who) {
    var div = document.createElement('div');
    div.className = 'mike-msg ' + who;
    div.textContent = text;
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
    return div;
  }

  function send() {
    var q = input.value.trim();
    if (!q) return;
    input.value = '';
    sendBtn.disabled = true;
    addMsg(q, 'user');

    var typing = addMsg('\u041f\u0435\u0447\u0430\u0442\u0430\u0435\u0442...', 'typing');

    fetch(API_URL + '/widget/ask', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question: q, api_key: API_KEY})
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      msgs.removeChild(typing);
      addMsg(data.answer || '\u041e\u0448\u0438\u0431\u043a\u0430. \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u043f\u043e\u0437\u0436\u0435.', 'bot');
    })
    .catch(function() {
      msgs.removeChild(typing);
      addMsg('\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u043e\u043b\u0443\u0447\u0438\u0442\u044c \u043e\u0442\u0432\u0435\u0442. \u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435.', 'bot');
    });
  }
})();
