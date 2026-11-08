# Colab T4 本地 OpenAI API 复刻包

Windows 本地网关 → Cloudflare 临时隧道 → Colab T4 上的 llama.cpp。模型可缓存到自己的 Google Drive。本地不需要 NVIDIA GPU。

**部署 agent 先读 [AGENTS.md](AGENTS.md)，按 [DEPLOYMENT.md](DEPLOYMENT.md) 顺序执行。** 原环境曾完成真实聊天与 Drive 完整恢复；本次审查后的代码完成情况见 [REVIEW.md](REVIEW.md)，不能把离线测试当成新账号云端部署成功。

## 快速准备

Windows PowerShell，进入本目录。参考环境 Python 3.13，最低 3.12。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
Copy-Item config.example.json config.json
```

填写自己的代理、Drive 配额项目并完成本人 OAuth 后：

```powershell
.\.venv\Scripts\python.exe doctor.py --json
.\.venv\Scripts\python.exe -m unittest discover -v
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

另一个终端执行真实请求验收：

```powershell
.\.venv\Scripts\python.exe _colab_verify_chain.py
.\.venv\Scripts\python.exe _colab_persist_drive.py --verify-restore
```

第二条进行完整模型副本恢复，耗费网络和时间。前置条件、OAuth、配置、停止和故障处理见部署手册。

| 项目 | 默认值 |
| --- | --- |
| 本地 Base URL | `http://127.0.0.1:11435/v1` |
| 模型 ID | `deepseek-heretic-9b` |
| 上下文 | `131072`，可在配置中降低 |
| 未指定生成上限时 | 网关注入 `max_tokens=4096` |
| 远端认证 | 每安装随机密钥，网关自动携带 |
| 本地认证 | loopback 接口，无客户端鉴权 |
| 持久化 | 自己的 Drive，`drive.file` 授权 |

仅开放健康、模型列表和聊天接口。客户端无需知道远端域名或密钥；同机程序都可能访问本地入口。不要改为公网/LAN 监听。默认 128K 是配置值，不保证任意提示长度都能在 T4 显存内处理。

- [部署手册](DEPLOYMENT.md)：当前步骤与验收标准。
- [审查记录](REVIEW.md)：修复、测试证据、未验证范围。
- [历史完整指南](完整复刻指南.md)：原部署过程与历史测量，不是当前命令权威来源。
- [第三方说明](THIRD_PARTY_NOTICES.md)：上游组件和模型条款。

只上传本目录的公开源代码和文档，不上传上层工作区。发布前运行 `python release_check.py` 并检查实际暂存文件。配置、token、密钥、日志、模型、虚拟环境不随包发布。尚未选择本项目 LICENSE，选择前不应宣称已按某开源许可证授权。

Colab 资源、资格和限制会变化。先核对 [Colab FAQ](https://research.google.com/colaboratory/faq.html) 与账号条件，本包不承诺免费、长期在线或无中断恢复。
