"""calib.measure 的验收测试。"""

import pytest

from calib.measure import (
    calibrate_batch,
    clock_offset,
    compensate,
    drift_estimate,
    find_pulse,
    roundtrip_delay,
    stability,
    validate_measure,
)


def _signal(length, pulse_index, amplitude=1.0):
    """构造一个只有单个正脉冲的确定性信号。"""
    samples = [0.0] * length
    samples[pulse_index] = amplitude
    return samples


def test_find_pulse_normalizes_threshold_and_is_strict():
    # 低幅度块：有效阈值是 0.5 * 0.2 = 0.1；固定 0.5 的绝对阈值会漏检。
    samples = [0.01, 0.0, 0.2, 0.05, 0.0]
    assert find_pulse(samples, 0.5) == 2

    # 安静全零块的峰值为 0；>= 0 会在索引 0 误触发，严格大于则返回 -1。
    assert find_pulse([0.0, 0.0, 0.0], 0.0) == -1

    # 峰值归一后按绝对值判定，负脉冲同样可以被定位。
    assert find_pulse([-0.01, -0.30, 0.0], 0.5) == 1


def test_roundtrip_delay_returns_one_way_latency():
    sample_rate = 1000
    sent = _signal(32, 4)
    received = _signal(32, 24)  # 索引差 20 个样本 = 20 ms 往返

    measured = roundtrip_delay(sent, received, 0.5, sample_rate)

    assert abs(measured - 10.0) <= 1000.0 / sample_rate


def test_roundtrip_delay_uses_detected_pulse_position():
    sample_rate = 1000
    sent = _signal(80, 3)
    received = _signal(80, 40)  # 索引差 37 个样本 = 18.5 ms 单程

    measured = roundtrip_delay(sent, received, 0.5, sample_rate)

    # 用 len(received)=80 会得到约 40 ms，与真实脉冲位置无关。
    assert abs(measured - 18.5) <= 1.0


def test_drift_estimate_divides_by_elapsed_time():
    # 4 次测量覆盖 3 秒，每 2 个样本/秒的线性漂移。
    assert drift_estimate([0.0, 2.0, 4.0, 6.0], 48000) == pytest.approx(
        2.0, abs=1e-9
    )
    assert drift_estimate([8.0, 6.0, 4.0, 2.0], 48000) == pytest.approx(
        -2.0, abs=1e-9
    )


def test_compensate_advances_signal_by_delay():
    samples = _signal(8, 5)
    compensated = compensate(samples, 3)

    # 正延迟应把内容左移 3 个样本：5 -> 2。
    assert find_pulse(compensated, 0.5) == 2


def test_compensate_preserves_length_for_positive_delays():
    samples = _signal(16, 10)
    for delay in (0, 1, 7, 16, 40):
        assert len(compensate(samples, delay)) == len(samples)


def test_clock_offset_is_relative_ppm():
    # 20.04 ms 相对 20 ms 是 +0.2%，即 +2000 ppm；线性差只有 0.04。
    assert clock_offset([20.04, 20.04, 20.04], 20.0) == pytest.approx(
        2000.0, rel=1e-9
    )
    assert clock_offset([19.6, 19.6, 19.6], 20.0) == pytest.approx(
        -20000.0, rel=1e-9
    )


def test_stability_is_scale_invariant_relative_dispersion():
    small = stability([10.0, 11.0, 9.0, 10.0, 11.0, 9.0])
    large = stability([100.0, 110.0, 90.0, 100.0, 110.0, 90.0])

    # 同样的相对抖动，即使绝对延迟放大 10 倍也必须给出同一个指标。
    assert small == pytest.approx(large, abs=1e-9)
    assert 0.0 <= small <= 1.0
    assert stability([12.0] * 4) == pytest.approx(0.0, abs=1e-12)

    # 变异系数可以超过 1，但返回值必须截断到 [0, 1]。
    assert stability([0.0, 0.0, 10.0]) == pytest.approx(1.0, abs=1e-12)


