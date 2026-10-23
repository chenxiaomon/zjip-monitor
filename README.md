# zjip-monitor

浙江省数据知识产权登记平台监控工具。项目可以用多个企业账号登录 `zjip.org.cn`，定时拉取登记申请状态，生成快照、变更事件和 HTML 报表，并通过钉钉、企业微信或邮件推送通知。

当前还包含一个轻量 Web 控制台，使用 FastAPI、Jinja2、HTMX 和 SSE 展示仪表盘、账号、记录、变更、通知、报表与设置页面；前端资源全部本地提供，支持暗色模式和手机端。

## 功能

- HTTP 直登（POST /v1/user/login，密码 md5）并缓存 token；网络或响应异常时自动回退到 Playwright 浏览器登录
- 分页拉取登记记录并按状态码映射展示；翻页不信任服务端 totalSize（以空页/短页/无新记录为准），中途页面出错保留已抓部分并与上份快照合并，不产生假「记录消失」
- 对比历史快照，输出新增、变更、移除事件
- 生成 HTML 汇总报表
- 支持命令行一次运行、定时守护进程和 Web 控制台
- Web 控制台按人登录：每位同事一个账号，分管理员 / 经办人 / 只读三种角色；默认监听 `127.0.0.1:8000`；「登记记录」页支持按数据名称 / 登记号 / 公司关键字搜索
- **操作日志**：谁、何时、从哪个 IP 改了什么——企业账号与密码、设置、通知、用户、巡检、登录登出、附件下载、CSV 导出、服务器上的归档释放，都记在一处
- 巡检/登录结果持久化到 `data/control/accounts_status.json`：定时巡检失败、部分抓取、平台总数与本地不一致都会在界面上标出
- 控制台顶部**运维横幅**：失败账号过多、长时间无成功巡检、磁盘告急、附件超量、巡检进程卡死、定时巡检暂停、公司名脱敏后重名
- **磁盘守护**：剩余空间低于阈值时停止下载附件；每轮巡检结束清理过期快照/报表并写一份用量快照
- **`GET /health`** 健康检查端点（免鉴权，`?strict=1` 在系统 degraded 时返回 503），供部署脚本与外部探针使用
- 三个进程统一的日志配置，控制台进程也落盘到 `logs/web_YYYY-MM-DD.log`

## 控制台用户与角色

控制台的登录账号在「用户」页（仅管理员）管理，存放在服务器的 `config/users.yaml`（口令为 PBKDF2 哈希，文件权限 600）。

| 角色 | 能做什么 |
|---|---|
| 管理员 | 全部：新增 / 删除企业、改分组、巡检、去平台办理、设置、通知、报表、用户与分组、操作日志 |
| 经办人 | 所在分组的企业：查看，新增 / 删除企业，修改平台密码和联系人，测试登录，重登，手动巡检（每家 10 分钟一次），去平台办理 |
| 只读 | 所在分组的企业：查看，重登 |

- 一个人可以属于多个分组；分组和角色改了**立即生效**，不用重新登录（改角色会让该用户重新登录一次）。
- 管理员给同事建账号时设初始口令，对方首次登录会被提示修改；左下角锁形图标是「修改密码」（需输入当前口令）。
- 「重置口令」「停用」「强制下线」都会让该用户在所有设备上立即退出；管理员不能对自己降级、停用或删除。
- `.env` 里的 `WEB_USER` / `WEB_PASS` 是**备用管理员**：不在用户文件里、页面上改不了，用于用户文件损坏或管理员都被误删时救场，也是脚本 / curl 的 Basic Auth 凭据。它的会话只能靠轮换 `SESSION_SECRET` 统一作废。
- 用户文件读不出来时，只有备用管理员能登录，「用户」页顶部会提示，并拒绝任何会覆盖它的保存。
- 旧的 `.env` → `WEB_USERS`（明文口令）已停用：升级后控制台第一次启动时，若 `config/users.yaml` 还不存在，会把其中的用户以「只读」导入并要求改口令，已登录的人不会掉线。导入后请从 `.env` 删除 `WEB_USERS`，之后每次启动都会提醒。
- 经办人和只读用户发起的平台登录（重登、测试登录、改密后的自动验证）按企业账号限频：10 分钟内重登 1 次、验证 3 次，超出时密码照常保存、只是不去平台验证——反复用错密码登录可能把客户的平台账号锁住。
- `/view` 只读看板**必须登录**，每人只看到自己能看的企业（原先不登录也能看到全部企业，`VIEW_TOKEN` 已取消）。

## 同事之间的隔离

分组就是隔离的边界：经办人和只读用户**只看得到自己分组里的企业**，「未分组」的企业只有管理员看得到。

