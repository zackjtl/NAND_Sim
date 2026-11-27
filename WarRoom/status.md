---
agent: Claude Code (Fable 5)
environment: local
workdir: C:\Users\eti032.ETI\Documents\Projects\NAND Sim
machine: ETI032（使用者的 Windows 工作機）
branch: main
phase: 開發中
progress: 70
updated_at: 2026-11-27T08:41:44Z
summary: Wear leveling 一輪完成：EC-aware 配置 + Δ 門檻閉環把 max−min EC 鉗在門檻內（WAF 幾乎零代價），自測全過；接下來是省 RAM 映射（DFTL）與 GC 改良
next_steps:
  - 省 RAM 映射實驗（block-level / hybrid / DFTL），讓 RAM 預算真正限制行為
  - GC 改良：cost-benefit victim selection、冷熱資料分流
  - Timing model（tR/tProg/tBERS + channel bus 競爭）估算 throughput
---

## 專案

NAND Flash 模擬器（metadata-only）+ 模擬 controller（固定 channel 數 / RAM 預算）+ page-level FTL，
含 stdlib-only 網頁前端（port 8732）供觀察與測試。Repo：https://github.com/zackjtl/NAND_Sim

## 本次進度（wear leveling 這一輪）

- [x] EC min/max 趨勢圖：兩線 + 差距陰影帶 + 目前 Δ，作為 WL 健康度的直接判讀指標
- [x] 系統保留 block：永不配置/抹除，排除於 user 容量與所有磨損統計，block 圖以斜線紋標示
- [x] 修正真 bug：free pool 原為 LIFO，GC 水位下每 die 堆底 2 block 永遠輪不到、EC 卡 0
- [x] Interval 觸發 static WL（公司做法）：每 N 次寫入回收最冷 block；間隔需與 usable block 數匹配
- [x] EC-aware 配置：host 熱寫入配最年輕 free block、GC/WL 搬遷配最老 → 冷資料停老 block 退休
- [x] Δ 門檻閉環觸發：Δ 超門檻即收割最冷 block，軌道寬度成為設計參數
- [x] 對照數據（hotcold 20 萬寫入）：僅 EC-aware Δ=17 / +interval Δ=18 / +Δ門檻(15) Δ=15，WAF 均 ≈2.74
- [x] 自測擴充（WL 三模式 + Δ 鉗制案例）全數通過，資料完整性驗證一致
- [ ] DFTL / hybrid 映射（RAM 預算目前只警示、不影響行為）
- [ ] cost-benefit GC 與冷熱分流
- [ ] Timing model 與真實產品幾何參數對接

## 卡關

無。
