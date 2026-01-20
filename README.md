# VRC-OBS-MicControl

监听 VRChat 的 OSC 参数 `muteself`，自动控制 OBS 麦克风源静音/取消静音。
<img width="310" height="355" alt="screenshot" src="https://github.com/TuTu475/VRC-OBS-MicControl/blob/main/screenshot.png" />

## 功能
- 监听 VRChat OSC（默认 UDP 9001）
- 根据 `muteself` 参数自动切换麦克风静音
- 防抖与定时纠偏，降低抖动与丢包影响
- 可在脚本面板一键启用/禁用
- 下拉列出所有音频源，避免手填

## 使用方法
1. OBS → 工具 → 脚本 → `+` → 选择 `VRC-OBS-MicControl.py`
2. 在脚本设置中选择“麦克风源名称”
3. 需要时勾选“启用脚本”

## 脚本设置（OBS 面板）
- 启用脚本
- 麦克风源名称
- 监听端口（默认 9001）
- 防抖时间（ms）
- 纠偏间隔（秒）
- 反向逻辑（可选）
- 调试日志（可选）

## 说明
- 监听 IP 与参数名默认固定：`127.0.0.1`、`muteself`
- 如需修改，请直接编辑脚本内的 `g_listen_ip` / `g_param_name`