- **你自己的企业放「未分组」**（或放进一个不分配给任何同事的分组），同事就看不到。
- **每位同事一个专属分组**：在「用户」页新建经办人 / 只读用户时，默认勾选「为这位同事新建专属分组」，系统建一个「显示名的企业」分组并只分给 TA；同事之间也互相看不到。
- 「用户」页的「分组」卡片可新建、改名、删除分组（还有企业或同事的分组不能删），并列出每个分组的企业数和成员；用户列表的「可见企业」一栏显示每人能看到几家。
- 经办人在「账号」页**自己新增企业**时只能选自己的分组、默认自己是负责人；与别人的企业或平台账号冲突时只提示「已在系统中」，不说是谁的。也可以删除、手动巡检自己分组的企业。
- 不要把同事设成管理员——管理员看得到全部。
- 隔离覆盖仪表盘、待办、日历、登记记录（含 CSV 导出）、变更、记录详情、附件下载、只读看板 `/view`、去平台办理、实时推送和小程序接口；`tests/test_isolation_sweep.py` 用同事身份逐一检查，看不到未分组企业的任何信息。
- 定时巡检碰上同事的单账号巡检时，会等它结束（最多 10 分钟）再开始，不会整轮跳过。

## 去平台办理

本系统对平台**只读**，提交登记、上传补正材料、续展都要在 zjip.org.cn 上由人完成。为了让同事不必记住每家企业的平台口令，「账号」页每行和记录详情抽屉里有「去平台办理」：

- 点开显示这家企业的平台账号与口令（口令默认打码，可「显示」「复制」），以及「打开平台登录页」；从记录详情点开的，还有「登录后打开这条记录」。
- 管理员可看任何企业，经办人只能看自己分组的企业，只读用户没有这个按钮。
- **每次点开都记入操作日志**（「查看平台口令」：谁、哪家企业、哪条记录），口令本身不入日志；响应不缓存。
- 控制台全站 HTTPS（见「访问地址与 HTTPS」），口令在网络上是加密的。「复制」在 HTTPS 下用浏览器的剪贴板接口；本地明文 HTTP 调试时退回兼容写法，个别浏览器失败时点「显示」后手动复制。

## 负责人与「我负责的」

同事按客户分工：管理员在「账号」页每行的「负责人」下拉框里，给每家企业指定跟进的同事（只列出看得到该分组的在职同事）。

- 负责人存在 `accounts.enc` 的账号记录里，`decrypt_accounts.py --out` 导出、改完再加密时会一并保留（`owner:` 字段）。
- 负责人登录后，仪表盘（指标、待办、时间线、趋势）、期限日历、登记记录、变更历史**默认只显示自己负责的企业**；顶栏的「我负责的 N / 全部 M」随时切换，选择记在浏览器里。没有负责任何企业的人看不到这个开关，照常看全部。
- 这只是显示偏好，不是权限：能看到、能改什么仍按角色和分组，运维横幅也始终看全部。
- 负责人被删除、停用或调离该分组时，账号页和仪表盘上会标出「（已删除）」「（已停用）」「（看不到该分组）」，提醒管理员换人。

## 微信小程序

同事用**控制台的个人账号**登录小程序，看到的企业和控制台一致（自己负责的排在前面，标「我负责」）。

小程序是个人主体，不能用 web-view 嵌网页，所以手机上常用的控制台功能做成了原生页面，底部三个标签：

- **概览**：「我负责的 N / 全部 M」切换（和控制台顶栏一样，默认「我负责的」，选择记在手机上）、年份、搜索框、三个指标、待办摘要、各企业卡片（上次巡检失败、部分抓取、数据可能过期都标出来）、「等待首次巡检」的企业。点企业进它的全部记录（不受「我负责的」影响）。
- **待办**：「待办」是仪表盘的待补正（按截止日期）和可续展；「日程」是期限日历：已逾期、按月按天的每个截止日期和证书到期、截止日期未知的补正。标签上的数字是已逾期和 3 天内到期的补正。
- **变更**：最近 30 天的状态变化，跟随「我负责的 / 全部」。
- **登记记录**：搜索、状态筛选、每页 50 条往下滑加载；点一条进**记录详情**——和控制台的详情抽屉同一份数据（补正截止和要点、证书有效期、通知书、附件、状态历史），附件在小程序里直接打开（可转发、保存到手机，每次打开记入操作日志）。
- **企业操作**（管理员和本组经办人）：从企业卡片进去，页头显示平台登录和巡检状态，可以「立即巡检」（经办人每家企业 10 分钟一次，巡检中每 3 秒刷新进度）和「去平台办理」（确认后显示平台账号和打码的口令，可复制；每次查看记入操作日志，页面一隐藏就清掉）。记录详情里也有「去平台办理」，多给一个这条记录的链接。
- **新增企业**（管理员和经办人）：概览的「+ 新增企业」，规则与控制台一样（经办人只能放进自己的分组、负责人默认自己）；保存后自动登录一次平台核对口令，页面等结果（约 2 分钟内）。新增的表单要收集客户的平台口令，**正式版提交审核时可能被拦**，目前按体验版使用。
- 用户、设置、通知、操作日志、报表、编辑和删除企业等管理功能只在控制台。

接口：

