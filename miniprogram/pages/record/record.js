const app = getApp()
const { decode } = require('../../utils/url')

function pill(code) {
  return 'pill pill-' + (code || 'none')
}

Page({
  data: {
    loading: true,
    error: '',
    r: null, // /api/record 的结果，加上页面要用的样式类
  },

  onLoad(options) {
    this._company = decode(options.company)
    this._regNo = decode(options.reg_no)
    this.fetch()
  },

  onPullDownRefresh() {
    this.fetch(() => wx.stopPullDownRefresh())
  },

  // 和控制台的记录详情抽屉同一份数据；看得到这家企业就能打开，不受「我负责的」影响
  fetch(done) {
    app.get('/api/record', { company: this._company, reg_no: this._regNo }, {
      success: (res) => {
        const r = res.data
        wx.setNavigationBarTitle({ title: r.name || '登记记录' })
        this.setData({
          loading: false,
          error: '',
          r: {
            ...r,
            pillCls: pill(r.status_code),
            dueCls: r.due ? 'pill u-' + r.due.urgency : '',
            cert: r.cert ? { ...r.cert, daysAgo: -r.cert.days_left } : null,
            // 当前这份补正通知书的要点已经在「补正」里整段显示了
            notices: (r.notices || []).map((n, i) => ({
              ...n, i, showReasons: false, hideReasons: !!(r.due && n.current && n.kind === '补正'),
            })),
            history: (r.history || []).map((h, i) => ({
              ...h, i, oldCls: pill(h.old_status_code), newCls: pill(h.new_status_code),
            })),
          },
        })
      },
      fail: (err) => {
        this.setData({ loading: false, error: (err && err.errMsg) || '加载失败' })
      },
      complete: () => done && done(),
    })
  },

  onOpenFile(e) {
    const ds = e.currentTarget.dataset
    app.openAttachment(this.data.r.company, this.data.r.reg_no, ds.name, ds.ext)
  },

  onArchived() {
    wx.showToast({ title: '已归档到本机，需要时请找管理员恢复', icon: 'none' })
  },

  onToggleReasons(e) {
    const i = e.currentTarget.dataset.i
    this.setData({ ['r.notices[' + i + '].showReasons']: !this.data.r.notices[i].showReasons })
  },

  // 小程序打不开外部网页：复制官网详情页链接，粘贴到浏览器打开（需登录平台）
  onCopyLink() {
    wx.setClipboardData({
      data: this.data.r.detail_url,
      success: () => wx.showToast({ title: '官网链接已复制', icon: 'success' }),
      fail: () => wx.showToast({ title: '复制失败，请检查小程序的剪贴板权限', icon: 'none' }),
    })
  },
})
