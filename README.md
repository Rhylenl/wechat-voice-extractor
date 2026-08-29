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

上面的解码器位置只是示例；如果你的安装位置不同，必须改成你自己的解码器文件路径。

### 哪些程序是 Python 自带的？

只有 Python 解释器和标准库（例如 `argparse`、`hashlib`、`mmap`）属于 Python 环境。`ffmpeg`、`ffprobe`、Speex 解码器和 Silk V3 解码器都不是 Python 自带程序，必须另外安装或准备。

在 Ubuntu 中，安装 `ffmpeg` 软件包通常会同时提供两个命令：

- `ffmpeg`：把解码后的 WAV 转成 128 kbps MP3。
- `ffprobe`：读取 WAV/MP3 的时长、采样率和声道，用于完成后的验证。

因此不需要单独寻找一个叫“ffprobe”的 Python 包，也不要执行 `pip install ffmpeg` 来代替系统安装。

## 从零开始准备环境

下面按“Windows 主机 → WSL Ubuntu → Python 脚本 → 解码器 → 微信目录”的顺序准备。所有命令中的路径都只是写法示例；看到 `D:\微信文件...`、`/path/to/...`、`<你的...>` 时，必须换成你自己的盘符、文件夹和文件路径。

### 1. Windows 主机准备

1. 安装并登录 Windows 微信，确认你有权处理这个账号的收藏数据。
2. 保持微信正常运行，不要在播放后退出、重启或切换账号。
3. 准备管理员权限。脚本创建 full dump 时，Windows 可能弹出 UAC 窗口；必须点击“是”。
4. 预留磁盘空间。full dump 的大小取决于 `Weixin.exe` 当前内存，常见为数百 MB，也可能超过 1 GB；`C:\Dump` 所在磁盘和导出目录都应有足够空间。
5. 先在微信设置或文件管理器中确认“微信文件存储位置”的盘和文件夹。不要把包含个人微信 ID 的真实路径提交到公开仓库或截图中。

检查 WSL 是否已安装（在 Windows PowerShell 中运行）：

```powershell
wsl --status
wsl --list --verbose
```

建议使用 WSL 2 的 Ubuntu。尚未安装时，可按微软文档安装；常见命令如下，但请先确认 Windows 版本和组织策略允许安装：

```powershell
wsl --install -d Ubuntu
```

### 2. WSL Ubuntu 准备

打开 Ubuntu（不是普通 Windows CMD），确认当前确实在 WSL：

```bash
cat /proc/version
```

输出中通常会出现 `Microsoft` 或 `WSL`。先检查程序是否已经存在：

```bash
command -v python3
command -v ffmpeg
command -v ffprobe
```

如果 `ffmpeg` 或 `ffprobe` 没有输出路径，再执行安装。下面命令在 WSL Ubuntu 中运行，不是在 Windows PowerShell 中运行：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg git build-essential autoconf automake libtool pkg-config
```

本脚本只使用 Python 标准库，不需要额外的 `pip` 第三方包。`git`、编译工具和虚拟环境是为了方便安装/维护解码器；如果你已经有可执行的解码器，可以不重复编译。

安装完成后再次检查：前三个命令应显示版本号，`wslpath` 应输出类似 `/mnt/c/Windows/Temp` 的 WSL 路径：

```bash
python3 --version
ffmpeg -version
ffprobe -version
wslpath -u 'C:\Windows\Temp'
```

如果 `ffmpeg -version` 和 `ffprobe -version` 都能显示版本号，说明音频转换环境已准备好。若仍提示 `command not found`，先重新打开 Ubuntu，再重复检查；不要继续运行提取脚本。

`wslpath` 能把 Windows 路径转换为 WSL 路径，说明 Windows 盘已经可以从 WSL 访问。你的微信文件在其他盘时，把示例中的 `C:` 换成实际盘符验证。

### 3. 放置 Python 脚本

推荐把脚本放在当前 WSL 用户的家目录，路径形如：

```text
/home/<你的 Linux 用户名>/wechat_voice_extract.py
```

例如从下载目录或仓库复制后执行：

```bash
cp /path/to/wechat_voice_extract.py ~/wechat_voice_extract.py
chmod 755 ~/wechat_voice_extract.py
python3 ~/wechat_voice_extract.py --help
```

如果你想使用虚拟环境（脚本本身并不强制要求）：

```bash
python3 -m venv ~/wechat-aes-venv
source ~/wechat-aes-venv/bin/activate
python3 ~/wechat_voice_extract.py --help
```

看到帮助信息就表示 Python 文件可读、可执行，且命令行入口正常。以后若看到 `can't open file '/home/.../wechat_voice_extract.py'`，先执行 `ls -l ~/wechat_voice_extract.py`；文件不存在时，重新用上面的 `cp` 复制到这个准确位置，或直接运行文件的绝对路径。

### 4. 准备 Speex、Silk 和 ffmpeg

本仓库不捆绑第三方解码器。请从你信任的来源按其许可证安装，并把实际可执行文件路径记下来：

```bash
test -x ~/wechat-speex-declib/bin/speex_decode
test -x ~/.local/bin/silk_v3_decoder
command -v ffmpeg
command -v ffprobe
```

如果检查失败，不要把示例路径原样传给脚本。改用你自己的路径，例如：

