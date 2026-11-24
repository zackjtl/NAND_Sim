"""Simulated controller: routes PPA-addressed commands to dies over channels.

PPA encoding (single int):
    ppa = die * (blocks_per_die * pages_per_block) + block * pages_per_block + page
    die = channel * dies_per_channel + die_in_channel

Tracks per-channel operation counters (parallelism distribution) and the
RAM budget the FTL has to live within.
"""

from .nand import Die, NandConfig


class Controller:
    def __init__(self, cfg: NandConfig):
        self.cfg = cfg
        self.dies = [Die(cfg) for _ in range(cfg.ndies)]
        self.ch_programs = [0] * cfg.channels
        self.ch_reads = [0] * cfg.channels
        self.ch_erases = [0] * cfg.channels
        self._pages_per_die = cfg.blocks_per_die * cfg.pages_per_block

    # ---- PPA helpers -------------------------------------------------
    def encode(self, die, block, page):
        return die * self._pages_per_die + block * self.cfg.pages_per_block + page

    def decode(self, ppa):
        die, rest = divmod(ppa, self._pages_per_die)
        block, page = divmod(rest, self.cfg.pages_per_block)
        return die, block, page

    def channel_of(self, die):
        return die // self.cfg.dies_per_channel

    # ---- NAND commands ----------------------------------------------
    def program(self, ppa, lba, tag):
        die, block, page = self.decode(ppa)
        self.dies[die].program(block, page, lba, tag)
        self.ch_programs[self.channel_of(die)] += 1

    def read(self, ppa):
        die, block, page = self.decode(ppa)
        self.ch_reads[self.channel_of(die)] += 1
        return self.dies[die].read(block, page)

    def invalidate(self, ppa):
        die, block, page = self.decode(ppa)
        self.dies[die].invalidate(block, page)

    def erase(self, die, block):
        self.dies[die].erase(block)
        self.ch_erases[self.channel_of(die)] += 1

    def next_page(self, die, block):
        return self.dies[die].next_page[block]

    # ---- RAM accounting ---------------------------------------------
    def ram_report(self, mapping_bytes, extra_bytes=0):
        budget = self.cfg.ram_kb * 1024
        used = mapping_bytes + extra_bytes
        return {
            "budget_bytes": budget,
            "mapping_bytes": mapping_bytes,
            "extra_bytes": extra_bytes,
            "used_bytes": used,
            "utilization": used / budget if budget else 0.0,
            "fits": used <= budget,
        }
