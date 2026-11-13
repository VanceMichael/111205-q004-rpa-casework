# 桌面表单录入的可恢复 RPA

财务共享中心把已复核的供应商资料从 CSV 录入旧版桌面客户端。客户端升级后保存
按钮偶尔变为二次确认；旧脚本按固定坐标盲点，造成重复供应商与半截地址。本项目把
该流程拆成**纯业务编排**与**可替换的界面动作层**，并让每一步都可以离线测试。

## 目录与职责

| 路径 | 职责 |
| --- | --- |
| `service.py` | 既有规范化接口 `plan_vendor`（税号去空白转大写、名称压缩空白） |
| `rpa/planner.py` | 批次导入、规范化、按税号去重、逐字段 dry-run 计划、状态定义 |
| `rpa/store.py` | 状态目录 `run-state.json`：记录状态、证据元数据、状态转移事件、人工队列 |
| `rpa/executor.py` | 按屏幕可见状态推进；确认框/校验失败/失焦/重启/超时均安全停下 |
| `desktop.py` | 本地桌面控件模拟器（默认保存门户状态，可注入故障） |
| `rpa/cli.py` | `dry-run` / `run` / `resume` / `status` 命令 |
| `demo.py` | 从导入到人工接管的完整离线演示 |
| `fixtures/vendors.{csv,json}` | 合成数据（含重复税号与缺地址行） |
| `tests/` | 基线回归、编排与执行、CLI/演示端到端测试 |

**不连接真实客户端。** 截图证据使用模拟器返回的可见屏幕元数据（窗口名、可见字段
列表与内容哈希），不是实际桌面截图。

## 环境

Python 3.11.10，仅标准库。无需登录、安装包或下载数据：

```sh
python3 -m unittest discover -s tests -v
python3 demo.py
```

演示实时运行，通常在数秒内结束，状态写入临时目录，重复执行结果一致。

## 从导入到人工接管

### 1. Dry-run：只生成逐字段计划

```sh
python3 -m rpa.cli dry-run --input fixtures/vendors.csv
```

输出每条记录的规范化计划与按顺序编号的动作（`open_form`、`refocus`、`fill`、
`verify_visible_values`、`click_save`）。批次内重复税号、缺地址行只显示 `skip`
及原因，不生成输入动作。**dry-run 不写门户、不写状态目录。**

若传入已存在的 `--workdir`，已提交记录显示为
`skip: refusing to replay: portal_committed`。

### 2. 执行：按可见状态推进，遇人工步骤暂停

```sh
python3 -m rpa.cli run --input fixtures/vendors.csv --workdir .rpa-state
```

- 保存前按规范化税号**先查门户**；已存在则记 `duplicate_portal`，不打开表单。
- 保存后读回门户记录并比对全部字段；不一致（例如半截地址）不记为成功。
- 二次确认框出现 → `awaiting_confirmation`，**不自动点第二次**，批次在此暂停。
- 校验失败 / 焦点丢失 → `unsaved_incomplete`，保留待办（如“补齐地址”）。
- 保存超时 → 立即查门户：已有该税号则对账为已提交；否则 `submit_unknown`，
  **绝不重放**。
- 客户端重启/崩溃 → `needs_review`，附人工步骤；已提交记录不会再被输入。

有需要人工的记录时退出码为 `1`，便于流水线拦截。

### 3. 选择性续做

修正输入后，只挑失败批次中的记录：

```sh
python3 -m rpa.cli resume --keys tax:CN124 \
  --input fixtures/vendors.fixed.csv --workdir .rpa-state
```

- `--keys` 外的记录一律跳过；已提交/重复记录即使被点名也不会重放。
- 确认框必须先由人工核对；`resume --confirm` 才允许点击确认，且确认后仍以可见
  屏幕与门户读回结果为准。
- 上次结果为 `submit_unknown` 的记录会先对账，门户已有则直接记为已提交。

### 4. 审计与人工队列

```sh
python3 -m rpa.cli status --workdir .rpa-state
```

`run-state.json` 为每条记录保存：

- `status` 与 `attempts`；
- `evidence`：每步的可见屏幕元数据（`kind`、`screen`、`window`、`visible_fields`、
  `sha256`、步名）；
- `transitions`：`from → to` 的事件序列（含动作、屏幕、说明）；
- `manual_steps`：交给人工的具体待办。

## 状态机

```
pending ──► [按税号查门户] ──► duplicate_portal
   │
   ├──► portal_committed       保存后读回字段一致
   ├──► duplicate_local        批次内规范化税号重复
   ├──► awaiting_confirmation  确认框可见，等人决策
   ├──► unsaved_incomplete     校验失败/失焦，未提交
   ├──► submit_unknown         保存超时且门户查不到，先对账
   └──► needs_review           数据缺失/字段不一致/未知屏幕/客户端异常
```

只有 `portal_committed` 表示可靠写入。`portal_committed`、`duplicate_local`、
`duplicate_portal` 是重放屏障；`unsaved_incomplete`、`awaiting_confirmation`、
`submit_unknown`、`needs_review` 需人工/显式续做。

## 测试如何注入故障

`desktop.py` 的 `DesktopControls` 与真实适配器接口一致，另可注入：

| 注入 | 效果 |
| --- | --- |
| `confirmation` | 保存时显示确认框，不提交 |
| `validation` | 保存时显示校验失败 |
| `focus_lost` | 输入或保存时窗口失焦 |
| `timeout_after_save` | **先落盘再抛** `TimeoutError`（验证不会重放） |
| `partial_write` | 落盘时地址被截断（验证读回比对） |
| `crash` | 下一次界面动作抛 `ConnectionError` 并重置会话 |

`DesktopControls(path)` 每次重建即模拟客户端重启：门户记录从磁盘读取，会话界面复位。

## 能力边界

- 模拟器只模拟本流程所需控件；分类器/OCR、真实客户端句柄与真实截图不在范围内。
- 证据是合成屏幕元数据；如需真实截屏需另接适配器。
- 去重以规范化税号为键，不做模糊匹配或跨系统合并。
- 演示不访问网络，状态目录由调用方指定。
