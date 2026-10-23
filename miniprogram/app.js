// 后端地址。本地联调别改这里（容易误提交上线）：复制 config.example.js 为 config.js
// （已被 .gitignore 排除）改 API_BASE。只有开发版（开发者工具、真机预览）读它，
// 体验版和正式版一律用线上地址，config.js 就算被打进上传包也不生效。
// 线上地址要在小程序后台「开发管理 → 开发设置 → 服务器域名」登记为 request 合法域名。
let API_BASE = 'https://monitor.chenranorchenfei.asia'
try {
  if (wx.getAccountInfoSync().miniProgram.envVersion === 'develop') {
    const config = require('./config')
    if (config && config.API_BASE) API_BASE = config.API_BASE
  }
} catch (e) {
  // 没有 config.js：用线上地址
}

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

  // 带登录凭证调用接口；凭证失效（改了口令、被停用或强制下线）时回到登录页。
  // 其它失败都交给页面的 fail，err.errMsg 是能直接给人看的原因——服务器出错（5xx）
  // 不能落到 success 里，否则页面会把它当成“没有数据”。
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
        if (res.statusCode !== 200) {
          const reason = (res.data && res.data.error) || '服务器出错（HTTP ' + res.statusCode + '）'
          if (opts.fail) opts.fail({ errMsg: reason, statusCode: res.statusCode })
          return
        }
        if (opts.success) opts.success(res)
      },
      fail: () => {
        if (opts.fail) opts.fail({ errMsg: '网络错误，请稍后重试' })
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
