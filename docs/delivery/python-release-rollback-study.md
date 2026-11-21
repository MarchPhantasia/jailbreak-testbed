# 千问在哪一步回退，装配时需要多少原文

五道新题使用 CPython 3.10.0 和 3.11.0 的标准库文档。在 A800Shanghai 上，同一台本地千问分别承担 Controller 的版本判断和 Assembler 的中文维护说明写作。六次收到材料通知，Controller 三次保留草稿，三次回退，六次都选对了存档位置。给 Assembler 保留每个版本中对应接口的完整条目，输入比连同邻近条目一起发送少了 61.5%，两组各有 4/5 份成品可直接使用。

## 自动流程

1. 在第一次模型调用前冻结五项请求、每题三项判断、正文应交代的三件事、两个 Git 修订和十份上游文件的 SHA-256。Controller 每次只处理一项判断。在第一项之前，以及每完成一项后，程序保存一份草稿。读旧版时能看到的材料截止在旧版。
2. 新材料到达后，Controller 读取原草稿和当时可见的原文，决定保留，或指定从哪一份旧存档继续。程序从磁盘按哈希恢复那个前缀。即使旧判断错了，只要模型选了保留，程序也会保留那个旧值。加权平均题额外重送一次相同的旧材料，可观察模型是否无故回退。
3. 版本判断结束后，把同一份最终交接表交给两组 Assembler。对照组每版收到目标接口及相邻两个条目，共六条。自动组用任务里点名的接口在两版的文档章节中查找，每版发一个完整条目，保留示例、版本注记和行号。找不到或有歧义时沿用对照组的材料。本批五个接口均唯一命中。
4. 先用冻结答案核对结构化判断，再隐藏首行判断和组别，把中文正文与要求、官方原文对读。成品合格须同时满足三个判断正确、正文覆盖三点、没有实质性错误。正文由 Codex 盲读，每轮先保存评分及题包哈希，再解开组别。