- 小程序接口和控制台页面**共用同一份逻辑**（`records.record_matches` / `record_detail_data`、`attachments.attachment_response`、`src/todo`；企业操作是 `web/routes/accounts.py` 的 `start_scan` / `reveal_account` / `add_account_core` / `add_form_options`，控制台路由和 `web/routes/api_accounts.py` 都只是外壳），手机上搜到的就是登记记录页搜到的，权限、冷却、操作日志也完全一样。
- `/api/status`、`/api/records`、`/api/changes`、`/api/me` 只加不改：都接受 `?scope=mine|all`，不带时是「全部」，旧版小程序拿到的和以前一样；`/api/records` 另有 `?q=`、多选 `?status=2,7`、`?page=`（每页 50 条，不带时一次给全部）。新增 `/api/deadlines`、`/api/record`、`/api/attachment`、`/api/company`、`/api/accounts/new-form`，以及写接口 `POST /api/accounts`、`/api/accounts/scan`、`/api/accounts/platform-access`（请求体必须是 JSON 对象）。
- 企业用公司名定位，平台登录名不发到手机上（「去平台办理」除外；错误信息里出现时也替换掉）。别组的企业和不存在的企业回答完全一样。
- 新增的接口**只认个人账号登录的凭证**，`X-API-Key` 和 Basic 一律 403：旧版小程序包里打包过管理员 `API_KEY`。
- `/api` 的写操作同样有「没记日志就补一条 `http.unaudited`」的兜底。
- 打开附件用 `wx.downloadFile`，所以 `https://monitor.chenranorchenfei.asia` 还要登记为 **downloadFile 合法域名**（和 request 合法域名在同一页）。凭证只放在请求头里，不放进网址（网址会写进访问日志）。
- 小程序在开发机上跑不起来，`tests/test_miniprogram_static.py` 是上传前的检查：页面文件齐全、JSON 能解析、`node --check`、模板里绑定的函数都存在、标签配对、调用的 `/api` 路径后端都有、不用 `?.` `??` `async/await`、不带密钥。

- 登录换到一个 30 天有效的凭证（`API_TOKEN_DAYS` 可调），存在手机上；在控制台给该同事重置口令、停用、强制下线或改角色，手机上的登录随即失效。小程序里「退出」只清本机凭证。
- **还没改初始口令的账号登录不了小程序**（管理员设的、或从 `WEB_USERS` 导入的口令）：控制台只是提醒，因为能当场改；小程序没有改口令的地方，30 天的凭证会让初始口令一直不改。登录时提示「请先登录控制台修改初始口令」，改完即可登录；这条规则之前发出的凭证也随之失效。
- 小程序包里**不再放 `API_KEY`**：旧版把管理员密钥打进包里，发给同事就等于给了全部企业。`.env` 的 `API_KEY` 仍可用于脚本（`X-API-Key` 头）。旧的本机 `miniprogram/config.js` 里如果还留着 `API_KEY`，删掉那一行。
- 本地联调：复制 `miniprogram/config.example.js` 为 `config.js`（已 gitignore）改 `API_BASE`，不要改 `app.js` 里的线上地址。`config.js` 只在开发版生效，体验版和正式版即使打包进去也用线上地址。
- 接口出错（服务器 5xx、被拒绝、断网）时页面提示具体原因，不会显示成「暂无数据」。
- **有新变化时弹窗提醒**：打开小程序、从后台切回来或返回首页时，把上次看过之后的状态变更、新增登记、记录消失弹出来（前三条 + 总数），点「查看」进「变更」标签页（最近 30 天，首页的「最近变化」也能进）。只在打开小程序时提醒，不是推送；书签存在手机上，每个登录的人各一份，第一次用只记书签不弹。接口 `GET /api/changes?since=`：`since` 传上次返回的 `next_since`，服务器从书签前一小时给起（巡检的变化按抓取时间记、稍后才写进日志，并行巡检时可能晚到），小程序按 `key` 去掉已经弹过的。
- 接口地址是 `https://monitor.chenranorchenfei.asia`（`app.js` 里的 `API_BASE`），要在小程序后台「开发管理 → 开发设置 → 服务器域名」登记为 request 合法域名；登记后体验版不用再打开「开发调试」，也可以提交审核正式发布。改完要在微信开发者工具里重新上传。
- 旧版小程序的接口是 `http://服务器IP`，服务器现在把 IP 访问一律跳转到 HTTPS 域名，旧版已经连不上，同事需要用重新上传后的新版。

## 操作日志

「操作日志」页（仅管理员）按时间倒序列出所有记录，可按日期、操作人、类别、关键字筛选，或只看失败；「用户」页的「最近登录」也取自这里。

