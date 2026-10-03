/* 共有UI: 押下フィードバック（処理中…）・トースト・コピー可能なコード表示 */
(function () {
  var CSS = [
    '.ui-toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(20px);',
    'background:#1b1d21;color:#fff;padding:10px 16px;border-radius:10px;font-size:13px;',
    'opacity:0;transition:.18s;z-index:9999;pointer-events:none;max-width:90vw}',
    '.ui-toast.show{opacity:1;transform:translateX(-50%) translateY(0)}',
    '.ui-toast.err{background:#e5484d}',
    '.ui-modal{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;',
    'align-items:center;justify-content:center;z-index:10000}',
    '.ui-modal[hidden]{display:none}',
    '.ui-modal-box{background:#fff;border-radius:12px;padding:16px;width:min(440px,90vw);',
    'box-shadow:0 10px 30px rgba(0,0,0,.2)}',
    '.ui-modal-title{font-size:14px;font-weight:700;margin-bottom:8px}',
    '.ui-modal-box input{width:100%;box-sizing:border-box;font:inherit;padding:11px;',
    'border:1px solid #e3e5e8;border-radius:8px;margin-bottom:10px;background:#f7f8fa}',
    '.ui-modal-actions{display:flex;gap:8px;justify-content:flex-end}',
    '.ui-modal-actions button{padding:8px 14px;border-radius:8px;border:none;',
    'background:#06c755;color:#fff;cursor:pointer;font:inherit}',
    '.ui-modal-actions button.ghost{background:#fff;color:#1b1d21;border:1px solid #e3e5e8}',
    'button.busy,button:disabled{opacity:.55;cursor:progress}',
    'button.busy::after{content:"";display:inline-block;width:10px;height:10px;margin-left:6px;',
    'border:2px solid rgba(255,255,255,.6);border-top-color:transparent;border-radius:50%;',
    'animation:uispin .7s linear infinite;vertical-align:-1px}',
    '@keyframes uispin{to{transform:rotate(360deg)}}'
  ].join('');
  var st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st);

  function el(id) { return document.getElementById(id); }

  function ensure() {
    if (!el('ui-toast')) {
      var d = document.createElement('div');
      d.id = 'ui-toast'; d.className = 'ui-toast';
      document.body.appendChild(d);
    }
    if (!el('ui-modal')) {
      var m = document.createElement('div');
      m.id = 'ui-modal'; m.className = 'ui-modal'; m.hidden = true;
      m.innerHTML = '<div class="ui-modal-box">'
        + '<div class="ui-modal-title" id="ui-modal-title"></div>'
        + '<input id="ui-modal-code" readonly>'
        + '<div class="ui-modal-actions">'
        + '<button id="ui-copy">コピー</button>'
        + '<button class="ghost" id="ui-close">閉じる</button></div></div>';
      document.body.appendChild(m);
      el('ui-close').onclick = function () { m.hidden = true; };
      el('ui-copy').onclick = function () { copy(el('ui-modal-code').value); };
      m.addEventListener('click', function (e) { if (e.target === m) m.hidden = true; });
    }
  }

  function toast(msg, ok) {
    ensure();
    var t = el('ui-toast');
    t.textContent = msg;
    t.className = 'ui-toast show' + (ok === false ? ' err' : '');
    clearTimeout(t._h);
    t._h = setTimeout(function () { t.className = 'ui-toast'; }, 2800);
  }

  function _fallback(text) {
    ensure();
    var i = el('ui-modal-code');
    i.value = text; i.select();
    try { document.execCommand('copy'); toast('コピーしました'); }
    catch (e) { toast('コピーできない場合は手動で選択してください', false); }
  }

  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        function () { toast('コピーしました'); },
        function () { _fallback(text); });
    } else { _fallback(text); }
  }

  /* 招待IDなどを「選択できる入力欄」で表示（alertの代替） */
  function showCode(title, code) {
    ensure();
    el('ui-modal-title').textContent = title;
    var i = el('ui-modal-code');
    i.value = code;
    el('ui-modal').hidden = false;
    i.focus(); i.select();
  }

  /* 押下中は無効化＋「処理中…」＋スピナー */
  function busy(btn, on, label) {
    if (!btn) return;
    if (on) {
      if (btn.dataset._t === undefined) btn.dataset._t = btn.textContent;
      btn.disabled = true; btn.classList.add('busy');
      btn.textContent = label || '処理中…';
    } else {
      btn.disabled = false; btn.classList.remove('busy');
      if (btn.dataset._t !== undefined) { btn.textContent = btn.dataset._t; delete btn.dataset._t; }
    }
  }

  async function get(path) {
    var r = await fetch(path, { headers: { 'Accept': 'application/json' } });
    var j = await r.json().catch(function () { return {}; });
    if (!r.ok) throw new Error(j.detail || ('読み込みに失敗しました (' + r.status + ')'));
    return j;
  }

  async function post(path, body) {
    var r = await fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
    var j = await r.json().catch(function () { return {}; });
    if (!r.ok) throw new Error(j.detail || ('失敗しました (' + r.status + ')'));
    return j;
  }

  async function run(btn, fn) {
    busy(btn, true);
    try { return await fn(); }
    catch (e) { toast(e.message, false); }
    finally { busy(btn, false); }
  }

  window.UI = { toast: toast, busy: busy, showCode: showCode,
                get: get, post: post, copy: copy, run: run };
})();
