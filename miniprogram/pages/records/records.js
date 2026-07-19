const app = getApp()

const STATUS_COLORS = {
  1:  { bg: '#E6F1FB', color: '#0C447C' },
  2:  { bg: '#FAEEDA', color: '#854F0B' },
  3:  { bg: '#EEEDFE', color: '#3C3489' },
  5:  { bg: '#F1EFE8', color: '#444441' },
  6:  { bg: '#FCEBEB', color: '#791F1F' },
  7:  { bg: '#EAF3DE', color: '#27500A' },
  11: { bg: '#F1EFE8', color: '#444441' },
}

Page({
  data: {
    company: '',
    loading: true,
    records: [],
    total: 0,
  },

  onLoad(options) {
    const company = decodeURIComponent(options.company || '')
    this.setData({ company })
    wx.setNavigationBarTitle({ title: company || '登记记录' })
    this.fetchRecords(company)
  },

  onPullDownRefresh() {
    this.fetchRecords(this.data.company, () => wx.stopPullDownRefresh())
  },

  fetchRecords(company, done) {
    const url = app.globalData.apiBase + '/api/records'
      + (company ? '?company=' + encodeURIComponent(company) : '')
    wx.request({
      url,
      header: { 'X-API-Key': app.globalData.apiKey },
      success: (res) => {
        const records = (res.data.records || []).map(r => ({
          ...r,
          pillStyle: this._pillStyle(r.status_code),
        }))
        this.setData({
          loading: false,
          records,
          total: res.data.total || 0,
        })
      },
      fail: () => {
        this.setData({ loading: false })
        wx.showToast({ title: '加载失败', icon: 'none' })
      },
      complete: () => done && done(),
    })
  },

  _pillStyle(code) {
    const c = STATUS_COLORS[code] || { bg: '#f0f0f0', color: '#666' }
    return `background:${c.bg};color:${c.color}`
  },

  // 小程序无法直接打开外部网页：点卡片复制官网详情页链接，用户粘贴到浏览器打开
  onTapRecord(e) {
    const url = e.currentTarget.dataset.url
    if (!url) return
    wx.setClipboardData({
      data: url,
      success: () => wx.showToast({ title: '详情链接已复制', icon: 'success' }),
    })
  },
})
