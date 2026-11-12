# 华工回放字幕采集器

[English README](README.en.md)

在本地按课程名和日期批量导出华南理工大学课程回放字幕。优先使用平台字幕；没有字幕时，可在 Apple Silicon Mac 上转写回放音频。输出包括带时间戳的 TXT、SRT 和 JSON。

## 安装

- **完整流程**：Apple Silicon（M 系列）Mac、Google Chrome、Python 3（带 pip）和终端。安装脚本会创建独立的 Python 3.11 环境。
- **仅平台字幕**：在能运行本项目依赖和 Chrome 的 macOS / Linux 环境，可尝试 `--site-only`。完整流程在 Apple Silicon macOS 上验证，Linux 和 Windows 未验证。
- 需要能访问学校回放站和 Python 包源。首次本地转写或预下载模型时还需访问 Hugging Face。模型权重约 1.6 GB，转写期间还要存放下载的视频和音轨。

获取源码并在项目根目录运行：

```sh
./setup.sh
```

脚本用 `uv.lock` 安装锁定依赖。如果已经安装了 Python 3.11，也可以使用从同一锁文件导出的 [requirements.txt](requirements.txt)，任选一种安装方式即可：

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
```

## 快速开始

```sh
./scut sync --course '操作系统' --since 2026-09-01 --through 2026-11-12
```

把课程名和日期换成自己的。程序会打开独立的 Chrome 窗口。首次运行在学校页面完成登录，页面提供二维码时可扫码。登录成功后程序自动继续，下次会复用仍有效的登录状态。只读取本人课表中的课程。

日期格式为 `YYYY-MM-DD`，首尾都包含。可在命令末尾加 `--dry-run` 先看将处理哪些课次。重复执行时会跳过已完成且校验通过的字幕。用 Ctrl+C 中断后，再运行同一命令可复用已完成的下载和转写分段。

## 命令行

| 命令 | 用途 |
| --- | --- |
| `./scut login` | 单独打开学校登录页并保存本地会话 |
| `./scut courses` | 列出本人课表的课程名和课程 ID |
| `./scut sync --course '操作系统' --on 2026-11-11` | 处理某一天 |
| `./scut sync --course '操作系统' --through 2026-11-12` | 处理从课程目录起始到指定日期的课次 |
| `./scut sync --course '操作系统' --through 2026-11-12 --site-only` | 只取平台字幕，缺失课次留待处理 |
| `./scut status` | 离线查看本地处理状态 |
| `./scut prepare-model` | 提前下载语音转写模型 |
| `./scut sync --help` | 查看完整参数 |

同名课可用 `--course-id` 代替 `--course`，ID 以 `./scut courses` 为准。已有有效会话时，可把 `--headless` 放在子命令前。若提示没有捕获课表请求，请用有窗口模式重试，确认回放站首页已加载本人课表。

## 本地语音转文字

首次遇到无平台字幕的回放时自动下载模型，也可以手动预下载：

```sh
./scut prepare-model
```

模型为 [mlx-community/whisper-large-v3-turbo](https://huggingface.co/mlx-community/whisper-large-v3-turbo/tree/a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb)，固定修订版本为 `a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb`，缓存于 `.data/models/`。程序使用随 Python 依赖提供的 FFmpeg 提取 16 kHz 单声道音频，以 Silero VAD 检测语音，再用 MLX Whisper 在本机按原语种转写。只下载模型权重，课堂音频留在本机。无需 API 密钥。字幕可能有误识别，`[待核对]` 标示部分低置信度片段。

## 输出与隐私

每节的字幕写入 `.data/courses/<课程ID>/<日期>_<课次ID>/`：

- `transcript.txt`：易读的带时间戳文字。
- `transcript.srt`：可导入播放器的标准字幕。
- `transcript.json`：结构化字幕、来源和转写信息。
- `slides.json`：平台课件截图的时间轴。截图序号未经原始课件页码校验。

当前 CLI 按课次输出。未上线的回放保持待处理，可在稍后重跑同一日期范围。工具仅做字幕采集和语音转写。

`.data/` 包含独立浏览器会话、处理记录、模型、媒体和字幕，`exports/` 可包含整理后的字幕。这些都是私有本地数据，已加入 Git 忽略规则。若使用自定义 `--data-dir`，请放到仓库外。使用本人有权访问的回放，分享课堂内容前请确认获得相应授权。

## 测试和许可证

```sh
.venv/bin/python -m unittest discover -s tests -v
```

源码使用 [MIT License](LICENSE)。模型和依赖遵循各自的许可证。
