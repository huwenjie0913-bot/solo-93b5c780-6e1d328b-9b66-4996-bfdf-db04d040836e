"""并联拓扑可用容量（回归测试）。

背景缺陷：4S2P、8 只 100Ah 电芯时旧实现返回 100Ah（误用了纯串联的
"最弱单体" 口径），正确结果应为每串 100+100=200Ah，整包由最弱并联串
限制为 200Ah。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LIBS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     ".pylibs", "lib",
                     f"python{sys.version_info.major}.{sys.version_info.minor}",
                     "site-packages")
if os.path.isdir(_LIBS):
    sys.path.insert(0, _LIBS)

from app.grouping import build_groups  # noqa: E402


def topo(s, p):
    return {"series": s, "parallel": p, "group_size": s * p, "raw": f"{s}S{p}P"}


def mk(vals, resistance=2.0):
    return [
        {
            "cell_id": f"C{i:03d}",
            "capacity_ah": float(v),
            "resistance_mohm": resistance,
            "ocv_v": 3.2,
            "cycles": 500,
            "temperature_c": 25.0,
        }
        for i, v in enumerate(vals)
    ]


def first_group(vals, s, p, thresholds=None):
    th = {"capacity_cv_max": 0.30, "resistance_cv_max": 0.30,
          "temperature_delta_max": 60.0, "ocv_delta_max": 1.0, "soh_min": 0.0}
    if thresholds:
        th.update(thresholds)
    result = build_groups(mk(vals), topo(s, p), thresholds=th,
                          rated_capacity_ah=105.0)
    complete = [g for g in result["groups"] if g["complete"]]
    assert complete, f"没有成包组: {[g['group_no'] for g in result['groups']]}"
    return complete[0]


def test_4s2p_uniform_capacity_is_doubled():
    """复现缺陷：8x100Ah 按 4S2P 成组，可用容量必须是 200Ah 而非 100Ah。"""
    g = first_group([100.0] * 8, 4, 2)
    m = g["metrics"]
    assert m["usable_capacity_ah"] == 200.0
    assert m["usable_capacity_method"] == "min_parallel_string"
    # 4 个并联串，每串恰好 2 只、容量 200Ah
    assert len(m["parallel_strings"]) == 4
    assert all(st["cell_count"] == 2 for st in m["parallel_strings"])
    assert all(st["capacity_ah"] == 200.0 for st in m["parallel_strings"])
    assert m["weakest_string_no"] in (1, 2, 3, 4)
    assert m["string_imbalance_pct"] == 0.0
    # 并联内阻：每串 2//2=1mΩ，4 串串联 = 4mΩ
    assert m["estimated_pack_resistance_mohm"] == 4.0


def test_8s1p_capacity_remains_weakest_cell():
    """纯串联时口径不变：可用容量 = 最弱单体。"""
    vals = [100.0] * 7 + [98.0]
    g = first_group(vals, 8, 1)
    assert g["metrics"]["usable_capacity_ah"] == 98.0
    assert all(st["cell_count"] == 1 for st in g["metrics"]["parallel_strings"])


def test_2s3p_uniform_capacity_tripled():
    """2S3P、6 只 100Ah：每串 300Ah，整包 300Ah；内阻 (2/3)*2 = 1.333mΩ。"""
    g = first_group([100.0] * 6, 2, 3)
    m = g["metrics"]
    assert m["usable_capacity_ah"] == 300.0
    assert len(m["parallel_strings"]) == 2
    assert all(st["capacity_ah"] == 300.0 for st in m["parallel_strings"])
    assert round(m["estimated_pack_resistance_mohm"], 4) == 1.3333


def test_4s2p_lpt_pairs_weak_with_strong():
    """含一只 90Ah、一只 110Ah：LPT 应把两者配进同一串，最弱串仍 200Ah。

    若错误地把 90Ah 与 100Ah 配串，最弱串只有 190Ah。
    """
    g = first_group([100.0] * 6 + [110.0, 90.0], 4, 2)
    m = g["metrics"]
    assert m["usable_capacity_ah"] == 200.0
    sums = sorted(st["capacity_ah"] for st in m["parallel_strings"])
    # LPT 把 90 与 110 配进同一串，四串恰好全部 200Ah
    assert sums == [200.0, 200.0, 200.0, 200.0]
    assert m["string_imbalance_pct"] == 0.0
    # 最弱电芯仍按单体识别（决定短板风险与替换候选）
    assert g["weakest_cell"]["cell_id"] == "C007"


def test_4s2p_weak_string_limits_pack():
    """7×100 + 1×90 时含 90Ah 的并联串只有 190Ah，整包受限于该串。

    纯串联旧口径会给出 90Ah；正确口径是 100+90=190Ah（并联叠加）。
    """
    g = first_group([100.0] * 7 + [90.0], 4, 2)
    m = g["metrics"]
    assert m["usable_capacity_ah"] == 190.0
    weakest = next(st for st in m["parallel_strings"]
                   if st["string_no"] == m["weakest_string_no"])
    assert weakest["capacity_ah"] == 190.0
    # 串间不均衡应被暴露（200 vs 190）
    assert m["string_imbalance_pct"] > 0


def test_1s4p_is_simple_parallel_sum():
    """1S4P：4 只 100Ah 直接并联，可用容量 400Ah，内阻 0.5mΩ。"""
    g = first_group([100.0] * 4, 1, 4)
    m = g["metrics"]
    assert m["usable_capacity_ah"] == 400.0
    assert len(m["parallel_strings"]) == 1
    assert m["estimated_pack_resistance_mohm"] == 0.5


def test_multiple_packs_each_scale_by_p():
    """16 只 100Ah、4S2P 成两个包，每个包都是 200Ah。"""
    result = build_groups(mk([100.0] * 16), topo(4, 2), rated_capacity_ah=105.0)
    packs = [g for g in result["groups"] if g["complete"]]
    assert len(packs) == 2
    assert all(g["metrics"]["usable_capacity_ah"] == 200.0 for g in packs)
    # 汇总口径"每包可用容量"同样按并联修正
    assert result["summary"]["usable_capacity_per_pack_ah"] == 200.0


def test_parallel_string_resistance_is_parallel_combination():
    """串内阻按并联公式计算：同串 2Ω 与 2Ω 并联 = 1Ω。"""
    g = first_group([100.0] * 8, 4, 2)
    for st in g["metrics"]["parallel_strings"]:
        assert st["resistance_mohm"] == 1.0
