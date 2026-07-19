const API_BASE = 'http://104.249.172.32'

// API_KEY 放在 config.js（gitignore），参见 config.example.js
let config = { API_KEY: '' }
try {
  config = require('./config')
} catch (e) {
  console.warn('miniprogram/config.js 缺失，请复制 config.example.js 并填入 API_KEY')
}

App({
  globalData: {
    apiBase: API_BASE,
    apiKey: config.API_KEY,
  },
})