- 文件：`data/control/audit/audit-YYYY-MM.jsonl`，按北京时间每月一个，只追加、**从不自动删除**（一年约几十 MB），权限 600；`python -m archive pull` 会把它们一并拉回本机长期保存。
- 记什么：控制台里的每个修改操作（表单校验没通过、什么也没改的不记）、登录 / 退出 / 登录失败 / 锁定、附件下载（同一人同一文件 10 分钟内只记一次）、CSV 导出（含筛选条件与行数）、服务器上的归档释放与取消归档（操作人「本机归档」，IP 取自 ssh）。
- **不记任何秘密**：口令、Webhook、签名密钥、SMTP 口令只记「改了」，不记值；登录失败时，不属于任何人的用户名（多半是把口令输进了用户名框）打码记录。有测试专门拦这件事。
- 兜底：哪个修改类请求成功了却没登记，中间件会补一条「未登记的写操作」，测试也会比对全部 POST 路由，新路由忘了接日志会直接失败。
- 记录里的 IP 取 nginx 设置的 `X-Real-IP`；客户端自己带的 `X-Forwarded-For` 不再被采信（否则可以伪造 IP、绕过登录锁定）。

## 企业账号管理与改密码

- 控制台「账号」页每行有 **编辑** 按钮：可直接修改密码（留空 = 不改）、联系人、分组与负责人（经办人只能改自己分组企业的密码和联系人）；公司名称与账号与历史数据绑定，不可修改
- 新增/编辑弹窗内的 **测试登录** 按钮会用表单中填写的账号密码向平台试登录一次（不保存、不写 token 缓存），先验证再保存
- 改密码后会自动清除旧 token 缓存并后台验证一次，结果直接显示在账号行
- **防锁号**：同一账号连续 N 次（`settings.yaml → login.max_consecutive_failures`，默认 3）被平台拒绝登录后，定时巡检自动跳过该账号并推送通知，界面标黄「已暂停 · 需更新密码」；在界面改密码或手动点「巡检」成功后自动恢复
- 收缩卫兵拒绝保存时（记录数骤减超 50%），若确认属实，可 `touch data/control/force_save/<账号>.flag` 放行下一次巡检

## 账号文件与加密密钥

运行时读的是加密文件 `config/accounts.enc`，密钥是 `.env` 里的 `FERNET_KEY`。
生产的 systemd 服务通过 `EnvironmentFile=/opt/zjip-monitor/.env` 拿到同一把密钥。

```bash
python scripts/encrypt_accounts.py              # accounts.yaml → accounts.enc
python scripts/decrypt_accounts.py              # 查看现有账号（密码打码）
python scripts/decrypt_accounts.py --out config/accounts.yaml   # 导出明文以便编辑（0600）
```

几条必须遵守的规则：

- **跑加密脚本前先 `unset FERNET_KEY`**。`load_dotenv` 不会覆盖 shell 里已有的变量，
  所以残留的 `FERNET_KEY`（哪怕是空值）会让脚本看到的密钥和生产进程不一致。现在脚本
  会直接报错退出；`.env` 里已有密钥时它绝不会改写。
- **不要拿一份过期的 `accounts.yaml` 重跑加密脚本**。控制台里新增或改密的账号只存在于
  `accounts.enc`。正确做法是先 `decrypt_accounts.py --out config/accounts.yaml` 导出当前
  内容再编辑。脚本在覆盖前会解密旧文件并打印新增/移除/密码有变化的摘要（不回显密码），
  终端里需要输入 `yes` 才继续；旧文件保留为 `accounts.enc.bak`。
- **不要把 `decrypt_accounts.py` 的默认输出当成 accounts.yaml 回灌**——里面的密码是
  `****xx` 掩码。脚本会识别并拒绝，但别依赖这道保险。
- 导出的明文文件用完立刻 `rm`。`--out` 拒绝写入符号链接。
- YAML 里纯数字的账号或密码要加引号（`password: "01234567"`），否则会被解析成数字而拒收。

## 配置

`config/settings.yaml` 是唯一的运行配置，完整说明见 `config/settings.yaml.example`
（每个键都有中文注释）。不写的键一律用默认值，默认值就是改成可配置之前代码里的字面量。

- 大部分键保存后**下一次巡检 / 下一次刷新页面即生效**，不用重启服务。
  `schedule.crons` 由 daemon 每分钟检查一次、计划真正变了才重排，约 1 分钟内生效；
  daemon 把正在执行的计划写进 `data/control/schedule.json`，设置页据此显示是否已采用。
  唯一需要重启进程的是日志保留天数。
- cron 第 5 位「星期」按 APScheduler 的规则 **0 = 周一**（常见 cron 是 0 = 周日），
  `0 9 * * 1-5` 实际是周二到周六。设置页列出接下来 5 次运行时间，以它为准。
- 设置页可直接改 6 项告警与磁盘阈值：失败账号告警数、无成功巡检小时数、补正到期提醒天数、
  连续登录失败暂停次数（0 = 从不暂停）、磁盘剩余下限、附件目录上限，带上下限校验。
  保留期限、并发数、爬虫参数只能改文件。
- 控制台每次保存都先**严格读取整个文件**：文件有格式错误时拒绝保存并提示，不会用单独一栏
  覆盖掉通知密钥、分组和计划；同一时间只有一个保存在写，文件原有权限（如 `chmod 600`）保留。
- 数值键填错（字符串、带引号的数字、负数、`true`）会回退到默认值并在日志里提示一次，
  不会让服务起不来。
