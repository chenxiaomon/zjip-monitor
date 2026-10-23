// 页面参数解码。navigateTo 传过来的值没解码，这里解；但万一哪天传进来的已经解过、
// 又带着「%」（比如搜「50%」），decodeURIComponent 会抛错让整页空白——那就用原值。
function decode(value) {
  try {
    return decodeURIComponent(value || '')
  } catch (e) {
    return value || ''
  }
}

module.exports = { decode }
