# dqtool —— 本地数据质量分析与验证工具

`dqtool` 是一个**完全本地化**的数据质量工具：所有数据集、规则、profile、基线和报告
只使用本地 CSV / JSONL / Parquet 文件或内存数据结构。**不依赖**数据仓库、
Great Expectations 服务、云平台或任何外部网络服务。

功能一览：

* **字段 profile**：类型、空值率、唯一值数量、数值范围（min/max/mean）、常见值等；
* **六类质量规则**：`not-null`、`unique`、`range`、`regex`、`allowed-values`、
  `composite-unique`（复合唯一键）；
* **逐行失败明细**：每条失败都带规则 ID、字段、0-based 行位置和简洁原因；
* **基线对比**：列缺失、新增列、类型变化、空值率显著变化、数值范围异常；
* **分块流式处理**：按块读取，不强制把整个数据集一次性装入内存；
* **JSON 报告 + 明确退出码**：适合终端人工查看，也适合 CI 流水线判定。

---

## 1. 安装与运行

需要 Python ≥ 3.9，依赖 `pandas`（CSV）和 `pyarrow`（Parquet）。

```bash
# 方式一：直接在仓库根目录用模块方式运行（无需安装）
python -m dqtool --help

# 方式二：可编辑安装，得到 dqtool 命令
pip install -e .
dqtool --help
```

## 2. 快速开始

示例数据位于 [`examples/`](examples/)。

```bash
# 生成 profile（默认打印到 stdout）
python -m dqtool profile examples/users.csv -o baseline.json

# 按规则校验，输出 JSON 报告
python -m dqtool validate examples/users.csv -r examples/rules.json -o report.json
echo $?   # 规则全部通过时为 0，存在失败时为 1

# 用新版本数据生成 profile，再与基线对比
python -m dqtool profile examples/users.csv -o current.json
python -m dqtool compare baseline.json current.json
```

也支持 JSONL 与 Parquet（扩展名自动识别，也可用 `-f/--format` 强制指定）：

```bash
python -m dqtool profile data.jsonl  --chunk-size 10000
python -m dqtool validate events.parquet -r rules.json
```

## 3. 规则文件格式

规则文件是 JSON，可以是一个对象 `{"rules": [...]}`，也可以直接是规则数组。
每条规则的通用字段：

| 字段 | 说明 |
|---|---|
| `id` | 规则 ID，可选；缺省时自动生成 `rule_1`、`rule_2`…… |
| `type` | 规则类型，见下表（必填） |
| `field` | 单字段规则的目标字段 |
| `fields` | 复合唯一键使用的字段列表 |
| `params` | 规则参数，因类型而异 |

### 3.1 规则类型与参数

| 类型 | 目标 | `params` | 失败原因（reason） |
|---|---|---|---|
| `not-null` | `field` | 无 | `value is null` |
| `unique` | `field` | `nulls`：`skip`（默认，忽略空值）/ `distinct`（空值也只允许出现一次）/ `fail`（任何空值都失败） | `duplicate value`、`duplicate null value`、`value is null` |
| `range` | `field` | `min`、`max`（至少给一个）；`min_inclusive`（默认 `true`）、`max_inclusive`（默认 `true`） | `value X below min Y`、`value X above max Y`、`not a numeric value: ...` |
| `regex` | `field` | `pattern`（正则字符串，必填，用 `re.search` 匹配）；`negate`（默认 `false`，为 `true` 时匹配反而失败） | `value does not match pattern ...` / `value matches forbidden pattern ...` |
| `allowed-values` | `field` | `allowed`：允许值列表（必填非空）；`coerce_numbers`（默认 `true`，数值串与数字等价，如 `"1"` 与 `1`） | `value 'x' is not in allowed set` |
| `composite-unique` | `fields` | `nulls`：`skip`（默认，任一组分列为空则整组跳过）/ `distinct`（空值形状相同的键也判重） | `duplicate composite key`、`duplicate composite key (null component)` |

通用语义：

* **空值默认被单字段检查“跳过”**（`not-null` 除外）——即 SQL 风格的谓词逻辑；
  要同时禁止空值，请额外配一条 `not-null` 规则。
