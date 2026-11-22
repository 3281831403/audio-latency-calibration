"""外置声卡往返延迟测量与录音补偿工具。

坐标约定
--------
* 样本索引 ``i`` 对应时刻 ``i / sample_rate`` 秒。
* 除特别说明外，延迟单位均为毫秒（ms）。
* 脉冲检测使用「块内相对阈值」：阈值按当前块的峰值归一，避免固定阈值
  在安静块和响亮块上表现不一致。

本模块只依赖标准库，所有函数都是确定性的。
"""

from __future__ import annotations

import math

__all__ = [
    "find_pulse",
    "roundtrip_delay",
    "drift_estimate",
    "compensate",
    "clock_offset",
    "stability",
    "validate_measure",
    "calibrate_batch",
]


def find_pulse(samples, threshold):
    """返回块内第一个越过阈值的样本索引。

    参数
    ----
    samples : sequence[float]
        一块音频样本。块内峰值取 ``max(abs(x) for x in samples)``，
        空块视为峰值为 0。
    threshold : float
        相对阈值，必须落在 ``[0.0, 1.0]``。有效阈值为
        ``threshold * peak``；这样同一个阈值可以用于不同增益的块。

    返回
    ----
    int
        第一个满足 ``abs(samples[i]) > threshold * peak`` 的索引；
        恰好等于有效阈值的样本不算「越过」。没有样本越过时返回 ``-1``。
        峰值为 0 的块（全零或空块）也返回 ``-1``，即使 ``threshold`` 为 0。

    异常
    ----
    ValueError
        当 ``threshold`` 不在 ``[0.0, 1.0]`` 内时抛出。

    容差
    ----
    本函数按样本精确定位，不做插值；下游允许 1 个样本的误差。
    """
    if not (0.0 <= threshold <= 1.0):
        raise ValueError("threshold must be within [0.0, 1.0]")

    peak = 0.0
    for x in samples:
        magnitude = abs(x)
        if magnitude > peak:
            peak = magnitude

    # 全零块（含空块）：有效阈值为 0，严格大于判定下任何零值都不算越过。
    if peak == 0.0:
        return -1

    effective = threshold * peak
    for i, x in enumerate(samples):
        if abs(x) > effective:
            return i
    return -1


def roundtrip_delay(sent, received, threshold, sample_rate):
    """从一次回环录音估计单程延迟（ms）。

    ``sent`` 是同一时间窗内送给声卡的发送块，``received`` 是回采块。两块的
    起点对齐，脉冲在回采块中相对发送块的索引差是「往返总延迟」。真实链路
    中输出与输入各占一半，因此本函数返回该差值的一半，即单程延迟；这个值
    就是补偿录音时应当左移的延迟量。

    参数
    ----
    sent, received : sequence[float]
        起点对齐的发送块与回采块，长度可以不同。
    threshold : float
        传给 :func:`find_pulse` 的块内相对阈值，``[0.0, 1.0]``。
    sample_rate : float
        采样率（Hz），必须大于 0。

    返回
    ----
    float
        ``(received_index - sent_index) / 2 / sample_rate * 1000``，单位 ms。
        若两块脉冲索引相同则返回 0.0。

    异常
    ----
    ValueError
        采样率非正、任一块找不到脉冲，或回采脉冲早于发送脉冲时抛出。

    容差
    ----
    在整数样本构造的信号上，返回值与真值的误差不超过 1 个样本
    （``1000 / sample_rate`` ms）。
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")

    sent_index = find_pulse(sent, threshold)
    received_index = find_pulse(received, threshold)
    if sent_index < 0:
        raise ValueError("no pulse detected in sent block")
    if received_index < 0:
        raise ValueError("no pulse detected in received block")

    roundtrip_samples = received_index - sent_index
    if roundtrip_samples < 0:
        raise ValueError("received pulse precedes sent pulse")

    # 索引差是往返总延迟，折半才是输出/输入各一段的单程延迟。
    return roundtrip_samples / 2.0 / sample_rate * 1000.0


def drift_estimate(offsets, sample_rate):
    """估计多次测量之间的时钟漂移斜率（样本/秒）。

    ``offsets`` 中第 ``k`` 项是第 ``k`` 秒测得的单程延迟偏移（单位：样本）。
    测量严格按 1 秒间隔进行，因此时间轴是 ``0, 1, ..., n-1`` 秒。函数返回
    首尾连线的斜率 ``(offsets[-1] - offsets[0]) / (n - 1)``，单位样本/秒；
    正值表示延迟随时间增大。

    参数
    ----
    offsets : sequence[float]
        至少 2 个按时间顺序排列的偏移量，单位样本。
    sample_rate : float
        采样率（Hz），必须大于 0；用于契约校验，斜率本身按「每秒」定义，
        不随 sample_rate 改变。

    返回
    ----
    float
        漂移斜率，单位样本/秒。

    异常
    ----
    ValueError
        采样率非正，或 ``offsets`` 少于 2 项时抛出。

    容差
    ----
    对严格线性的输入，返回值是精确斜率；测试允许 1e-9 的浮点误差。
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if len(offsets) < 2:
        raise ValueError("at least two offsets are required")

    elapsed_seconds = len(offsets) - 1
    return (offsets[-1] - offsets[0]) / elapsed_seconds


