const API_BASE = 'http://104.249.172.32'

// 每位同事用自己的控制台账号登录，换到的凭证存在本机；
// 小程序包里不再放任何密钥（旧版的 API_KEY 是管理员权限，发给同事就等于给了全部企业）。
const AUTH_KEY = 'zjip_auth'

App({
  globalData: {
    apiBase: API_BASE,
    auth: null, // { token, expires_at, user: { username, display_name, role, groups } }
  },

  onLaunch() {
    this.globalData.auth = wx.getStorageSync(AUTH_KEY) || null
  },

  setAuth(auth) {
    this.globalData.auth = auth
    wx.setStorageSync(AUTH_KEY, auth)
  },

  clearAuth() {
    this.globalData.auth = null
    wx.removeStorageSync(AUTH_KEY)
  },

  toLogin() {
    wx.reLaunch({ url: '/pages/login/login' })
  },

  // 带登录凭证调用接口；凭证失效（改了口令、被停用或强制下线）时回到登录页
  request(path, options) {
    const opts = options || {}
    const auth = this.globalData.auth
    if (!auth || !auth.token) {
      this.toLogin()
      return
    }
    wx.request({
      ...opts,
      url: this.globalData.apiBase + path,
      header: { ...(opts.header || {}), Authorization: 'Bearer ' + auth.token },
      success: (res) => {
        if (res.statusCode === 401) {
          this.clearAuth()
          wx.showToast({ title: '登录已失效，请重新登录', icon: 'none' })
          this.toLogin()
          return
        }
        if (opts.success) opts.success(res)
      },
    })
  },

  logout() {
    const auth = this.globalData.auth
    if (auth && auth.token) {
      // 只是让服务器记一笔；本机凭证无论结果如何都清掉
      wx.request({
        url: this.globalData.apiBase + '/api/logout',
        method: 'POST',
        header: { Authorization: 'Bearer ' + auth.token },
      })
    }
    this.clearAuth()
    this.toLogin()
  },
})
