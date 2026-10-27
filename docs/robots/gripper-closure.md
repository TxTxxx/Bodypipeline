# 转换时修正夹持阶段的最小闭合度

Server 的原始数据转换表单支持可选的夹爪动作修正。当前只有
`configs/arx-x5-hdf5-v1.json` 声明此能力。选择该 Runtime 时显示开关，
默认关闭；启用后输入 0–100 的百分比，初始值为 90。
其他 Runtime 隐藏控件，切换 Runtime 会清空启用状态；后端也拒绝不支持的选项。

百分比定义为 **Profile 配置的电机名义行程比例**，不表示实际开口宽度、
力矩或夹持力。ARX 当前参考端点为 open=-3.14、closed=0：
90% 对应 -0.314，95% 对应 -0.157。沿用原数据的数值单位，
不在此功能中自动纠正旧数据将夹爪标为 `m` 的元数据。

## 修改范围

`AlohaHDF5Provider.prepare()` 在独立的转换工作副本上处理：

1. 每个 episode、每个配置的夹爪单独处理，从实测 state 识别位置变化。
2. 中值滤波只辅助识别；需要先出现闭合，再找到部分闭合的稳定平台。
   静止的初始姿态、空夹全闭、短暂停留不作为夹持平台。
3. 识别松开或空夹全闭位置作为阶段结束；只选择阶段内稳定窗口覆盖、
   持续足够久且整体位置变化足够小的平台。识别阈值及时间窗口由 Profile 声明。
4. 将选中帧的夹爪 action 提高到输入的最小闭合比例；已经更闭合的 action 保留。
   开合方向由各通道的 open/closed 端点决定，通道下标从 joint_names 解析。
5. 后续正常转换到 LeRobot v2.1 或 v3.0，由 LeTools 基于修改后的 action 生成统计。

原始 HDF5、实测 state、非夹爪 action 和图像保持不变。现有转换链路只输出
`action`，本功能不新增 `action_eef`。输出继续使用既有的共享读写权限策略。
未找到需要提高闭合度的帧时正常生成数据，报告警告与零修改数。

检测器版本为 `closing_hold_plateaus_v1`，参考
`relabel_arx_gripper_closure.py` 和既有 `closing_hold_plateaus_only_v2` 数据处理清单。
它不是历史 v2 处理的逐帧复刻，也不会将所有命中帧强制改为固定值。
位置推断无法证明真实接触，需在新任务和真机上验证。

## 公共接口和追溯

`POST /api/uploads/{id}/process` 和 `POST /api/collections/{id}/process`：

```json
{
  "instruction": "Unscrew the screw.",
  "target_format": "v3.0",
  "run_doctor": true,
  "conversion_options": {
    "gripper_closure": {"minimum_percent": 90}
  }
}
```

省略 conversion_options、传 `{}` 或传 `{"gripper_closure": null}` 均表示关闭。
百分比必须是有限数值，拒绝字符串、布尔值、未知字段及不支持此能力的 Runtime。
百分比进入任务去重标识；任务持久化当时的选项、Profile 和算法版本。
Worker 使用任务快照，修改部署配置不会改变已排队任务的目标值。

`GET /api/config` 的 runtimes[].conversion_capabilities 由 Provider 生成。
前端只根据能力渲染，不判断机器人型号、不接触关节下标和位置端点。
新 Provider 可实现 conversion_capabilities(profile) 并扩展 prepare(..., options=...)；
未声明能力的旧 Provider 继续只接收无选项转换。

任务详情展示百分比及修改帧数。alignment-report.json、lineage.json 保存
每个 episode 的算法、名义端点版本、阈值、目标值、选中/实际修改帧数及半开帧区间。

## 部署已有 ARX Runtime

此次为 ARX Profile 增加 conversion 配置，会改变 Profile 摘要。
若要继续接收旧版采集配置，审核后在 **ARX Runtime 自己的**
accepted_capture_profile_hashes 中保留旧摘要：

```text
0afd93593551d0bd55fc55b3a650a24fe71a101d54d223d648f48146f85b3b14
```

不要把此摘要放到其他机器人的兼容列表，也不要自动接受所有旧摘要。
已有部署若使用独立的 Profile 副本，应同步完整 conversion 配置后再重启；
仅替换前端不会启用能力。名义端点应与对应硬件/采集版本核对，不能由样本极值推断。
