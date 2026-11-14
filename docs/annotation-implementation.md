# BODY-2 实施覆盖与验证记录

基线：`server@49aff8e`。原始两份共识及历史背景按原样保存，本文件是实施记录，不改写原始共识。

## 补齐的公共规则

- 已记录次数按目标 subtask 的独立 attempt ID 去重；计数依据与 Episode 完成分别记录。恰好 N 次默认要求充分依据；UNKNOWN 的人工关联或逐 subtask 计数证据可以确定次数。不确定的 Episode 仍可手选导出。
- AI 应用校验来源、Episode 修订及完整模板签名。不同快照须先显式解决；范围应用仅导入独立关联，不暗改共享结果、评分或 Episode 字段。

## 原文条款覆盖

状态：待实施 → 实现中 → 已验证；每项保留入口及实测，未测不写通过。

| 原文条款 | 行为／范围 | 状态 | 实现与验证 |
| --- | --- | --- | --- |
| [自动 L8](automatic-annotation-consensus.md#L8) | 1. 系统定位与本轮范围 | 实现与验证进行中 | `argus_template_contract.py`、`argus_template_runtime.py`、补丁 0003、`template_annotation_v2.py`；已有单契约与真实固定版离线链路回归 |
| [自动 L16](automatic-annotation-consensus.md#L16) | 2. 自由分析与模板标注分为两种模式 | 实现与验证进行中 | `argus_template_contract.py`、`argus_template_runtime.py`、补丁 0003、`template_annotation_v2.py`；已有单契约与真实固定版离线链路回归 |
| [自动 L27](automatic-annotation-consensus.md#L27) | 3. 模板定义与切分单位 | 实现与验证进行中 | `argus_template_contract.py`、`argus_template_runtime.py`、补丁 0003、`template_annotation_v2.py`；已有单契约与真实固定版离线链路回归 |
| [自动 L42](automatic-annotation-consensus.md#L42) | 4. 时间边界与精度 | 实现与验证进行中 | `argus_template_contract.py`、`argus_template_runtime.py`、补丁 0003、`template_annotation_v2.py`；已有单契约与真实固定版离线链路回归 |
| [自动 L51](automatic-annotation-consensus.md#L51) | 5. NULL、未知和模板未覆盖 | 实现与验证进行中 | `argus_template_contract.py`、`argus_template_runtime.py`、补丁 0003、`template_annotation_v2.py`；已有单契约与真实固定版离线链路回归 |
| [自动 L63](automatic-annotation-consensus.md#L63) | 6. 试跑与全量运行 | 实现与验证进行中 | `argus_template_contract.py`、`argus_template_runtime.py`、补丁 0003、`template_annotation_v2.py`；已有单契约与真实固定版离线链路回归 |
| [自动 L74](automatic-annotation-consensus.md#L74) | 7. 人工审核与导出 | 实现与验证进行中 | `argus_template_contract.py`、`argus_template_runtime.py`、补丁 0003、`template_annotation_v2.py`；已有单契约与真实固定版离线链路回归 |
| [自动 L84](automatic-annotation-consensus.md#L84) | 8. 自定义字段与评分 | 实现与验证进行中 | `argus_template_contract.py`、`argus_template_runtime.py`、补丁 0003、`template_annotation_v2.py`；已有单契约与真实固定版离线链路回归 |
| [自动 L90](automatic-annotation-consensus.md#L90) | 9. 参数、重跑与标注版本 | 实现与验证进行中 | `argus_template_contract.py`、`argus_template_runtime.py`、补丁 0003、`template_annotation_v2.py`；已有单契约与真实固定版离线链路回归 |
| [自动 L98](automatic-annotation-consensus.md#L98) | 10. 自动重试与预算 | 实现与验证进行中 | `argus_template_contract.py`、`argus_template_runtime.py`、补丁 0003、`template_annotation_v2.py`；已有单契约与真实固定版离线链路回归 |
| [自动 L108](automatic-annotation-consensus.md#L108) | 11. 审核证据与可追溯性 | 实现与验证进行中 | `argus_template_contract.py`、`argus_template_runtime.py`、补丁 0003、`template_annotation_v2.py`；已有单契约与真实固定版离线链路回归 |
| [自动 L114](automatic-annotation-consensus.md#L114) | 12. 后续实施的使用方式 | 实现与验证进行中 | `argus_template_contract.py`、`argus_template_runtime.py`、补丁 0003、`template_annotation_v2.py`；已有单契约与真实固定版离线链路回归 |
| [工作区 L11](subtask-annotation-workbench-consensus.md#L11) | 1. 本次讨论的背景与结论边界 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L26](subtask-annotation-workbench-consensus.md#L26) | 1.1 与相关文档的分工 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L33](subtask-annotation-workbench-consensus.md#L33) | 2. 当前实现中看到的问题 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L56](subtask-annotation-workbench-consensus.md#L56) | 3. 系统范围与交付原则 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L58](subtask-annotation-workbench-consensus.md#L58) | 3.1 本轮负责的内容 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L66](subtask-annotation-workbench-consensus.md#L66) | 3.2 已明确的范围边界 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L74](subtask-annotation-workbench-consensus.md#L74) | 3.3 关于“把尝试信息贴到标注文字后面”的讨论结果 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L80](subtask-annotation-workbench-consensus.md#L80) | 4. 核心概念与记录关系 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L84](subtask-annotation-workbench-consensus.md#L84) | 4.1 模板与 subtask | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L96](subtask-annotation-workbench-consensus.md#L96) | 4.2 片段与尝试 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L118](subtask-annotation-workbench-consensus.md#L118) | 4.3 结果、评分与备注的归属 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L127](subtask-annotation-workbench-consensus.md#L127) | 4.4 示例：一次失败和一次成功，不等于三个片段就是三次尝试 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L141](subtask-annotation-workbench-consensus.md#L141) | 5. 区间内容、人工审核与保存是不同维度 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L143](subtask-annotation-workbench-consensus.md#L143) | 5.1 区间内容状态 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L160](subtask-annotation-workbench-consensus.md#L160) | 5.2 人工审核状态 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L172](subtask-annotation-workbench-consensus.md#L172) | 5.3 保存状态 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L178](subtask-annotation-workbench-consensus.md#L178) | 6. 帧浏览与主时间轴 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L180](subtask-annotation-workbench-consensus.md#L180) | 6.1 帧定位要求 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L194](subtask-annotation-workbench-consensus.md#L194) | 6.2 界面首末帧语义 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L215](subtask-annotation-workbench-consensus.md#L215) | 6.3 单主轨道 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L223](subtask-annotation-workbench-consensus.md#L223) | 6.4 浏览和编辑的操作区域 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L236](subtask-annotation-workbench-consensus.md#L236) | 7. 新建和修订的操作规则 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L238](subtask-annotation-workbench-consensus.md#L238) | 7.1 从零标注 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L252](subtask-annotation-workbench-consensus.md#L252) | 7.2 相邻边界联动 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L266](subtask-annotation-workbench-consensus.md#L266) | 7.3 框选范围重标 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L281](subtask-annotation-workbench-consensus.md#L281) | 7.4 修改标签时的作用范围 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L290](subtask-annotation-workbench-consensus.md#L290) | 7.5 切分、合并片段与合并尝试 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L298](subtask-annotation-workbench-consensus.md#L298) | 7.6 所有编辑形成可撤销的操作 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L304](subtask-annotation-workbench-consensus.md#L304) | 8. 草稿、自动保存、离线与冲突 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L306](subtask-annotation-workbench-consensus.md#L306) | 8.1 一次有效编辑就进入草稿 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L316](subtask-annotation-workbench-consensus.md#L316) | 8.2 保存状态必须可见 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L322](subtask-annotation-workbench-consensus.md#L322) | 8.3 断网与恢复 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L330](subtask-annotation-workbench-consensus.md#L330) | 8.4 Episode 级隔离 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L339](subtask-annotation-workbench-consensus.md#L339) | 9. 人工确认与“已完成”的定义 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L341](subtask-annotation-workbench-consensus.md#L341) | 9.1 确认入口 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L349](subtask-annotation-workbench-consensus.md#L349) | 9.2 整条 Episode 声明完成的条件 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L360](subtask-annotation-workbench-consensus.md#L360) | 9.3 未完成草稿仍可导出 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L371](subtask-annotation-workbench-consensus.md#L371) | 10. 模板升级与 AI 重跑 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L373](subtask-annotation-workbench-consensus.md#L373) | 10.1 模板快照与显式升级 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L382](subtask-annotation-workbench-consensus.md#L382) | 10.2 AI 重跑产生候选，不静默覆盖当前人工工作 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L398](subtask-annotation-workbench-consensus.md#L398) | 11. 默认导出为结构化标注包 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L400](subtask-annotation-workbench-consensus.md#L400) | 11.1 默认交付内容 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L418](subtask-annotation-workbench-consensus.md#L418) | 11.2 完整数据集按需导出 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L427](subtask-annotation-workbench-consensus.md#L427) | 11.3 补充需求 R31：筛选、选择并导出部分 Episode | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L451](subtask-annotation-workbench-consensus.md#L451) | 11.4 子集以完整 Episode 为单位 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L466](subtask-annotation-workbench-consensus.md#L466) | 11.5 子集数据与来源的一致性要求 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L481](subtask-annotation-workbench-consensus.md#L481) | 11.6 尝试次数筛选：按指定 subtask 统计 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L516](subtask-annotation-workbench-consensus.md#L516) | 12. 典型使用情境 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L518](subtask-annotation-workbench-consensus.md#L518) | 12.1 从零标注一条任意顺序的数据 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L527](subtask-annotation-workbench-consensus.md#L527) | 12.2 修正 AI 的边界偏差 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L533](subtask-annotation-workbench-consensus.md#L533) | 12.3 修正 AI 的过度合并或过度切分 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L539](subtask-annotation-workbench-consensus.md#L539) | 12.4 调整后成功，但下游用途未定 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L543](subtask-annotation-workbench-consensus.md#L543) | 12.5 长时间离线后恢复 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L547](subtask-annotation-workbench-consensus.md#L547) | 12.6 按不同尝试次数分别提取 Episode | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L553](subtask-annotation-workbench-consensus.md#L553) | 13. 后续实现与验收检查表 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L604](subtask-annotation-workbench-consensus.md#L604) | 14. 30 项决策及后续补充需求索引 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |
| [工作区 L642](subtask-annotation-workbench-consensus.md#L642) | 15. 后续实现需要关注的代码入口 | 实现与验证进行中 | 公共模型与统一工作区；对应 A 场景记录为准 |

## 决策与验收场景

| 编号 | 已确认条款／场景 | 状态 | 实现与验证 |
| --- | --- | --- | --- |
| A01 | 模板标签按 `A → C → A → B` 出现；正常表达，不强制重排、补齐或合并 | 已实现；交互验收进行中 | `workbench-model.ts`、`workbench.ts`；TS 7 项及带帧号双相机页面已跑，完整交互回归未结束 |
| A02 | 标记单帧及最后一帧；界面首末帧包含规则正确，内部换算无越界和少一帧 | 已实现；交互验收进行中 | `workbench-model.ts`、`workbench.ts`；TS 7 项及带帧号双相机页面已跑，完整交互回归未结束 |
| A03 | 拖动松手、逐帧前后和多相机定位；画面、数据帧号和标注位置一致 | 已实现；交互验收进行中 | `workbench-model.ts`、`workbench.ts`；TS 7 项及带帧号双相机页面已跑，完整交互回归未结束 |
| A04 | 长视频定位一个短片段；可粗定位后局部放大、逐帧微调 | 已实现；交互验收进行中 | `workbench-model.ts`、`workbench.ts`；TS 7 项及带帧号双相机页面已跑，完整交互回归未结束 |
| A05 | 点选片段、输入备注、使用快捷键；点选不强制跳起点，文字输入不触发编辑快捷键 | 已实现；交互验收进行中 | `workbench-model.ts`、`workbench.ts`；TS 7 项及带帧号双相机页面已跑，完整交互回归未结束 |
| A06 | 拖动首尾贴合的 A/B 公共边界；两边一起更新，不需先手工制造空隙 | 已实现；交互验收进行中 | `workbench-model.ts`、`workbench.ts`；TS 7 项及带帧号双相机页面已跑，完整交互回归未结束 |
| A07 | 调整原本不相邻的两段边缘；保留原空白，不自动填标签 | 已实现；交互验收进行中 | `workbench-model.ts`、`workbench.ts`；TS 7 项及带帧号双相机页面已跑，完整交互回归未结束 |
| A08 | 在 A 内框选范围重标 B；正确切出局部，前后 A 保留，整体可撤销 | 已实现；交互验收进行中 | `workbench-model.ts`、`workbench.ts`；TS 7 项及带帧号双相机页面已跑，完整交互回归未结束 |
| A09 | 切开一段属于同一次尝试的记录；尝试数量不自动增加 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A10 | 同名的失败与成功尝试相邻；保留两次身份和各自结果，不自动合并 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A11 | 明确合并不同尝试且字段冲突；人工选择最终值，原值可追溯 | 已实现；交互验收进行中 | `workbench-model.ts`、`workbench.ts`；TS 7 项及带帧号双相机页面已跑，完整交互回归未结束 |
| A12 | 多个片段属于同一次尝试，修改评分；共用评分一致，局部备注保持各自归属 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A13 | 只重标同次尝试的一个范围；明确作用范围，重新核对归属与结果，不静默改其他段 | 已实现；交互验收进行中 | `workbench-model.ts`、`workbench.ts`；TS 7 项及带帧号双相机页面已跑，完整交互回归未结束 |
| A14 | 留空、NULL、UNKNOWN、OUT_OF_TEMPLATE 并存；四种情况分清，导出仍可区分 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A15 | 知道在抓取，仅不能判断结果；保留抓取，结果为无法判断 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A16 | 特殊状态出现多段；不根据片段数自动增加尝试次数 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A17 | 新片段信息和边界输入后切换编辑对象；输入有明确保留，不游离于草稿保护之外 | 已实现；交互验收进行中 | `workbench-model.ts`、`workbench.ts`；TS 7 项及带帧号双相机页面已跑，完整交互回归未结束 |
| A18 | 连续调整、切分、换标签后撤销和重做；完整恢复范围、稳定身份、关联信息和相关状态 | 已实现；交互验收进行中 | `workbench-model.ts`、`workbench.ts`；TS 7 项及带帧号双相机页面已跑，完整交互回归未结束 |
| A19 | 保存成功但尚未人工确认；明确显示待核对，不误称完成 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A20 | 确认一段范围，里面仍有空白；仅确认已有内容，不自动把空白变成 NULL | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A21 | 修改已确认片段；受影响内容重新待核对，可追溯先前状态 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A22 | 尝试结果缺省或 OOT 缺描述；草稿可留存，声明完成时提示缺失 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A23 | 全片人工确认且包含 UNKNOWN/OOT；必要信息完整时允许完成 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A24 | 两个窗口编辑不同 Episode；不因共享数据集版本号产生无关冲突 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A25 | 两个窗口编辑同一 Episode；冲突可见，双方内容保留，不自动覆盖 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A26 | 断网、切换 Episode、恢复连接；本地草稿可恢复，未同步可见，恢复后重试 | 已实现；交互验收进行中 | `workbench-model.ts`、`workbench.ts`；TS 7 项及带帧号双相机页面已跑，完整交互回归未结束 |
| A27 | 共享模板更新后打开旧标注；保持原快照，只有明确升级才改变 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A28 | 升级时旧标签被删除或含义改变；展示差异并人工处理，不能自动按近似名称映射 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A29 | 对人工修订过的数据重新运行 AI；保留当前内容，生成独立候选，选择范围应用后待核对且可撤销 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A30 | 导出部分草稿及完整数据集；默认标注包轻量；草稿状态真实；完整副本仅按需生成 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A31 | 导出后重新读取；模板、片段、尝试、结果、特殊状态、备注、核对状态的含义与身份一致 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A32 | 手动选择来源 Episode 1、4、9 导出；输出恰好包含这三条 Episode 的完整内容与对应标注 | 已实现；交互验收进行中 | `workbench-model.ts`、`workbench.ts`；TS 7 项及带帧号双相机页面已跑，完整交互回归未结束 |
| A33 | 同次尝试拆为多个片段后按次数筛选；去重后的尝试次数不变，筛选不受片段切分影响；采用的统计口径明确 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A34 | 筛选后批量勾选并翻页；当前页、全部筛选结果和实际选择范围明确，导出清单可预览 | 已实现；交互验收进行中 | `workbench-model.ts`、`workbench.ts`；TS 7 项及带帧号双相机页面已跑，完整交互回归未结束 |
| A35 | 没有匹配项或选择的 Episode 已失效；清楚提示，不能悄然导出全部来源数据 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A36 | 子集涉及共享 Parquet／共享视频文件；数据、视频区间、媒体偏移和标注保持正确对应 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A37 | 输出重新编号 Episode 或帧索引；元数据、统计、标注及 sidecar 同步更新，并保留来源映射 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A38 | 排队后继续修改筛选或草稿；已提交导出采用固定的选择和版本，不受后续编辑静默影响 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A39 | 标注不完整的 Episode 参与查看或导出；不把次数缺失当零；输出保留未完成和待核对信息 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A40 | 子集数据集脱离来源目录后读取；输出仍能独立使用，不依赖原目录位置；来源数据未被改写 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A41 | 同一 Episode 中抓取 2 次、搬运 1 次、放置 1 次；“抓取 = 2 次”正确匹配，不以全部尝试总数 4 替代 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| A42 | 人工加入未匹配筛选条件的 Episode，并重复加入已有条目；允许人工选择，来源 Episode 去重，最终清单可增删并准确导出 | 逻辑／API 已验证；界面综合验收待跑 | `tests/test_annotation_contract.py`、`test_annotation_workbench.py`、`test_annotation_subset.py`；场景逐项浏览器回归继续补充 |
| Q1 | 正式标注用模板词表；新增改名独立操作；模板提供含义及起止判据；第 4.1 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q2 | 尝试按实际执行判断，切分本身不自动增加次数；第 4.2、7.5 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q3 | 采用 NULL、UNKNOWN、OUT_OF_TEMPLATE 三种特殊状态；第 5.1 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q4 | 一条主时间轨道，任意顺序、重复和空白均可表达；第 6.3 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q5 | 以统一数据帧精确定位，多相机跟随，提供粗细两种浏览尺度；第 6.1 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q6 | 有效编辑即入草稿，自动保存，全部编辑支持撤销重做；第 7.6、8.1 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q7 | 未标注空白与三个特殊状态分别保留；第 5.1 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q8 | 已知 subtask 但结果不明时，保留标签，结果填无法判断；第 5.1 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q9 | OOT 必须有动作描述；UNKNOWN 原因与 NULL 备注可选；第 5.1 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q10 | 同次尝试可跨多个片段，人工明确关联；第 4.2、4.4 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q11 | 界面首帧末帧都包含，内部明确转换边界约定；第 6.2 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q12 | 保存与冲突按 Episode 隔离，同 Episode 冲突保留双方；第 8.4 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q13 | 原标注保留模板快照，升级明确进行并展示差异；第 10.1 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q14 | 同次尝试共用结果评分，局部观察另记；第 4.3 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q15 | 首尾贴合的片段联动调公共边界，有空白则分别调边缘；第 7.2 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q16 | 允许框选范围重标，自动保留前后内容，整体可撤销；第 7.3 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q17 | 普通合并片段与合并尝试分开，不同尝试需明确确认；第 7.5 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q18 | 内容与人工审核状态独立，AI 默认待核，变更后重新核对；第 5.2、9.1 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q19 | 离线可继续工作，本地恢复、未同步提示、重试及冲突保护；第 8.3 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q20 | 合并尝试时，冲突信息由人决定，原记录保留历史；第 7.5 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q21 | 改标签明确当前范围或整次尝试；局部变更后重新核对关联和信息；第 7.4 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q22 | 全片处理且人工确认才算完成；特殊状态允许；草稿仍可导出；第 9.2、9.3 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q23 | 支持范围批量确认和创建并确认，确认不填补空白；第 9.1 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q24 | AI 重跑生成独立候选，按范围明确应用，应用后待核且可撤销；第 10.2 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q25 | 模板升级不匹配时由人处理，不近似自动映射，可继续旧快照；第 10.1 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q26 | 从零以选范围再贴标签为主，框选与标起止等价，可快捷延续下一段起点；第 7.1 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q27 | 浏览、选中、边缘调整、框选、整段移动入口明确，快捷键避开文字输入；第 6.4 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q28 | 完成时尝试结果必填，评分普通备注可选，草稿允许暂缺；第 4.3、9.2 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q29 | 特殊状态不自动计尝试；NULL 不关联，UNKNOWN 可关联已有，OOT 可人工建立或关联；第 5.1 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| Q30 | 默认导出完整结构化标注 JSON，完整数据集副本按需生成；第 11 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |
| R31 | 按指定 subtask 的独立尝试次数筛选，也可人工逐条加入目标集合；导出部分完整 Episode 为独立子集，保留标注与来源映射；第 11.3–11.6、12.6 节 | 实现与集成验证进行中 | 公共契约／工作区／候选／交付与子集链路；对应 A 场景记录为准 |

## 验证记录

全部验证使用离线合成 fixture 与本机可控模型，不调用付费模型、真实数据、GPU 或用户部署。

## 未证明范围

- 真实模型边界误差 ±0.2s、现场真机、真实规模性能。
- 后续将记录全部实际验证和剩余限制。

## 阶段记录：公共模型与 Server（实现中）

- 新增 v2 独立 attempt/segment 契约、v1 无损迁移、逐片段审核、Episode 修订与可信历史撤销；旧 `attempt-comment` 仍属于 attempt。
- 新增 Server 工作区 API；Viewer 的 JSON 操作转发到 Server，媒体只读。来源共享锁与 Episode 写锁分开。
- `tests/test_annotation_contract.py`、`tests/test_annotation_workbench.py`：当前 12 项通过，覆盖 UNKNOWN 计数依据、特殊关联、并发、候选身份与重复局部应用、结果兼容和审核恢复。
- `tests/test_annotation_subset.py`：v2.1/v3.0 两条真实工具链回归通过；队列冻结选择与修订，实际子集重编号、共享视频偏移、sidecar 稳定身份、源文件哈希不变，来源移走后可深度读取并解码末帧。
- `tests/workbench_model.test.ts`：当前 7 项通过，覆盖包含首末帧、公共边界、切分/局部替换、合并冲突、完整撤销及本地输入恢复。工作区界面尚在接入，不把模型测试当作浏览器验收。

## 阶段记录：统一工作区与自动标注（集成验收中）

- 工作区已接入 Server 和原 Viewer 的 iframe 入口；单轨编辑、首末帧包含、共享边界、框选／标起止、完整撤销、尝试字段、特殊状态、确认与清单均有实际控件。浏览器已查看双相机真实帧 0 与 47；严格 CSP 下时间轴动态位置已修正，不能以帧号代替画面验收。
- 本地草稿绑定数据集、来源签名及 Episode，保存异步返回与列表请求有 generation 隔离；失败按 Episode 重试，不覆盖冲突双方。未完成输入进入持久备份；配额失败可见。网络／多窗口综合浏览器回归尚未结束。
- 模板模式专用提示词、解析、完整性和请求身份均使用业务契约 v2；自由分析走原生提示词与解析。旧模板安装不可误报新能力，安装脚本会加入 0003 与 runtime；本任务未更新用户部署。
- 固定 Argus 的实际 Python 3.11、准备／采样／write_units／收据／模型传输路径已用合成双相机数据和 loopback 模型跑通：先任选一条试跑，再明确全量恢复其余 Episode，已成功结果复用；边界细化使用真实解码的局部密集帧。正在扩大格式、网络、模板校验混合失败与预算回归。
- 模型全部为可控模拟。没有测得真实模型 ±0.2 秒质量或真实规模性能，也未进行真机验证。

### 本轮已通过与待验证（阶段提交）

- 固定 Argus 实际链路回归通过：原生解析／原生重试、混合网络／格式／模板错误共享四次发送上限、任选一条试跑后显式全量且成功结果复用、真实视频边界取证。全部模型回复来自本地可控模拟服务。
- 新增标注／自动流程回归当前 74 项通过；范围确认支持切开已有片段但不增加尝试，撤销回到可信历史。
- 自由分析与模板标注共享执行范围能力探测。未安装 0003 的旧工具允许原生全量；限定范围请求明确拒绝，绝不忽略选择。完整新重试／预算契约要求重新运行安装脚本更新工具链；本任务未操作用户部署。
- 全套 Python：623 通过、10 跳过、15 失败。同环境固定 `49aff8e` 独立临时基线复跑，原样重现全部 15 个失败（静止区间旧测试期待 v2 规则而实现为 v7；回收站分片测试固定权限与当前 umask 不符），相关源文件未改。全套 TS/Bun：62 通过。
- 全库 Ruff lint 通过。全库 format 的遗留未格式文件基线为 24 个，本任务当前 21 个；新改文件单独检查。浏览器综合操作、原 Viewer 入口及两模式 Server 页面、R31 清单／实际子集仍待本轮验收，不提前声明完成。

### 浏览器与账本集成阶段

- 实际浏览器 `tests/fixtures/workbench_browser_probe.py editing/offline/r31` 已通过：双相机 0/3/47 帧、切分同次尝试、联动边界、局部替换保留两侧、范围确认及可信撤销；离线跨 Episode、无效输入/新片段输入恢复、冲突双方保留、来源 409 与网络故障区分、失败后自动重试。
- R31 从真实控件筛选“抓取 = 2”，手工加入不匹配的来源 Episode 1、9，与筛选 Episode 4 形成去重清单；实际下载 JSON 并排队独立子集。输出恰好 1/4/9 的完整 144 帧，来源映射、稳定标注身份、VQA、失败/特殊状态与冻结修订一致；临时移走合成来源后输出独立深度读取通过，来源全部文件哈希未变。
- Server 两模式已通过本地模拟模型实际执行：自由分析使用原生提示词/原生时间线与任务结果并免模板下载；模板使用业务契约及密集边界证据，独立候选发送到工作区。无付费模型调用，截图将在 Proof 中明确模拟。
- 崩溃账本与固定 Argus 原生 reader 的六项 parity 通过，包括仅有证据缓存、未结算派发、同 index 的 primary/failed/noreply 权威顺序及无效 dictionary 预留。累计预留不再丢失或重复扣减；自由分析显式续跑只补坏的已收到回复，不重试未知结算或已成功请求。
- 延续标志在共享 validator 校验严格布尔类型、合成切片及首末边界。实际 Argus 混合故障回归证明坏延续在 Server 组装前进入共享重试上限；续跑分类只列失败请求。当前相关 Python 回归 118 通过，实际 Argus 集成 1 通过。
- Viewer 外层负责嵌入模式的身份导航；内层锁定同一数据集/Episode，独立工作区入口用于跨数据集切换。最终原入口切换、从零任意顺序与正常多 Episode 批量确认、手机布局及最终 Proof 预览继续验收。
