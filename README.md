# screenfile

`screenfile` 是一个 Python 小工具，用来把文件编码成适合屏幕播放的视频，再从录下来的视频里恢复原文件。

它的设计目标不是“视频播放”，而是“视觉数据传输”：
- 发送端把文件切片成数据包
- 每个数据包渲染成高对比度视觉帧，支持黑白矩阵和 4 色彩色模式
- 每帧现在带有 Reed-Solomon 帧级纠错，能更好地抵抗拍屏时的局部误码
- 接收端从录屏或拍屏视频中自动识别码区、透视校正、逐帧识别、去重、投票恢复、重组
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

### 命令总览

```bash
python -m screenfile encode <input_file> <output_video> [options]
python -m screenfile decode <input_video> <output_file>
python -m screenfile estimate <input_file> [options]
```

### 1. 把文件编码成视频

```bash
python -m screenfile encode ./input.bin ./transfer.mp4
```

可选参数：

```bash
python -m screenfile encode ./input.bin ./transfer.mp4 \
  --chunk-size 640 \
  --repeat 3 \
  --fps 8 \
  --compression zstd \
  --mode matrix
```

默认行为：
- 先打印当前压缩方案的预计大小和预计时长
- 再询问你是否继续生成视频
- 每一帧左上角会显示 `screenfile` 版本号和布局标识，右上角会显示缩小后的 `chunk x/y`

如果你要跳过确认，直接生成：

```bash
python -m screenfile encode ./input.bin ./transfer.mp4 --yes
```

#### `encode` 入参说明

`encode` 必填参数：

- `input_file`
  - 含义：要编码成视频的源文件路径
  - 默认值：无，必填
- `output_video`
  - 含义：输出视频路径
  - 默认值：无，必填
  - 推荐：优先试 `.avi`，当前通常比 `.mp4` 更稳

`encode` 可选参数：

- `--chunk-size`
  - 含义：每个视频数据分片携带的 payload 字节数
  - 默认值：`640`
  - 当前上限：`1032`
  - 推荐范围：`512` 到 `768`
  - 推荐起点：`640`
  - 使用建议：
    - `512`：更稳，适合手机拍屏排查
    - `640`：默认平衡值
    - `768`：拍摄条件较好时可试，仍在低密度布局的建议范围内
    - `960`：偏激进，视频更短，但更容易拍屏失败
    - `1032`：当前默认格式极限值，更适合系统录屏或非常稳定的拍摄环境

- `--repeat`
  - 含义：整轮分片重复播放的次数
  - 默认值：`3`
  - 推荐范围：`2` 到 `3`
  - 推荐起点：`2`
  - 使用建议：
    - `3`：更稳，但时长直接乘 3
    - `2`：通常能明显缩短时长，适合拍摄条件较好时
    - `1`：仅适合非常理想的测试环境，不建议默认使用

- `--fps`
  - 含义：输出视频帧率
  - 默认值：`8`
  - 黑白矩阵推荐范围：`6` 到 `12`
  - 彩色模式推荐范围：`30` 到 `60`
  - 黑白矩阵推荐起点：`8`
  - 彩色模式推荐起点：`60`
  - 使用建议：
    - `6`：黑白矩阵更保守，更稳
    - `8`：黑白矩阵默认平衡值
    - `10` 到 `12`：黑白矩阵想进一步缩短时长时可试
    - `30`：彩色模式较稳，但视频更长
    - `60`：彩色模式推荐值，手机通常会以 60fps 或 120fps 录到足够多的重复帧
    - 更高 fps 不一定更好，可能让播放端/录制端产生更多压缩和滚动快门问题

- `--compression`
  - 含义：视频分片前先对原文件做的预压缩算法
  - 默认值：`zstd`
  - 可选值：`none`、`gzip`、`zstd`
  - 推荐值：`zstd`
  - 使用建议：
    - `zstd`：首选。压缩率和速度通常最均衡
    - `gzip`：兼容型备选，通常不如 `zstd`
    - `none`：仅在文件本身已高度压缩、或你想做最原始对比时使用

