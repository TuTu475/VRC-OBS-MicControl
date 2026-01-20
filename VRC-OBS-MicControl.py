# vrc_osc_muteself_to_obs.py
# 功能：监听 VRChat 的 OSC 输出（默认 UDP 9001），读取 bool 参数 muteself
#      muteself=True  -> OBS 麦克风源静音
#      muteself=False -> OBS 麦克风源取消静音
#      支持防抖与纠偏，减少抖动与丢包影响
# 使用：OBS -> 工具 -> 脚本 -> + -> 选择脚本文件
#      在脚本里选择“麦克风源名称”（常见为“麦克风/Aux”或“Mic/Aux”）
#
# 说明：监听 IP 与参数名固定在脚本常量中，仅端口可在设置中调整。

import obspython as obs
import socket
import struct
import time

# ---------------------------
# 全局配置（由 OBS 脚本设置页驱动）
# ---------------------------

g_listen_ip = "127.0.0.1"   # 默认固定，如需修改请直接改脚本
g_listen_port = 9001
g_param_name = "muteself"   # 默认固定，如需修改请直接改脚本
g_mic_source_name = "麦克风/Aux"  # 按你 OBS 混音器里显示的名字填
g_invert = False                  # 反向逻辑（一般不用）
g_debug = False
g_debounce_ms = 200        # 防抖时间（毫秒）
g_correction_sec = 3.0     # 纠偏间隔（秒）
g_sock = None
g_pending_value = None      # 等待防抖确认的值
g_pending_time = 0.0        # 该值的接收时间
g_last_received_value = None
g_last_correction_time = 0.0
g_last_state = None         # 上一次是否静音（True/False）
g_enabled = True

# ---------------------------
# OSC 解析（支持 message / bundle）
# ---------------------------

def _pad4(i: int) -> int:
    return (i + 3) & ~3


def _read_osc_string(data: bytes, idx: int):
    # 读 null-terminated string，返回 (str, new_idx)
    if idx >= len(data):
        return "", idx
    end = data.find(b"\0", idx)
    if end == -1:
        return "", len(data)
    s = data[idx:end].decode("utf-8", errors="ignore")
    idx = _pad4(end + 1)
    return s, idx


def _parse_osc_message(packet: bytes):
    # 返回 (address, args) 或 None
    idx = 0
    address, idx = _read_osc_string(packet, idx)
    if not address:
        return None

    tags, idx = _read_osc_string(packet, idx)
    if not tags or not tags.startswith(","):
        return None

    args = []
    for t in tags[1:]:
        if t == "i":
            if idx + 4 > len(packet):
                break
            args.append(struct.unpack(">i", packet[idx:idx + 4])[0])
            idx += 4
        elif t == "f":
            if idx + 4 > len(packet):
                break
            args.append(struct.unpack(">f", packet[idx:idx + 4])[0])
            idx += 4
        elif t == "T":
            args.append(True)
        elif t == "F":
            args.append(False)
        elif t == "s":
            s, idx = _read_osc_string(packet, idx)
            args.append(s)
        elif t == "b":
            if idx + 4 > len(packet):
                break
            size = struct.unpack(">i", packet[idx:idx + 4])[0]
            idx += 4
            blob = packet[idx:idx + size]
            idx = _pad4(idx + size)
            args.append(blob)
        else:
            # 其它类型先忽略（不阻断）
            pass

    return (address, args)


def _iter_osc_messages(packet: bytes):
    # 迭代产出 (address, args)
    if packet.startswith(b"#bundle\0"):
        # bundle: "#bundle\0" + timetag(8) + [size(4)+elem(size)]...
        idx = 16
        while idx + 4 <= len(packet):
            size = struct.unpack(">i", packet[idx:idx + 4])[0]
            idx += 4
            elem = packet[idx:idx + size]
            idx += size
            # elem 可能还是 bundle
            for m in _iter_osc_messages(elem):
                yield m
    else:
        m = _parse_osc_message(packet)
        if m:
            yield m


def _to_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0 and v != 0.0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "t", "yes", "on")
    return False

# ---------------------------
# OBS 控制：静音/取消静音
# ---------------------------

def _set_mic_muted(muted: bool, force: bool = False):
    global g_last_state
    if not force and g_last_state is not None and muted == g_last_state:
        return

    src = obs.obs_get_source_by_name(g_mic_source_name)
    if not src:
        obs.script_log(obs.LOG_WARNING, f"[VRC-OSC] 找不到麦克风源：{g_mic_source_name}")
        return

    obs.obs_source_set_muted(src, muted)
    obs.obs_source_release(src)

    g_last_state = muted
    if g_debug:
        obs.script_log(obs.LOG_INFO, f"[VRC-OSC] mic_muted -> {muted}")

# ---------------------------
# OBS 属性：列出所有音频源
# ---------------------------

