"""Metadata-only NAND flash model.

No page payload is stored. Each physical page keeps only:
  - state  : FREE / VALID / INVALID (1 byte)
  - rmap   : which LBA is stored here (reverse map, 4 bytes)
  - tag    : host write sequence number, used for integrity verification (8 bytes)

The model enforces the three NAND rules:
  1. read  : page granularity
  2. program: page granularity, strictly sequential inside a block
  3. erase : block granularity; a programmed page cannot be re-programmed
             before its block is erased
"""

from array import array

FREE, VALID, INVALID = 0, 1, 2


class SimViolation(Exception):
    """Raised when an FTL issues an operation that violates NAND rules."""


class NandConfig:
    def __init__(self, channels=4, dies_per_channel=1, blocks_per_die=64,
                 pages_per_block=64, page_size=16384, op_ratio=0.125,
                 ram_kb=512):
        self.channels = int(channels)
        self.dies_per_channel = int(dies_per_channel)
        self.blocks_per_die = int(blocks_per_die)
        self.pages_per_block = int(pages_per_block)
        self.page_size = int(page_size)
        self.op_ratio = float(op_ratio)
        self.ram_kb = int(ram_kb)

    @property
    def ndies(self):
        return self.channels * self.dies_per_channel

    @property
    def total_blocks(self):
        return self.ndies * self.blocks_per_die

    @property
    def total_pages(self):
        return self.total_blocks * self.pages_per_block

    @property
    def user_pages(self):
        """Exported capacity in pages (total minus over-provisioning)."""
        return int(self.total_pages * (1.0 - self.op_ratio))

    @property
    def nominal_bytes(self):
        return self.total_pages * self.page_size

    def as_dict(self):
        return {
            "channels": self.channels,
            "dies_per_channel": self.dies_per_channel,
            "blocks_per_die": self.blocks_per_die,
            "pages_per_block": self.pages_per_block,
            "page_size": self.page_size,
            "op_ratio": self.op_ratio,
            "ram_kb": self.ram_kb,
            "total_blocks": self.total_blocks,
            "total_pages": self.total_pages,
            "user_pages": self.user_pages,
            "nominal_mb": self.nominal_bytes // (1024 * 1024),
        }


class Die:
    """One NAND die: blocks_per_die x pages_per_block pages of metadata."""

    def __init__(self, cfg: NandConfig):
        nb, ppb = cfg.blocks_per_die, cfg.pages_per_block
        self.ppb = ppb
        self.nblocks = nb
        npages = nb * ppb
        self.state = bytearray(npages)              # FREE
        self.rmap = array("i", [-1]) * npages       # LBA stored at page
        self.tag = array("q", [0]) * npages         # write sequence tag
        self.next_page = array("i", [0]) * nb       # sequential-program cursor
        self.erase_count = array("i", [0]) * nb
        self.valid_count = array("i", [0]) * nb

    def program(self, block, page, lba, tag):
        if page != self.next_page[block]:
            raise SimViolation(
                f"non-sequential program: block {block} expects page "
                f"{self.next_page[block]}, got {page}")
        idx = block * self.ppb + page
        if self.state[idx] != FREE:
            raise SimViolation(
                f"program to non-free page: block {block} page {page}")
        self.state[idx] = VALID
        self.rmap[idx] = lba
        self.tag[idx] = tag
        self.next_page[block] += 1
        self.valid_count[block] += 1

    def read(self, block, page):
        idx = block * self.ppb + page
        if self.state[idx] == FREE:
            raise SimViolation(f"read of unwritten page: block {block} page {page}")
        return self.rmap[idx], self.tag[idx]

    def invalidate(self, block, page):
        idx = block * self.ppb + page
        if self.state[idx] != VALID:
            raise SimViolation(
                f"invalidate of non-valid page: block {block} page {page}")
        self.state[idx] = INVALID
        self.valid_count[block] -= 1

    def erase(self, block):
        base = block * self.ppb
        for i in range(base, base + self.ppb):
            self.state[i] = FREE
            self.rmap[i] = -1
            self.tag[i] = 0
        self.next_page[block] = 0
        self.valid_count[block] = 0
        self.erase_count[block] += 1

    def page_state(self, block, page):
        return self.state[block * self.ppb + page]
