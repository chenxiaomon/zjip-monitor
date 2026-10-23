const app = getApp()

Page({
  data: {
    loading: true,
    rows: [],
    total: 0,
  },

  onLoad() {
    this.fetchChanges()
  },

  onPullDownRefresh() {
    this.fetchChanges(() => wx.stopPullDownRefresh())
  },

  // 最近 30 天，最新的在前；同一次巡检的几条共用 key，列表用序号区分
  fetchChanges(done) {
    app.request('/api/changes', {
      success: (res) => {
        const rows = (res.data.rows || []).map((r, i) => ({
          ...r,
          id: i + '|' + r.key,
          oldCls: 'pill pill-' + (r.old_status_code || 'none'),
          newCls: 'pill pill-' + (r.new_status_code || 'none'),
        }))
        this.setData({
          loading: false,
          rows,
          total: res.data.total || 0,
        })
      },
      fail: (err) => {
        this.setData({ loading: false })
        wx.showToast({ title: (err && err.errMsg) || '加载失败', icon: 'none' })
      },
      complete: () => done && done(),
    })
  },

  // 和登记记录页一样：小程序打不开外部网页，点卡片复制官网详情页链接
  onTapRow(e) {
    const url = e.currentTarget.dataset.url
    if (!url) return
    wx.setClipboardData({
      data: url,
      success: () => wx.showToast({ title: '详情链接已复制', icon: 'success' }),
    })
  },
})
