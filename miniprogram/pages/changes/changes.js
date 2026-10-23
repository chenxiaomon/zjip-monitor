const app = getApp()

// 标签页回到前台时，数据超过这么久就重新拉一次
const STALE_MS = 5 * 60 * 1000

Page({
  data: {
    loading: true,
    scope: null,
    rows: [],
    total: 0,
  },

  onLoad() {
    if (!app.globalData.auth) {
      app.toLogin()
      return
    }
    this.fetchChanges()
  },

  onShow() {
    if (!app.globalData.auth || this._fetchedAt === undefined) return
    if (this._scopeVersion !== app.globalData.scopeVersion || Date.now() - this._fetchedAt > STALE_MS) {
      this.fetchChanges()
    } else {
      this.setData({ scope: app.scopeView() })
    }
  },

  onPullDownRefresh() {
    this.fetchChanges(() => wx.stopPullDownRefresh())
  },

  // 最近 30 天，最新的在前；同一次巡检的几条共用 key，列表用序号区分
  fetchChanges(done) {
    this._scopeVersion = app.globalData.scopeVersion
    this._fetchedAt = Date.now()
    app.get('/api/changes', null, {
      success: (res) => {
        const rows = (res.data.rows || []).map((r, i) => ({
          ...r,
          id: i + '|' + r.key,
          oldCls: 'pill pill-' + (r.old_status_code || 'none'),
          newCls: 'pill pill-' + (r.new_status_code || 'none'),
        }))
        this.setData({
          loading: false,
          scope: app.scopeView(),
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

  onScopeChange() {
    this.fetchChanges()
  },

  // 点一条看这条记录的详情；从平台列表消失的记录已经没有详情可看
  onTapRow(e) {
    const ds = e.currentTarget.dataset
    if (ds.type === 'removed') {
      wx.showToast({ title: '这条记录已从平台列表消失，请到平台核查', icon: 'none' })
      return
    }
    wx.navigateTo({
      url: '/pages/record/record?company=' + encodeURIComponent(ds.company) + '&reg_no=' + encodeURIComponent(ds.reg),
    })
  },
})
