const app = getApp()

Page({
  data: {
    username: '',
    password: '',
    error: '',
    submitting: false,
  },

  onUsername(e) {
    this.setData({ username: e.detail.value })
  },

  onPassword(e) {
    this.setData({ password: e.detail.value })
  },

  onSubmit() {
    const username = this.data.username.trim()
    const password = this.data.password
    if (!username || !password) {
      this.setData({ error: '请填写用户名和口令' })
      return
    }
    this.setData({ submitting: true, error: '' })
    wx.request({
      url: app.globalData.apiBase + '/api/login',
      method: 'POST',
      data: { username, password },
      success: (res) => {
        if (res.statusCode === 200 && res.data && res.data.token) {
          app.setAuth(res.data)
          wx.reLaunch({ url: '/pages/index/index' })
          return
        }
        const msg = (res.data && res.data.error) || ('登录失败（HTTP ' + res.statusCode + '）')
        this.setData({ error: msg, password: '' })
      },
      fail: () => {
        this.setData({ error: '网络错误，请稍后重试' })
      },
      complete: () => {
        this.setData({ submitting: false })
      },
    })
  },
})
