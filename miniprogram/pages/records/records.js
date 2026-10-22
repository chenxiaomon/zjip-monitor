const app = getApp()

const STATUS_COLORS = {
  1:  { bg: '#E6F1FB', color: '#0C447C' },  // 待审核
  2:  { bg: '#FAEEDA', color: '#854F0B' },  // 待补正
  3:  { bg: '#EEEDFE', color: '#3C3489' },  // 公示中
  4:  { bg: '#FAEEDA', color: '#854F0B' },  // 异议
  5:  { bg: '#F1EFE8', color: '#444441' },  // 主动撤回
  6:  { bg: '#FCEBEB', color: '#791F1F' },  // 不予登记
  7:  { bg: '#EAF3DE', color: '#27500A' },  // 登记成功
  8:  { bg: '#F1EFE8', color: '#444441' },  // 终止登记
  9:  { bg: '#F1EFE8', color: '#444441' },  // 登记撤销
  10: { bg: '#F1EFE8', color: '#444441' },  // 未续展失效
  11: { bg: '#F1EFE8', color: '#444441' },  // 视为撤回
  12: { bg: '#E6F1FB', color: '#0C447C' },  // 待审核
  13: { bg: '#EAF3DE', color: '#27500A' },  // 续展登记
  14: { bg: '#FCEBEB', color: '#791F1F' },  // 批量登记失败
  15: { bg: '#F1EFE8', color: '#444441' },  // 撤回审查
  16: { bg: '#F1EFE8', color: '#444441' },  // 撤回补正
  17: { bg: '#EEEDFE', color: '#3C3489' },  // 待发证
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
    const path = '/api/records' + (company ? '?company=' + encodeURIComponent(company) : '')
    app.request(path, {
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
      fail: (err) => {
        this.setData({ loading: false })
        wx.showToast({ title: (err && err.errMsg) || '加载失败', icon: 'none' })
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