def _fill_audio_sources_list(list_prop):
    obs.obs_property_list_clear(list_prop)
    sources = obs.obs_enum_sources()
    if sources is None:
        return
    for src in sources:
        try:
            flags = obs.obs_source_get_output_flags(src)
            if (flags & obs.OBS_SOURCE_AUDIO) != 0:
                name = obs.obs_source_get_name(src)
                obs.obs_property_list_add_string(list_prop, name, name)
        except Exception as e:
            if g_debug:
                obs.script_log(obs.LOG_WARNING, f"[VRC-OSC] 枚举音频源时发生异常：{e}")
    obs.source_list_release(sources)

# ---------------------------
# Socket & 轮询
# ---------------------------

def _close_socket():
    global g_sock
    if g_sock:
        try:
            g_sock.close()
        except Exception:
            pass
    g_sock = None


def _open_socket():
    global g_sock
    _close_socket()

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((g_listen_ip, int(g_listen_port)))
        s.setblocking(False)
        g_sock = s
        obs.script_log(obs.LOG_INFO, f"[VRC-OSC] 监听 UDP {g_listen_ip}:{g_listen_port}")
    except Exception as e:
        g_sock = None
        obs.script_log(obs.LOG_ERROR, f"[VRC-OSC] 绑定端口失败：{e}")


def _tick():
    global g_pending_value, g_pending_time, g_last_received_value, g_last_correction_time
    if not g_enabled:
        return
    if not g_sock:
        return

    now = time.time()
    target = f"/avatar/parameters/{g_param_name}".lower()
    while True:
        try:
            data, _addr = g_sock.recvfrom(65535)
        except BlockingIOError:
            break
        except Exception as e:
            obs.script_log(obs.LOG_ERROR, f"[VRC-OSC] recv 错误：{e}")
            break

        for address, args in _iter_osc_messages(data):
            a = (address or "").lower()
            if a == target:
                val = args[0] if args else False
                muteself = _to_bool(val)
                if g_invert:
                    muteself = not muteself
                ts = time.time()
                g_pending_value = muteself
                g_pending_time = ts
                g_last_received_value = muteself

    if g_pending_value is not None and (now - g_pending_time) >= (g_debounce_ms / 1000.0):
        _set_mic_muted(g_pending_value)
        g_pending_value = None

    # 定时纠偏：避免丢包导致状态长时间错误
    if g_last_received_value is not None and (now - g_last_correction_time) >= g_correction_sec:
        _set_mic_muted(g_last_received_value, force=True)
        g_last_correction_time = now

# ---------------------------
# OBS 脚本接口
# ---------------------------

def script_description():
    return (
        "监听 VRChat OSC(UDP 9001) 的 muteself 参数，自动静音/取消静音 OBS 麦克风源。"
        "防抖用于过滤短时间抖动；纠偏用于周期性校正状态。"
    )


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_bool(props, "enabled", "启用脚本")
    mic_list = obs.obs_properties_add_list(
        props,
        "mic_source_name",
        "麦克风源名称",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_STRING,
    )
    _fill_audio_sources_list(mic_list)
    obs.obs_properties_add_int(props, "listen_port", "监听端口", 1, 65535, 1)
    obs.obs_properties_add_int(props, "debounce_ms", "防抖时间（毫秒）", 0, 2000, 10)
    obs.obs_properties_add_int(props, "correction_sec", "纠偏间隔(秒)", 1, 30, 1)

    # 注意：obs_properties_add_bool 在 OBS Python API 里只有 3 个参数(props, name, desc)
    obs.obs_properties_add_bool(props, "invert", "反向逻辑")
    obs.obs_properties_add_bool(props, "debug", "调试日志")

    return props


def script_defaults(settings):
    obs.obs_data_set_default_int(settings, "listen_port", 9001)
    obs.obs_data_set_default_string(settings, "mic_source_name", "麦克风/Aux")
    obs.obs_data_set_default_bool(settings, "enabled", True)
    obs.obs_data_set_default_int(settings, "debounce_ms", 200)
    obs.obs_data_set_default_int(settings, "correction_sec", 3)
    obs.obs_data_set_default_bool(settings, "invert", False)
    obs.obs_data_set_default_bool(settings, "debug", False)


def script_update(settings):
    global g_listen_port, g_mic_source_name
    global g_invert, g_debug, g_debounce_ms, g_correction_sec, g_enabled

    g_listen_port = obs.obs_data_get_int(settings, "listen_port")
    g_mic_source_name = obs.obs_data_get_string(settings, "mic_source_name")
    g_enabled = obs.obs_data_get_bool(settings, "enabled")
    g_debounce_ms = obs.obs_data_get_int(settings, "debounce_ms")
    g_correction_sec = float(obs.obs_data_get_int(settings, "correction_sec"))
    g_invert = obs.obs_data_get_bool(settings, "invert")
    g_debug = obs.obs_data_get_bool(settings, "debug")

    if g_enabled:
        _open_socket()
    else:
        _close_socket()
    obs.timer_remove(_tick)
    obs.timer_add(_tick, 50)


def script_unload():
    obs.timer_remove(_tick)
    _close_socket()
