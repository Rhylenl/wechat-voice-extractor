# WeChat Voice Extractor

在用户本人授权的 Windows 微信环境中，将刚刚播放的收藏中的笔记语音恢复为 WAV 和 128 kbps MP3。

> 仅限处理你本人有权访问的微信数据。不要使用本工具提取他人的聊天内容，也不要把 dump、raw 音频或日志上传到公共平台。

> **所有示例中的盘符、文件夹和文件路径都必须改成你自己的目录，不能原样复制。**

## 工作思路

微信播放语音时，会在 `Weixin.exe` 内存中出现已经解密的 Speex 或 Silk 数据。本工具按以下链路处理：

1. 用户完整播放收藏中的笔记语音。
2. 使用 Windows `comsvcs.dll` 创建主 `Weixin.exe` 的 full minidump。
3. 解析 `Memory64ListStream`，取得 dump 中的内存范围。
4. 从 dump 提取 `duration`、`fullsize`、`fullmd5`、`head256md5` 和 `datapath`。
5. 在内存中定位明文音频，并同时校验长度、头部 MD5 和完整 MD5。
6. 使用 Speex/Silk 解码器生成 WAV，再用 ffmpeg 生成 128 kbps MP3。
7. 用 ffprobe 验证输出后，默认删除临时 raw 文件和脚本创建的 dump。

## 系统环境

- Windows 微信（已在 4.1.13.12 验证；其他版本需自行验证）
- WSL Ubuntu
- Python 3.10+
- `ffmpeg` 和 `ffprobe`
- Speex 解码器：`~/wechat-speex-declib/bin/speex_decode`
- Silk V3 解码器：`~/.local/bin/silk_v3_decoder`
- Windows 管理员权限（创建 full dump 时会弹出 UAC）

## 安装解码器

本项目不分发第三方解码器。请分别按照对应项目的许可和说明安装，并确保以下文件可执行：

```bash
test -x ~/wechat-speex-declib/bin/speex_decode
test -x ~/.local/bin/silk_v3_decoder
command -v ffmpeg
command -v ffprobe
```

也可以通过参数传入不同位置：

```bash
python3 wechat_voice_extract.py \
  --decoder /path/to/speex_decode \
  --silk-decoder /path/to/silk_v3_decoder
```

## 填写你微信文件存储位置的盘和文件夹

源码中的默认值是脱敏占位符：

```python
DEFAULT_ACCOUNT_ROOT = r"E:\\微信文件\\xwechat_files\\YOUR_WECHAT_ID"
```

请把这里的示例路径改成你自己的目录，也就是你微信文件存储位置的盘和文件夹，或每次运行时传入：

```bash
python3 wechat_voice_extract.py \
  --account-root 'D:\\微信文件\\xwechat_files\\<你的微信文件夹>'
```

请把 `D:`、`微信文件` 和尖括号中的内容全部改成你自己的目录，不能原样复制上面的示例。脚本会在 dump 中匹配 Windows 风格的 `datapath`。

## 日常使用

1. 打开 Windows 微信收藏中的笔记语音。
2. 从头到尾完整播放收藏中的笔记语音。
3. 播放结束后立即在 WSL 运行脚本。
4. 如果弹出管理员授权窗口，点击“是”。

```bash
# 已知总时长时，单位是总秒数
python3 wechat_voice_extract.py --duration 13
python3 wechat_voice_extract.py --duration 138   # 2 分 18 秒

# 不知道时长时可以省略；多条候选会要求选择
python3 wechat_voice_extract.py
```

输出默认写入当前 Windows 用户桌面：

```text
微信语音_时长.wav
微信语音_时长.mp3
```

同名文件不会被覆盖，脚本会追加时间戳。

## 常用参数

```text
--pid PID                  手动指定主 Weixin.exe PID；通常不需要
--dump PATH                使用已有 full dump 文件位置，跳过创建
--duration SECONDS         按目标总秒数筛选元数据
--output-dir PATH          指定导出文件保存位置
--account-root PATH        填写你微信文件存储位置的盘和文件夹
--decoder PATH             指定 Speex 解码器文件位置
--silk-decoder PATH        指定 Silk 解码器文件位置
--keep-dump                成功后仍保留脚本创建的 dump
--keep-raw                 成功后仍保留 raw 音频
--non-interactive          多候选时不询问，直接失败
```

其中凡是出现 `PATH`、`/path/to/...`、`D:\\微信文件...` 或其他目录示例的地方，都必须替换成你自己的实际目录。

## 失败处理

### 没有 `Memory64ListStream`

通常表示创建的是普通小型 minidump，而不是 full dump。确认已点击 UAC，并重新播放后立即运行。脚本会在分析前检查这一点。

### 没有在你微信文件存储位置的盘和文件夹下找到元数据

确认 `--account-root` 已填写你微信文件存储位置的盘和文件夹，并确认收藏中的笔记语音刚刚完整播放过。

### 没有通过三项 MD5 校验的音频

最常见原因是 dump 建立得太晚、播放了其他语音，或选错了进程。重新播放收藏中的笔记语音并立即运行，不要关闭微信。失败时脚本创建的 dump 会保留供 `--dump` 重试；使用完后应按精确路径删除。

### 找不到解码器或 ffmpeg

检查文件是否存在、可执行，并确认程序在 WSL 的 `PATH` 中。第三方解码器请遵守其各自许可证。

## 安全说明

full dump 可能包含聊天内容、联系人、登录状态、令牌和其他进程内存：

- 不要分享或上传 `.dmp` 文件。
- 不要把 dump 提交到 Git 仓库。
- 不要把 raw 音频提交到 Git 仓库。
- 成功后默认删除脚本创建的 dump；使用 `--dump` 指定的原有 dump 不会自动删除。
- 需要保留 dump 时，使用 `--keep-dump`，复查结束后手动清理。

仓库的 `.gitignore` 已忽略 dump、raw、PCM、WAV/MP3 输出和本地配置文件。

## 开发与测试

运行合成单元测试：

```bash
python3 -m unittest -v test_wechat_voice_extract.py
```

测试只使用合成 minidump、占位路径和合成哈希，不包含真实微信账户信息或真实语音数据。

## 免责声明

本工具按“原样”提供。微信版本、进程布局、编码格式和系统权限变化都可能导致流程失效。使用者应自行确认法律授权、隐私保护、磁盘空间和数据备份责任。

