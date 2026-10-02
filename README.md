# 协同事务平台

协同事务平台为需要多机构参与的业务提供统一的机构、授权、案件、证据、审批、资源预约、资金分录、消息归并、通知、定时任务和历史回放能力。项目只使用 Python 标准库与 SQLite，适合在单个 Linux 应用容器内运行。

## 目录

- `src/civicflow/`：领域服务、SQLite 持久化、权限和命令行入口。
- `tests/`：核心流程、边界条件和异常路径测试。
- `examples/`：本地演示输入。

## 配置

通过 `CIVICFLOW_DB` 指定 SQLite 文件路径；不设置时命令行使用当前目录下的 `civicflow.sqlite3`。所有时间使用带时区的 ISO 8601 字符串。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 编译或构建

```bash
PYTHONPATH=src python3 -m compileall -q src
```

## 使用

初始化数据库并运行离线演示：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 demo
```

查看当前案件：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 list-cases
```

## 园区服务结算（仓位租约 → 服务账单）

`civicflow.park_*` 在平台既有能力上构建“广州公铁联运枢纽高标仓”结算依据链：

- **带有效期主数据**（`park_master.py` / `park_effective_records`）：仓位单元、交付验收、
  租约版本、设备计量点、服务目录、价格规则、维保停机、费用承担关系都带
  `valid_from/valid_to`。后签租约/价格变更新增版本并从未来时点生效、自动收口旧版本，
  历史账期始终按当时有效版本计价；历史差错只通过贷项/补单纠正。
- **能力预约与拼单拆分**（`park_ops.py`）：月台、冷库、充电、铁路接驳按容量窗口预约；
  跨租户作业确认时必须按租户拆清实际数量，不能全部落到一家；设备故障只取消该资源
  故障窗口内的安排，其它能力不动。
- **抄表接入**（`park_metering.py` + 事件收件箱）：计量编号即来源标识，同编号重复到达
  不重复入账，同编号不同读数写入收件箱冲突并暂停结算，运营裁决后恢复；共享计量点
  总用量按各租户实际数量分摊。
- **结算与关账**（`park_billing.py`）：账期计价自动扣除维保停机窗口、按有效期切片选择
  适用价目；缺抄表、读号冲突、找不到价目、未处理维保补偿都会产生挂起项并阻断计价/关账。
  贷项/补单走申请-审批，录入人不能批准自己的申请；关账把费用行落入不可变资金分录。
- **可下钻依据**：每笔费用行可下钻到占用记录（租约版本/作业用量）、原始抄表读数、
  适用价目版本和调整批准人。
- **租户边界**：租户上下文只能查询本企业（`org:<id>` scope）的账期、费用和用量。
- **重启可恢复**：缺抄表核查、维保补偿、租约到期提醒都持久化为 `scheduled_jobs`，
  进程重启后仍按原期限触发，无需人工重新登记。

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/park-demo.sqlite3 park-demo
```