* **相等性按规范化值判断**：`1`、`"1"`、`1.0` 视为相同；布尔值保持独立身份
  （`True` ≠ `1`）。
* **行位置**：`row` 是数据记录在文件中的 **0-based 序号**（CSV 表头不计），
  跨块保持稳定。
* 规则引用的字段在整个数据集中**完全不存在**时，报告会在顶层 `missing_fields`
  中列出并判定失败（属于配置问题），不会再额外产生逐行噪声。

示例：

```json
{
  "rules": [
    {"id": "id_nn",  "type": "not-null", "field": "id"},
    {"id": "id_uq",  "type": "unique",   "field": "id",
     "params": {"nulls": "distinct"}},
    {"id": "age_r",  "type": "range",    "field": "age",
     "params": {"min": 0, "max": 120}},
    {"id": "mail",   "type": "regex",    "field": "email",
     "params": {"pattern": "^[^@]+@[^@]+\\.[^@]+$"}},
    {"id": "st_av",  "type": "allowed-values", "field": "status",
     "params": {"allowed": ["active", "banned"]}},
    {"id": "ck",     "type": "composite-unique",
     "fields": ["id", "created_at"]}
  ]
}
```

## 4. Profile 的统计口径

`profile` 输出的每个字段包含：

| 指标 | 口径 |
|---|---|
| `total_count` | 该列出现的总行数（包括空值） |
| `null_count` / `null_rate` | 空值数量；`null_rate = null_count / total_count`，保留 6 位小数 |
| `unique_count` | 非空**规范化值**的去重计数；超过跟踪上限时为 `null`（其他指标不受影响） |
| `inferred_type` | `int` / `float` / `bool` / `string` / `numeric`（int+float 混合）/ `mixed`（其他混合）/ `null`（整列为空） |
| `type_counts` | 各推断类型的实际计数，便于查看“混合”的具体构成 |
| `min_value` / `max_value` / `mean` | 仅对**可解析为数字**的值统计（字符串 `"42"` 会计入）；均值用 Kahan 补偿求和，保留 6 位小数；非数值列为 `null` |
| `common_values` | 出现频次最高的若干值（默认前 10，`--top-k` 可调），含 `value` 与 `count`，按频次降序排列 |

**空值判定**：`None`、`NaN`/`NaT`、空字符串 `""`，以及 `null`、`none`、`nan`、
`na`、`n/a`、`#n/a`、`nil`（忽略大小写与首尾空白）都算空值。
CSV 以字符串方式读入（不让 pandas 自动猜类型），因此类型推断由 dqtool 统一完成。

常见值/去重的内存是有界的（内部 heavy-hitters 草图上限 10000 个键）：
基数低于上限时 `unique_count` 与频次为**精确**值；超过后 `unique_count` 返回
`null` 以示不可精确给出，常见值仍可用。

## 5. 基线比较

`compare` 比较的是两份 **profile JSON**（即 `profile` 命令的产物），不直接读取数据，
检测五类变化：

| 变化 | 说明 |
|---|---|
| `missing_columns` | 基线中存在、当前缺失的列 |
| `new_columns` | 当前新增的列 |
| `type_changes` | `inferred_type` 发生变化（全空列 `null` 不参与类型变化判定） |
| `null_rate_changes` | 空值率绝对变化量达到阈值（`--null-rate-delta`，默认 **0.10**，即 10 个百分点，含等于阈值） |
| `range_anomalies` | 数值列当前 min 低于基线 min，或 max 高于基线 max；`--range-tolerance` 给基线跨度（max−min）的容忍比例，默认 `0`（精确边界） |

输出带 `status: matched | changed`；只要有任何一类变化，退出码即为 `1`。

## 6. 命令行参考

```
dqtool profile  DATA [-f csv|jsonl|parquet] [--chunk-size N] [--top-k K] [-o OUT]
dqtool validate DATA -r RULES [-f ...] [--chunk-size N]
                 [--max-failures N] [--include-values] [-o REPORT]
dqtool compare  BASELINE_CURRENT_PROFILE CURRENT_PROFILE_JSON
                 [--null-rate-delta F] [--range-tolerance F] [-o OUT]
```

常用参数：

