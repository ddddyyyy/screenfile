# screenfile

`screenfile` 是一个 Python 小工具，用来把文件编码成适合屏幕播放的视频，再从录下来的视频里恢复原文件。

它的设计目标不是“视频播放”，而是“视觉数据传输”：
- 发送端把文件切片成数据包
- 每个数据包渲染成高对比度黑白帧
- 每帧现在带有 Reed-Solomon 帧级纠错，能更好地抵抗拍屏时的局部误码
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
  --compression zstd
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
  - 推荐范围：`640` 到 `896`
  - 推荐起点：`768`
  - 使用建议：
    - `640`：更稳，视频更长
    - `768`：通常是比较好的平衡点
    - `896`：更激进，视频更短，但更容易拍屏失败
    - `1024+`：更偏实验参数，不建议一开始就用

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
  - 推荐范围：`6` 到 `12`
  - 推荐起点：`8`
  - 使用建议：
    - `6`：更保守，更稳
    - `8`：默认平衡值
    - `10`：想进一步缩短时长时可试
    - `12`：偏激进，拍屏环境不好时更容易掉帧或模糊

- `--compression`
  - 含义：视频分片前先对原文件做的预压缩算法
  - 默认值：`zstd`
  - 可选值：`none`、`gzip`、`zstd`
  - 推荐值：`zstd`
  - 使用建议：
    - `zstd`：首选。压缩率和速度通常最均衡
    - `gzip`：兼容型备选，通常不如 `zstd`
    - `none`：仅在文件本身已高度压缩、或你想做最原始对比时使用

- `-y` / `--yes`
  - 含义：跳过“先评估再确认”的交互步骤，直接生成视频
  - 默认值：关闭
  - 推荐：脚本化调用或批处理时开启；手动调参时关闭

帧内辅助标记：

- 左上角
  - 内容：`screenfile <version> layout=v1`
  - 作用：方便确认编码器版本和帧布局是否一致
- 右上角
  - 内容：`chunk x/y`
  - 作用：方便肉眼定位当前分片进度
- 布局原则
  - 这两块信息都放在码框外侧留白区，默认不会压到主体码区

#### `encode` 推荐配置

按拍摄稳定性从稳到快，大致可以这样用：

- 稳妥首选
  - `--chunk-size 640 --repeat 3 --fps 8 --compression zstd`
- 推荐平衡值
  - `--chunk-size 768 --repeat 2 --fps 8 --compression zstd`
- 偏激进缩时长
  - `--chunk-size 896 --repeat 2 --fps 10 --compression zstd`

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

### 3. 先做参数评估

如果你想在真正生成视频前先对比 `none/gzip/zstd` 三种模式的预计效果：

```bash
python -m screenfile estimate ./input.bin
```

也可以带参数一起估算：

```bash
python -m screenfile estimate ./input.bin \
  --chunk-size 768 \
  --repeat 2 \
  --fps 10
```

#### `estimate` 入参说明

- `input_file`
  - 含义：待评估的源文件路径
  - 默认值：无，必填

- `--chunk-size`
  - 含义：用于估算的视频分片大小
  - 默认值：`640`
  - 推荐范围：`640` 到 `896`

- `--repeat`
  - 含义：用于估算的重复轮数
  - 默认值：`3`
  - 推荐范围：`2` 到 `3`

- `--fps`
  - 含义：用于估算的目标视频帧率
  - 默认值：`8`
  - 推荐范围：`6` 到 `12`

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

### 2. 从录下来的视频恢复文件

```bash
python -m screenfile decode ./transfer.mp4 ./restored.bin
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
