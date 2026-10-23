const app = getApp()

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

Page({
  data: {
    company: '', // 从企业卡片进来：只看这家企业
    year: '', // 概览选了年份时跟过来
    q: '',
    status: '',
    groups: STATUS_GROUPS,
    scope: null,
    loading: true,
    loadingMore: false,
    records: [],
    total: 0,
    page: 1,
    pages: 1,
  },

  onLoad(options) {
    const company = decodeURIComponent(options.company || '')
    const q = decodeURIComponent(options.q || '')
    this.setData({ company, q, year: options.year || '', scope: app.scopeView() })
    wx.setNavigationBarTitle({ title: company || '搜索登记记录' })
    this.fetchRecords(1)
  },

  onPullDownRefresh() {
    this.fetchRecords(1, () => wx.stopPullDownRefresh())
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