- `--mode`
  - 含义：视觉编码模式
  - 默认值：`matrix`
  - 可选值：`matrix`、`color`
  - 使用建议：
    - `matrix`：默认黑白矩阵模式，兼容当前最完整的定位和纠错逻辑
    - `color`：彩色 4 色模式，每个单元承载 2 bit；单元更大，适合提高单帧数据量，但对屏幕色彩、反光和相机压缩更敏感
  - 彩色模式建议：
    - 推荐搭配 `--chunk-size 768 --repeat 3 --fps 60 --compression none`
    - 如果拍屏环境不稳定，可以降到 `--chunk-size 640` 或把 `--repeat` 提到 `4`
    - 如果文件本身可压缩，仍可以试 `--compression zstd`
    - 如果文件本身已经是 zip/mp4/png 等高度压缩格式，`--compression none` 更利于估算和调试

- `-y` / `--yes`
  - 含义：跳过“先评估再确认”的交互步骤，直接生成视频
  - 默认值：关闭
  - 推荐：脚本化调用或批处理时开启；手动调参时关闭

帧内辅助标记：

- 左上角
  - 内容：`screenfile <version> layout=v3`
  - 作用：方便确认编码器版本和帧布局是否一致
- 右上角
  - 内容：`chunk x/y`
  - 作用：方便肉眼定位当前分片进度
- 布局原则
  - 这两块信息都放在码框外侧留白区，默认不会压到主体码区

帧布局：
- 默认使用 `layout=v3`
- `layout=v3` 会把码区扩展到约 90% 屏幕宽度，使用 `144x72` 的低密度横向矩形数据网格
- 相比 `layout=v2` 的 `160x80` 网格，v3 的单格更大，牺牲一部分单帧极限容量，换取更稳定的手机拍屏识别
- 彩色模式使用 `color-v1`，数据网格为 `96x54`，每个单元使用 4 种高对比颜色承载 2 bit，单元尺寸为 `16px`
- `color-v1` 当前仍属于实验模式：实测已经可以从 iPhone 拍屏视频恢复文件，但解码速度和候选识别仍在优化中
- 解码端仍会尝试读取旧的 `layout=v2` 和 `layout=v1` 视频

#### `encode` 推荐配置

按拍摄稳定性从稳到快，大致可以这样用：

- 稳妥首选
  - `--chunk-size 512 --repeat 4 --fps 6 --compression zstd`
- 推荐平衡值
  - `--chunk-size 640 --repeat 3 --fps 8 --compression zstd`
- 偏激进缩时长
  - `--chunk-size 768 --repeat 3 --fps 8 --compression zstd`
- 彩色模式试验
  - `--mode color --chunk-size 768 --repeat 3 --fps 60 --compression none`
- 彩色模式更稳一点
  - `--mode color --chunk-size 640 --repeat 4 --fps 60 --compression none`

编码时会输出：
- 压缩算法
- 原文件大小
  - 显示格式示例：`12.11 KB (12,397 B)`
- 压缩后大小
  - 显示格式示例：`5.08 KB (5,209 B)`
- 压缩率
- 分片数量
- 总帧数
- 重复轮数
- 预计视频时长
- 预计视频大小
- 播放建议

当前版本补充说明：
- 新增帧级 Reed-Solomon 纠错，对少量局部误码更稳
- 预计视频大小改成基于多帧真实内容采样，结果比旧版更接近真实输出
- 新增彩色 `color-v1` 模式，单个色块承载 2 bit，目标是在不继续缩小单元格的情况下提高单帧数据量
- 解码端新增彩色采样偏移/缩放变体，用来兼容手机拍屏后的轻微透视误差、码区裁切偏差和网格错位
- 解码端新增 bit voting 和 temporal bit voting，遇到多帧重复但单帧 CRC 失败时，会尝试用多帧投票补回 packet

### 2. 先做参数评估

如果你想在真正生成视频前先对比 `none/gzip/zstd` 三种模式的预计效果：

```bash
python -m screenfile estimate ./input.bin
```

也可以带参数一起估算：

```bash
python -m screenfile estimate ./input.bin \
  --chunk-size 768 \
  --repeat 2 \
  --fps 10 \
  --mode color
```

#### `estimate` 入参说明

- `input_file`
  - 含义：待评估的源文件路径
  - 默认值：无，必填