- **cron 表达式按北京时间解释**，与服务器系统时区无关；服务器系统时区是 UTC，
  所以日志时间戳比 cron 里写的早 8 小时。
- 在控制台「设置」页保存任何一项，都会用程序重写这个文件，里面的注释会全部消失
  （值不受影响）。想看说明请对照 `settings.yaml.example`。

## 磁盘与数据保留

生产服务器根分区只有 9.6G，`data/attachments/` 是唯一无上限增长的目录。策略是
**测量 + 告警 + 停止新增，绝不自动删除任何通知书或证书**：

- 剩余空间低于 `disk.min_free_mb`（默认 500MB）时跳过附件下载，已跳过的记录不写
  manifest，下一轮会重试。
- 每轮巡检结束时清理：每家企业保留最近 `retention.snapshots_keep_per_company` 份快照、
  报表保留 `retention.reports_keep_days` 天、`events.jsonl` 超过 `retention.events_rotate_mb`
  就轮转并只留最近几份、清扫超过 1 小时的 `.part` 下载残留。
- 清理在巡检的**每一条出口**执行（正常完成、被取消、出错、单账号巡检），因为磁盘最紧张的
  时候恰恰是巡检失败的时候；清理全程持有全局运行锁，不会和另一个进程的写入打架。
- 每轮结束写一份 `data/control/disk_stats.json`（各目录用量与剩余空间），控制台横幅与
  `/health` 都读它。

## 运维横幅与健康检查

三个通知渠道（钉钉/企业微信/邮件）默认全部关闭，所以运维告警的出口是**控制台顶部横幅**
和日志。八项检查：

| 检查 | 触发条件 | 阈值来源 |
|---|---|---|
| 失败账号过多 | 本轮 status 不是 ok/partial 的账号数达到阈值 | `ops_alerts.min_failed_accounts` |
| 长时间无成功巡检 | 所有账号最近一次成功距今超过阈值，或从未成功过 | `ops_alerts.stale_hours` |
| 磁盘告急 | 剩余空间低于阈值（附件下载已暂停） | `disk.min_free_mb` |
| 附件目录超量 | `data/attachments/` 总量超过阈值 | `attachments.max_total_mb` |
| 巡检进程卡死 | 持锁进程还活着但心跳已停 | — |
| 定时巡检已暂停 | 控制台里主动暂停了 | — |
| 公司名重名 | 两家公司脱敏后同名，快照与附件会互相覆盖 | — |
| 补正即将到期 | 待补正的截止日期（来自补正通知书）不足 N 天；逾期或不足 2 天为严重 | `ops_alerts.correction_warn_days` |

- 告警去重记在 `data/control/ops_alerts.json`：新激活时打一条 critical/warning 日志，
  恢复时打一条 info，持续存在的不再刷屏。日志里搜 `运维告警[` 可查全部激活/恢复记录。
  这个文件可以安全删除，删掉后下一轮会重新激活并重打日志。
- 经办人和只读用户的横幅只统计自己能看到的企业；磁盘、卡死、暂停这类系统级告警对所有人可见。
- 之后如果启用了任一通知渠道，同样的告警会自动推送，不需要额外配置。

`GET /health` 返回 JSON：版本（来自服务器上的 `RELEASE`）、控制台启动时间、最近一轮巡检、
上次成功巡检距今小时数、各状态计数、磁盘用量，以及 `checks[]` 逐项结论。

- **它是免鉴权的公开端点**（任何人知道域名或 IP 都能访问），所以响应里只有计数、时间、
  状态码和阈值，**绝不能往里加企业名、用户名、错误原文或文件路径**——有测试专门拦这件事。
- 不带参数永远返回 200（方便直接 curl 读 JSON）；`?strict=1` 才会在任一检查不通过时返回 503。
- degraded 不等于部署失败：`deploy.sh` 收到 503 会打印 JSON 并警告，仍算部署成功。
  排查看 `checks[]` 里 `ok: false` 那一项的 `detail`。
- 全新安装在第一轮巡检成功之前，`?strict=1` 会返回 503（`recent_success` 为红），属预期。

## 待办与期限

补正的截止日期和证书的有效期**只写在通知书 PDF 里**（平台记录的 `auditDeadLine` 一直是空的），所以巡检下载通知书时会
用 `src/pdftext.py`（pypdf）读出落款日、截止日、补正要点、证书有效期，写进该记录 `.sync.json` 的 `facts`。控制台据此显示：

- 仪表盘顶部「待办」：待补正按截止日期排序（剩余天数红/黄/绿，逾期视为撤回登记申请，能看到补正要点）；
  登记证书期满前 180 天起列入「可续展」（续展登记后不再列出）。经办人和只读用户只看自己分组的企业。
- 登记记录页：待补正的行带「截止 MM-DD · 剩 N 天」标签，快到期的登记成功记录带「MM-DD 到期」标签。
- 运维横幅：补正不足 `ops_alerts.correction_warn_days` 天到期时提醒。
- 「期限日历」页（`/calendar?month=YYYY-MM`）：补正截止与证书到期排在月历上，下方是本月议程，侧栏列出
  所有已逾期的期限和「截止日期未知」的补正。证书到期在这里不受 180 天窗口限制，远期的也能看到。
