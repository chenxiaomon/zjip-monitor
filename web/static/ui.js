/* 数智通监控 · 页面脚本：明暗切换、移动端菜单、HTMX 进度条与轻提示（无依赖） */
function toggleDark() {
  var dark = document.documentElement.classList.toggle('dark');
  try { localStorage.setItem('dark', dark ? '1' : '0'); } catch (e) { /* 隐私模式下忽略 */ }
}

function closeDrawer() {
  var d = document.getElementById('drawer');
  if (d) d.innerHTML = '';
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

// 复制：明文 HTTP 下没有 navigator.clipboard（只在 HTTPS / localhost 可用），退回 execCommand。
// data-copy-from 指向一个输入框的 id，复制它的值（口令只在那一个地方出现在页面上）。
function copyText(btn) {
  var src = document.getElementById(btn.getAttribute('data-copy-from'));
  var text = src ? (src.value || src.textContent || '') : '';
  var ok = function () { showToast('已复制', 'success'); };
  var fallback = function () {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    var done = false;
    try { done = document.execCommand('copy'); } catch (e) { done = false; }
    document.body.removeChild(ta);
    if (done) { ok(); } else { showToast('复制失败，请点「显示」后手动选中复制', 'danger'); }
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(ok, fallback);
  } else {
    fallback();
  }
}

function togglePassword(btn) {
  var el = document.getElementById(btn.getAttribute('data-target'));
  if (!el) return;
  el.type = el.type === 'password' ? 'text' : 'password';
  btn.textContent = el.type === 'password' ? '显示' : '隐藏';
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
    var xhr = e.detail.xhr, s = xhr ? xhr.status : 0;
    if (s === 401) return; // 会话过期：服务端已通过 HX-Redirect 跳转登录页
    // Short plain-text refusals (记录不存在或无权查看 / 无权限) are shown as they are.
    var type = (xhr && xhr.getResponseHeader('content-type')) || '';
    var text = xhr && type.indexOf('text/plain') === 0 ? (xhr.responseText || '').trim() : '';
    showToast(text && text.length <= 60 ? text : '请求失败（HTTP ' + s + '）', 'danger');
  });
  document.addEventListener('htmx:afterSwap', function (e) {
    var t = e.detail.target;
    if (t && t.id === 'drawer') {
      var f = t.querySelector('[data-autofocus]');
      if (f) f.focus();
    }
  });
  document.addEventListener('htmx:sendError', function () { showToast('网络错误，请稍后重试', 'danger'); });
  document.addEventListener('accountSaved', function () { showToast('账号已保存', 'success'); });
  // HX-Trigger: {"toast": {"msg": "…", "kind": "success|danger"}} — a route's own message.
  document.addEventListener('toast', function (e) {
    var d = e.detail || {};
    if (d.msg) showToast(d.msg, d.kind || '');
  });
  document.addEventListener('userSaved', function () { showToast('用户已保存', 'success'); });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var m = document.getElementById('modal-container');
    if (m && m.innerHTML) { m.innerHTML = ''; return; }  // the modal first, the drawer on the next Esc
    closeDrawer();
    document.body.classList.remove('nav-open');
  });
})();

/* 折叠区记住开合（<details data-keep-open id=…>）：仪表盘每次刷新都整块重画，
   开着的折叠区不能因此自己合上。只记在这个标签页里，新打开的页面照样默认收起。 */
