"""Workload generators: produce LBA streams for the FTL."""

import random


class Workload:
    """Stateful LBA generator over the user LBA span."""

    def __init__(self, user_pages, kind="rand", seed=12345):
        self.user_pages = user_pages
        self.kind = kind
        self.rng = random.Random(seed)
        self.cursor = 0
        # hot/cold: 80% of writes hit the first 20% of the span
        self.hot_span = max(1, int(user_pages * 0.2))

    def next_lba(self):
        if self.kind == "seq":
            lba = self.cursor
            self.cursor = (self.cursor + 1) % self.user_pages
            return lba
        if self.kind == "hotcold":
            if self.rng.random() < 0.8:
                return self.rng.randrange(self.hot_span)
            return self.hot_span + self.rng.randrange(self.user_pages - self.hot_span)
        # uniform random
        return self.rng.randrange(self.user_pages)


def run_ops(ftl, workload, nops):
    for _ in range(nops):
        ftl.write(workload.next_lba())
