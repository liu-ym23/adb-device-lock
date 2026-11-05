#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
adb 包装器（真身: /opt/homebrew/bin/adb）

作用：被他人锁占用的设备，拒绝 adb 写操作，防止多人互相干扰。
- 只读命令（devices/version/...）与空闲设备不受影响，完全透传。
- 确认是你自己的锁（DEV_USER 匹配或同一 SSH 会话）则放行。
- 紧急放行: ADB_LOCK_BYPASS=1 adb ...（会打扰正在用的人，请慎用）

本文件由 dev 设备锁工具配套生成，请勿手工改动真身路径。
"""
import json
import os
import re
import subprocess
import sys
import time

REAL = "/opt/homebrew/bin/adb"
if not os.path.exists(REAL):  # 兜底：真身不在就尝试常见位置
    for cand in ("/usr/local/bin/adb", os.path.expanduser("~/Library/Android/sdk/platform-tools/adb")):
        if os.path.exists(cand):
            REAL = cand
            break
LOCK_DIR = "/Users/Shared/adb-locks"
MAX_TTL = 24 * 3600

# 需要锁检查的（会动设备的）子命令
CHECK_VERBS = {
    "shell", "exec-out", "install", "uninstall", "push", "pull", "sync",
    "backup", "restore", "reboot", "root", "unroot", "remount",
    "tcpip", "usb", "forward", "reverse", "logcat", "bugreport",
}


def caller_ip():
    sc = os.environ.get("SSH_CLIENT", "")
    return sc.split()[0] if sc else "local"


def identity():
    return os.environ.get("DEV_USER") or ("ssh:" + caller_ip())


def deny(serial, lk):
    remain = int(lk["expires_at"] - time.time())
    mm, ss = divmod(max(0, remain), 60)
    hh, mm = divmod(mm, 60)
    sys.stderr.write(
        "✗ 设备 {serial} 正被 {holder} 占用（剩余 {h}h{m:02d}m），adb 操作已拦截\n"
        "  用途: {purpose}\n"
        "  是你自己的锁? export DEV_USER=\"{holder}\" 后重试\n"
        "  紧急放行(会打扰对方): ADB_LOCK_BYPASS=1 adb {argv}\n"
        "  查看/占用: dev list / dev acquire {serial}\n".format(
            serial=serial, holder=lk.get("holder", "?"),
            h=hh, m=mm, purpose=lk.get("purpose") or "-",
            argv=" ".join(sys.argv[1:])))
    sys.exit(1)


def read_lock(serial):
    path = os.path.join(LOCK_DIR, re.sub(r"[^A-Za-z0-9._:-]", "_", serial) + ".lock")
    try:
        with open(path) as f:
            lk = json.load(f)
        if lk.get("expires_at", 0) <= time.time():
            return None  # 过期视同空闲
        return lk
    except (OSError, ValueError):
        return None


def allowed_for(lk):
    if os.environ.get("ADB_LOCK_BYPASS") == "1":
        return True
    if lk.get("holder") == identity():
        return True
    if lk.get("ssh") == caller_ip():  # 同一个 SSH 会话里 acquire 后直接用
        return True
    return False


def target_serial(args):
    """返回 (serial, verb)。serial 可能为 None（未指定目标）。"""
    serial = None
    verb = ""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-s" and i + 1 < len(args):
            serial = args[i + 1]
            i += 2
            continue
        if a == "--serial" and i + 1 < len(args):
            serial = args[i + 1]
            i += 2
            continue
        if a.startswith("--serial="):
            serial = a.split("=", 1)[1]
            i += 1
            continue
        if a == "-t" and i + 1 < len(args):  # transport_id: 换算不了，交真身处理
            i += 2
            continue
        if not a.startswith("-"):
            verb = a
            break
        i += 1
    if serial is None:
        env_serial = os.environ.get("ANDROID_SERIAL")
        if env_serial:
            serial = env_serial
    return serial, verb


def connected_serials():
    try:
        out = subprocess.check_output([REAL, "devices"], stderr=subprocess.DEVNULL)
        serials = []
        for line in out.decode("utf-8", "replace").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serials.append(parts[0])
        return serials
    except (OSError, subprocess.CalledProcessError):
        return []


def any_active_lock():
    try:
        names = os.listdir(LOCK_DIR)
    except OSError:
        return False
    for fn in names:
        if not fn.endswith(".lock"):
            continue
        try:
            with open(os.path.join(LOCK_DIR, fn)) as f:
                lk = json.load(f)
            if lk.get("expires_at", 0) > time.time():
                return True
        except (OSError, ValueError):
            continue
    return False


def main():
    args = sys.argv[1:]
    if not os.path.exists(REAL):
        sys.stderr.write("adb 真身不存在: {}\n".format(REAL))
        sys.exit(1)

    serial, verb = target_serial(args)

    # kill-server 会断掉所有人的 logcat/scrcpy/测试会话
    if verb == "kill-server" and any_active_lock():
        if os.environ.get("ADB_LOCK_BYPASS") == "1":
            sys.stderr.write(
                "⚠ 硬闯 kill-server：请至少等 10 秒再继续用 adb——旧 server 的 USB 接口释放有延迟，\n"
                "  立刻重启的 adb 可能枚举不到设备（列表变空），届时按 README-dev.md「故障恢复」处理。\n")
        else:
            sys.stderr.write("✗ 当前有设备处于占用中，禁止 adb kill-server。\n"
                             "  确认无人使用后可用 ADB_LOCK_BYPASS=1 adb kill-server\n")
            sys.exit(1)

    if verb not in CHECK_VERBS:
        os.execv(REAL, [REAL] + args)  # 只读/服务类命令直接透传

    if serial is None:
        devs = connected_serials()
        if len(devs) == 1:
            serial = devs[0]
        else:
            os.execv(REAL, [REAL] + args)  # 0 台或多台，真身自己会报错/多选一

    lk = read_lock(serial)
    if lk is not None and not allowed_for(lk):
        deny(serial, lk)

    os.execv(REAL, [REAL] + args)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # 拦截器自身故障时放行（fail-open），不能让 adb 整体不可用
        sys.stderr.write("⚠ adb 拦截器异常({})，本次跳过锁检查直接执行，请告知管理员。\n".format(e))
        os.execv(REAL, [REAL] + sys.argv[1:])
