"""Self-test: NAND rule enforcement + end-to-end integrity under heavy GC."""

import sys
import time

sys.path.insert(0, ".")
from nandsim import NandConfig, Controller, PageFTL, Workload, run_ops, SimViolation


def test_nand_rules():
    cfg = NandConfig(channels=1, blocks_per_die=4, pages_per_block=4,
                     reserved_blocks_per_die=0)
    ctrl = Controller(cfg)
    ctrl.program(ctrl.encode(0, 0, 0), lba=7, tag=1)
    # non-sequential program must fail
    try:
        ctrl.program(ctrl.encode(0, 0, 2), lba=8, tag=2)
        raise AssertionError("non-sequential program was allowed")
    except SimViolation:
        pass
    # overwrite without erase must fail
    ctrl.erase(0, 0)
    ctrl.program(ctrl.encode(0, 0, 0), lba=9, tag=3)
    try:
        d = ctrl.dies[0]
        d.next_page[0] = 0          # force cursor back to a programmed page
        ctrl.program(ctrl.encode(0, 0, 0), lba=10, tag=4)
        raise AssertionError("overwrite without erase was allowed")
    except SimViolation:
        pass
    print("NAND rule enforcement: OK")


def test_ftl(kind, ops):
    cfg = NandConfig()           # 4ch x 64 blocks x 64 pages, 12.5% OP
    ctrl = Controller(cfg)
    ftl = PageFTL(ctrl)
    wl = Workload(cfg.user_pages, kind=kind, seed=42)
    t0 = time.time()
    run_ops(ftl, wl, ops)
    dt = time.time() - t0
    m = ftl.metrics()
    v = ftl.verify()
    assert v["ok"], f"integrity FAILED: {v}"
    print(f"{kind:8s} ops={ops:8d}  WAF={m['waf']:.3f}  gc_runs={m['gc_runs']:5d} "
          f"erases={m['erases']:5d}  free={m['free_blocks']:3d}  "
          f"verify={v['checked']} ok  ({ops/dt/1000:.0f} kops/s)")


def test_static_wl(ops=200_000):
    """EC-aware allocation alone must bound the EC gap, every mode must lift
    the minimum erase count above zero, and integrity must always hold."""
    cfg = NandConfig()
    rsv = cfg.reserved_blocks_per_die
    for wl_i, wl_d, tag in ((0, 0, "EC-aware alloc only"),
                            (500, 0, "interval trigger"),
                            (5000, 20, "combined")):
        ctrl = Controller(cfg)
        ftl = PageFTL(ctrl, wl_interval=wl_i, wl_delta=wl_d)
        run_ops(ftl, Workload(cfg.user_pages, kind="hotcold", seed=7), ops)
        v = ftl.verify()
        assert v["ok"], f"integrity FAILED ({tag}): {v}"
        lo = min(min(d.erase_count[rsv:]) for d in ctrl.dies)
        delta = ftl.ec_delta()
        assert lo > 0, f"{tag}: cold blocks untouched"
        assert delta <= 25, f"{tag}: EC gap too wide: {delta}"
        print(f"{tag:20s} min EC={lo:3d}  Δ={delta:3d}  wl_runs={ftl.wl_runs}  OK")


def test_wl_delta_clamp(ops=200_000, threshold=15):
    """The closed-loop trigger must hold the EC gap near the threshold."""
    cfg = NandConfig()
    ctrl = Controller(cfg)
    ftl = PageFTL(ctrl, wl_interval=0, wl_delta=threshold)
    run_ops(ftl, Workload(cfg.user_pages, kind="hotcold", seed=7), ops)
    v = ftl.verify()
    assert v["ok"], f"integrity FAILED: {v}"
    delta = ftl.ec_delta()
    # the check runs every 128 writes and moves one block per trigger,
    # so allow a small convergence overshoot
    assert delta <= threshold + 5, f"EC gap not clamped: Δ={delta}"
    print(f"WL Δ-clamp: threshold={threshold}, final Δ={delta}, "
          f"wl_runs={ftl.wl_runs}  OK")


if __name__ == "__main__":
    test_nand_rules()
    for kind in ("seq", "rand", "hotcold"):
        test_ftl(kind, 200_000)
    test_static_wl()
    test_wl_delta_clamp()
    print("ALL PASSED")
