# CRAFTS `_T` 手动 RFI Mask 软件操作指南

## 1. 软件用途

本软件用于检查定标完成后的 CRAFTS `*_T.fits` 时频数据，并通过浏览器交互式绘制手动 RFI mask。数据和 mask 始终留在服务器上；本地 Mac 只通过 SSH 端口转发接收网页和压缩后的瀑布图，因此不需要 XQuartz，也不需要把 19 beam FITS 下载到本地。

一个 nfile 必须包含同一扫描、同一窗口和同一四位编号的 M01--M19：

```text
<scan>_arcdrift-M01_<window>_<nfile>_T.fits
...
<scan>_arcdrift-M19_<window>_<nfile>_T.fits
```

程序可显示：

- 19 beam 的逐像素 `nanmean`；所有 beam 都无效时该 pixel 显示为 NaN。
- M01--M19 任意一个 beam 的原始瀑布图。
- Pol0、Pol1 瀑布图及对应的时间平均频谱。
- OneChannel 与 science-mask-eligible SAT1380 的自动 RFI 并集。
- Mean 公共手动 mask 和每个 beam 的专属手动 mask。

Seeded RFI 没有作为 science RFI mask 加入，因为当前 pipeline 中它是 `FIT_ONLY`。

## 2. 推荐运行方式

### 2.1 当前 Dec+2654 数据的一键启动

在本地 Mac 的仓库目录运行：

```bash
cd /path/to/CRAFTS_rfi_mask_manual
./run_dec2654_native_ui.sh 0002 8997
```

- 第一个参数是 nfile，可写 `2` 或 `0002`。
- 第二个参数是本地和服务器转发端口。
- 页面会自动打开在 `http://127.0.0.1:8997/`。
- 19 beam 第一次载入通常需要几十秒到几分钟；右侧命令行日志会显示 M01--M19 的进度。
- 启动终端必须保持打开。完成工作后在该终端按 `Ctrl+C`，关闭 SSH 隧道和本次服务器 UI 进程。

如果端口被占用，换一个端口：

```bash
./run_dec2654_native_ui.sh 0002 8998
```

### 2.2 用于其他数据目录的通用启动

先在服务器启动程序：

```bash
cd /server/path/to/CRAFTS_rfi_mask_manual
python crafts_tod_native_web.py 'SCAN_NAME:0002' \
  --input-dir /server/path/to/T_fits_directory \
  --mask-dir /server/path/to/manual_mask_output \
  --one-channel-dir /server/path/to/one_channel_rfi_v2_products \
  --sat1380-dir /server/path/to/satellite_rfi_1380_products \
  --require-all-auto-masks \
  --block-records 64 \
  --address 127.0.0.1 \
  --port 8997
```

然后在本地 Mac 另开一个终端建立隧道：

```bash
ssh -N -L 8997:127.0.0.1:8997 crafts
```

浏览器打开 `http://127.0.0.1:8997/`。如果暂时允许缺少自动 mask，可去掉 `--require-all-auto-masks`；缺失的层会记录为 warning，而不会被伪装成零 mask。

主要 Python 依赖为 NumPy、Astropy、Pillow 和 Tornado；传统桌面界面还需要 Matplotlib。推荐 Python 3.10 或更高版本。

## 3. 界面快速上手

### 3.1 选择查看对象

顶部“查看”菜单有两种模式：

- `19-beam Mean`：显示 19 beam 平均。这里绘制的手动区域属于 Mean 公共层，保存时会应用到所有 beam。
- `Single beam`：旁边会出现 M01--M19 选择框。这里绘制的区域只属于当前 beam。

在不同 beam 之间切换不会丢失尚未保存的编辑；最终保存会一次写出全部 beam 的结果。

### 3.2 绘制和删除 mask

先选择编辑模式，再在 Pol0 或 Pol1 瀑布图中拖动：

- `频率 F`：mask 一段频率，覆盖全部时间记录。
- `时间 T`：mask 一段时间记录，覆盖全部频率。
- `矩形 B`：mask 一个局部时频矩形。
- `删除 D`：删除与拖动矩形相交的手动区域。
- `平移 G`：拖动当前视图。
- `双偏振`：勾选后，下一次编辑同时作用于 Pol0 和 Pol1。
- `撤销 U`：撤销最近一次编辑。
- `恢复已保存`：丢弃当前未保存修改，重新读取磁盘 JSON。

红色半透明区域是手动 mask。橙色区域是自动 RFI，只用于检查，不会写入 manual mask 产品。

### 3.3 自动 RFI 显示

- `应用自动 RFI`：打开时，瀑布图和 mean spectrum 使用应用 OneChannel ∪ SAT1380 后的有效样本；关闭时显示未应用自动 RFI 的数据。
- `显示自动 RFI`：只控制橙色覆盖层是否显示，不改变数据值。
- Mean 模式先对每个 beam 应用自己的自动 mask，再对剩余有效值做逐 pixel `nanmean`。

### 3.4 缩放、参考频率和图像清晰度

- 瀑布图或 mean spectrum 上 `Option + 滚轮`：以鼠标位置为中心缩放频率，三个面板保持频率对齐。
- 瀑布图上 `Shift + 滚轮`：缩放时间轴。
- 点击 mean spectrum：生成红色频率参考虚线，并同步显示在 Pol0、Pol1。
- `DPI − / DPI +`：在 0.5×--4× 间调整服务器图像分辨率。最高请求限制为 3200×1600；放大频率和时间窗口后更容易查看单个 pixel。
- `RFI − / RFI +`：调整橙色自动 RFI 覆盖层宽度，范围 1--9 pixels。

### 3.5 Mean Spectrum 的 Y 轴

“Y轴”下拉框提供：

