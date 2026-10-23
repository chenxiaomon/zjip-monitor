const app = getApp()
const { decode } = require('../../utils/url')

// 状态筛选，分组和控制台「状态」排序一致：先要处理的，再在途的，最后是已结束的
const STATUS_GROUPS = [
  { key: '', label: '全部' },
  { key: '2,4,16', label: '需处理' },
  { key: '1,12', label: '待审核' },
  { key: '3,17', label: '公示/待发证' },
  { key: '7,13', label: '登记成功' },
  { key: '6,14', label: '不予登记' },
  { key: '5,8,9,10,11,15', label: '撤回/终止' },
]
// 巡检中每隔这么久问一次进度
const POLL_MS = 3000

Page({
  data: {
    company: '', // 从企业卡片进来：只看这家企业
    year: '', // 概览选了年份时跟过来
    q: '',
    status: '',
    groups: STATUS_GROUPS,
    scope: null,
    state: null, // 企业模式：这家企业的登录与巡检状态（/api/company）
    loading: true,
    loadingMore: false,
    records: [],
    total: 0,
    page: 1,
    pages: 1,
  },

  onLoad(options) {
    const company = decode(options.company)
    const q = decode(options.q)
    this.setData({ company, q, year: options.year || '', scope: app.scopeView() })
    wx.setNavigationBarTitle({ title: company || '搜索登记记录' })
    this.fetchRecords(1)
    this.fetchState()
  },

  // 从别处回来时状态可能变了（冷却到期、巡检结束）；第一次显示时 onLoad 已经拉过
  onShow() {
    this._hidden = false
    if (this._shown) this.fetchState()
    this._shown = true
  },

  // 页面不在前台就不再问进度；路上的那次回来也不再续上，回到前台时 onShow 重新接上
  onHide() {
    this._hidden = true
    this._stopPoll()
  },

  onUnload() {
    this._hidden = true
    this._stopPoll()
  },

  onPullDownRefresh() {
    this.fetchState()
    this.fetchRecords(1, () => wx.stopPullDownRefresh())
  },

  fetchState() {
    if (!this.data.company) return
    app.get('/api/company', { company: this.data.company }, {
      success: (res) => {
        if (this._hidden) return
        const s = res.data
        const wasRunning = this._scanning
        this._scanning = s.scan.state === 'running'
        this.setData({ state: { ...s, waitMinutes: Math.ceil(s.scan_wait / 60) } })
        if (this._scanning) {
          this._schedulePoll()
        } else if (wasRunning) {
          app.invalidate()
          if (s.scan.state === 'done') wx.showToast({ title: '巡检完成', icon: 'success' })
          // idle = 全局巡检锁被别的巡检占着，这次没跑
          else if (s.scan.state === 'idle') wx.showToast({ title: '有别的巡检在进行，请稍后再试', icon: 'none' })
          else wx.showToast({ title: '巡检没有成功', icon: 'none' })
          this.fetchRecords(1)
        }
      },
      // 页头是顺带的，记录列表照常显示；巡检中手机网络抖一下，过一会儿接着问
      fail: () => {
        if (this._scanning && !this._hidden) this._schedulePoll()
      },
    })
  },

  _schedulePoll() {
    this._stopPoll()
    this._poll = setTimeout(() => {
      this._poll = null
      this.fetchState()
    }, POLL_MS)
  },

  _stopPoll() {
    if (this._poll) {
      clearTimeout(this._poll)
      this._poll = null
    }
  },

  // 立即巡检：经办人每家企业 10 分钟一次（和控制台一样），管理员不限
  onScan() {
    app.post('/api/accounts/scan', { company: this.data.company }, {
      success: () => {
        wx.showToast({ title: '已开始巡检', icon: 'none' })
        this._scanning = true
        this.setData({ 'state.scan.state': 'running' })
        if (!this._hidden) this._schedulePoll()
      },
      fail: (err) => {
        wx.showToast({ title: (err && err.errMsg) || '巡检没有开始', icon: 'none' })
        this.fetchState()
      },
    })
  },

  // 每页 50 条，滑到底再加载下一页
  onReachBottom() {
    if (!this.data.loadingMore && this.data.page < this.data.pages) this.fetchRecords(this.data.page + 1)
  },

  fetchRecords(page, done) {
    const params = { page: page, q: this.data.q, status: this.data.status, year: this.data.year }
    if (this.data.company) {
      params.company = this.data.company
      params.scope = 'all' // 点进来的企业不管是不是自己负责的，都要看得到
    }
    if (page > 1) this.setData({ loadingMore: true })
    app.get('/api/records', params, {
      success: (res) => {
        const d = res.data
        const rows = (d.records || []).map(r => ({
          ...r,
          key: r.company + '|' + r.reg_no,
          pillCls: 'pill pill-' + (r.status_code || 'none'),
          dueCls: r.due ? 'pill u-' + r.due.urgency : '',
          certCls: r.cert ? 'pill u-' + r.cert.urgency : '',
        }))
        this.setData({
          loading: false,
          loadingMore: false,
          records: page > 1 ? this.data.records.concat(rows) : rows,
          total: d.total || 0,
          page: d.page || 1,
          pages: d.pages || 1,
        })
      },
      fail: (err) => {
        this.setData({ loading: false, loadingMore: false })
        wx.showToast({ title: (err && err.errMsg) || '加载失败', icon: 'none' })
      },
      complete: () => done && done(),
    })
  },

  onSearch(e) {
    this.setData({ q: (e.detail.value || '').trim() })
    this.fetchRecords(1)
  },

  onStatus(e) {
    this.setData({ status: e.currentTarget.dataset.key })
    this.fetchRecords(1)
  },

  onClearYear() {
    this.setData({ year: '' })
    this.fetchRecords(1)
  },

  onScopeChange() {
    this.setData({ scope: app.scopeView() })
    this.fetchRecords(1)
  },

  onTapRecord(e) {
    const ds = e.currentTarget.dataset
    wx.navigateTo({
      url: '/pages/record/record?company=' + encodeURIComponent(ds.company) + '&reg_no=' + encodeURIComponent(ds.reg),
    })
  },
})
