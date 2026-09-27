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

/* 趋势折线的悬停读数：数据在 .chart 的 data-chart 里（服务端算好的绘图坐标 0–1000）。 */
(function () {
  function setup(el) {
    if (el._chart) return;
    el._chart = true;
    var data;
    try { data = JSON.parse(el.getAttribute('data-chart')); } catch (e) { return; }
    var plot = el.querySelector('.chart-plot');
    if (!plot || !data || !data.x || !data.x.length) return;
    var cursor = plot.querySelector('.chart-cursor'), tip = plot.querySelector('.chart-tip');
    var marks = data.s.map(function (s) {
      var m = document.createElement('span');
      m.className = 'chart-dot key-' + s.color;
      m.hidden = true;
      plot.appendChild(m);
      return m;
    });
    function show(clientX) {
      var r = plot.getBoundingClientRect();
      if (!r.width) return;
      var x = (clientX - r.left) / r.width * 1000, best = 0;
      for (var i = 1; i < data.x.length; i++) {
        if (Math.abs(data.x[i] - x) < Math.abs(data.x[best] - x)) best = i;
      }
      var left = data.x[best] / 10;
      cursor.style.left = left + '%';
      cursor.hidden = false;
      tip.textContent = '';
      var head = document.createElement('div');
      head.className = 'tip-date';
      head.textContent = data.d[best];
      tip.appendChild(head);
      data.s.forEach(function (s, k) {
        marks[k].style.left = left + '%';
        marks[k].style.top = (s.y[best] / 10) + '%';
        marks[k].hidden = false;
        var row = document.createElement('div');
        row.className = 'tip-row';
        var key = document.createElement('i');
        key.className = 'key key-' + s.color;
        var val = document.createElement('b');
        val.textContent = s.v[best];
        row.appendChild(key);
        row.appendChild(document.createTextNode(s.label));
        row.appendChild(val);
        tip.appendChild(row);
      });
      tip.hidden = false;
      // Keep the readout inside the plot: past the middle it sits left of the cursor.
      if (left > 50) { tip.style.left = 'auto'; tip.style.right = 'calc(' + (100 - left) + '% + 10px)'; }
      else { tip.style.right = 'auto'; tip.style.left = 'calc(' + left + '% + 10px)'; }
    }
    function hide() {
      cursor.hidden = true;
      tip.hidden = true;
      marks.forEach(function (m) { m.hidden = true; });
    }
    plot.addEventListener('pointermove', function (e) { show(e.clientX); });
    plot.addEventListener('pointerdown', function (e) { show(e.clientX); });
    plot.addEventListener('pointerleave', hide);
  }
  function init(root) {
    var scope = root && root.querySelectorAll ? root : document;
    if (scope.matches && scope.matches('.chart[data-chart]')) setup(scope);
    Array.prototype.forEach.call(scope.querySelectorAll('.chart[data-chart]'), setup);
  }
  if (window.htmx) htmx.onLoad(init);
  document.addEventListener('DOMContentLoaded', function () { init(document); });
})();
