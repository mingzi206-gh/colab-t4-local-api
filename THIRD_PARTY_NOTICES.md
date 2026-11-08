# 第三方来源与许可边界

本仓库只分发本项目脚本、配置模板与文档，不打包模型、CUDA 动态库、llama.cpp、cloudflared 或 Google CLI 二进制。

| 上游 | 来源 | 发布注意 |
| --- | --- | --- |
| google-colab-cli | https://github.com/googlecolab/google-colab-cli | 依赖其 API 与 OAuth 实现；许可证以所安装版本元数据/仓库为准 |
| llama.cpp | https://github.com/ggml-org/llama.cpp | MIT，下载 release 时保留归档许可文件 |
| cloudflared | https://github.com/cloudflare/cloudflared | Apache-2.0，使用官方仓库二进制与许可 |
| NVIDIA CUDA runtime | llama.cpp release 的 cudart 分发包 / https://docs.nvidia.com/cuda/eula/index.html | 受 NVIDIA 对应版本条款约束，不能把它当成本项目自有 MIT 代码 |
| 模型 GGUF | https://huggingface.co/mradermacher/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic-i1-GGUF | 量化文件与基础模型/微调的来源、许可分别核查；本项目没有授予模型权重的再分发权 |
| 其他 Python 依赖 | requirements.txt 与安装包 METADATA | 各包许可证独立有效 |

公共 GitHub 仓库不自动使内容成为开源。当前未替仓库所有者选择自有代码的开源许可证；发布者应在发布前决定许可证并新增 LICENSE。若尚未决定，可公开用于审阅，但不要宣传已获得某种开源复用许可。本项目不能替上游权利人许可模型或 CUDA。

本次审查未完成模型整个训练/微调来源链的许可审计，不宣称可商用。模型名称不等于其来源与能力已获验证。
