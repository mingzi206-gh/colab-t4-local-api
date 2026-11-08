# 当前部署手册

本手册为当前代码的操作入口。历史指南中的时间和性能来自旧实现；新账号、当前修订版的完整云端部署尚未验证。agent 应先读 AGENTS.md，并如实记录每一步结果。

## 1. 前置条件

- Windows PowerShell，Python 3.12+，参考为 3.13；本地磁盘保留虚拟环境空间。Linux 仅安排离线 CI，未承诺云端部署兼容性。
- 主人自己的 Google 账号、可用 Colab T4 资格/配额、可访问 Google/GitHub/Hugging Face/Cloudflare 的网络。
- 自己启用 Google Drive API 的 Google Cloud 配额项目，账号具备 `serviceusage.services.use`；Drive 可用空间至少容纳约 5.63GB 模型，建议另留余量。
- Colab 临时磁盘须能放模型、依赖及恢复验证副本。申请 GPU 可能消耗计算单元，启动不等于免费服务。

先核对 https://research.google.com/colaboratory/faq.html 。FAQ 禁止使用多个账号绕过访问或资源限制；免费且没有正计算单元余额的托管运行时还限制远程控制和绕过 notebook UI 主要通过 Web UI 交互等用途。付费且有正余额只取消 FAQ 列出的特定附加限制。账号条件、用途和条款需匹配，不承诺任意账号均可运行本方案。不要添加模拟保活、进程伪装或耗尽后轮换身份。

## 2. 创建独立环境

从本目录运行，不能在包含私人文件的上层工作区发布。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
Copy-Item config.example.json config.json
```

已有 config.json 时保留并人工核对，不要覆盖主人配置。requirements 固定直接依赖版本，传递依赖仍由 pip 解析；不是全部依赖的跨平台锁文件。安装失败记录具体错误，不使用全局 pip 绕过。

## 3. 本机配置

用编辑器修改 config.json：

| 字段 | 填写方式 |
| --- | --- |
| `config_dir` | 默认 `~/.config/colab-cli`，凭据目录；相对路径以项目目录解析 |
| `proxy` | 能直连填空字符串；否则填自己的 HTTP(S) 代理，必须包含端口 |
| `accounts` | 初次保留一个 `token.account1.json`；不用于绕过限制 |
| `drive_owner_token` | 初次同上，固定由该身份管理 Drive 文件 |
| `drive_quota_project` | 自己已启用 Drive API 的项目 ID 或项目号 |
| `gateway_host` | `127.0.0.1`，仅允许 loopback |
| `gateway_port` | 默认 `11435`，被占用时检查归属或选其他端口 |
| `context_size` | 默认 `131072`；显存不足时先降到 `32768`，重新部署 |
| `state_file` / `log_file` | 本地状态和日志路径，不能提交 |
| `verification_targets` | 可选额外客户端 API，默认为空 |

本包 Python 程序明确清除继承的 HTTP/HTTPS/ALL_PROXY，按 proxy 字段工作；空值就是直连。CLI 的 OAuth 命令独立运行，不读取本包 config.json。

## 4. 由主人完成 OAuth

CLI 需要代理时，仅在当前 PowerShell 设置自己的地址：

```powershell
$env:HTTPS_PROXY = 'http://127.0.0.1:7890'
$env:HTTP_PROXY = $env:HTTPS_PROXY
```

能直连则不设置。运行：

```powershell
.\.venv\Scripts\colab.exe --auth oauth2 whoami
Copy-Item "$HOME\.config\colab-cli\token.json" "$HOME\.config\colab-cli\token.account1.json"
```

按 CLI 实际提示在浏览器完成授权。已有 token.json 时 CLI 可能复用授权；不要误覆盖其他身份。若 config_dir 改过，复制目标对应调整。`colab auth login --no-browser` 不是此版本本地登录命令。

CLI 0.7.5 公共 scopes 包含 Colab 和 drive.file。可选自建 Desktop OAuth client：

```powershell
.\.venv\Scripts\python.exe authorize.py --client-secrets 'C:\credentials\desktop-client.json' --token token.account1.json
```

此路径需主人配置 consent/test users/Desktop client，尚未完成新账号云端验收。授权 token、client JSON、验证码都不进入聊天、仓库或发布包。Windows 上 chmod 并非 NTFS ACL 隔离保证，凭据应放在主人的私有目录。

Drive 的 drive.file 只允许该 OAuth 应用可访问的文件，不等于任意读取整个 Drive。换 OAuth client 后可能无法访问旧缓存。能登录 Google 也不等于 Drive API/IAM/配额已可用。

## 5. 离线验收

```powershell
.\.venv\Scripts\python.exe doctor.py --json
.\.venv\Scripts\python.exe -m unittest discover -v
```

doctor 检查依赖、配置凭据字段/scopes、项目配置、端口；不会刷新 token、联网验证 IAM、申请 GPU。缺失凭据应返回失败而非假装成功。已有服务运行时可用 `doctor.py --skip-port`，但启动前仍须确认端口。

## 6. 启动与真实 API 验收

确认主人已授权此次云计算操作后：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

保持终端和电脑运行。程序先绑定本地端口，再申请或恢复运行时。只恢复项目标记过的 T4，不接管其他 notebook；busy kernel 不重复执行。若账号已有无关 assignment，本包可能无法申请新 GPU，应报告并由主人选择，不自动删除它。

恢复 Drive 失败时可回退到 Hugging Face 下载，因此推理成功不证明 Drive 成功。远端完成模型校验、加载、带认证预热及未认证拒绝测试后才发布隧道。本地健康检查要求 health 正常且携带本安装 key 能取得模型。

```powershell
.\.venv\Scripts\python.exe _colab_verify_chain.py
```

验收标准：模型列表 HTTP 200；真实聊天有正文且 `finish_reason=stop`。再换提示词验证实际客户端。流式客户端应另测正常结束和中断表现，不能用非流式验收替代。

客户端配置：Base URL `http://127.0.0.1:11435/v1`，模型 `deepseek-heretic-9b`。自定义端口同步替换。客户端强制要求 API Key 时可填本地占位值，网关自动替换上游 Authorization。不要把 runtime_api_key.txt 内容给客户端。