def compensate(samples, delay_samples):
    """按延迟平移样本，输出长度始终与输入相同。

    正值表示录音内容比画面「晚」了 ``delay_samples`` 个样本：丢弃开头
    ``delay_samples`` 个样本，并在尾部补零，使内容提前。负值表示内容偏早：
    在开头补零，并丢弃尾部多余样本，使内容推后。

    参数
    ----
    samples : sequence[float]
        输入样本。
    delay_samples : float
        延迟样本数，按四舍五入取最近整数；可以大于块长或为负。

    返回
    ----
    list[float]
        新列表，长度严格等于 ``len(samples)``。当平移量绝对值不小于块长时，
        返回等长的全零列表。``delay_samples == 0`` 时返回逐元素相同的副本，
        最大绝对误差不超过 1e-9。

    异常
    ----
    无；非有限值不在契约范围内。

    容差
    ----
    平移量按整数样本执行，补偿后的脉冲位置允许 ±1 个样本的残差。
    """
    n = len(samples)
    if n == 0:
        return []

    shift = int(round(delay_samples))

    # 平移量不小于块长时，所有原始样本都被移出窗口，结果是等长全零。
    if abs(shift) >= n:
        return [0.0] * n

    if shift > 0:
        # 正延迟：内容偏晚，左移 shift 个样本并在尾部补零。
        return list(samples[shift:]) + [0.0] * shift
    if shift < 0:
        # 负延迟：内容偏早，右移 |shift| 个样本，在开头补零并丢弃尾部。
        pad = -shift
        return [0.0] * pad + list(samples[: n - pad])
    return list(samples)


def clock_offset(delays, nominal):
    """计算设备时钟相对标称延迟的偏差（ppm）。

    参数
    ----
    delays : sequence[float]
        同一设备的多次延迟测量值，单位 ms，不能为空。
    nominal : float
        标称延迟，单位 ms，必须大于 0。

    返回
    ----
    float
        ``(mean(delays) / nominal - 1) * 1_000_000``，即相对比例换算出的
        ppm。正值表示实测延迟大于标称值。

    异常
    ----
    ValueError
        ``delays`` 为空或 ``nominal <= 0`` 时抛出。

    容差
    ----
        使用算术平均；测试允许 1e-6 ppm 的相对误差。
    """
    if len(delays) == 0:
        raise ValueError("delays must not be empty")
    if nominal <= 0:
        raise ValueError("nominal must be positive")

    mean_delay = sum(delays) / len(delays)
    return (mean_delay / nominal - 1.0) * 1_000_000.0


def stability(delays):
    """返回 0..1 的归一化延迟稳定性指标。

    使用总体标准差（除以 ``n``）与平均延迟绝对值之比，即变异系数，并截断
    到 ``[0.0, 1.0]``。该指标无量纲，因此延迟整体放大或采样率改变时仍可比。
    值越小越稳定。

    参数
    ----
    delays : sequence[float]
        同一设备的延迟测量值，单位 ms，不能为空。

    返回
    ----
    float
        ``min(1.0, population_std(delays) / abs(mean(delays)))``。
        只有一个样本时返回 0.0；均值恰为 0 时，全零返回 0.0，否则返回 1.0。

    异常
    ----
    ValueError
        ``delays`` 为空时抛出。

    容差
    ----
        测试使用 1e-9 的绝对误差。
    """
    n = len(delays)
    if n == 0:
        raise ValueError("delays must not be empty")
    if n == 1:
        return 0.0

    mean_delay = sum(delays) / n
    if mean_delay == 0.0:
        return 0.0 if all(x == 0.0 for x in delays) else 1.0

    variance = sum((x - mean_delay) ** 2 for x in delays) / n
    coefficient = math.sqrt(variance) / abs(mean_delay)
    # 相对离散度本身非负，只需截断上界；保留 max 以防浮点产生极小负值。
    return min(1.0, max(0.0, coefficient))


