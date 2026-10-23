// 去平台办理：查看并复制这家企业的平台账号口令，到 zjip.org.cn 自己办理。
// 和控制台一样：先确认，每次查看都记入操作日志；口令默认打码；页面一隐藏就清掉。
// 系统对平台始终只读——提交办理是人在平台网站上的操作。
Component({
  options: { styleIsolation: 'apply-shared' }, // 用 app.wxss 里的 .link

  properties: {
    company: { type: String, value: '' },
    regNo: { type: String, value: '' }, // 从某条记录点进来时带上，多给一个「这条记录的链接」
  },

  data: {
    loading: false,
    access: null, // { username, password, login_url, detail_url }
    shown: false,
  },

  lifetimes: {
    detached() { this._clear() },
  },

  pageLifetimes: {
    hide() { this._clear() },
  },

  methods: {
    onOpen() {
      wx.showModal({
        title: '去平台办理',
        content: '将显示这家企业的平台账号和口令，查看会记入操作日志。',
        confirmText: '查看',
        success: (r) => { if (r.confirm) this._fetch() },
      })
    },

    _fetch() {
      this.setData({ loading: true })
      getApp().post('/api/accounts/platform-access', { company: this.data.company, reg_no: this.data.regNo }, {
        success: (res) => this.setData({ access: res.data, shown: false }),
        fail: (err) => wx.showToast({ title: (err && err.errMsg) || '查看失败', icon: 'none' }),
        complete: () => this.setData({ loading: false }),
      })
    },

    onToggle() {
      this.setData({ shown: !this.data.shown })
    },

    // field: username | password | login_url | detail_url
    onCopy(e) {
      wx.setClipboardData({
        data: this.data.access[e.currentTarget.dataset.field] || '',
        fail: () => wx.showToast({ title: '复制失败，请检查小程序的剪贴板权限', icon: 'none' }),
      })
    },

    onClose() {
      this._clear()
    },

    _clear() {
      if (this.data.access) this.setData({ access: null, shown: false })
    },
  },
})
