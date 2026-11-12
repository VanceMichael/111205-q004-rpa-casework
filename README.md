# 桌面表单录入的可恢复 RPA

## 现有能力

供应商字段规范化及桌面控件模拟器。模拟器保存门户侧状态，支持确认框、失焦、校验失败和保存后超时；业务流程、批次续做与人工接管尚未实现。

`service.py` 保持已有规范化接口。根目录 `test_service.py` 为原有回归测试，`tests/test_baseline.py` 会同时加载原测试和离线演示验证。

## 环境和运行

使用 Python 3.11.10，仅依赖标准库，无需登录账号、安装第三方包或下载数据。`.python-version` 固定解释器版本；`pyproject.toml` 仅登记 unittest 入口，项目不需要打包安装。

在仓库根目录执行：

```sh
python3 -m unittest discover -s tests -v
python3 demo.py
```

完整演示命令：

```sh
python3 -m unittest discover -s tests -v && python3 demo.py
```

演示实时读取合成输入并运行当前接口，通常在 90 秒以内结束，不回放历史日志。状态写入临时目录，重复执行不污染仓库；本地 HTTP 示例只访问回环地址，不连接生产系统。

## 合成来源

`fixtures/vendors.csv` 和 `fixtures/vendors.json` 为合成供应商。`desktop.py` 提供查询、打开表单、字段输入、保存及可见状态证据；`timeout_after_save` 会先保存再抛超时，重建模拟器可查询实际记录。模拟屏幕元数据不是实际桌面截图。

## 能力边界

当前自动化验证覆盖已有接口和离线响应器。业务编排、持久化的流程决策与跨模块恢复不属于当前实现范围，演示输出不代表这些扩展能力已经存在。