def _median(values):
    """返回序列的中位数（偶数项取中间两项的算术平均）。"""
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def validate_measure(delays, sample_rate):
    """检查一批延迟测量，返回问题描述列表。

    检查三类问题，全部以稳定的英文关键字返回，便于上层聚合：

    * 采样率：``sample_rate <= 0`` 时返回 ``"sample_rate must be positive"``；
    * 负延迟：每个负值返回 ``"negative delay at index <i>: <value>"``；
    * 离群值：样本数不少于 3 时，以中位数和 MAD 判定。偏离中位数超过
      ``max(3 * MAD, 0.05 * abs(median), 1e-9)`` 的点返回
      ``"outlier delay at index <i>: <value>"``。

    返回列表为空表示全部通过；问题顺序为：采样率、负延迟（按索引）、
    离群值（按索引）。空输入返回 ``["no delay measurements"]``。

    参数
    ----
    delays : sequence[float]
        延迟测量值，单位 ms。
    sample_rate : float
        本次采集的采样率（Hz）。

    返回
    ----
    list[str]
        人类可读的问题列表，可能为空。

    异常
    ----
    无；非法输入通过返回值报告。

    容差
    ----
    离群判定是确定性的，不使用随机数。
    """
    if len(delays) == 0:
        return ["no delay measurements"]

    issues = []

    if sample_rate <= 0:
        issues.append("sample_rate must be positive")

    for i, value in enumerate(delays):
        if value < 0:
            issues.append(f"negative delay at index {i}: {value}")

    if len(delays) >= 3:
        median = _median(delays)
        mad = _median([abs(x - median) for x in delays])
        cutoff = max(3.0 * mad, 0.05 * abs(median), 1e-9)
        for i, value in enumerate(delays):
            if abs(value - median) > cutoff:
                issues.append(f"outlier delay at index {i}: {value}")

    return issues


def calibrate_batch(devices, config):
    """批量校准多台设备，每台设备只使用自己的测量值。

    参数
    ----
    devices : sequence[mapping]
        每项包含 ``"name"``（唯一字符串）与 ``"delays"``（非空的 ms 测量
        序列）。设备之间互不影响。
    config : mapping
        包含 ``"sample_rate"``（Hz，> 0）与 ``"nominal_ms"``（标称延迟 ms，
        > 0）。

    返回
    ----
    dict
        以设备名为键，值为包含以下字段的字典::

            {
                "mean_delay_ms": float,
                "clock_offset_ppm": float,
                "stability": float,
                "issues": list[str],
            }

        结果按输入顺序插入。每台设备的 ``clock_offset_ppm`` 使用该设备自己的
        ``delays`` 计算，不能复用其他设备的状态。

    异常
    ----
    ValueError
        设备名为空或重复、``delays`` 为空、配置非法时抛出。

    容差
    ----
    批量结果必须与逐台单独调用 :func:`clock_offset`、:func:`stability`、
    :func:`validate_measure` 的结果一致（浮点比较允许 1e-9）。
    """
    sample_rate = config["sample_rate"]
    nominal_ms = config["nominal_ms"]
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if nominal_ms <= 0:
        raise ValueError("nominal_ms must be positive")

    results = {}
    for device in devices:
        name = device["name"]
        if not name:
            raise ValueError("device name must not be empty")
        if name in results:
            raise ValueError(f"duplicate device name: {name}")

        delays = device["delays"]
        if len(delays) == 0:
            raise ValueError(f"device {name} has no delay measurements")

        # 所有中间量都在本轮循环内重新计算，绝不跨设备复用。
        results[name] = {
            "mean_delay_ms": sum(delays) / len(delays),
            "clock_offset_ppm": clock_offset(delays, nominal_ms),
            "stability": stability(delays),
            "issues": validate_measure(delays, sample_rate),
        }

    return results