def test_validate_measure_flags_outlier():
    clean = validate_measure([10.0, 10.05, 9.95, 10.0], 48000)
    assert clean == []

    negative = validate_measure([-0.5, 10.0, 10.1], 48000)
    assert any("negative" in issue.lower() for issue in negative)

    # 单次 100 ms 的误测会把均值从 10 拉到 28，必须作为离群值报告。
    outliers = validate_measure([10.0, 10.0, 10.0, 10.0, 100.0], 48000)
    assert any("outlier" in issue.lower() for issue in outliers)

    bad_rate = validate_measure([10.0], 0)
    assert any("sample_rate" in issue.lower() for issue in bad_rate)


def test_calibrate_batch_keeps_devices_independent():
    devices = [
        {"name": "interface-a", "delays": [10.0, 10.1, 9.9]},
        {"name": "interface-b", "delays": [40.0, 40.2, 39.8]},
    ]
    config = {"sample_rate": 48000, "nominal_ms": 10.0}

    batch = calibrate_batch(devices, config)

    assert set(batch) == {"interface-a", "interface-b"}
    assert batch["interface-a"]["mean_delay_ms"] == pytest.approx(10.0, abs=1e-12)
    assert batch["interface-b"]["mean_delay_ms"] == pytest.approx(40.0, abs=1e-12)
    assert batch["interface-a"]["clock_offset_ppm"] == pytest.approx(0.0, abs=1e-6)
    # 40 ms 相对 10 ms 是 +300%，即 3,000,000 ppm；复用 A 会得到 0。
    assert batch["interface-b"]["clock_offset_ppm"] == pytest.approx(
        3_000_000.0, rel=1e-9
    )


def test_known_ten_ms_latency_within_one_sample():
    sample_rate = 48000
    roundtrip_samples = 960  # 20 ms 往返 -> 10 ms 单程
    sent = _signal(1400, 17)
    received = _signal(1400, 17 + roundtrip_samples, 0.8)

    measured = roundtrip_delay(sent, received, 0.5, sample_rate)
    sample_ms = 1000.0 / sample_rate
    assert abs(measured - 10.0) <= sample_ms

    # 把同一个 10 ms 延迟补偿到一条独立录音上，脉冲应回到 0（±1 样本）。
    delay_samples = int(round(measured * sample_rate / 1000.0))
    track = _signal(512, 480, 0.9)
    aligned = compensate(track, delay_samples)
    position = find_pulse(aligned, 0.5)
    assert 0 <= position <= 1


def test_compensated_pulse_returns_to_zero_within_one_sample():
    track = _signal(64, 10, 0.7)
    compensated = compensate(track, 10)

    position = find_pulse(compensated, 0.5)
    assert 0 <= position <= 1


def test_compensate_output_length_matches_input():
    samples = _signal(9, 2, 0.5)
    for delay in (0, 2, -2, 9, 25, -25):
        assert len(compensate(samples, delay)) == len(samples)

    assert compensate([], 3) == []


def test_zero_delay_compensation_is_identity():
    samples = [0.0, 1.0, -2.5, 3.25, -0.125]
    compensated = compensate(samples, 0)

    assert len(compensated) == len(samples)
    for original, actual in zip(samples, compensated):
        assert abs(original - actual) <= 1e-9


def test_batch_two_devices_match_individual_calibration():
    devices = [
        {"name": "usb-2i2", "delays": [12.0, 12.2, 11.8, 12.1]},
        {"name": "thunderbolt-8", "delays": [7.0, 7.3, 6.9, 7.1]},
    ]
    config = {"sample_rate": 96000, "nominal_ms": 10.0}

    batch = calibrate_batch(devices, config)

    for device in devices:
        delays = device["delays"]
        expected = {
            "mean_delay_ms": sum(delays) / len(delays),
            "clock_offset_ppm": clock_offset(delays, config["nominal_ms"]),
            "stability": stability(delays),
            "issues": validate_measure(delays, config["sample_rate"]),
        }
        actual = batch[device["name"]]

        assert set(actual) == set(expected)
        assert actual["mean_delay_ms"] == pytest.approx(
            expected["mean_delay_ms"], abs=1e-9
        )
        assert actual["clock_offset_ppm"] == pytest.approx(
            expected["clock_offset_ppm"], abs=1e-9
        )
        assert actual["stability"] == pytest.approx(
            expected["stability"], abs=1e-9
        )
        assert actual["issues"] == expected["issues"]
