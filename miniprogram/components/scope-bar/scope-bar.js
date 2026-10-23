// 「我负责的 n / 全部 m」，和控制台顶栏的切换一样。自己不负责任何企业时不显示。
// 切换后记在 app 里（每个标签页回到前台时据此重新加载），并通知所在页面马上刷新。
Component({
  options: { styleIsolation: 'apply-shared' }, // 用 app.wxss 里的 .chip

  properties: {
    scope: { type: Object, value: null }, // app.scopeView()：{ mine, owned, visible }
  },

  methods: {
    onPick(e) {
      const to = e.currentTarget.dataset.to
      const scope = this.data.scope
      if (!scope || (to === 'mine') === scope.mine) return
      getApp().setScope(to)
      this.triggerEvent('change', { scope: to })
    },
  },
})
