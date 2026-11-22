接上外置声卡之后，录进去的吉他总比画面慢半口气，越录越偏：现有实现有十处隐蔽缺陷——`find_pulse` 用 `>=` 判定且阈值没按块峰值归一，安静段的零值会误触发；`roundtrip_delay` 不把往返值除以 2、直接当单程用，补偿量翻倍；`roundtrip_delay` 用 `len(received)` 代替检测到的脉冲位置，延迟恒等于信号长度；`drift_estimate` 用首尾差除以测量次数而不是时间间隔，换算出的 ppm 偏差可达 100 倍；`compensate` 只把零补在尾部，延迟根本没有被抵消；`compensate` 的输出长度随延迟变化，后续对齐全部失效；`clock_offset` 用线性差而不是相对比例，ppm 量级错；`stability` 用绝对标准差而不是相对值，延迟量级或采样率一变就不可比；`validate_measure` 不检查离群值，单次误测污染平均；`calibrate_batch` 复用上一台设备的偏移量，第二台设备拿到第一台的延迟。calib/measure.py 是核心，tests/test_measure.py 15个红。要修的是：按块峰值归一且严格大于阈值、往返测量折半成单程、用检测到的脉冲索引作差、按秒级时间间隔求斜率、正延迟左移并在尾部补零、任何延迟都保持输入长度、按相对比例换算 ppm、用相对离散度并截断到 0..1、检测负值/离群/非法采样率、逐设备独立计算。

## 运行方式

```powershell
python -m pytest tests -q
```

初始状态是刻意的失败环境：`calib/measure.py` 的 8 个函数全部 `raise NotImplementedError`，测试应当恰好收集 15 条并全部失败，输出 `15 failed`，没有 collection/import/syntax error。完成实现后应为 `15 passed`；不要通过删改测试来变绿。

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 游戏、图形与多媒体 |
| 子方向 | 音视频处理 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## 契约摘要

| 函数 | 契约 |
| --- | --- |
| `find_pulse(samples, threshold)` | 阈值按块峰值归一为 `threshold * max(abs(x))`，返回首个严格大于有效阈值的索引；找不到或全零块返回 `-1`。 |
| `roundtrip_delay(sent, received, threshold, sample_rate)` | 用两块中检测到的脉冲索引差计算往返，再折半为单程毫秒数；误差不超过 1 个样本。 |
| `drift_estimate(offsets, sample_rate)` | `offsets` 每秒一项，返回首尾斜率（样本/秒），分母是时间间隔 `n - 1`。 |
| `compensate(samples, delay_samples)` | 正延迟左移、负延迟右移，前补零或后补零，输出长度恒等于输入。 |
| `clock_offset(delays, nominal)` | 返回 `(mean(delays) / nominal - 1) * 1e6` ppm，而不是毫秒线性差。 |
| `stability(delays)` | 总体标准差除以平均延迟的绝对值，截断到 `[0, 1]`；单样本为 0。 |
| `validate_measure(delays, sample_rate)` | 报告非法采样率、负延迟和基于中位数/MAD 的离群值；干净数据返回 `[]`。 |
| `calibrate_batch(devices, config)` | 对每台设备独立计算均值、ppm、稳定性和问题列表，禁止跨设备复用中间量。 |

## 测试不可修改

`tests/test_measure.py` 是验收面，15 条测试覆盖上述缺陷与 1 个样本容差，不得修改、跳过或放宽。只允许修改 `calib/measure.py`（必要时可调整 `calib/__init__.py` 的导出）。所有实现必须保持离线、确定性、无随机数、无 `sleep`，且只使用标准库。
