// 后端地址。本地联调别改这里（容易误提交上线）：复制 config.example.js 为 config.js
// （已被 .gitignore 排除）改 API_BASE。只有开发版（开发者工具、真机预览）读它，
// 体验版和正式版一律用线上地址，config.js 就算被打进上传包也不生效。
// 线上地址要在小程序后台「开发管理 → 开发设置 → 服务器域名」登记为 request 合法域名，
// 打开通知书用 wx.downloadFile，同一个地址还要登记为 downloadFile 合法域名。
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
// 「我负责的 / 全部」，每个登录的人各记一份，和控制台一样默认「我负责的」
// （自己不负责任何企业时，服务器把「我负责的」当成「全部」）
const SCOPE_KEY = 'zjip_scope:'
// wx.openDocument 能打开的文件类型
const DOC_TYPES = ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx']
const TODO_TAB = 1 // app.json tabBar 里「待办」的位置

App({
  globalData: {
    apiBase: API_BASE,
    auth: null, // { token, expires_at, user: { username, display_name, role, groups } }
    scope: 'mine',
    scopeVersion: 0, // 每切换一次加一，标签页回到前台时据此判断要不要重新加载
    scopeInfo: null, // { owned, visible }：接口最近一次告诉我们的企业数
    can: { add: false, scan: false, reveal: false },
  },

  onLaunch() {
    this.globalData.auth = wx.getStorageSync(AUTH_KEY) || null
    this.globalData.scope = this._loadScope()
    if (this.globalData.auth) this.refreshMe()
  },

  setAuth(auth) {
    this.globalData.auth = auth
    wx.setStorageSync(AUTH_KEY, auth)
    this.globalData.scope = this._loadScope()
    this.refreshMe()
  },

  clearAuth() {
    this.globalData.auth = null
    wx.removeStorageSync(AUTH_KEY)
  },

  toLogin() {
    wx.reLaunch({ url: '/pages/login/login' })
  },

  _scopeKey() {
    const auth = this.globalData.auth
    return SCOPE_KEY + ((auth && auth.user && auth.user.username) || '')
  },

  _loadScope() {
    return wx.getStorageSync(this._scopeKey()) === 'all' ? 'all' : 'mine'
  },

  setScope(scope) {
    this.globalData.scope = scope === 'all' ? 'all' : 'mine'
    this.globalData.scopeVersion += 1
    wx.setStorageSync(this._scopeKey(), this.globalData.scope)
  },

  // 接口回来的 { owned, visible } 记下来，切换条用它显示数字
  noteScope(info) {
    if (info) this.globalData.scopeInfo = { owned: info.owned, visible: info.visible }
  },

  // 切换条要显示的样子：{ mine, owned, visible }；还不知道企业数时为 null（不显示）
  scopeView() {
    const info = this.globalData.scopeInfo
    if (!info) return null
    return { mine: this.globalData.scope === 'mine' && info.owned > 0, owned: info.owned, visible: info.visible }
  },

  // 「待办」标签上的数字：已逾期和 3 天内到期的补正
  setTodoBadge(urgent) {
    if (urgent > 0) wx.setTabBarBadge({ index: TODO_TAB, text: String(urgent), fail: () => {} })
    else wx.removeTabBarBadge({ index: TODO_TAB, fail: () => {} })
  },

  // 角色和分组每次请求都按服务器上的为准，所以启动时重新问一遍能做什么
  refreshMe(done) {
    this.get('/api/me', null, {
      success: (res) => {
        this.globalData.can = res.data.can || this.globalData.can
        this.noteScope(res.data.scope)
      },
      fail: () => {},
      complete: () => done && done(),
    })
  },

  // 凭证失效（改了口令、被停用或强制下线）：回到登录页。
  // 首页同时发两个请求，只让第一个 401 处理，免得提示两遍、跳两次登录页
  _expired(auth) {
    if (this.globalData.auth !== auth) return
    this.clearAuth()
    wx.showToast({ title: '登录已失效，请重新登录', icon: 'none' })
    this.toLogin()
  },

  // 带登录凭证调用接口；凭证失效时回到登录页。
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
          this._expired(auth)
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

  // GET，自动带上「我负责的 / 全部」；params 里给了 scope 就用给的
  get(path, params, options) {
    this.request(path + this.query({ scope: this.globalData.scope, ...(params || {}) }), options)
  },

  query(params) {
    const parts = Object.keys(params)
      .filter(k => params[k] !== undefined && params[k] !== null && params[k] !== '')
      .map(k => encodeURIComponent(k) + '=' + encodeURIComponent(params[k]))
    return parts.length ? '?' + parts.join('&') : ''
  },

  // 在小程序里打开一份通知书/证书。凭证只放在请求头里：放进网址会被写进服务器的访问日志
  openAttachment(company, regNo, name, ext) {
    const auth = this.globalData.auth
    if (!auth || !auth.token) {
      this.toLogin()
      return
    }
    if (DOC_TYPES.indexOf(ext) < 0) {
      wx.showToast({ title: '这种文件在手机上打不开，请在电脑上的控制台下载', icon: 'none' })
      return
    }
    const fail = (title) => {
      wx.hideLoading()
      wx.showToast({ title: title, icon: 'none' })
    }
    wx.showLoading({ title: '正在打开', mask: true })
    wx.downloadFile({
      url: this.globalData.apiBase + '/api/attachment' + this.query({ company: company, reg_no: regNo, name: name }),
      header: { Authorization: 'Bearer ' + auth.token },
      success: (res) => {
        if (res.statusCode === 401) {
          wx.hideLoading()
          this._expired(auth)
          return
        }
        if (res.statusCode !== 200) {
          fail(res.statusCode === 404 ? '文件不存在或无权查看' : '服务器出错（HTTP ' + res.statusCode + '）')
          return
        }
        wx.openDocument({
          filePath: res.tempFilePath,
          fileType: ext,
          showMenu: true, // 允许转发给客户、存到手机
          success: () => wx.hideLoading(),
          fail: () => fail('文件打不开，请在电脑上的控制台下载'),
        })
      },
      fail: () => fail('网络错误，请稍后重试'),
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
