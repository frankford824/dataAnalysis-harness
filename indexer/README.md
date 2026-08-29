# LedgerIndexer

Windows 单机索引服务：监听 NAS 台账目录，稳定 60 秒后以 Calamine 流式读取 XLSX/CSV，
一次生成 Parquet 和 Tantivy 行级全文索引。它不决定财务金额；金额仍由 Ledger 的
Python/Polars 确定性引擎计算。

## 构建

```powershell
$env:CARGO_INCREMENTAL = '0'
$env:CARGO_TARGET_DIR = "$env:TEMP\dataAnalysis-indexer-target"
cargo test --manifest-path indexer\Cargo.toml
cargo build --release --manifest-path indexer\Cargo.toml
```

真实 106,437,779 字节、325,106 行、104 列售后单的当前门禁结果：索引和 Parquet
总用时约 9.8 秒，峰值私有内存约 420 MiB；产物约 330 MiB。完整订单号以 CLI 冷启动
测得 p95 约 58 ms，常驻 HTTP 服务不承担进程启动时间。

## 运行

```powershell
LedgerIndexer.exe serve `
  --root 'X:\台账系统' `
  --data 'D:\ledger\index' `
  --bind '127.0.0.1:8765'
```

只绑定环回地址。对外 HTTP 仍由 Ledger 的 `/api/index/*` 和 `/api/search` 提供。

安全规则：

- 新文件连续 60 秒大小和 mtime 不变才读取；
- 每 30 秒做完整目录对账，目录通知只能作为后续加速，不能代替对账；
- 相同 SHA 只解析一次；路径/店铺关联保存在 catalog；
- 扫描不完整、NAS 不可达或链路低于 1Gbps 时，不增加缺失计数；
- 撤表由 Ledger 在连续三次缺失且满十分钟后执行；相同 SHA 换路径不撤表；
- `90_历史版本`、`20_需修正`、临时文件和系统目录不进入计算扫描。

生产切换使用 `D:\ledger\bin\cutover-nas.ps1`。它会交互获取 SMB 密码、等待首次索引
门禁通过，最后才写入 `D:\ledger\nas.enabled` 并关闭网页上传。
