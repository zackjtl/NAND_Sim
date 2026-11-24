"""HTTP front end for the NAND/FTL simulator (stdlib only).

GET  /                 -> web/index.html
GET  /api/state        -> config, metrics, RAM report, per-block map, history
POST /api/init         -> rebuild simulator with given config
POST /api/run          -> {"workload": "seq|rand|hotcold", "ops": N}
POST /api/verify       -> shadow-table integrity check
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from nandsim import NandConfig, Controller, PageFTL, Workload, run_ops, SimViolation
PORT = 8732


class Sim:
    def __init__(self, **kw):
        self.cfg = NandConfig(**kw)
        self.ctrl = Controller(self.cfg)
        self.ftl = PageFTL(self.ctrl)
        self.workloads = {}
        self.history = []          # per-run-interval snapshots
        self._last = (0, 0, 0, 0)  # (host_w, nand_w, gc_reads+host_reads, erases)

    def run(self, kind, ops):
        wl = self.workloads.get(kind)
        if wl is None:
            wl = Workload(self.cfg.user_pages, kind=kind)
            self.workloads[kind] = wl
        run_ops(self.ftl, wl, ops)
        m = self.ftl.metrics()
        reads = sum(self.ctrl.ch_reads)
        h0, n0, r0, e0 = self._last
        dh, dn = m["host_writes"] - h0, m["nand_writes"] - n0
        self.history.append({
            "host_writes": m["host_writes"],
            "waf_cum": m["waf"],
            "waf_win": round(dn / dh, 4) if dh else 0.0,
            "d_host": dh,
            "d_gc": dn - dh,
            "d_read": reads - r0,
            "d_erase": m["erases"] - e0,
        })
        if len(self.history) > 400:
            self.history = self.history[-400:]
        self._last = (m["host_writes"], m["nand_writes"], reads, m["erases"])
        return m

    def state(self):
        blocks = []
        ppb = self.cfg.pages_per_block
        for d_idx, die in enumerate(self.ctrl.dies):
            ch = self.ctrl.channel_of(d_idx)
            for b in range(self.cfg.blocks_per_die):
                np_ = die.next_page[b]
                if np_ == 0:
                    st = "free"
                elif np_ < ppb:
                    st = "open"
                else:
                    st = "full"
                blocks.append({
                    "ch": ch, "die": d_idx, "blk": b, "st": st,
                    "valid": die.valid_count[b], "written": np_,
                    "ec": die.erase_count[b],
                })
        return {
            "config": self.cfg.as_dict(),
            "metrics": self.ftl.metrics(),
            "ram": self.ftl.ram_report(),
            "ftl": self.ftl.alloc_state(),
            "lba_heat": self.ftl.lba_heat(),
            "oplog": [list(e) for e in self.ctrl.oplog],
            "channels": {
                "programs": self.ctrl.ch_programs,
                "reads": self.ctrl.ch_reads,
                "erases": self.ctrl.ch_erases,
            },
            "blocks": blocks,
            "pages_per_block": ppb,
            "history": self.history,
        }


SIM = Sim()
LOCK = threading.Lock()

CONFIG_KEYS = ("channels", "dies_per_channel", "blocks_per_die",
               "pages_per_block", "page_size", "op_ratio", "ram_kb")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = (ROOT / "web" / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
        elif self.path == "/api/state":
            with LOCK:
                self._send(200, SIM.state())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        global SIM
        try:
            req = self._body()
            if self.path == "/api/init":
                kw = {k: req[k] for k in CONFIG_KEYS if k in req}
                with LOCK:
                    SIM = Sim(**kw)
                    self._send(200, {"ok": True, "config": SIM.cfg.as_dict()})
            elif self.path == "/api/run":
                kind = req.get("workload", "rand")
                ops = max(1, min(int(req.get("ops", 1000)), 2_000_000))
                with LOCK:
                    m = SIM.run(kind, ops)
                self._send(200, {"ok": True, "metrics": m})
            elif self.path == "/api/verify":
                with LOCK:
                    self._send(200, SIM.ftl.verify())
            else:
                self._send(404, {"error": "not found"})
        except SimViolation as e:
            self._send(400, {"ok": False, "error": f"SimViolation: {e}"})
        except Exception as e:
            self._send(400, {"ok": False, "error": str(e)})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"NAND/FTL simulator UI on http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
