# screenfile

`screenfile` 是一个 Python 小工具，用来把文件编码成适合屏幕播放的视频，再从录下来的视频里恢复原文件。

它的设计目标不是“视频播放”，而是“视觉数据传输”：
- 发送端把文件切片成数据包
- 每个数据包渲染成高对比度黑白帧
- 接收端从录屏或拍屏视频中逐帧识别、去重、重组
- 只有校验通过才写回最终文件

首版重点是“恢复成功率优先”，尤其针对“手机拍另一台显示器”的场景。

## 安装

建议使用虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

安装后也可以直接使用命令：

```bash
screenfile --help
```

如果你只想直接运行当前目录代码，也可以：

```bash
source .venv/bin/activate
python -m pip install numpy opencv-python-headless
```

## 用法

### 1. 把文件编码成视频

```bash
python -m screenfile encode ./input.bin ./transfer.mp4
```

可选参数：

```bash
python -m screenfile encode ./input.bin ./transfer.mp4 \
  --chunk-size 640 \
  --repeat 3 \
  --fps 8
```

参数说明：
- `--chunk-size`：每个分片的原始字节数。更小更稳，但视频更长。
- `--repeat`：整轮重复播放次数。更大更稳，但视频更长。
- `--fps`：写出视频帧率。建议保守使用 `6-12`。

编码时会输出：
- 原文件大小
- 分片数量
- 重复轮数
- 预计视频时长
- 播放建议

### 2. 从录下来的视频恢复文件

```bash
python -m screenfile decode ./transfer.mp4 ./restored.bin
```

解码时会输出：
- 已恢复的唯一分片数
- 扫描帧数
- 重复分片数
- 未识别四边形帧数
- 帧级 CRC 失败数
- 数据包解析失败数

如果分片没有收齐，程序会直接报错并指出缺失区间，不会写出损坏文件。

## Demo 脚本

如果你想快速验证整条链路，可以直接运行示例脚本：

```bash
source .venv/bin/activate
python scripts/demo_roundtrip.py ./demo-output
```

它会自动生成：
- 一个演示源文件
- 一个编码后的视频
- 一个解码恢复出来的文件

可选参数：

```bash
python scripts/demo_roundtrip.py ./demo-output \
  --payload-size 65536 \
  --repeat 3 \
  --fps 8
```

## 打包成可执行程序

这个项目已经加了可执行打包入口，默认使用 `PyInstaller` 生成单文件可执行程序。

先安装打包依赖：

```bash
source .venv/bin/activate
python -m pip install -e ".[build]"
```

然后执行打包：

```bash
source .venv/bin/activate
python scripts/build_executable.py
```

生成结果默认在：

```bash
./dist/screenfile
```

构建完成后可直接运行：

```bash
./dist/screenfile --help
./dist/screenfile encode ./input.bin ./transfer.mp4
./dist/screenfile decode ./transfer.mp4 ./restored.bin
```

说明：
- 当前打包结果是“按当前系统构建当前系统可执行文件”，不是跨平台通用二进制。
- 如果你在 macOS 上构建，产物就是 macOS 可执行程序；Windows 需要在 Windows 上构建。
- `opencv-python-headless` 已适合命令行打包，不依赖图形界面窗口。

### Win10 可执行文件

`PyInstaller` 官方不支持“在 macOS 直接打出 Windows `.exe`”。如果你要 Win10 可执行文件，需要在 Windows 10 环境里构建。

项目里已经补了两个 Windows 打包脚本：
- `scripts/build_windows.ps1`
- `scripts/build_windows.bat`

在 Win10 上推荐用 PowerShell：

```powershell
cd path\to\screenfile
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
```

或者用 `cmd`：

```bat
cd path\to\screenfile
scripts\build_windows.bat
```

构建完成后，产物默认在：

```text
dist\screenfile.exe
```

如果你没有现成的 Windows 机器，最稳的选择是：
- Windows 10 真机
- Windows 虚拟机
- GitHub Actions 的 `windows-latest` Runner

## GitHub Actions 自动打包

项目已经带了一个跨平台构建工作流：

[`/.github/workflows/build-binaries.yml`](/Users/madongyu/Documents/Codex/2026-05-26/python/.github/workflows/build-binaries.yml)

它会在以下平台分别构建对应原生可执行文件：
- `ubuntu-latest` -> Linux 可执行文件
- `macos-latest` -> macOS 可执行文件
- `windows-latest` -> Windows `screenfile.exe`

触发方式：
- 手动触发 `workflow_dispatch`
- 推送形如 `v1.0.0` 的 tag

工作流会自动：
- 安装依赖
- 跑测试
- 用 `PyInstaller` 打包
- 上传产物为 GitHub Actions artifacts

产物名称：
- `screenfile-linux-x64.zip`
- `screenfile-macos.zip`
- `screenfile-windows-x64.zip`

注意：
- 这是“同一仓库自动构建全平台产物”，不是“单台 mac 直接本地交叉编译所有平台”。
- 每个平台仍然是在各自的原生 runner 上构建，这也是最稳的方案。

## GitHub Release 自动发布

如果你希望打 tag 之后自动把全平台压缩包挂到 GitHub Release，上面的构建工作流之外，项目还带了：

[`/.github/workflows/release-binaries.yml`](/Users/madongyu/Documents/Codex/2026-05-26/python/.github/workflows/release-binaries.yml)

触发条件：
- 推送 tag，例如 `v1.0.0`

它会在 Linux、macOS、Windows 各自 runner 上：
- 安装依赖
- 跑测试
- 打包可执行文件
- 压缩成命名清晰的 zip
- 自动附加到对应 GitHub Release

如果仓库里还没有这个 tag 的 Release，`softprops/action-gh-release` 会自动创建它。

## 工作原理

每个数据包包含：
- 协议版本
- 文件 ID
- 文件名摘要
- 文件总大小
- 文件 SHA-256
- 分片序号
- 总分片数
- 当前分片 CRC32
- 分片 payload

每一帧包含：
- 外层黑色边框
- 四角定位标记
- 中央黑白网格数据区
- 帧内可见分片序号文本

解码流程：
1. 从视频读取一帧
2. 找到最大四边形候选区域
3. 做透视校正
4. 二值化并按网格采样
5. 还原数据包并校验 CRC
6. 按分片序号去重
7. 分片收齐后重组并校验 SHA-256

## 推荐拍摄方式

为了提高恢复成功率，播放端建议：
- 全屏播放
- 屏幕亮度拉高
- 关闭窗口动画和弹窗
- 保持画面静止，不要滚动或切换

拍摄端建议：
- 尽量正对屏幕
- 避免强反光
- 让编码区域完整入镜
- 尽量保持对焦稳定

## 已知边界

这个版本是可靠概念验证，不是极限吞吐方案。

当前不包含：
- 音频通道传输
- 多区域并行编码
- 真正的前向纠错库集成
- 多视频合并补片
- 图形界面

如果目标是更大的文件或更短的视频时长，下一步通常会做：
- 更高密度但仍可拍摄的码型
- 前向纠错
- 更智能的重排与重复策略
- 多区域并行传输

## 测试

运行全部测试：

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
```

当前测试覆盖：
- 数据包序列化/反序列化
- 分片恢复
- 单帧编码/解码
- 轻度与更强的拍屏式图像劣化
- 视频端到端恢复
- 丢帧导致的缺片失败路径
- demo 脚本端到端生成与恢复
- CLI 显式 argv 调用