(function () {
  var SEL = 'details[data-keep-open][id]';
  function key(el) { return 'open:' + el.id; }
  function restore(root) {
    var scope = root && root.querySelectorAll ? root : document;
    var list = Array.prototype.slice.call(scope.querySelectorAll(SEL));
    if (scope.matches && scope.matches(SEL)) list.push(scope);
    list.forEach(function (el) {
      try { if (sessionStorage.getItem(key(el)) === '1') el.open = true; } catch (e) { /* 存储不可用：保持默认收起 */ }
    });
  }
  // toggle 不冒泡，在捕获阶段统一接住
  document.addEventListener('toggle', function (e) {
    var el = e.target;
    if (!el || !el.matches || !el.matches(SEL)) return;
    try { sessionStorage.setItem(key(el), el.open ? '1' : '0'); } catch (err) { /* 忽略 */ }
  }, true);
  if (window.htmx) htmx.onLoad(restore);
  document.addEventListener('DOMContentLoaded', function () { restore(document); });
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

// 登记记录页的勾选：勾了哪几条，「导出 CSV / 导出台账 / 交付包」就只含哪几条，
// 不勾就按当前筛选。勾选按企业记在 sessionStorage（翻页、换筛选、排序都还在，
// 关掉标签页就清掉）；存不了时只记在这一页里。每条存「是否登记成功、是否在审」，
// 别的页上勾的也能算进交付包按钮的条数。
(function () {
  var memory = {};
  function box() { return document.getElementById('records-table-box'); }
  function load(key) {
    try {
      var raw = sessionStorage.getItem(key);
      if (raw) { var v = JSON.parse(raw); if (v && typeof v === 'object') return v; }
    } catch (e) { /* 存储不可用：退回本页内存 */ }
    return memory[key] || {};
  }
  function save(key, picked) {
    memory[key] = picked;
    try {
      if (Object.keys(picked).length) sessionStorage.setItem(key, JSON.stringify(picked));
      else sessionStorage.removeItem(key);
    } catch (e) { /* 忽略 */ }
  }
  function facts(cb) { return { r: cb.dataset.reg === '1' ? 1 : 0, o: cb.dataset.open === '1' ? 1 : 0 }; }
  function exportHref(base, regs) {
    if (!regs.length) return base;
    return base + (base.indexOf('?') < 0 ? '?' : '&') +
      regs.map(function (r) { return 'reg=' + encodeURIComponent(r); }).join('&');
  }
  function refresh() {
    var b = box();
    if (!b || !b.dataset.pickKey) return;
    var key = b.dataset.pickKey, picked = load(key);
    var boxes = Array.prototype.slice.call(b.querySelectorAll('input.rec-pick'));
    boxes.forEach(function (cb) {
      cb.checked = !!picked[cb.value];
      if (cb.checked) picked[cb.value] = facts(cb);  // 状态可能变了：以页面上的为准
    });
    save(key, picked);
    var regs = Object.keys(picked).sort(), n = regs.length;

    var page = document.getElementById('pick-page');
    if (page) {
      var on = boxes.filter(function (cb) { return cb.checked; }).length;
      page.checked = boxes.length > 0 && on === boxes.length;
      page.indeterminate = on > 0 && on < boxes.length;
    }
    var note = document.getElementById('pick-note');
    if (note) {
      note.hidden = !n;
      var count = note.querySelector('[data-pick-count]');
      if (count) count.textContent = n;
    }
    Array.prototype.forEach.call(b.querySelectorAll('a[data-export]'), function (a) {
      if (!a.dataset.base) a.dataset.base = a.getAttribute('href');
      a.setAttribute('href', exportHref(a.dataset.base, regs));
    });
    Array.prototype.forEach.call(b.querySelectorAll('[data-pick-label]'), function (el) {
      el.textContent = el.dataset.pickLabel + (n ? '（勾选的 ' + n + ' 条）' : '');
    });
    var ho = document.getElementById('handover-link');
    if (ho) {
      var label = ho.querySelector('[data-handover-label]');
      var reg, open;
      if (n) {
        reg = 0; open = 0;
        regs.forEach(function (r) { reg += picked[r].r || 0; open += picked[r].o || 0; });
      } else {
        reg = +ho.dataset.registered || 0; open = +ho.dataset.open || 0;
      }
      ho.hidden = !reg;
      if (label) {
        label.textContent = '交付包' + (n
          ? '（勾选的 ' + reg + ' 条登记成功' + (open ? '，' + open + ' 条在审不列入' : '') + '）'
          : (open ? '（尚有 ' + open + ' 条在审）' : ''));
      }
    }
  }
  document.addEventListener('change', function (e) {
    var t = e.target, b = box();
    if (!b || !b.dataset.pickKey || !t || !t.matches) return;
    if (t.matches('input.rec-pick')) {
      var picked = load(b.dataset.pickKey);
      if (t.checked) picked[t.value] = facts(t); else delete picked[t.value];
      save(b.dataset.pickKey, picked);
      refresh();
    } else if (t.id === 'pick-page') {
      var all = load(b.dataset.pickKey);
      Array.prototype.forEach.call(b.querySelectorAll('input.rec-pick'), function (cb) {
        if (t.checked) all[cb.value] = facts(cb); else delete all[cb.value];
      });
      save(b.dataset.pickKey, all);
      refresh();
    }
  });
  document.addEventListener('click', function (e) {
    var clear = e.target && e.target.closest && e.target.closest('[data-pick-clear]');
    var b = box();
    if (!clear || !b || !b.dataset.pickKey) return;
    e.preventDefault();
    save(b.dataset.pickKey, {});
    refresh();
  });
  // 翻页、输入关键字只换掉表格这一块：换完重新对上勾选。afterSwap 让勾选框立刻对上；
  // 带 id 的元素（勾选提示、交付包按钮）在 settle 时会被 htmx 换回服务器给的属性
  // （hidden、href），所以 afterSettle 再来一次。
  document.addEventListener('htmx:afterSwap', refresh);
  document.addEventListener('htmx:afterSettle', refresh);
  document.addEventListener('DOMContentLoaded', refresh);
})();

/* 仪表盘「排序与显示」：[data-move=up|down|top] 在同一个 [data-move-list] 里移动最近的
   [data-move-item]（企业行或整个来源）。表单按行在页面上的先后提交，不另存顺序。
   有改动时表单加 is-dirty，提示「保存后生效」；[data-confirm] 的提交按钮先确认。 */
(function () {
  function dirty(el) {
    var form = el.closest('[data-dirty-form]');
    if (form) form.classList.add('is-dirty');
  }
  document.addEventListener('click', function (e) {
    var ask = e.target.closest('button[data-confirm]');
    if (ask && !window.confirm(ask.getAttribute('data-confirm'))) { e.preventDefault(); return; }
    var btn = e.target.closest('[data-move]');
    if (!btn) return;
    var item = btn.closest('[data-move-item]');
    var list = item && item.parentElement;
    if (!list || !list.hasAttribute('data-move-list')) return;
    e.preventDefault();
    var how = btn.getAttribute('data-move');
    var before = list.firstElementChild;
    if (how === 'up') before = item.previousElementSibling;
    else if (how === 'down') before = item.nextElementSibling && item.nextElementSibling.nextElementSibling;
    if (how === 'down' && !item.nextElementSibling) return;
    if (how !== 'down' && (!before || before === item)) return;
    list.insertBefore(item, before);  // before = null → append at the end
    // A brief highlight on what moved, fading out (the .6s transition in ui.css).
    clearTimeout(item._movedTimer);
    item.classList.add('just-moved');
    item._movedTimer = setTimeout(function () { item.classList.remove('just-moved'); }, 350);
    btn.focus();
    dirty(item);
  });
  document.addEventListener('change', function (e) {
    if (e.target.matches && e.target.matches('[data-dirty-form] input')) dirty(e.target);
  });
})();