- 记录详情抽屉：在登记记录、待办、变更历史、日历里点一条记录，右侧滑出它的补正要点（含历次补正通知）、
  证书有效期、附件、按登记号过滤的完整状态历史和平台原始数据。

历史文件的 `facts` 由 `python scripts/backfill_notice_facts.py` 一次性补齐；已经释放到本机归档、服务器上没有文件的公告，
用本机的 `python -m archive push-facts` 把解析结果送到服务器。通知书没有文字层或解析失败时，待办里显示「截止日期未知」，
不会漏掉这条申请。

## 登记趋势（每日统计）

仪表盘「登记趋势」和指标卡上的迷你折线，读的是 `data/control/daily_stats.json`：每轮巡检收尾时
（所有出口，持全局巡检锁）按北京日期记下各公司「申请年份 × 状态码」的记录数，同一天后一轮覆盖前一轮，
保留 400 天，全年约 1.3MB。口径与仪表盘卡片完全一致，web 请求只读这个小文件（按 mtime 缓存）。

- 文件按公司名记录，经办人和只读用户只汇总自己能看到的企业；已删除账号的历史自然不再显示。
- 新部署后先回填一次，用服务器上现存的快照补出之前约两周：

  ```bash
  .venv/bin/python scripts/backfill_daily_stats.py --dry-run   # 看能补多少天
  .venv/bin/python scripts/backfill_daily_stats.py
  ```

  回填持巡检锁（有巡检在跑会退出码 2，稍后再试），已有的日期不覆盖；某家公司某天巡检失败时沿用
  它上一次的数字，不会出现假的下跌。
- 文件损坏时会被改名为 `daily_stats.json.bad-<时间>` 留存，然后从空白重新记录。

## 控制台界面

控制台的样式、脚本和图标全部在 `web/static/` 本地提供，**不再依赖任何公网 CDN**（旧版从
cdn.tailwindcss.com 和 unpkg 现场拉 Tailwind 与 htmx，外网一抖整站就没样式、按钮全失效）。
整页首次加载约 100KB，之后全部走浏览器缓存，服务器端只多了几个静态文件，不增加 CPU 与内存开销。

| 文件 | 作用 |
|---|---|
| `ui.css` | 手写的设计系统：颜色变量（明/暗两套）、卡片、按钮、表单、徽章、表格、侧边栏、弹窗、轻提示 |
| `ui.js` | 明暗切换、手机端抽屉菜单、HTMX 请求进度条、错误 / 保存成功的轻提示（约 60 行，无依赖） |
| `icons.svg` | Tabler Icons（MIT）裁剪出的 SVG sprite，模板里用 `{{ icon('bell') }}` 引用 |
| `htmx.min.js` / `htmx-sse.js` | htmx 1.9.12 及其 SSE 扩展的本地副本 |

- **状态徽章颜色只有一份定义**：`web/deps.py → STATUS_COLORS`（17 个状态码 → 六个色族，含暗色值），
  由 `status_css()` 内联进每页 `<head>`；仪表盘的「状态分布」条也按同一色族着色，
  色序（绿 → 蓝 → 橙 → 紫 → 红，灰为撤回/终止类弱化色）经过色盲区分度校验。
- **加图标**：到 https://tabler.io/icons 找 outline 版 SVG，把其中的 `<path>` 放进 `icons.svg`
  的一个 `<symbol id="i-名称" viewBox="0 0 24 24">` 里即可；测试会检查模板用到的每个图标都在 sprite 里。
- **缓存**：静态资源 URL 带内容哈希 `?v=`（`web/deps.py → static_url()`），带版本号的响应
  `Cache-Control: immutable` 一年，不带的一小时；部署新版本后浏览器自动拿到新文件，不需要强刷。
- **手机端**：900px 以下侧边栏折叠成抽屉，表格在卡片内横向滚动，次要列（行业、申请时间）在
  640px 以下隐藏；顶栏的全局搜索框直接跳到「登记记录」页按关键字过滤。
- **手机桌面图标**：`manifest.webmanifest` 加三枚 PNG 图标，手机浏览器「添加到主屏幕」后有正式
  图标和名称。控制台没有 Service Worker（离线时没有可看的），所以 Android 不一定弹安装提示，
  用浏览器菜单里的「添加到主屏幕」即可。
  图标从 `web/static/app-icon.svg` 渲染，改了图形后用本机 Playwright 重新生成：

  ```bash
  .venv/bin/python - <<'PY'
  from pathlib import Path
  from playwright.sync_api import sync_playwright
  svg = Path("web/static/app-icon.svg").read_text(encoding="utf-8")
  with sync_playwright() as p:
      b = p.chromium.launch()
      for name, n in (("icon-512.png", 512), ("icon-192.png", 192), ("apple-touch-icon.png", 180)):
          page = b.new_page(viewport={"width": n, "height": n})
          page.set_content('<body style="margin:0">' + svg.replace("<svg ", '<svg width="%d" height="%d" ' % (n, n), 1))
          page.locator("svg").screenshot(path="web/static/" + name)
      b.close()
  PY
  ```