```bash
python3 ~/wechat_voice_extract.py \
  --decoder '/home/<你的 Linux 用户名>/tools/speex_decode' \
  --silk-decoder '/home/<你的 Linux 用户名>/tools/silk_v3_decoder'
```

`--decoder` 用于 Speex，`--silk-decoder` 用于 Silk V3；两者可以只配置实际需要的那个。解码器必须能在 WSL 中直接执行，并且与其输入格式匹配。

### 5. 配置微信文件存储目录
<img width="198" height="204" alt="image" src="https://github.com/user-attachments/assets/efe5b780-7784-4c5a-bbb4-9e032d1efc36" />
<img width="640" height="560" alt="image" src="https://github.com/user-attachments/assets/3265a2b4-ad8f-4d60-918e-003b4191a453" />
<img width="1040" height="640" alt="image" src="https://github.com/user-attachments/assets/85173d56-2fa7-4f8a-bb66-f8527cfd34e6" />


公开源码故意只保留脱敏占位符：

```python
DEFAULT_ACCOUNT_ROOT = r"E:\微信文件\xwechat_files\wxid_xxxxxx"
```

这不是可直接使用的目录。你必须把整行改成自己微信文件存储位置的盘和文件夹，或者每次运行时传入 `--account-root`。例如：

```bash
python3 ~/wechat_voice_extract.py \
  --account-root 'D:\微信文件\xwechat_files\wxid_xxxxx'
```

请同时替换 `D:`、中间的文件夹名称以及尖括号内容；“所有目录的地方都要改成自己的目录”包括 `--account-root`、`--dump`、`--output-dir`、`--decoder`、`--silk-decoder` 和任何复制命令中的源/目标路径。不要把真实微信 ID 写回公开仓库。

### 6. 首次运行前自检

按下面顺序逐项确认。Speex 和 Silk 按实际音频格式至少准备一个，不要求两个都安装：

```bash
python3 ~/wechat_voice_extract.py --help
# 如果目标是 Speex，检查这一项
test -x ~/wechat-speex-declib/bin/speex_decode
# 如果目标是 Silk V3，检查这一项（不需要时可跳过）
test -x ~/.local/bin/silk_v3_decoder
command -v ffmpeg && command -v ffprobe
```

然后确认脚本中没有忘记替换的路径占位符：

```bash
grep -n '/path/to\|<你的' ~/wechat_voice_extract.py
```

如果仍有输出，先按上一节逐项修改；这些占位符不能用于真实提取。公开源码可以保留 `YOUR_WECHAT_ID`，前提是每次运行都传入你自己的 `--account-root`；如果你改过源码默认值，也应确认其中没有真实账号 ID。最后确认 `--output-dir` 指向你有写入权限、且不会自动同步到云盘或公共目录的位置。

### 7. 更新脚本时的准备

更新前先保留当前可用版本：

```bash
cp ~/wechat_voice_extract.py ~/wechat_voice_extract.py.bak
```

复制新版本后重新赋予权限并做帮助检查：

```bash
cp /path/to/new/wechat_voice_extract.py ~/wechat_voice_extract.py
chmod 755 ~/wechat_voice_extract.py
python3 ~/wechat_voice_extract.py --help
```

确认新版本能正常启动后，再进行下一次语音提取；不要在正在分析 dump 时覆盖脚本文件。

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

请把 `/path/to/...` 改成你自己的解码器文件路径，不能原样复制。

## 填写你微信文件存储位置的盘和文件夹

源码中的默认值是脱敏占位符：

```python
DEFAULT_ACCOUNT_ROOT = r"E:\微信文件\xwechat_files\YOUR_WECHAT_ID"
```

请把这里的示例路径改成你自己的目录，也就是你微信文件存储位置的盘和文件夹，或每次运行时传入：

```bash
python3 wechat_voice_extract.py \
  --account-root 'D:\微信文件\xwechat_files\<你的微信文件夹>'
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
--dump PATH                改成你自己的 full dump 文件路径，跳过创建
--duration SECONDS         按目标总秒数筛选元数据
--output-dir PATH          改成你自己的导出文件保存目录
--account-root PATH        填写你微信文件存储位置的盘和文件夹
--decoder PATH             改成你自己的 Speex 解码器文件路径
--silk-decoder PATH        改成你自己的 Silk 解码器文件路径
--keep-dump                成功后仍保留脚本创建的 dump
--keep-raw                 成功后仍保留 raw 音频
--non-interactive          多候选时不询问，直接失败
```

其中凡是出现 `PATH`、`/path/to/...`、`D:\微信文件...` 或其他目录示例的地方，都必须替换成你自己的实际目录。

## 失败处理

### 没有 `Memory64ListStream`

通常表示创建的是普通小型 minidump，而不是 full dump。确认已点击 UAC，并重新播放后立即运行。脚本会在分析前检查这一点。

### 没有在你微信文件存储位置的盘和文件夹下找到元数据

确认 `--account-root` 已填写你微信文件存储位置的盘和文件夹，并确认收藏中的笔记语音刚刚完整播放过。

### 没有通过三项 MD5 校验的音频

最常见原因是 dump 建立得太晚、播放了其他语音，或选错了进程。重新播放收藏中的笔记语音并立即运行，不要关闭微信。失败时脚本创建的 dump 会保留供 `--dump` 重试；如果使用 `--dump`，请传入你自己的 dump 文件路径，使用完后应按精确路径删除。

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

