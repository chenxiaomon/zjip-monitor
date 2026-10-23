const app = getApp()
const { describe } = require('../../utils/changes')

// 生命周期顺序展示各状态计数（含全部 17 个状态码）
const STATUS_ORDER = [1, 12, 2, 4, 3, 17, 7, 13, 6, 14, 5, 8, 9, 10, 11, 15, 16]

// 「有新变化」的书签，每个登录的人各一份：
// { since: 上次接口给的 next_since, seen: 上次返回的各条 key }
const CHANGES_SEEN_KEY = 'zjip_changes_seen:'
const POPUP_LINES = 3
// 标签页回到前台时，数据超过这么久就重新拉一次
const STALE_MS = 5 * 60 * 1000

Page({
  data: {
    userLabel: '',
    loading: true,
    totalRecords: 0,
    successCount: 0,
    pendingCorrection: 0,
    accounts: [],
    unscanned: [], // 还没巡检过的企业：没有快照，不能和「0 条记录」混为一谈
    todo: null, // { corrections, urgent, renewals }
    scope: null, // app.scopeView()
    years: [],
    yearOptions: ['全部年份'],
    yearIndex: 0, // 0 = 全部年份，其余对应 years[yearIndex - 1]
    canAdd: false, // 管理员和经办人能新增企业
  },

  onLoad() {
    const auth = app.globalData.auth
    if (!auth || !auth.token) {
      app.toLogin()
      return
    }
    const u = auth.user || {}
    this.setData({ userLabel: u.display_name || u.username || '' })
    this.fetchStatus()
    app.refreshMe(() => this.setData({ canAdd: app.globalData.can.add }))
  },

  // 打开小程序、从后台切回来、从别的页面返回都会走这里
  onShow() {
    if (!app.globalData.auth) return
    // 在别的标签页切换了「我负责的 / 全部」、新增了企业、巡检完了，或者数据放久了
    if (this._fetchedAt !== undefined &&
        (this._viewVersion !== app.globalData.viewVersion || Date.now() - this._fetchedAt > STALE_MS)) {
      this.fetchStatus()
    }
    this.checkNewChanges()
  },

  onLogout() {
    wx.showModal({
      title: '退出登录',
      content: '退出后需要重新输入口令',
      success: (r) => { if (r.confirm) app.logout() },
    })
  },

  onPullDownRefresh() {
    this.fetchStatus(() => wx.stopPullDownRefresh())
  },

  fetchStatus(done) {
    this._viewVersion = app.globalData.viewVersion
    this._fetchedAt = Date.now()
    const year = this.data.yearIndex > 0 ? this.data.years[this.data.yearIndex - 1] : null
    app.get('/api/status', { year: year }, {
      success: (res) => {
        const d = res.data
        app.noteScope(d.scope)
        const accounts = (d.accounts || []).map(acc => ({
          ...acc,
          pills: this._buildPills(acc.status_counts, d.status_labels),
        }))
        const years = d.years || []
        if (year && years.indexOf(year) < 0) {
          // 选的年份在新范围里没有记录（比如切到「我负责的」）：回到全部年份重新拉，
          // 免得选择器写着「全部年份」、数字却是按那一年筛的
          this.setData({ years, yearIndex: 0 })
          this.fetchStatus(done)
          return
        }
        this.setData({
          loading: false,
          totalRecords: d.total_records,
          successCount: d.success_count,
          pendingCorrection: d.pending_correction,
          accounts,
          unscanned: d.unscanned || [],
          todo: d.todo || null,
          scope: app.scopeView(),
          years,
          yearOptions: ['全部年份'].concat(years.map(y => y + ' 年')),
          yearIndex: year ? years.indexOf(year) + 1 : 0,
        })
        if (d.todo) app.setTodoBadge(d.todo.urgent)
      },
      fail: (err) => {
        this.setData({ loading: false })
        wx.showToast({ title: (err && err.errMsg) || '加载失败', icon: 'none' })
      },
      complete: () => done && done(),
    })
  },

  _buildPills(statusCounts, labels) {
    return STATUS_ORDER
      .filter(code => (statusCounts[String(code)] || 0) > 0)
      .map(code => ({
        code,
        label: labels[String(code)] || String(code),
        count: statusCounts[String(code)],
        cls: 'pill pill-' + code,
      }))
  },

  // 先把高亮挪过去，不等数据回来——否则回来前再点一次会被当成没切换
  onScopeChange() {
    this.setData({ scope: app.scopeView() })
    this.fetchStatus()
  },

  onYear(e) {
    this.setData({ yearIndex: Number(e.detail.value) })
    this.fetchStatus()
  },

  onSearch(e) {
    const q = (e.detail.value || '').trim()
    if (!q) return
    wx.navigateTo({ url: '/pages/records/records?q=' + encodeURIComponent(q) })
  },

  // 把上次看过之后的新变化弹出来。第一次用只记下书签，不把 30 天的历史
  // 一股脑弹出来——历史在「变更」里看。
  checkNewChanges() {
    if (this._checkingChanges) return
    const auth = app.globalData.auth
    const storeKey = CHANGES_SEEN_KEY + ((auth.user && auth.user.username) || '')
    const stored = wx.getStorageSync(storeKey)
    const saved = stored && typeof stored.since === 'string' ? stored : null
    this._checkingChanges = true
    app.get('/api/changes', saved ? { since: saved.since } : null, {
      success: (res) => {
        const rows = res.data.rows || []
        // 接口从书签前一小时给起（补上晚写进日志的变化），上次给过的按 key 去掉
        const seen = (saved && saved.seen) || []
        const fresh = saved ? rows.filter(r => seen.indexOf(r.key) < 0) : []
        wx.setStorageSync(storeKey, {
          since: res.data.next_since,
          seen: rows.map(r => r.key).filter((k, i, all) => all.indexOf(k) === i),
        })
        if (fresh.length) this._showNewChanges(fresh, res.data.total > rows.length)
      },
      fail: () => {}, // 提醒是顺带的，失败不打扰；书签没动，下次打开再补
      complete: () => { this._checkingChanges = false },
    })
  },

  _showNewChanges(fresh, truncated) {
    const lines = fresh.slice(0, POPUP_LINES).map(describe)
    if (fresh.length > POPUP_LINES) lines.push('……')
    wx.showModal({
      title: '有 ' + fresh.length + (truncated ? '+' : '') + ' 条新变化',
      content: lines.join('\n'),
      confirmText: '查看',
      cancelText: '知道了',
      success: (r) => { if (r.confirm) this.onTapChanges() },
    })
  },

  onTapChanges() {
    wx.switchTab({ url: '/pages/changes/changes' })
  },

  onTapTodo() {
    wx.switchTab({ url: '/pages/todo/todo' })
  },

  onAdd() {
    wx.navigateTo({ url: '/pages/account-new/account-new' })
  },

  // 看这家企业的全部记录（不受「我负责的」影响），年份跟着概览走
  onTapAccount(e) {
    const company = e.currentTarget.dataset.company
    const year = this.data.yearIndex > 0 ? this.data.years[this.data.yearIndex - 1] : ''
    wx.navigateTo({
      url: '/pages/records/records?company=' + encodeURIComponent(company) + (year ? '&year=' + year : ''),
    })
  },
})