- 想再省一点带宽，可以在 nginx 里对 `text/css application/javascript image/svg+xml` 开 `gzip on`
  （压缩后总量约 30KB）；应用层没有开 gzip，是为了不影响 SSE 长连接。

## 日志

三个进程共用 `src/logging_setup.py`：

| 进程 | 文件 | 保留 |
|---|---|---|
| `run_daemon.py` / `run_once.py` | `logs/run_YYYY-MM-DD.log` | `retention.logs_keep_days`（默认 30 天） |
| 控制台（`uvicorn web.app:app`） | `logs/web_YYYY-MM-DD.log` | `retention.web_logs_keep_days`（默认 14 天） |

文件日志是 DEBUG 级、每天 0 点轮转；stderr 是 INFO 级，只有连着终端时才上色（journald 里
不再有转义码）。uvicorn 与 APScheduler 的日志也汇进同一份文件。
`ZJIP_LOG_TO_FILE=0` 只输出到 stderr，`ZJIP_LOG_DIR` 改目录。

## 本地启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

复制配置模板：

```bash
cp .env.example .env
cp config/settings.yaml.example config/settings.yaml
cp config/accounts.yaml.example config/accounts.yaml
```

填写 `.env`、`config/settings.yaml` 和 `config/accounts.yaml` 后，加密账号配置：

```bash
python scripts/encrypt_accounts.py
```

加密完成后可以删除明文账号文件：

```bash
rm config/accounts.yaml
```

运行一次巡检：

```bash
python run_once.py
```

启动定时巡检：

```bash
python run_daemon.py
```

启动 Web 控制台：

```bash
python run_web.py
```

浏览器访问 `http://127.0.0.1:8000`（线上是 `https://monitor.chenranorchenfei.asia`），先用 `.env` 的 `WEB_USER` / `WEB_PASS`（备用管理员）登录，到「用户」页给自己和同事建个人账号。

## 访问地址与 HTTPS

控制台和小程序接口都走 `https://monitor.chenranorchenfei.asia`。TLS 由服务器上的 nginx 终止，证书是 certbot 签的
Let's Encrypt（90 天，`certbot.timer` 自动续期），应用仍只监听 `127.0.0.1:8000`。

- nginx 配置只在服务器上，不在仓库里：`/etc/nginx/conf.d/zjip-monitor-domain.conf` 是域名站点（443，
  带 HSTS），`/etc/nginx/sites-available/zjip-monitor` 是 80 端口的默认站点，把用 IP 或其它主机名
  访问的请求 301 跳转到域名。
- 浏览器经 HTTPS 访问时，登录 cookie 带 `Secure`：有人手动输入 `http://` 时，cookie 不会在跳转前明文发出。
  这依赖 nginx 传 `X-Forwarded-Proto`，uvicorn 默认只信任来自 `127.0.0.1` 的这个头。
- `/api/*` 的响应一律 `Cache-Control: no-store`，以后在前面加 CDN 也不会把凭证或某人的企业列表缓存给别人。
- 本地 `python run_web.py` 仍是明文 HTTP，cookie 不带 `Secure`，照常能登录。

## 部署到服务器

服务器上没有 git，代码靠 `./deploy.sh` 用 rsync 推送。详细说明见 `deploy/README.md`。

```bash
./deploy.sh status     # 看服务状态、当前版本、/health、磁盘、备份列表
./deploy.sh dry-run    # 跑本地测试 + 列出将传输的文件，不改服务器
./deploy.sh            # 正式部署
./deploy.sh rollback   # 回滚到最近一份备份（默认只恢复代码）
```

`deploy` 的每一步：本地工作区检查与全量 pytest（不通过就中止）→ 远端预检（目录、venv、
剩余磁盘 ≥ 200MB）→ 把服务器上的代码打包成 `code-<时间>-pre-<新sha>.tgz` 备份（只留最近
3 份，权限 600）→ rsync → 写 `RELEASE` 版本文件 → `pip install -r requirements.lock` →
导入冒烟 → 重启两个服务 → `/health?strict=1` 健康检查。任何一步失败都会停下并说明服务器
当前处于什么状态、怎么回滚。

几个要点：

- **备份名里的 sha 是即将部署的新版本，内容是被它替换掉的旧代码**。
- **回滚默认只覆盖代码**，不动服务器上的 `.env` 与 `config/`（否则会把部署之后在控制台改过的
  账号密码和设置一起退回去）。真正的灾难恢复用 `./deploy.sh rollback --with-config`。
- 备份里**含 `.env` 明文口令**，所以备份目录 700、文件 600。
- 默认**不带 `--delete`**。要用就 `DEPLOY_DELETE=1 ./deploy.sh`，它会先列出将被删除的远端
  文件并要求输入 `yes`。
