// 一条变化的一行说明，「有新变化」弹窗里每条一行
function short(text, n) {
  const s = text || ''
  return s.length > n ? s.slice(0, n) + '…' : s
}

function describe(row) {
  const head = '【' + short(row.company, 8) + '】' + short(row.name || row.reg_no, 10) + '：'
  if (row.type === 'added') return head + '新增登记（' + row.new_status + '）'
  if (row.type === 'removed') return head + '记录消失，请核查'
  return head + row.old_status + ' → ' + row.new_status
}

module.exports = { describe }
