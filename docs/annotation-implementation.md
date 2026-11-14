# BODY-2 实施覆盖与验证记录

基线：`server@49aff8e`。原始两份共识及历史背景按原样保存，本文件是实施记录，不改写原始共识。

## 补齐的公共规则

- 已记录次数按目标 subtask 的独立 attempt ID 去重；计数依据与 Episode 完成分别记录。恰好 N 次默认要求充分依据；UNKNOWN 的人工关联或逐 subtask 计数证据可以确定次数。不确定的 Episode 仍可手选导出。
- AI 应用校验来源、Episode 修订及完整模板签名。不同快照须先显式解决；范围应用仅导入独立关联，不暗改共享结果、评分或 Episode 字段。

## 原文条款覆盖

状态：待实施 → 实现中 → 已验证；每项保留入口及实测，未测不写通过。

| 原文条款 | 行为／范围 | 状态 | 实现与验证 |
| --- | --- | --- | --- |
| [自动 L8](automatic-annotation-consensus.md#L8) | 1. 系统定位与本轮范围 | 待实施 | — |
| [自动 L16](automatic-annotation-consensus.md#L16) | 2. 自由分析与模板标注分为两种模式 | 待实施 | — |
| [自动 L27](automatic-annotation-consensus.md#L27) | 3. 模板定义与切分单位 | 待实施 | — |
| [自动 L42](automatic-annotation-consensus.md#L42) | 4. 时间边界与精度 | 待实施 | — |
| [自动 L51](automatic-annotation-consensus.md#L51) | 5. NULL、未知和模板未覆盖 | 待实施 | — |
| [自动 L63](automatic-annotation-consensus.md#L63) | 6. 试跑与全量运行 | 待实施 | — |
| [自动 L74](automatic-annotation-consensus.md#L74) | 7. 人工审核与导出 | 待实施 | — |
| [自动 L84](automatic-annotation-consensus.md#L84) | 8. 自定义字段与评分 | 待实施 | — |
| [自动 L90](automatic-annotation-consensus.md#L90) | 9. 参数、重跑与标注版本 | 待实施 | — |
| [自动 L98](automatic-annotation-consensus.md#L98) | 10. 自动重试与预算 | 待实施 | — |
| [自动 L108](automatic-annotation-consensus.md#L108) | 11. 审核证据与可追溯性 | 待实施 | — |
| [自动 L114](automatic-annotation-consensus.md#L114) | 12. 后续实施的使用方式 | 待实施 | — |
| [工作区 L11](subtask-annotation-workbench-consensus.md#L11) | 1. 本次讨论的背景与结论边界 | 待实施 | — |
| [工作区 L26](subtask-annotation-workbench-consensus.md#L26) | 1.1 与相关文档的分工 | 待实施 | — |
| [工作区 L33](subtask-annotation-workbench-consensus.md#L33) | 2. 当前实现中看到的问题 | 待实施 | — |
| [工作区 L56](subtask-annotation-workbench-consensus.md#L56) | 3. 系统范围与交付原则 | 待实施 | — |
| [工作区 L58](subtask-annotation-workbench-consensus.md#L58) | 3.1 本轮负责的内容 | 待实施 | — |
| [工作区 L66](subtask-annotation-workbench-consensus.md#L66) | 3.2 已明确的范围边界 | 待实施 | — |
| [工作区 L74](subtask-annotation-workbench-consensus.md#L74) | 3.3 关于“把尝试信息贴到标注文字后面”的讨论结果 | 待实施 | — |
| [工作区 L80](subtask-annotation-workbench-consensus.md#L80) | 4. 核心概念与记录关系 | 待实施 | — |
| [工作区 L84](subtask-annotation-workbench-consensus.md#L84) | 4.1 模板与 subtask | 待实施 | — |
| [工作区 L96](subtask-annotation-workbench-consensus.md#L96) | 4.2 片段与尝试 | 待实施 | — |
| [工作区 L118](subtask-annotation-workbench-consensus.md#L118) | 4.3 结果、评分与备注的归属 | 待实施 | — |
| [工作区 L127](subtask-annotation-workbench-consensus.md#L127) | 4.4 示例：一次失败和一次成功，不等于三个片段就是三次尝试 | 待实施 | — |
| [工作区 L141](subtask-annotation-workbench-consensus.md#L141) | 5. 区间内容、人工审核与保存是不同维度 | 待实施 | — |
| [工作区 L143](subtask-annotation-workbench-consensus.md#L143) | 5.1 区间内容状态 | 待实施 | — |
| [工作区 L160](subtask-annotation-workbench-consensus.md#L160) | 5.2 人工审核状态 | 待实施 | — |
| [工作区 L172](subtask-annotation-workbench-consensus.md#L172) | 5.3 保存状态 | 待实施 | — |
| [工作区 L178](subtask-annotation-workbench-consensus.md#L178) | 6. 帧浏览与主时间轴 | 待实施 | — |
| [工作区 L180](subtask-annotation-workbench-consensus.md#L180) | 6.1 帧定位要求 | 待实施 | — |
| [工作区 L194](subtask-annotation-workbench-consensus.md#L194) | 6.2 界面首末帧语义 | 待实施 | — |
| [工作区 L215](subtask-annotation-workbench-consensus.md#L215) | 6.3 单主轨道 | 待实施 | — |
| [工作区 L223](subtask-annotation-workbench-consensus.md#L223) | 6.4 浏览和编辑的操作区域 | 待实施 | — |
| [工作区 L236](subtask-annotation-workbench-consensus.md#L236) | 7. 新建和修订的操作规则 | 待实施 | — |
| [工作区 L238](subtask-annotation-workbench-consensus.md#L238) | 7.1 从零标注 | 待实施 | — |
| [工作区 L252](subtask-annotation-workbench-consensus.md#L252) | 7.2 相邻边界联动 | 待实施 | — |
| [工作区 L266](subtask-annotation-workbench-consensus.md#L266) | 7.3 框选范围重标 | 待实施 | — |
| [工作区 L281](subtask-annotation-workbench-consensus.md#L281) | 7.4 修改标签时的作用范围 | 待实施 | — |
| [工作区 L290](subtask-annotation-workbench-consensus.md#L290) | 7.5 切分、合并片段与合并尝试 | 待实施 | — |
| [工作区 L298](subtask-annotation-workbench-consensus.md#L298) | 7.6 所有编辑形成可撤销的操作 | 待实施 | — |
| [工作区 L304](subtask-annotation-workbench-consensus.md#L304) | 8. 草稿、自动保存、离线与冲突 | 待实施 | — |
| [工作区 L306](subtask-annotation-workbench-consensus.md#L306) | 8.1 一次有效编辑就进入草稿 | 待实施 | — |
| [工作区 L316](subtask-annotation-workbench-consensus.md#L316) | 8.2 保存状态必须可见 | 待实施 | — |
| [工作区 L322](subtask-annotation-workbench-consensus.md#L322) | 8.3 断网与恢复 | 待实施 | — |
| [工作区 L330](subtask-annotation-workbench-consensus.md#L330) | 8.4 Episode 级隔离 | 待实施 | — |
| [工作区 L339](subtask-annotation-workbench-consensus.md#L339) | 9. 人工确认与“已完成”的定义 | 待实施 | — |
| [工作区 L341](subtask-annotation-workbench-consensus.md#L341) | 9.1 确认入口 | 待实施 | — |
| [工作区 L349](subtask-annotation-workbench-consensus.md#L349) | 9.2 整条 Episode 声明完成的条件 | 待实施 | — |
| [工作区 L360](subtask-annotation-workbench-consensus.md#L360) | 9.3 未完成草稿仍可导出 | 待实施 | — |
| [工作区 L371](subtask-annotation-workbench-consensus.md#L371) | 10. 模板升级与 AI 重跑 | 待实施 | — |
| [工作区 L373](subtask-annotation-workbench-consensus.md#L373) | 10.1 模板快照与显式升级 | 待实施 | — |
| [工作区 L382](subtask-annotation-workbench-consensus.md#L382) | 10.2 AI 重跑产生候选，不静默覆盖当前人工工作 | 待实施 | — |
| [工作区 L398](subtask-annotation-workbench-consensus.md#L398) | 11. 默认导出为结构化标注包 | 待实施 | — |
| [工作区 L400](subtask-annotation-workbench-consensus.md#L400) | 11.1 默认交付内容 | 待实施 | — |
| [工作区 L418](subtask-annotation-workbench-consensus.md#L418) | 11.2 完整数据集按需导出 | 待实施 | — |
| [工作区 L427](subtask-annotation-workbench-consensus.md#L427) | 11.3 补充需求 R31：筛选、选择并导出部分 Episode | 待实施 | — |
| [工作区 L451](subtask-annotation-workbench-consensus.md#L451) | 11.4 子集以完整 Episode 为单位 | 待实施 | — |
| [工作区 L466](subtask-annotation-workbench-consensus.md#L466) | 11.5 子集数据与来源的一致性要求 | 待实施 | — |
| [工作区 L481](subtask-annotation-workbench-consensus.md#L481) | 11.6 尝试次数筛选：按指定 subtask 统计 | 待实施 | — |
| [工作区 L516](subtask-annotation-workbench-consensus.md#L516) | 12. 典型使用情境 | 待实施 | — |
| [工作区 L518](subtask-annotation-workbench-consensus.md#L518) | 12.1 从零标注一条任意顺序的数据 | 待实施 | — |
| [工作区 L527](subtask-annotation-workbench-consensus.md#L527) | 12.2 修正 AI 的边界偏差 | 待实施 | — |
| [工作区 L533](subtask-annotation-workbench-consensus.md#L533) | 12.3 修正 AI 的过度合并或过度切分 | 待实施 | — |
| [工作区 L539](subtask-annotation-workbench-consensus.md#L539) | 12.4 调整后成功，但下游用途未定 | 待实施 | — |
| [工作区 L543](subtask-annotation-workbench-consensus.md#L543) | 12.5 长时间离线后恢复 | 待实施 | — |
| [工作区 L547](subtask-annotation-workbench-consensus.md#L547) | 12.6 按不同尝试次数分别提取 Episode | 待实施 | — |
| [工作区 L553](subtask-annotation-workbench-consensus.md#L553) | 13. 后续实现与验收检查表 | 待实施 | — |
| [工作区 L604](subtask-annotation-workbench-consensus.md#L604) | 14. 30 项决策及后续补充需求索引 | 待实施 | — |
| [工作区 L642](subtask-annotation-workbench-consensus.md#L642) | 15. 后续实现需要关注的代码入口 | 待实施 | — |

## 决策与验收场景

| 编号 | 已确认条款／场景 | 状态 | 实现与验证 |
| --- | --- | --- | --- |
| A01 | 模板标签按 `A → C → A → B` 出现；正常表达，不强制重排、补齐或合并 | 待实施 | — |
| A02 | 标记单帧及最后一帧；界面首末帧包含规则正确，内部换算无越界和少一帧 | 待实施 | — |
| A03 | 拖动松手、逐帧前后和多相机定位；画面、数据帧号和标注位置一致 | 待实施 | — |
| A04 | 长视频定位一个短片段；可粗定位后局部放大、逐帧微调 | 待实施 | — |
| A05 | 点选片段、输入备注、使用快捷键；点选不强制跳起点，文字输入不触发编辑快捷键 | 待实施 | — |
| A06 | 拖动首尾贴合的 A/B 公共边界；两边一起更新，不需先手工制造空隙 | 待实施 | — |
| A07 | 调整原本不相邻的两段边缘；保留原空白，不自动填标签 | 待实施 | — |
| A08 | 在 A 内框选范围重标 B；正确切出局部，前后 A 保留，整体可撤销 | 待实施 | — |
| A09 | 切开一段属于同一次尝试的记录；尝试数量不自动增加 | 待实施 | — |
| A10 | 同名的失败与成功尝试相邻；保留两次身份和各自结果，不自动合并 | 待实施 | — |
| A11 | 明确合并不同尝试且字段冲突；人工选择最终值，原值可追溯 | 待实施 | — |
| A12 | 多个片段属于同一次尝试，修改评分；共用评分一致，局部备注保持各自归属 | 待实施 | — |
| A13 | 只重标同次尝试的一个范围；明确作用范围，重新核对归属与结果，不静默改其他段 | 待实施 | — |
| A14 | 留空、NULL、UNKNOWN、OUT_OF_TEMPLATE 并存；四种情况分清，导出仍可区分 | 待实施 | — |
| A15 | 知道在抓取，仅不能判断结果；保留抓取，结果为无法判断 | 待实施 | — |
| A16 | 特殊状态出现多段；不根据片段数自动增加尝试次数 | 待实施 | — |
| A17 | 新片段信息和边界输入后切换编辑对象；输入有明确保留，不游离于草稿保护之外 | 待实施 | — |
| A18 | 连续调整、切分、换标签后撤销和重做；完整恢复范围、稳定身份、关联信息和相关状态 | 待实施 | — |
| A19 | 保存成功但尚未人工确认；明确显示待核对，不误称完成 | 待实施 | — |
| A20 | 确认一段范围，里面仍有空白；仅确认已有内容，不自动把空白变成 NULL | 待实施 | — |
| A21 | 修改已确认片段；受影响内容重新待核对，可追溯先前状态 | 待实施 | — |
| A22 | 尝试结果缺省或 OOT 缺描述；草稿可留存，声明完成时提示缺失 | 待实施 | — |
| A23 | 全片人工确认且包含 UNKNOWN/OOT；必要信息完整时允许完成 | 待实施 | — |
| A24 | 两个窗口编辑不同 Episode；不因共享数据集版本号产生无关冲突 | 待实施 | — |
| A25 | 两个窗口编辑同一 Episode；冲突可见，双方内容保留，不自动覆盖 | 待实施 | — |
| A26 | 断网、切换 Episode、恢复连接；本地草稿可恢复，未同步可见，恢复后重试 | 待实施 | — |
| A27 | 共享模板更新后打开旧标注；保持原快照，只有明确升级才改变 | 待实施 | — |
| A28 | 升级时旧标签被删除或含义改变；展示差异并人工处理，不能自动按近似名称映射 | 待实施 | — |
| A29 | 对人工修订过的数据重新运行 AI；保留当前内容，生成独立候选，选择范围应用后待核对且可撤销 | 待实施 | — |
| A30 | 导出部分草稿及完整数据集；默认标注包轻量；草稿状态真实；完整副本仅按需生成 | 待实施 | — |
| A31 | 导出后重新读取；模板、片段、尝试、结果、特殊状态、备注、核对状态的含义与身份一致 | 待实施 | — |
| A32 | 手动选择来源 Episode 1、4、9 导出；输出恰好包含这三条 Episode 的完整内容与对应标注 | 待实施 | — |
| A33 | 同次尝试拆为多个片段后按次数筛选；去重后的尝试次数不变，筛选不受片段切分影响；采用的统计口径明确 | 待实施 | — |
| A34 | 筛选后批量勾选并翻页；当前页、全部筛选结果和实际选择范围明确，导出清单可预览 | 待实施 | — |
| A35 | 没有匹配项或选择的 Episode 已失效；清楚提示，不能悄然导出全部来源数据 | 待实施 | — |
| A36 | 子集涉及共享 Parquet／共享视频文件；数据、视频区间、媒体偏移和标注保持正确对应 | 待实施 | — |
| A37 | 输出重新编号 Episode 或帧索引；元数据、统计、标注及 sidecar 同步更新，并保留来源映射 | 待实施 | — |
| A38 | 排队后继续修改筛选或草稿；已提交导出采用固定的选择和版本，不受后续编辑静默影响 | 待实施 | — |
| A39 | 标注不完整的 Episode 参与查看或导出；不把次数缺失当零；输出保留未完成和待核对信息 | 待实施 | — |
| A40 | 子集数据集脱离来源目录后读取；输出仍能独立使用，不依赖原目录位置；来源数据未被改写 | 待实施 | — |
| A41 | 同一 Episode 中抓取 2 次、搬运 1 次、放置 1 次；“抓取 = 2 次”正确匹配，不以全部尝试总数 4 替代 | 待实施 | — |
| A42 | 人工加入未匹配筛选条件的 Episode，并重复加入已有条目；允许人工选择，来源 Episode 去重，最终清单可增删并准确导出 | 待实施 | — |
| Q1 | 正式标注用模板词表；新增改名独立操作；模板提供含义及起止判据；第 4.1 节 | 待实施 | — |
| Q2 | 尝试按实际执行判断，切分本身不自动增加次数；第 4.2、7.5 节 | 待实施 | — |
| Q3 | 采用 NULL、UNKNOWN、OUT_OF_TEMPLATE 三种特殊状态；第 5.1 节 | 待实施 | — |
| Q4 | 一条主时间轨道，任意顺序、重复和空白均可表达；第 6.3 节 | 待实施 | — |
| Q5 | 以统一数据帧精确定位，多相机跟随，提供粗细两种浏览尺度；第 6.1 节 | 待实施 | — |
| Q6 | 有效编辑即入草稿，自动保存，全部编辑支持撤销重做；第 7.6、8.1 节 | 待实施 | — |
| Q7 | 未标注空白与三个特殊状态分别保留；第 5.1 节 | 待实施 | — |
| Q8 | 已知 subtask 但结果不明时，保留标签，结果填无法判断；第 5.1 节 | 待实施 | — |
| Q9 | OOT 必须有动作描述；UNKNOWN 原因与 NULL 备注可选；第 5.1 节 | 待实施 | — |
| Q10 | 同次尝试可跨多个片段，人工明确关联；第 4.2、4.4 节 | 待实施 | — |
| Q11 | 界面首帧末帧都包含，内部明确转换边界约定；第 6.2 节 | 待实施 | — |
| Q12 | 保存与冲突按 Episode 隔离，同 Episode 冲突保留双方；第 8.4 节 | 待实施 | — |
| Q13 | 原标注保留模板快照，升级明确进行并展示差异；第 10.1 节 | 待实施 | — |
| Q14 | 同次尝试共用结果评分，局部观察另记；第 4.3 节 | 待实施 | — |
| Q15 | 首尾贴合的片段联动调公共边界，有空白则分别调边缘；第 7.2 节 | 待实施 | — |
| Q16 | 允许框选范围重标，自动保留前后内容，整体可撤销；第 7.3 节 | 待实施 | — |
| Q17 | 普通合并片段与合并尝试分开，不同尝试需明确确认；第 7.5 节 | 待实施 | — |
| Q18 | 内容与人工审核状态独立，AI 默认待核，变更后重新核对；第 5.2、9.1 节 | 待实施 | — |
| Q19 | 离线可继续工作，本地恢复、未同步提示、重试及冲突保护；第 8.3 节 | 待实施 | — |
| Q20 | 合并尝试时，冲突信息由人决定，原记录保留历史；第 7.5 节 | 待实施 | — |
| Q21 | 改标签明确当前范围或整次尝试；局部变更后重新核对关联和信息；第 7.4 节 | 待实施 | — |
| Q22 | 全片处理且人工确认才算完成；特殊状态允许；草稿仍可导出；第 9.2、9.3 节 | 待实施 | — |
| Q23 | 支持范围批量确认和创建并确认，确认不填补空白；第 9.1 节 | 待实施 | — |
| Q24 | AI 重跑生成独立候选，按范围明确应用，应用后待核且可撤销；第 10.2 节 | 待实施 | — |
| Q25 | 模板升级不匹配时由人处理，不近似自动映射，可继续旧快照；第 10.1 节 | 待实施 | — |
| Q26 | 从零以选范围再贴标签为主，框选与标起止等价，可快捷延续下一段起点；第 7.1 节 | 待实施 | — |
| Q27 | 浏览、选中、边缘调整、框选、整段移动入口明确，快捷键避开文字输入；第 6.4 节 | 待实施 | — |
| Q28 | 完成时尝试结果必填，评分普通备注可选，草稿允许暂缺；第 4.3、9.2 节 | 待实施 | — |
| Q29 | 特殊状态不自动计尝试；NULL 不关联，UNKNOWN 可关联已有，OOT 可人工建立或关联；第 5.1 节 | 待实施 | — |
| Q30 | 默认导出完整结构化标注 JSON，完整数据集副本按需生成；第 11 节 | 待实施 | — |
| R31 | 按指定 subtask 的独立尝试次数筛选，也可人工逐条加入目标集合；导出部分完整 Episode 为独立子集，保留标注与来源映射；第 11.3–11.6、12.6 节 | 待实施 | — |

## 验证记录

尚未执行。本任务只使用离线合成 fixture 与可控模型，不调用付费模型或真实数据。

## 未证明范围

- 真实模型边界误差 ±0.2s、现场真机、真实规模性能。
- 后续将记录全部实际验证和剩余限制。
