# 数字助教支持分流

面向老年学习服务的内部资料分析工具。当前支持版本资料检查、数字助教支持分流的描述性汇总、支持分流公平性评估以及分析历史。`overview` 只描述已有观察；`support_fairness` 用实际随机抽查机制估计现有评分在不同群体的误差，并比较有限名额下的候选分流规则，只形成可复核建议，不自动生成干预结论。`learning/domain.py` 包含业务校验及汇总逻辑；`learning/fairness.py` 包含公平性分析。资料源和应用是同一机器的两个本地 HTTP 进程，分别保留来源契约和分析消费契约。

## 安装和验证

使用 Python 3.11.2，数值依赖固定在 requirements.txt；部署时先安装，不在服务启动或测试过程中下载。

```bash
python3 -m pip install -r requirements.txt
python3 -m compileall -q learning scripts tests
python3 -m unittest discover -s tests -v
python3 scripts/demo.py
```

最后一条命令启动两个真实服务，等待就绪后读取目录、执行当前汇总和公平性分析、保存并回读结果，然后退出。它使用临时数据库，不会覆盖已有分析；服务就绪即继续，最长就绪期限为 65 秒。正常执行只需数秒。使用容器时先构建再运行，构建不计入业务演示时间：

```bash
docker build -t support-fairness:local .
docker run --rm support-fairness:local
```

## 持续运行

```bash
python3 -m learning.provider
python3 -m learning.service
```

以上命令在不同终端执行，应用默认把结果写入 var/analyses.sqlite3。数据字段、缺失含义和版本差异见 docs/data-contract.md；应用接口与幂等约定见 docs/api.md。扩展业务分析时应保持原有 overview 和已保存分析可查询。
