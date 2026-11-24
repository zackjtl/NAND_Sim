"""Page-level FTL with round-robin channel striping and greedy GC.

RAM model: the full LBA->PPA mapping table (4 bytes per user page) is assumed
to be resident in controller RAM; ram_report() shows whether it fits the
configured budget. Swapping to a demand-paged mapping (DFTL) is the natural
next experiment on top of this interface.

Integrity: every host write gets a monotonically increasing tag. A host-side
shadow table keeps the expected tag per LBA; verify() re-reads every mapped
LBA through the FTL and compares both the reverse-mapped LBA and the tag.
"""

from array import array

from .nand import VALID, SimViolation
from .controller import Controller


class PageFTL:
    GC_LOW_WATER = 2          # run GC when a die's free-block pool drops to this
    MAP_ENTRY_BYTES = 4

    def __init__(self, ctrl: Controller, wl_interval=0, wl_delta=0):
        self.ctrl = ctrl
        cfg = ctrl.cfg
        self.cfg = cfg
        self.user_pages = cfg.user_pages
        self.map = array("i", [-1]) * self.user_pages

        nd = cfg.ndies
        # free-block pools, one per die. Allocation is EC-aware (see
        # _take_free): host writes take the youngest free block, relocated
        # (cold) data takes the oldest, so worn blocks retire under cold data
        # and young blocks absorb the hot churn. The reserved system area
        # [0, reserved_blocks_per_die) is never handed to the FTL.
        rsv = cfg.reserved_blocks_per_die
        self.free = [list(range(rsv, cfg.blocks_per_die)) for _ in range(nd)]
        self.host_open = [None] * nd    # active block receiving host writes
        self.gc_open = [None] * nd      # active block receiving GC relocations
        self.rr = 0                     # round-robin die cursor for striping

        # metrics
        self.host_writes = 0
        self.gc_writes = 0
        self.gc_runs = 0
        self.erases = 0
        self.wseq = 0
        self.lba_writes = array("i", [0]) * self.user_pages   # logical heat

        # static wear leveling, two triggers (either may be disabled with 0):
        #   wl_interval - open loop: every N host writes, collect the coldest
        #                 fully-programmed block (background rotation rate)
        #   wl_delta    - closed loop: whenever max-min erase count (reserved
        #                 area excluded) exceeds this, collect the coldest
        #                 block until the gap closes - clamps the EC band
        self.wl_interval = int(wl_interval)
        self._wl_countdown = self.wl_interval
        self.wl_delta = int(wl_delta)
        self._wl_check = 0            # re-evaluate the gap every 128 writes
        self.wl_runs = 0

        # host-side shadow table for verification (not controller RAM)
        self.shadow = {}

    # ---- host interface ----------------------------------------------
    def write(self, lba):
        if not 0 <= lba < self.user_pages:
            raise ValueError(f"LBA {lba} out of range (user_pages={self.user_pages})")
        self.wseq += 1
        tag = self.wseq

        old = self.map[lba]
        if old >= 0:
            self.ctrl.invalidate(old)

        die = self.rr
        self.rr = (self.rr + 1) % self.cfg.ndies
        ppa = self._alloc(die, for_gc=False)
        self.ctrl.program(ppa, lba, tag, origin="host")
        self.map[lba] = ppa
        self.host_writes += 1
        self.lba_writes[lba] += 1
        self.shadow[lba] = tag

        if self.wl_interval > 0:
            self._wl_countdown -= 1
            if self._wl_countdown <= 0:
                self._wl_countdown = self.wl_interval
                self._static_wl()
        if self.wl_delta > 0:
            self._wl_check -= 1
            if self._wl_check <= 0:
                self._wl_check = 128
                if self.ec_delta() > self.wl_delta:
                    self._static_wl()

    def read(self, lba):
        ppa = self.map[lba]
        if ppa < 0:
            return None
        return self.ctrl.read(ppa)   # (lba, tag)

    def trim(self, lba):
        ppa = self.map[lba]
        if ppa >= 0:
            self.ctrl.invalidate(ppa)
            self.map[lba] = -1
            self.shadow.pop(lba, None)

    # ---- allocation & GC ----------------------------------------------
    def _alloc(self, die, for_gc):
        open_list = self.gc_open if for_gc else self.host_open
        blk = open_list[die]
        if blk is None:
            blk = self._take_free(die, for_gc)
            open_list[die] = blk
        page = self.ctrl.next_page(die, blk)
        if page == self.cfg.pages_per_block - 1:
            open_list[die] = None      # block becomes full after this program
        return self.ctrl.encode(die, blk, page)

    def _take_free(self, die, for_gc):
        if not for_gc:
            self._gc_if_needed(die)
        pool = self.free[die]
        if not pool:
            raise SimViolation(f"die {die}: free-block pool exhausted "
                               f"(over-provisioning too small?)")
        # EC-aware allocation: hot host writes wear the youngest free block;
        # GC/WL relocations (cold-leaning data) park on the oldest, giving
        # worn blocks a rest. This is the second half of static WL - without
        # it, freed cold data lands back on young blocks and the EC gap keeps
        # widening.
        ec = self.ctrl.dies[die].erase_count
        pick = (max(pool, key=lambda b: ec[b]) if for_gc
                else min(pool, key=lambda b: ec[b]))
        pool.remove(pick)
        return pick

    def _gc_if_needed(self, die):
        while len(self.free[die]) <= self.GC_LOW_WATER:
            victim = self._pick_victim(die)
            if victim is None:
                return
            self._collect(die, victim)

    def _pick_victim(self, die):
        """Greedy: the fully-programmed, non-open block with fewest valid pages."""
        d = self.ctrl.dies[die]
        ppb = self.cfg.pages_per_block
        best, best_valid = None, ppb + 1
        for b in range(self.cfg.blocks_per_die):
            if b == self.host_open[die] or b == self.gc_open[die]:
                continue
            if d.next_page[b] < ppb:       # not fully programmed (free or open)
                continue
            v = d.valid_count[b]
            if v < best_valid:
                best, best_valid = b, v
                if v == 0:
                    break
        if best is None or best_valid >= ppb:
            return None                     # nothing reclaimable
        return best

    def _collect(self, die, victim):
        d = self.ctrl.dies[die]
        ppb = self.cfg.pages_per_block
        for page in range(ppb):
            if d.page_state(victim, page) != VALID:
                continue
            lba, tag = self.ctrl.read(self.ctrl.encode(die, victim, page),
                                      origin="gc")
            dst = self._alloc(die, for_gc=True)
            self.ctrl.program(dst, lba, tag, origin="gc")
            self.map[lba] = dst
            self.gc_writes += 1
        self.ctrl.erase(die, victim, origin="gc")
        self.erases += 1
        self.gc_runs += 1
        self.free[die].append(victim)

    def ec_delta(self):
        """max - min erase count over usable (non-reserved) blocks."""
        rsv = self.cfg.reserved_blocks_per_die
        lo = min(min(d.erase_count[rsv:]) for d in self.ctrl.dies)
        hi = max(max(d.erase_count[rsv:]) for d in self.ctrl.dies)
        return hi - lo

    def _static_wl(self):
        """Periodic cold-block GC: force-collect the fully-programmed block
        with the lowest erase count, valid data and all, so wear spreads to
        blocks the greedy victim policy would never touch."""
        cfg = self.cfg
        rsv = cfg.reserved_blocks_per_die
        ppb = cfg.pages_per_block
        best = None            # (die, block)
        best_ec = None
        for die in range(cfg.ndies):
            if len(self.free[die]) < 2:     # keep headroom for the relocation
                continue
            d = self.ctrl.dies[die]
            for b in range(rsv, cfg.blocks_per_die):
                if b == self.host_open[die] or b == self.gc_open[die]:
                    continue
                if d.next_page[b] < ppb:    # free or still open
                    continue
                ec = d.erase_count[b]
                if best_ec is None or ec < best_ec:
                    best, best_ec = (die, b), ec
        if best is not None:
            self._collect(*best)
            self.wl_runs += 1

    # ---- reporting ------------------------------------------------------
    def verify(self):
        """Re-read every mapped LBA and compare against the shadow table."""
        checked = mismatches = 0
        for lba, want_tag in self.shadow.items():
            checked += 1
            got = self.read(lba)
            if got is None:
                mismatches += 1
                continue
            got_lba, got_tag = got
            if got_lba != lba or got_tag != want_tag:
                mismatches += 1
        return {"checked": checked, "mismatches": mismatches, "ok": mismatches == 0}

    def metrics(self):
        nand_writes = self.host_writes + self.gc_writes
        waf = (nand_writes / self.host_writes) if self.host_writes else 0.0
        return {
            "host_writes": self.host_writes,
            "gc_writes": self.gc_writes,
            "nand_writes": nand_writes,
            "waf": round(waf, 4),
            "gc_runs": self.gc_runs,
            "wl_runs": self.wl_runs,
            "erases": self.erases,
            "free_blocks": sum(len(f) for f in self.free),
            "mapped_lbas": len(self.shadow),
        }

    def ram_report(self):
        mapping_bytes = self.user_pages * self.MAP_ENTRY_BYTES
        return self.ctrl.ram_report(mapping_bytes)

    def alloc_state(self):
        """FTL-internal allocation state, for visualization."""
        return {
            "host_open": list(self.host_open),
            "gc_open": list(self.gc_open),
            "free_per_die": [len(f) for f in self.free],
            "gc_low_water": self.GC_LOW_WATER,
        }

    def lba_heat(self, nbuckets=128):
        """Logical-space view: host-write heat and mapped ratio per bucket."""
        n = self.user_pages
        nb = min(nbuckets, n)
        writes = [0] * nb
        mapped = [0] * nb
        size = [0] * nb
        for i in range(n):
            b = i * nb // n
            writes[b] += self.lba_writes[i]
            size[b] += 1
            if self.map[i] >= 0:
                mapped[b] += 1
        return {"buckets": nb, "writes": writes, "mapped": mapped, "size": size}
