// 本地联调：复制为 config.js（已被 .gitignore 排除），把 API_BASE 改成本机后端，
// 例如 python run_web.py 默认的 http://127.0.0.1:8000。只在开发版（开发者工具、
// 真机预览）生效；开发者工具里还要勾选「不校验合法域名」（project.config.json 已设 urlCheck: false）。
module.exports = {
  API_BASE: 'http://127.0.0.1:8000',
}