Magpie 是可选项，需自行安装并添加 provider。其 Base URL 指向本地网关，模型同上。要让验证脚本测试 Magpie，给 verification_targets 添加实际 URL/model，例如 URL `http://127.0.0.1:3425/v1`、model `colab-t4/deepseek-heretic-9b`，provider ID 以本人配置为准。

## 7. Drive 完整验收

已有本项目空闲内核时：

```powershell
.\.venv\Scripts\python.exe _colab_persist_drive.py --verify-restore
```

必须看到上传/已有缓存校验与独立完整恢复成功。恢复副本校验后删除。仅列出文件、创建目录或取得 access token 不算通过。固定模型大小 `5629111584` bytes，MD5 `e241e16944eba37f2761f0d0b6e76acf`，SHA256 `8cf793a36e62fb078ac05c2e1bdb2121ff94736dccbede7cf81b76346e868949`。

若 Drive 中同名项均不符合完整性要求，缓存步骤明确失败，网关可回退 Hugging Face；需要另行修复 Drive 缓存后再做持久化验收。该失败不能算 Drive 验收成功。

模型仓库 `mradermacher/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic-i1-GGUF`，revision `fff50f6bb44cefedd08217dbc285fb2c41213fff`，文件 `DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic.i1-Q4_K_M.gguf`。程序验证固定完整性信息，不接受同名不同内容。llama.cpp 固定发布版本；cloudflared 从官方 apt 元数据按 SHA 校验但版本未固定，远端系统包也会变化。

## 8. 恢复、停止与故障

健康循环只做 HTTP 探测，不合成 notebook 活动。断线或超时不证明远端未执行，先查忙碌状态和健康，不立刻重复部署。聊天 POST 不自动重试，流中断由客户端作为失败处理。

本地 Ctrl+C 仅停网关，不释放 Colab VM。关闭本地终端后远端仍可能消耗资源。要停止远端服务：先停网关，避免健康循环重启；从本目录运行下列命令，通过项目自己的 PID/start-time 记录清理：

```powershell
.\.venv\Scripts\python.exe _colab_stop_existing.py
```

该命令只在已有的本项目 T4 上执行，不申请新 VM，不需要打印或传入远端密钥。busy kernel 时会拒绝执行。

该操作只停止记录过的服务进程，不释放 VM。若要结束计费/配额占用，由主人在 Colab 界面核对对应运行时后断开并删除；Drive 模型保留。不能广泛 pkill 或删除未知 assignment。

| 现象 | 检查与处理 |
| --- | --- |
| doctor 缺凭据/scopes | 主人重新 OAuth；不抄其他机器 token |
| Drive 403 | 自己项目启用 Drive API、IAM serviceusage.services.use、应用文件可见性 |
| 401 | token 失效重新授权；远端 key 不匹配则协调重部署，不能只删本地 key |
| 端口占用 | 确认已有服务或换端口，不强杀 |
| busy kernel | 等任务结束，不重入部署 |
| 无 GPU/配额不足 | 报告资格/配额，不轮换账号规避 |
| 模型 OOM | 降 context_size、确认 T4 和日志，再重部署 |
| 530/隧道失效 | 查 runtime 是否仍存活；恢复只接管项目标记实例 |
| 流式输出中断 | 视为请求失败，不自动重放聊天 |
| 下载/上传超时 | 有界重试，保留旧有效模型；长任务 token 过期需重新调度 |

## 9. 发布

只发布本目录公开文件。运行 `python release_check.py`，检查 `git status --short` 和 `git diff --cached`。不要提交配置、OAuth、key、日志、状态、模型、二进制或 .venv。扫描器不是任意隐私的完整证明。项目尚待主人选 LICENSE，第三方权利见 THIRD_PARTY_NOTICES.md。

部署结果分别报告：离线测试、健康接口、真实聊天、实际客户端、Drive 完整恢复、新账号从零复刻。未执行的项写未验证。
