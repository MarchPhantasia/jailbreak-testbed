# fixtures

| 文件 | 用途 | 说明 |
|---|---|---|
| `synthetic_library/tasks.jsonl` | 合成 episode 闭环 | 图书馆资料整理形态；含模拟 Target 脚本，不含真实有害载荷 |
| `semantic_control_cases.jsonl` | 语义裁判控制样例 | 6 个无害对照情形（条件保留/条件丢失/缺项/来源无外部参照/有效不足/裁判不可用） |

`semantic_control_cases.jsonl` 来自执行包 `execution1-r2`，只用于人工校准与测试设计，
不是 benchmark 输入，也不能当作语义裁判的准确率验证。
