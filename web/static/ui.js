/* 数智通监控 · 页面脚本：明暗切换、移动端菜单、HTMX 进度条与轻提示（无依赖） */
function toggleDark() {
  var dark = document.documentElement.classList.toggle('dark');
  try { localStorage.setItem('dark', dark ? '1' : '0'); } catch (e) { /* 隐私模式下忽略 */ }
}

function showToast(msg, kind) {
  var box = document.getElementById('toasts');
  if (!box) return;
  var el = document.createElement('div');
  el.className = 'toast' + (kind ? ' toast-' + kind : '');
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(function () {
    el.style.transition = 'opacity .3s';
    el.style.opacity = '0';
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 320);
  }, 3200);
}

(function () {
  // 顶部细进度条：只对用户触发、且超过 150ms 的请求显示；轮询（every）和 SSE 刷新不闪。
  var timer = null, safety = null;
  function isBackground(elt) {
    var t = (elt && elt.getAttribute && elt.getAttribute('hx-trigger')) || '';
    return /every |sse:/.test(t);
  }
  function stop() {
    clearTimeout(timer); timer = null;
    clearTimeout(safety); safety = null;
    document.body.classList.remove('busy');
  }
  document.addEventListener('htmx:beforeRequest', function (e) {
    if (isBackground(e.detail.elt)) return;
    if (!timer) timer = setTimeout(function () { document.body.classList.add('busy'); }, 150);
    clearTimeout(safety);
    safety = setTimeout(stop, 15000); // 兜底：发起请求的元素被换掉时收不到完成事件
  });
  ['htmx:afterRequest', 'htmx:afterSettle', 'htmx:afterSwap'].forEach(function (name) {
    document.addEventListener(name, function (e) {
      if (isBackground(e.detail.elt)) return;
      stop();
    });
  });

  document.addEventListener('htmx:responseError', function (e) {
    var s = e.detail.xhr ? e.detail.xhr.status : 0;
    if (s === 401) return; // 会话过期：服务端已通过 HX-Redirect 跳转登录页
    showToast('请求失败（HTTP ' + s + '）', 'danger');
  });
  document.addEventListener('htmx:sendError', function () { showToast('网络错误，请稍后重试', 'danger'); });
  document.addEventListener('accountSaved', function () { showToast('账号已保存', 'success'); });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var m = document.getElementById('modal-container');
    if (m && m.innerHTML) m.innerHTML = '';
    document.body.classList.remove('nav-open');
  });
})();
