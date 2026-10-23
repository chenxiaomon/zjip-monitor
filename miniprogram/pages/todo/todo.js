const app = getApp()

const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六']
const KIND_LABELS = { correction: '补正截止', certificate: '证书到期' }
// 标签页回到前台时，数据超过这么久就重新拉一次
const STALE_MS = 5 * 60 * 1000

function withPill(item) {
  return { ...item, cls: 'pill u-' + item.urgency, kindLabel: KIND_LABELS[item.kind] || '' }
}

Page({
  data: {
    loading: true,
    view: 'todo', // 待办 | 日程（agenda）
    scope: null,
    corrections: [],
    renewals: [],
    nextRenewal: null,
    overdue: [],
    months: [], // [{ key, title, days: [{ date, title, items }] }]
    undated: [],
  },

  onLoad() {
    if (!app.globalData.auth) {
      app.toLogin()
      return
    }
    this.fetch()
  },

  onShow() {
    if (!app.globalData.auth || this._fetchedAt === undefined) return
    if (this._scopeVersion !== app.globalData.scopeVersion || Date.now() - this._fetchedAt > STALE_MS) {
      this.fetch()
    } else {
      this.setData({ scope: app.scopeView() })
    }
  },

  onPullDownRefresh() {
    this.fetch(() => wx.stopPullDownRefresh())
  },

  fetch(done) {
    this._scopeVersion = app.globalData.scopeVersion
    this._fetchedAt = Date.now()
    app.get('/api/deadlines', null, {
      success: (res) => {
        const d = res.data
        const items = d.items || []
        const corrections = (d.corrections || []).map(withPill)
        this.setData({
          loading: false,
          scope: app.scopeView(),
          corrections,
          renewals: (d.renewals || []).map(withPill),
          nextRenewal: d.next_renewal || null,
          overdue: items.filter(i => i.days_left < 0).map(withPill),
          months: this._byMonth(items.filter(i => i.days_left >= 0)),
          undated: (d.undated || []).map(withPill),
        })
        app.setTodoBadge(corrections.filter(c => c.urgency === 'overdue' || c.urgency === 'red').length)
      },
      fail: (err) => {
        this.setData({ loading: false })
        wx.showToast({ title: (err && err.errMsg) || '加载失败', icon: 'none' })
      },
      complete: () => done && done(),
    })
  },

  // 接口已按日期排好；按月、再按天分组，和控制台的期限日历一样
  _byMonth(items) {
    const months = []
    items.forEach(item => {
      const key = item.date.slice(0, 7)
      let month = months[months.length - 1]
      if (!month || month.key !== key) {
        month = { key, title: key.slice(0, 4) + ' 年 ' + Number(key.slice(5)) + ' 月', days: [] }
        months.push(month)
      }
      let day = month.days[month.days.length - 1]
      if (!day || day.date !== item.date) {
        day = { date: item.date, title: this._dayTitle(item.date, item.days_left), items: [] }
        month.days.push(day)
      }
      day.items.push(withPill(item))
    })
    return months
  },

  _dayTitle(date, daysLeft) {
    const parts = date.split('-')
    const weekday = WEEKDAYS[new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2])).getDay()]
    const when = daysLeft === 0 ? '今天' : (daysLeft === 1 ? '明天' : '还有 ' + daysLeft + ' 天')
    return date.slice(5) + ' 周' + weekday + ' · ' + when
  },

  onScopeChange() {
    this.fetch()
  },

  onView(e) {
    this.setData({ view: e.currentTarget.dataset.view })
  },

  onTapItem(e) {
    const ds = e.currentTarget.dataset
    wx.navigateTo({
      url: '/pages/record/record?company=' + encodeURIComponent(ds.company) + '&reg_no=' + encodeURIComponent(ds.reg),
    })
  },
})