- `--chunk-size`
  - 含义：用于估算的视频分片大小
  - 默认值：`640`
  - 当前上限：`1032`
  - 推荐范围：`512` 到 `768`

- `--repeat`
  - 含义：用于估算的重复轮数
  - 默认值：`3`
  - 推荐范围：`2` 到 `3`

- `--fps`
  - 含义：用于估算的目标视频帧率
  - 默认值：`8`
  - 黑白矩阵推荐范围：`6` 到 `12`
  - 彩色模式推荐范围：`30` 到 `60`

- `--mode`
  - 含义：用于估算的视觉编码模式
  - 默认值：`matrix`
  - 可选值：`matrix`、`color`

它会分别输出每种压缩模式下的：
- 原文件大小
- 压缩后大小
- 压缩率
- 分片数
- 总帧数
- 预计视频时长
- 预计视频大小

关于“预计视频大小”：
- 会基于当前编码器先生成一段由多帧真实内容组成的采样视频，再按总帧数折算
- 默认按更适合阅读的单位显示：`MB` 或 `GB`
- `encode` 会按你的实际输出后缀来估算；`estimate` 当前按 `.mp4` 口径估算
- 这是近似值；不同平台和不同 OpenCV/系统编解码后端之间仍可能有明显差异，Windows 上尤其可能偏大

### 3. 从录下来的视频恢复文件

```bash
python -m screenfile decode ./transfer.mp4 ./restored.bin
```

如果想手动控制并行解码线程数：

```bash
python -m screenfile decode ./transfer.mp4 ./restored.bin --workers 8
```

如果要导出失败帧诊断图：

```bash
python -m screenfile decode ./transfer.mp4 ./restored.bin \
  --debug-dir ./debug-frames \
  --debug-limit 12
```

也可以查看当前程序版本：

```bash
python -m screenfile --version
```

#### `decode` 入参说明

- `input_video`
  - 含义：录制得到的视频文件
  - 默认值：无，必填

- `output_file`
  - 含义：恢复出的目标文件路径
  - 默认值：无，必填

- `--workers`
  - 含义：并行解码视频帧的 worker 数量
  - 默认值：自动，最多使用 `8` 个 worker
  - 推荐范围：`4` 到 `10`
  - 使用建议：CPU 核心多时可以提高；如果机器发热或占用太高，可以手动降到 `4`
  - 说明：worker 数量只影响逐帧识别并行度，不会改变解码结果

- `--debug-dir`
  - 含义：把失败帧的原图、码区裁剪图和二值化图写到指定目录
  - 默认值：关闭
  - 使用建议：只在排查解码失败时开启

- `--debug-limit`
  - 含义：最多导出多少个失败帧样本
  - 默认值：`12`
  - 推荐范围：`8` 到 `30`
  - 使用建议：排查普通失败时 `12` 足够；需要观察更多时间点时可提高

解码时会输出：
- 已恢复的唯一分片数
- 扫描帧数
- 彩色快速路径采样帧数
- 重复分片数
- 未识别四边形帧数
- 帧级 CRC 失败数
- 数据包解析失败数
- bit voting 额外恢复的分片数
- 自动检测到的有效数据帧区间

如果分片没有收齐，程序会直接报错并指出缺失区间，不会写出损坏文件。

当前解码能力：
- 支持 `.mp4`、`.mov` 等 OpenCV 能读取的视频容器，实际取决于本机 OpenCV/系统解码器
- 支持视频前后包含无关画面，会根据有效 packet 自动判断数据区间
- 支持非全屏拍摄，会自动在画面中寻找码区并做透视校正
- 支持轻微歪斜、缩放、亮度变化和部分采样偏移
- 支持旧版 `layout=v1`、`layout=v2`、当前默认 `layout=v3` 和实验性 `color-v1`
- 对彩色视频，会额外尝试小范围数据网格偏移/缩放，改善手机拍屏造成的采样错位
- 对彩色视频，会优先尝试 fast color decode：先找首个有效 packet，再按总 chunk 数预测每个 chunk 的时间位置，只采样关键帧和邻近偏移帧，成功时不再做慢速全帧扫描
- 对重复帧，会尝试 bit voting 和 temporal bit voting，减少单帧局部误码导致的失败

