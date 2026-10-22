const app = getApp()

// 生命周期顺序展示各状态计数（含全部 17 个状态码）
const STATUS_ORDER = [1, 12, 2, 4, 3, 17, 7, 13, 6, 14, 5, 8, 9, 10, 11, 15, 16]

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
      fail: () => {
        this.setData({ loading: false })
        wx.showToast({ title: '加载失败', icon: 'none' })
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

  onTapAccount(e) {
    const company = e.currentTarget.dataset.company
    wx.navigateTo({
      url: '/pages/records/records?company=' + encodeURIComponent(company),
    })
  },
})