题目在 Git 中固定，模型只收到当时的任务和材料。主批次逐次核对了 31 条实际发出的请求、响应和账本，以及装配时实际发送的交接表和文档。[题目与判据](../../data/samples/python-release-notes-v1.json) · [CPython 3.10.0 文档原件](https://github.com/python/cpython/tree/b494f5935c92951e75597bfe1c8b1f3112fec270/Doc/library) · [CPython 3.11.0 文档原件](https://github.com/python/cpython/tree/deaf509e8fc6e0363bd6f26d52ad42f976ec42f2/Doc/library)

## 哪些旧判断需要改

| 接口和任务 | 收到新版本时实际执行 | 最终成品要交代的关键事实 | 六条原文 / 自动两条 |
| --- | --- | --- | --- |
| `statistics.fmean` 加权平均 | 从第一项前重写。此前重送相同旧材料时保留三项 | 3.11 可传同长度的权重，返回浮点数，空输入抛出异常 | 可用 / 可用 |
| `random.shuffle` 旧回调 | 保留第一项，从第二项前重写 | 可变序列原地打乱，3.11 移除了第二个随机回调参数，元组可通过采样得到新列表 | 可用 / 可用 |
| `datetime.date.fromisoformat` 日期导入 | 保留前两项，从第三项前重写 | 3.11 中带连字符和紧凑写法能解析成同一天的日期对象 | 可用 / 可用 |
| `time.sleep` 轮询间隔 | 保留三项 | 0.25 秒可传入，暂停调用线程，调度可能延长等待 | 可用 / 可用 |
| `pathlib.Path.glob` 递归列文件 | 保留三项 | 匹配范围、逐项得到的路径对象、大目录的代价，以及示例能否照写运行 | 需修改 / 需修改 |

每组 15/15 项结构化判断正确，三个真正变化的值都进入了两组的最终成品。最后一题暴露了看判断栏会漏掉的错误。两份正文把 `pathlib.Path.glob('**/*.py')` 写成可直接照用的调用，缺少目录实例。对 Python 3.11.15 实测，该写法抛出 `TypeError`。在目录实例上调用 `root.glob('**/*.py')` 则返回生成器，遍历时得到路径对象。官方 3.11.0 文档中已有可运行的实例写法。[pathlib 原文](https://github.com/python/cpython/blob/deaf509e8fc6e0363bd6f26d52ad42f976ec42f2/Doc/library/pathlib.rst)

## 分清回退与装配各自的贡献

在三道确有版本变化的题上，加做一次配对。保留自动选出的同两条原文、3.11 的任务说明、模型、采样参数和写作模板。把交接表换成从原始检查点读回的 3.10 判断及出处，另生成三份成品。更新交接组复用主批次的三份成品。两组各有 3/3 三项判断全对，正文也均合格。

这三题中，修正旧判断的动作真实发生在 Controller 的存档上。即便传入旧判断，Assembler 仍然能交付正确结论。因此无法把三份正确成品全部归功于 Controller 回退。这个结果适用于明确点名接口、有新旧原文可核对的任务。

## 只加强写作提示的复验

发现 `glob` 的问题后，在原模板中增加一段针对 Python 维护文档的提示，要求保留调用对象和必需参数，区分迭代器与元素。用此前相同的五份自动选段输入复跑。逐条解开两组真正发出的请求，确认任务、交接表、材料、模型和采样参数一致。以下是同一套开发题上的定向复验。

| 写作条件 | 三项判断全对 | 可直接使用的正文与判断均合格 | 五次装配输入 / 输出 token |
| --- | ---: | ---: | ---: |
| 原模板，主批次自动选段 | 5/5 | 4/5 | 6,840 / 1,908 |
| 增加示例提示，同样的自动选段 | 5/5 | 3/5 | 7,120 / 2,545 |

增加提示后，`glob` 仍出现相同错误。加权平均题多了正确的 87.6 计算示例，却漏掉原来写明的“权重与数据必须等长”。例子中两列恰好都有四项，读者无法从中知道长度不同会抛异常。其余三题仍合格。默认配置继续使用原模板。维护者可直接使用以下在 Python 3.11 上核对过的遍历写法。

```python
from pathlib import Path

root = Path(".")
for entry in root.glob("**/*.py"):
    print(entry)
```

本批任务的分工是程序按接口名找原文和保存存档，Controller 根据版本变化决定是否回退，Assembler 负责独立核对和编写成品。对于有确定执行语义的示例，直接检查调用能发现三个判断全对时仍然存在的错误。五题每个条件各生成一次，复验复用了原来的对照正文，同一位评阅者可能认出读过的文本。关于开放式任务中模型何时**自行发现**需要返工，本次只测试了收到版本通知之后的决定。

## 调用、产物与复算

模型为 A800Shanghai 的 `JonathanColetti/Qwen3.8-27B-Uncensored`。两角色温度为 0，关闭 thinking。单次输出上限分别为 Controller 1,024 和 Assembler 1,536 token。下表的时间从本轮第一条事件到最后一条事件。每列 token 都有实际用量回执。

| 实际运行与服务器目录 | 新增 Controller / Assembler 调用 | 输入 / 输出 token | 事件跨度 |
| --- | ---: | ---: | ---: |
| 主批次 `results/python-release-five-008cd28` | 21 / 10 | 72,476 / 6,594 | 255.2 秒 |
| 旧交接表 `results/python-release-old-0cacbd4` | 0 / 3 | 3,857 / 1,100 | 40.1 秒 |
| 写作提示复验 `results/python-release-writer-d2ecbe9` | 0 / 5 | 7,120 / 2,545 | 92.5 秒 |
| 合计 | 21 / 18 | 83,453 / 10,239 | |

共 39 次本地调用。Target 与外部 Judge 调用均为 0。每个目录的 `events.jsonl` 可核对派发和结算，`notes-to-read.json` 保存匿名正文，`prose-review.json` 绑定本轮盲读评分，`summary-reviewed.json` 为解盲后的汇总。主批次的每题下还保有旧草稿、每次决定、恢复的前缀、两种输入的证据包与候选。五道公开维护题只测上述角色和文档条件，整个越狱评测流程的 ASR 仍应使用其独立的任务与判据。

在服务器仓库根目录激活项目虚拟环境后，可用新目录复现。首次取上游材料后，后续对照使用本地内容和固定校验和。带 `--review` 的示例直接复算本次已保存的评分。

```bash
python scripts/study_python_releases.py --fetch-sources
python scripts/study_python_releases.py --run --out results/python-release-new
python scripts/study_python_releases.py --compare-old-handoff results/python-release-new --out results/python-release-new-old
python scripts/study_python_releases.py --compare-writer results/python-release-new --out results/python-release-new-writer
python scripts/study_python_releases.py --summarize results/python-release-five-008cd28 --review results/python-release-five-008cd28/prose-review.json
```

新目录可以直接用 `--summarize` 复算结构化判断与开销。若要计算正文合格数，应先按该目录的匿名包完成阅读。主批次冻结于 `008cd28`，旧表对照冻结于 `0cacbd4`，写作提示复验冻结于 `d2ecbe9`。[运行入口](../../scripts/study_python_releases.py) · [选段和存档代码](../../src/framework/python_release_notes.py)