- 健康检查：200 = 正常；`/health` 不存在（404/302/401/403）时退回检查登录页；**503 = 服务
  起来了但系统自评 degraded**，只警告不算部署失败；连不上或其它 5xx 才算失败。
- `requirements.lock` 是生产 venv（Linux / Python 3.12）的 pip freeze，**不要在本地
  macOS / Python 3.10 上装**。服务器上升级过依赖后跑 `./deploy.sh lock` 重新生成并提交。
- systemd unit 模板在 `deploy/`，改动后用 `./deploy.sh install-units`（先 diff、再确认、
  不自动重启）。

## 本机归档（archive）

服务器上的 `data/` 只是工作缓存：快照每家只留 30 份、事件日志满 5MB 就轮转，被新一轮替换掉的旧通知书平台上也不再提供。
`archive/` 是**只在本机运行**的归档工具，把这些都拉回本地长期保存，并建成一个可查询的 SQLite 库。

```bash
cp archive/archive.env.example archive/archive.env   # 可选：改服务器地址或归档目录（默认 ~/zjip-archive）
pip install -r requirements-dev.txt                   # 需要 pypdf（只装在本机）
python -m archive pull     # rsync 拉取附件、快照、事件日志
python -m archive index    # 增量更新 ~/zjip-archive/archive.sqlite
```

- **只增不减**：`pull` 永远不带 `--delete`。服务器删掉的快照、轮转掉的日志，本地都留着；服务器原地改写过的文件，
  rsync 会先把本地旧版挪到 `.overwritten/<时间>/`。归档目录是正本，请纳入 Time Machine 等备份。
- `archive.sqlite` 里有：`records`（每条登记申请的首次出现时间与最新状态）、`status_events`（全部状态变化）、
  `documents`（每个文件的哈希，以及从正文解析出的落款日、补正截止日、补正理由、公告里的证书有效期、证书编号等）、
  `documents_fts`（正文全文检索，3 个字以上用 `MATCH`，更短的词对 `documents.text` 用 `LIKE`）。
- 例：`sqlite3 ~/zjip-archive/archive.sqlite "select reg_no, deadline, reasons from documents where doc_kind='补正' order by deadline desc limit 5"`
- 不写 SQL 也能用：`python -m archive search 固定数据量`（可加 `--kind 补正`）在全部通知书与证书正文里搜；
  `python -m archive report` 输出补正理由分布、补正轮数、从申请到公告的用时、一年内到期且尚未续展的证书，
  并存到 `~/zjip-archive/reports/`。已续展的证书不会被当成即将到期（公告里写的是第一届有效期）。

**释放服务器空间**（本机有了经过哈希核对的副本后，把旧文件从服务器删掉）：

```bash
python -m archive pull && python -m archive index   # 先同步，保证本机副本是最新的
python -m archive release                          # 预演：列出可释放的文件与合计，服务器上什么都不删
python -m archive release --company 某企业目录 --yes # 先小范围正式释放
python -m archive release --yes                    # 全量
python -m archive restore 企业目录 登记号           # 客户要旧证书时：把这条申请的文件送回服务器
python -m archive push-facts                       # 把已释放公告的证书有效期等送到服务器，供控制台「可续展」显示
```

- 哪些可以释放：已办结（登记成功/不予登记/撤回等）且平台 `archive.release_after_terminal_days`（默认 90）天没动过的申请的
  全部文件，以及被新一轮替换下来的旧通知书。还在办理中的申请一个文件都不动。
- 服务器端 `scripts/archive_release.py` 不信任本机发来的任何东西：持巡检锁、只接受 `data/attachments/` 下的文件、
  逐个重算 sha256 与本机副本比对、按**服务器自己**的最新快照和 settings.yaml 复核释放条件，全部通过才删。
- 删除前先在该记录的 `.sync.json` 里记下 `archived`，所以之后的巡检不会把它重新下载回来；控制台登记记录页显示为
  灰色「已归档」标记（带归档日期），不再是下载链接。

## 运行测试

```bash
pip install -r requirements-dev.txt
pytest
```

`pytest.ini` 已设置 `testpaths=tests` 与 `pythonpath=.`，所以裸 `pytest` 即可运行，
`scripts/test_login.py`（手动排障脚本，不是测试）不会被收集。测试依赖只在
`requirements-dev.txt` 中，`deploy.sh` 在服务器上安装的是 `requirements.lock`
（生产 venv 的 pip freeze），测试依赖不会进生产环境。

测试用例自带隔离：账号状态文件、控制目录、settings.yaml、快照目录都指向临时路径，
Web 用例通过 `tests/conftest.py` 固定的测试账号登录，不会读写真实数据。

## 不进仓库的文件

以下文件包含本机配置、账号、token 或运行数据，已在 `.gitignore` 中排除：

- `.env`
- `config/accounts.yaml`
- `config/accounts.enc`
- `config/settings.yaml`
- `config/users.yaml`（控制台用户，只在服务器上由控制台写入；`deploy.sh` 不传、不删）
- `data/*`
- `logs/`

GitHub 仓库只保留代码、模板和示例配置。