- `强抗RFI`：默认，最不容易被极高 RFI 峰顶高 Y 轴。
- `抗RFI`：较宽的稳健范围。
- `99%数据`：显示当前频率窗口内中间 99% 的有效值。
- `完整范围`：显示完整最小值和最大值。

Y 轴会随当前可见频率窗口自动更新，Pol0 和 Pol1 共用同一范围。NaN 频段保持断开，不会跨过空段连直线。

### 3.6 手动调整瀑布图 colorbar

每个瀑布图的 colorbar 左侧有两个三角箭头：

- 蓝色箭头调整下限。
- 浅黄色箭头调整上限。

拖动箭头时，页面顶部显示当前上下限（K）；松开后服务器只重绘对应偏振。Pol0 和 Pol1 可独立调节。点击顶部 `色标自动` 可同时恢复两个偏振的自动范围。该操作只改变显示色阶，不改变 FITS、自动 mask 或手动 mask。

## 4. 推荐工作流程

1. 启动一个 nfile，等待状态变成 `ready`。
2. 在 Mean 模式分别开关“应用自动 RFI”和“显示自动 RFI”，检查自动处理结果。
3. 用 Option/Shift 滚轮定位异常时频区域；必要时调 DPI、Mean Spectrum Y 轴和 colorbar。
4. 在 Mean 模式绘制所有 beam 都应应用的公共区域。
5. 切换到 Single beam，检查 M01--M19；只在异常 beam 上绘制专属区域。
6. 点击 `保存 S`。确认框会列出 Mean 和每个 beam 在 Pol0/Pol1 中的区域数量。
7. 核对后点击“确认并保存”。取消不会写磁盘。
8. 保存完成后再切换到上一个或下一个 nfile。

## 5. 保存逻辑和文件命名

保存目录由 `--mask-dir` 指定。每个 nfile 写出一个 JSON 和 19 个逐 beam NPZ。

JSON 示例：

```text
Dec+2654_22_09_W_0002_T_manual_mask.json
```

逐 beam NPZ 示例：

```text
Dec+2654_22_09_arcdrift-M01_W_0002_T_manual_rfi_mask.npz
...
Dec+2654_22_09_arcdrift-M19_W_0002_T_manual_rfi_mask.npz
```

每个 beam 的最终手动 mask 为：

```text
Mean 公共层 OR 当前 beam 专属层
```

NPZ 中 `mask` 的轴顺序为 `(time, frequency, polarization)`，值定义为：

- `0`：保留。
- `1`：手动判定的 science-bad RFI。

NPZ 只保存 manual mask，不把 OneChannel 或 SAT1380 复制进去。后续 pipeline 在使用时再组合这些独立层。即使某个 nfile 完全干净，仍会生成全零逐 beam 产品并写入 `reviewed_clean=True`，以区别“已经人工检查且干净”和“从未检查/文件缺失”。

## 6. 常见问题

### 页面打开但按钮暂时没有响应

先看右侧日志是否仍在载入 M01--M19。数据载入完成前部分操作会禁用。应使用 `run_dec2654_native_ui.sh`；`run_dec2654_web_ui.sh` 是旧 WebAgg 入口，XQuartz 版本也更慢。

### Pol1 一直显示正在载入

检查右侧是否有具体 beam 或自动 mask 报错。使用 `--require-all-auto-masks` 时，任一所需产品缺失或坐标不一致都会明确报错。

### 页面显示旧按钮

刷新浏览器。服务器在启动时读取 HTML，因此更新代码后需要在没有未保存编辑时重启 UI 服务。

### 图像响应较慢

先把 DPI 降到 1× 或更低；定位到目标区域后再提高 DPI。Single beam 首次切换会从服务器读取该 beam，之后交互会更快。

### 端口被占用

换一个本地和服务器都空闲的端口，例如 8998。不要直接结束不确定归属的服务器进程。

## 7. 测试

在仓库目录运行：

```bash
python -m pytest -q
```

测试覆盖 mask 归一化、旧 JSON 兼容、Mean/Single 保存并集、自动 RFI 显示、NaN 处理、图像范围、colorbar 自定义范围及保存确认界面。

## 8. 更新记录

### 2026-09-16 网页视口导航与 beam 对比优化

- 瀑布图和频谱支持以指针为中心的缩放：`Option/Alt + 滚轮` 缩放频率，`Shift + 滚轮` 缩放 Record 轴；`Option/Alt + 左拖`、`Shift + 左拖` 或两个修饰键同时按住左拖可框选放大对应轴。`显示设置 → 频率放大 / 时间放大` 提供免修饰键的等价工具。缩放操作只改变查看范围，不会编辑 mask。
- 新增 `返回视图`（回到上一次缩放或平移范围）与 `重置视图 0`（恢复全频率、全 Record 范围）；两者均不影响手动 mask。
- 导航过程中页面先在本地预览已缓存的图像，并把连续操作合并为最新视口的请求；标记和删除需等待清晰图像（`清晰视图`）到位后进行，避免对过期视图误标。
- Single 模式频谱中，当前 beam 以实线绘制，同时保留 48% 不透明度的 19-beam Mean 淡虚线参考。参考谱仍为 `global_manual_only`：不合并各 beam 专属手动层，"应用自动 RFI"开关对两者同时生效。谱线绘制保留每个像素区间的极大/极小值和 NaN 断点。
- 布局可调：`显示设置 → 频谱高度` 调整频谱占比（默认 24%，范围 18%–40%，两幅瀑布图保持等高）；任务/日志侧栏可通过分隔条调宽，也可从顶栏整体折叠，相关偏好会保存在当前浏览器中。
- 自动 RFI 覆盖层的池化顺序改为先 Record 后频率，CRAFTS 量级输入下单张视图渲染时间约降低 23%–35%。