当前限制：
- 如果码区被浮层、宠物、反光或窗口遮挡到实际数据区域，仍可能无法恢复
- 彩色模式解码仍比黑白模式慢，尤其是手机拍屏视频需要尝试多个候选区域时
- fast color decode 依赖“chunk 基本按顺序播放、每个 chunk 连续重复”的当前编码策略；如果视频被剪辑、变速或严重丢帧，可能会 fallback 到慢速扫描
- 实测中，一个 `460KB`、`614` 个 chunk、约 `32s` 的彩色 iPhone 拍屏视频已经可以完整恢复；更大文件仍建议先用小样本验证拍摄环境

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

#### `demo_roundtrip.py` 入参说明

- `output_dir`
  - 含义：演示文件输出目录
  - 默认值：`demo-output`

- `--payload-size`
  - 含义：自动生成的演示源文件大小
  - 默认值：`65536`
  - 推荐范围：`32768` 到 `262144`

- `--repeat`
  - 含义：演示视频使用的重复轮数
  - 默认值：`3`
  - 推荐范围：`2` 到 `3`

- `--fps`
  - 含义：演示视频使用的帧率
  - 默认值：`8`
  - 推荐范围：`6` 到 `10`

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
- `screenfile-v0.3.0-linux-x64.zip`
- `screenfile-v0.3.0-macos-arm64.zip`
- `screenfile-v0.3.0-windows-x64.zip`

命名规则：
- `screenfile-v<version>-<os>-<arch>.zip`
- 其中 `<version>` 来自当前项目版本号或 release tag，例如 `v0.3.0`
- `<arch>` 当前默认是 `linux-x64`、`macos-arm64`、`windows-x64`

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
- 中央数据网格区，黑白模式为二值矩阵，彩色模式为 4 色矩阵
- 帧内可见分片序号文本

解码流程：
1. 从视频读取一帧
2. 自动寻找可能的码区四边形候选区域
3. 做透视校正
4. 按布局采样；黑白模式做二值化，彩色模式做颜色分类并尝试小范围采样偏移
5. 还原数据包并校验 CRC
6. 对重复帧和失败帧尝试 bit voting / temporal bit voting
7. 按分片序号去重
8. 分片收齐后重组并校验 SHA-256

彩色快速解码流程：
1. 先从视频开头少量帧里寻找首个有效 `color-v1` packet
2. 读取 packet 里的 `total_chunks`
3. 根据总帧数和 chunk 数预测每个 chunk 的中心帧
4. 对每个中心帧额外取 `-2/+2/-4/+4/-8/+8` 等偏移帧
5. 每帧只尝试少量最像 `color-v1` 码区的候选区域
6. 如果全部 chunk 收齐，直接写出恢复文件
7. 如果没收齐，再回退到完整逐帧扫描和投票恢复

在进入视频分片前，当前版本会先把原始文件包装成一个传输 payload，其中包含：
- 原始文件名
- 原始文件大小
- 原始文件 SHA-256
- 压缩算法标记
- 压缩后的字节流

这样接收端在视频解码完成后可以自动解压并恢复原始文件内容。

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
- 彩色模式下尽量避免开启会改变颜色的护眼模式、夜览模式或过强 HDR 显示效果
- 如果使用 iPhone 拍摄，`60fps` 输出通常可以被稳定记录；部分场景下视频元数据可能显示接近 `120fps`，这是正常的高帧率录制结果

## 已知边界

这个版本是可靠概念验证，不是极限吞吐方案。

当前不包含：
- 音频通道传输
- 多区域并行编码
- 跨 chunk 的前向纠错
- 多视频合并补片
- 图形界面

如果目标是更大的文件或更短的视频时长，下一步通常会做：
- 更高密度但仍可拍摄的码型
- 跨 chunk 前向纠错
- 更智能的重排与重复策略
- 多区域并行传输
- 进一步优化 fast color decode，减少大文件彩色视频的解码时间

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
- 彩色 `color-v1` 帧编码/解码
- 彩色网格偏移解码
- bit voting 与 temporal bit voting
- demo 脚本端到端生成与恢复
- CLI 显式 argv 调用