* `--chunk-size`：每个流式分块的行数，默认 `50000`。校验/profile 结果与分块大小无关。
* `--max-failures`：每条规则在报告中**保留**的失败明细上限，默认 `1000`。
  超出后只保留前 N 条并标记 `truncated: true`；`failure_count` 始终是精确总数。
* `--include-values`：默认失败明细不含具体取值，避免把敏感数据写进 CI 日志；
  加上该标志才会输出 `value` 字段。

### 退出码（CI 可直接判定）

| 退出码 | 含义 |
|---|---|
| `0` | 校验全部通过 / 两份 profile 匹配 / profile 生成成功 |
| `1` | 校验发现规则失败，或基线对比发现变化 |
| `2` | 输入错误：文件不存在、空文件、JSONL 某行 JSON 损坏、Parquet 损坏、格式无法识别等 |
| `3` | 配置错误：规则文件 JSON 非法、规则类型未知、正则非法、range 参数错误、profile 文件不合法等 |
| `4` | 未预期的内部错误 |

工具自身报错时，错误信息以 JSON 打印到 **stderr**（`{"status":"error",...}`），
因此 `--report` 指定的文件不会被写成一份看似“通过”的报告。

GitHub Actions 片段示例：

```yaml
- run: |
    python -m dqtool validate data/latest.csv -r dq-rules.json \
      --chunk-size 100000 -o dq-report.json
```

## 7. 内存与分块处理

* CSV 使用 `pandas.read_csv(chunksize=...)` 迭代器；JSONL 逐行读取后按块聚合；
  Parquet 使用 `pyarrow.parquet.ParquetFile.iter_batches(batch_size=...)`。
  三条路径都不会一次性载入全文件。
* profile 的数值统计为单遍流式累加；唯一键检查需要为每个已见键保存状态
  （精确去重的固有代价），但失败明细与分块缓冲都有上界。
* 已用 1000 万行 / 188MB 的 CSV 验证：`--chunk-size 10000` 做 profile 时峰值 RSS
  约 135MB；同机校验结果与分块大小无关（测试中以 1、2、7、32、100、1000
  行分块对比，失败计数与行号完全一致）。

## 8. Python API（纯内存使用）

不落地文件也可以直接在内存里用：

```python
from dqtool import parse_rules, validate_records, ProfileBuilder, compare_profiles

rules = parse_rules([
    {"id": "id_nn", "type": "not-null", "field": "id"},
    {"id": "id_uq", "type": "unique",   "field": "id"},
])
report = validate_records(rules, [{"id": 1}, {"id": 1}, {"id": None}])
print(report.passed, report.total_failures)
for result in report.results.values():
    for f in result.failures:
        print(f.rule_id, f.field, f.row, f.reason)

builder = ProfileBuilder(top_k=5)
builder.update(["id", "g"], [{"id": 1, "g": "a"}, {"id": 2, "g": "a"}])
profile = builder.to_dict()
diff = compare_profiles(baseline_profile, profile)
```

## 9. 运行测试

所有测试都在终端直接执行，无需任何外部服务：

```bash
# 在仓库根目录
python -m pytest
```

测试覆盖：空值判定、重复值（含三种 null 策略）、数值/类型错误、range 边界、
正则、允许值、复合唯一键、profile 统计、基线对比（五类变化与阈值边界）、
分块大小/三种格式等价性、损坏输入（坏 JSONL 带行号、坏 Parquet、空文件、
缺文件）以及 CLI 退出码（含用 `python -m dqtool` 起真实子进程验证退出码）。

## 10. 项目结构

```
dqtool/
  __init__.py     # 公共 API
  __main__.py     # python -m dqtool 入口
  cli.py          # profile / validate / compare 三个子命令与退出码
  readers.py      # CSV / JSONL / Parquet 分块读取
  values.py       # 空值判定、数值解析、类型推断、规范化
  rules.py        # 规则模型与规则文件校验
  validation.py   # 流式规则引擎与逐行失败明细
  profiler.py     # 流式字段 profile
  baseline.py     # 基线 profile 比较
  report.py       # JSON 报告输出
  errors.py       # DQInputError / DQConfigError
examples/         # 示例数据与规则
tests/            # pytest 自动化测试（110 个用例）
```
