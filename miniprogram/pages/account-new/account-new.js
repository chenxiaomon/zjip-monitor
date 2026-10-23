const app = getApp()

// 保存后服务器自动用这个口令登录一次平台：每隔这么久问一次结果，最多问这么多次（约 2 分钟）
const POLL_MS = 2000
const POLL_MAX = 60

Page({
  data: {
    loading: true,
    company: '',
    username: '',
    password: '',
    contact: '',
    groups: [], // [{ key, label }]：经办人只有自己的分组，管理员多一个「未分组」
    groupIndex: 0,
    ownersByGroup: {}, // 分组 → 能看到它的同事
    owners: [], // 当前分组的负责人候选，第一项是「未指定」
    ownerIndex: 0,
    submitting: false,
    verifying: false,
    error: '',
  },

  onLoad() {
    app.get('/api/accounts/new-form', null, {
      success: (res) => {
        const d = res.data
        const groupIndex = Math.max(0, d.groups.findIndex(g => g.key === d.default_group))
        this.setData({ loading: false, groups: d.groups, ownersByGroup: d.owners, groupIndex })
        this._setOwners(d.default_owner)
      },
      fail: (err) => {
        this.setData({ loading: false, error: (err && err.errMsg) || '加载失败' })
      },
    })
  },

  // 离开了就什么都不再做：还在路上的请求回来时也不再接着问、不弹窗，
  // 更不能 navigateBack——那会把用户此时所在的页面关掉
  onUnload() {
    this._gone = true
    this._stopPoll()
  },

  // 换了分组，负责人名单跟着换；原来选的人在新名单里就留着
  _setOwners(preferred) {
    const group = this.data.groups[this.data.groupIndex]
    const owners = [{ username: '', label: '未指定' }].concat((group && this.data.ownersByGroup[group.key]) || [])
    const i = owners.findIndex(o => o.username === preferred)
    this.setData({ owners, ownerIndex: i > 0 ? i : 0 })
  },

  onInput(e) {
    this.setData({ [e.currentTarget.dataset.field]: e.detail.value })
  },

  onGroup(e) {
    const current = this.data.owners[this.data.ownerIndex]
    this.setData({ groupIndex: Number(e.detail.value) })
    this._setOwners(current ? current.username : '')
  },

  onOwner(e) {
    this.setData({ ownerIndex: Number(e.detail.value) })
  },

  onSubmit() {
    const d = this.data
    if (!d.company.trim() || !d.username.trim() || !d.password.trim()) {
      this.setData({ error: '公司名称、平台账号、平台口令都要填' })
      return
    }
    const group = d.groups[d.groupIndex]
    const owner = d.owners[d.ownerIndex]
    this.setData({ submitting: true, error: '' })
    app.post('/api/accounts', {
      company: d.company,
      username: d.username,
      password: d.password,
      contact: d.contact,
      group: group ? group.key : '',
      owner: owner ? owner.username : '',
    }, {
      success: (res) => {
        app.invalidate()
        if (this._gone) return
        this._company = res.data.company
        this._polls = 0
        this.setData({ password: '', verifying: true }) // 口令不留在页面上
        this._poll()
      },
      fail: (err) => this.setData({ error: (err && err.errMsg) || '保存失败' }),
      complete: () => this.setData({ submitting: false }),
    })
  },

  _poll() {
    if (this._gone) return
    this._timer = setTimeout(() => {
      this._timer = null
      app.get('/api/company', { company: this._company }, {
        success: (res) => {
          if (this._gone) return
          const login = res.data.login
          this._polls += 1
          if (login.state === 'pending' && this._polls < POLL_MAX) {
            this._poll()
            return
          }
          this._finish(login)
        },
        fail: () => this._finish(null),
      })
    }, POLL_MS)
  },

  _stopPoll() {
    if (this._timer) {
      clearTimeout(this._timer)
      this._timer = null
    }
  },

  _finish(login) {
    if (this._gone) return
    this.setData({ verifying: false })
    let content = '首次登录平台还没有结果，稍后在概览里查看这家企业。'
    if (login && login.state === 'ok') {
      content = '首次登录平台成功，下一轮巡检会抓取它的记录；也可以在它的记录页点「立即巡检」。'
    } else if (login && login.state === 'error') {
      content = '但首次登录平台没有成功：' + (login.error || '原因不明') + '。请到电脑上的控制台「账号」页核对口令。'
    }
    wx.showModal({
      title: '已新增 ' + this._company,
      content,
      showCancel: false,
      success: () => wx.navigateBack(),
    })
  },
})
