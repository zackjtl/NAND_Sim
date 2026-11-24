# NAND / FTL Simulator（metadata-only 原型）

純 Python（標準函式庫，無相依套件）的 NAND Flash + Controller + FTL 模擬器。
不儲存 page 資料本體，每個實體 page 只保留 metadata（狀態 / 反向映射 LBA /
寫入序號標籤），因此模擬 256MB 只需約 1MB 記憶體，硬碟需求為零。

## 架構

```
nandsim/nand.py        NAND die 模型：強制執行三條規則
                       （page 讀寫、block 內循序 program、先抹除才能再寫）
                       違規丟 SimViolation —— FTL 的 bug 會立刻現形
nandsim/controller.py  Controller：channel/die 路由、PPA 編解碼、
                       per-channel 操作計數、RAM 預算回報
nandsim/ftl.py         Page-level FTL：round-robin channel striping、
                       greedy GC、host 端影子表完整性驗證
nandsim/workload.py    Workload：循序 / 均勻隨機 / 冷熱 80-20
server.py              HTTP API + 前端（stdlib http.server，port 8732）
web/index.html         觀察前端，分成兩個模組區塊呈現互動：
                       「FTL（控制器韌體）」邏輯視角 —— LBA 熱度圖、
                       WAF 曲線、open block / free pool 配置表、RAM 使用；
                       「NAND Flash（實體）」實體視角 —— block 狀態圖、
                       磨損分布、channel 負載；兩區之間是 NAND 命令
                       介面帶，顯示 program(host/GC)/read/erase 流量
                       與最近命令
selftest.py            自我測試：NAND 規則 + 三種 workload 各 20 萬次寫入
                       後做全量完整性驗證
```

## 執行

```powershell
python selftest.py     # 核心自測
python server.py       # 開 http://localhost:8732
```

## 預設幾何（可在前端「重設…」調整）

4 channel × 1 die × 64 block × 64 page × 16KB = 256MB，OP 12.5%，
controller RAM 512KB（page-level 全表 56KB，放得下）。

## 實測基準（selftest, 200k 寫入）

| workload | WAF | 備註 |
|---|---|---|
| 循序寫 | 1.000 | GC 全是零成本（victim valid=0） |
| 均勻隨機 | ~4.5 | 12.5% OP + greedy GC 的典型值 |
| 冷熱 80/20 | ~2.6 | 介於中間；冷熱分流是下一個改進點 |

## 下一步實驗方向

- RAM 放不下時的 mapping：block-level / hybrid（BAST/FAST）/ DFTL
- GC victim 改 cost-benefit、加冷熱資料分流（hot/cold stream）
- static wear leveling（目前抹除分布可在前端直方圖觀察）
- timing model（tR/tProg/tBERS + channel bus 競爭）估算 throughput
- 對接真實產品幾何參數做對照實驗
