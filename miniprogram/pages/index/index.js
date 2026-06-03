const app = getApp()

const STATUS_ORDER = [1, 2, 3, 5, 6, 7, 11]

Page({
  data: {
    loading: true,
    totalRecords: 0,
    successCount: 0,
    pendingCorrection: 0,
    accounts: [],
  },

  onLoad() {
    this.fetchStatus()
  },

  onPullDownRefresh() {
    this.fetchStatus(() => wx.stopPullDownRefresh())
  },

  fetchStatus(done) {
    wx.request({
      url: app.globalData.apiBase + '/api/status',
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
