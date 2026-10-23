const app = getApp()
const { describe } = require('../../utils/changes')

// 生命周期顺序展示各状态计数（含全部 17 个状态码）
const STATUS_ORDER = [1, 12, 2, 4, 3, 17, 7, 13, 6, 14, 5, 8, 9, 10, 11, 15, 16]

// 「有新变化」的书签，每个登录的人各一份：
// { since: 上次接口给的 next_since, seen: 上次返回的各条 key }
const CHANGES_SEEN_KEY = 'zjip_changes_seen:'
const POPUP_LINES = 3

Page({
  data: {
    userLabel: '',
    loading: true,
    totalRecords: 0,
    successCount: 0,
    pendingCorrection: 0,
    accounts: [],
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
  },

  // 打开小程序、从后台切回来、从别的页面返回都会走这里
  onShow() {
    if (app.globalData.auth) this.checkNewChanges()
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
    app.request('/api/status', {
      success: (res) => {
        const d = res.data
        const accounts = (d.accounts || []).map(acc => ({
          ...acc,
          pills: this._buildPills(acc.status_counts, d.status_labels),
        }))
        this.setData({
          loading: false,
          totalRecords: d.total_records,
          successCount: d.success_count,
          pendingCorrection: d.pending_correction,
          accounts,
        })
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

  // 把上次看过之后的新变化弹出来。第一次用只记下书签，不把 30 天的历史
  // 一股脑弹出来——历史在「最近变化」里看。
  checkNewChanges() {
    if (this._checkingChanges) return
    const auth = app.globalData.auth
    const storeKey = CHANGES_SEEN_KEY + ((auth.user && auth.user.username) || '')
    const stored = wx.getStorageSync(storeKey)
    const saved = stored && typeof stored.since === 'string' ? stored : null
    this._checkingChanges = true
    app.request('/api/changes' + (saved ? '?since=' + encodeURIComponent(saved.since) : ''), {
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
    wx.navigateTo({ url: '/pages/changes/changes' })
  },

  onTapAccount(e) {
    const company = e.currentTarget.dataset.company
    wx.navigateTo({
      url: '/pages/records/records?company=' + encodeURIComponent(company),
    })
  },
})
