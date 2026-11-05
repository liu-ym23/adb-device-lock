#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dev — mac mini 共享服务器上的 ADB 设备占用锁工具

同一台手机同时被多人 adb shell / scrcpy / 安装会互相干扰，本工具用
/Users/Shared/adb-locks/ 下的锁文件做「预约制」独占：

    dev list                          # 所有设备 + 锁状态一览
    dev acquire <serial> -p 跑自动化  # 占用设备（默认 4h，到期自动失效）
    dev release <serial>              # 用完归还
    dev extend <serial> --ttl 2h     # 续期
    dev exec <serial> -- adb install x.apk   # 临时锁住执行一条命令

持有人记录：优先 --by 参数，其次环境变量 DEV_USER，最后 SSH 来源 IP。
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

LOCK_DIR = "/Users/Shared/adb-locks"
ADB_CANDIDATES = [
    "/opt/homebrew/bin/adb",
    "/usr/local/bin/adb",
    os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
]
MAX_TTL = 24 * 3600
DEFAULT_TTL = 4 * 3600
EXEC_TTL = 15 * 60

# ---------- ANSI ----------
_is_tty = sys.stdout.isatty()


def c(code, s):
    return "\033[{}m{}\033[0m".format(code, s) if _is_tty else s


def red(s):
    return c("31", s)


def green(s):
    return c("32", s)


def yellow(s):
    return c("33", s)


def cyan(s):
    return c("36", s)


# ---------- 基础工具 ----------
def adb_path():
    for p in ADB_CANDIDATES:
        if os.path.exists(p):
            return p
    found = shutil.which("adb")
    return found or "adb"


def run_adb(*args):
    try:
        out = subprocess.check_output([adb_path()] + list(args), stderr=subprocess.DEVNULL)
        return out.decode("utf-8", "replace")
    except (OSError, subprocess.CalledProcessError):
        return ""


def who_am_i():
    env = os.environ.get("DEV_USER")
    if env:
        return env
    sc = os.environ.get("SSH_CLIENT", "")
    if sc:
        return "ssh:" + sc.split()[0]
    return "console"


def parse_ttl(s):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", s.strip().lower())
    if not m:
        raise SystemExit("无法解析 TTL {!r}（示例: 90m / 4h / 2d）".format(s))
    n, unit = float(m.group(1)), m.group(2) or "h"
    return int(n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])


def fmt_dur(sec):
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m = rem // 60
    if h:
        return "{}h{}m".format(h, m) if m else "{}h".format(h)
    if m:
        return "{}m".format(m)
    return "{}s".format(sec)


def fmt_ts(epoch):
    return time.strftime("%m-%d %H:%M", time.localtime(epoch))


# ---------- 锁操作 ----------
def lock_path(serial):
    safe = re.sub(r"[^A-Za-z0-9._:-]", "_", serial)
    return os.path.join(LOCK_DIR, safe + ".lock")


def read_lock(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_lock(path, holder, purpose, ttl):
    now = time.time()
    data = {
        "holder": holder,
        "ssh": os.environ.get("SSH_CLIENT", "").split()[0] if os.environ.get("SSH_CLIENT") else "local",
        "purpose": purpose or "",
        "acquired_at": now,
        "expires_at": now + ttl,
        "pid": os.getpid(),
    }
    tmp = path + ".tmp.{}".format(os.getpid())
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)  # 原子落盘，读到半截文件的只会看到旧内容/无内容


def acquire_lock(serial, holder, purpose, ttl):
    os.makedirs(LOCK_DIR, exist_ok=True)
    path = lock_path(serial)
    took_over = False
    attempts = 0
    while attempts < 10:
        attempts += 1
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            write_lock(path, holder, purpose, ttl)
            return took_over
        except FileExistsError:
            lk = read_lock(path)
            if lk is not None and lk.get("expires_at", 0) > time.time():
                raise Busy(lk)  # 活锁，拒绝
            # 过期锁 / 损坏文件 / 刚创建还没写入的空文件
            if lk is None:
                try:
                    age = time.time() - os.path.getmtime(path)
                except OSError:
                    age = 999
                if age <= 60:  # 可能是并发的另一方刚建还没写入，稍等重试
                    time.sleep(0.05)
                    continue
            try:
                os.unlink(path)  # 过期或损坏，接管
            except OSError:
                pass
            took_over = True
    raise SystemExit("锁目录竞争异常，请重试")


class Busy(Exception):
    def __init__(self, lk):
        super(Busy, self).__init__("busy")
        self.lk = lk


def get_lock(serial):
    lk = read_lock(lock_path(serial))
    if lk and lk.get("expires_at", 0) <= time.time():
        return None  # 已过期视同空闲
    return lk


def check_holder(lk, serial, by, force):
    if force or (by and by == lk.get("holder")) or (not by and who_am_i() == lk.get("holder")):
        return
    raise SystemExit(
        "{}: 设备由 {} 持有（你是 {}）。不是你的锁请加 --force。".format(serial, lk.get("holder"), by or who_am_i())
    )


# ---------- 设备清单 ----------
def device_list():
    """[(serial, state, model), ...]"""
    out = run_adb("devices", "-l")
    devs = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        serial, state = parts[0], parts[1]
        model = ""
        for kv in parts[2:]:
            if kv.startswith("model:"):
                model = kv[len("model:"):]
        devs.append((serial, state, model))
    return devs


# ---------- 子命令 ----------
def cmd_list(_args):
    devs = device_list()
    now = time.time()
    rows = []
    for serial, state, model in devs:
        lk = get_lock(serial)
        rows.append((serial, state, model, lk))
    locked_stale = []
    if os.path.isdir(LOCK_DIR):
        known = {s for s, _, _ in devs}
        for fn in os.listdir(LOCK_DIR):
            if not fn.endswith(".lock"):
                continue
            serial = fn[:-5]
            if serial in known:
                continue
            lk = get_lock(serial)
            if lk:
                locked_stale.append((serial, lk))

    if not rows and not locked_stale:
        print("没有任何设备连接，也没有锁文件。")
        return

    print("{:<24} {:<8} {:<14} {:<10} {:<8} {}".format("设备", "adb", "型号", "锁状态", "剩余", "持有人 / 用途"))
    for serial, state, model, lk in rows:
        if lk:
            remain = lk["expires_at"] - now
            note = "{} / {}".format(lk.get("holder", "?"), lk.get("purpose") or "-")
            lock_cell = red("🔒占用")
            remain_cell = red(fmt_dur(remain)) if remain < 1800 else yellow(fmt_dur(remain))
            row = "{:<24} {:<8} {:<14} {:<10} {:<8} {}".format(serial, state, model or "-", lock_cell, remain_cell, note)
            print(row)
        else:
            print("{:<24} {:<8} {:<14} {:<10}".format(serial, state, model or "-", green("空闲")))
    for serial, lk in locked_stale:
        remain = lk["expires_at"] - now
        print("{:<24} {:<8} {:<14} {:<10} {:<8} {} / {}".format(
            serial, "已拔出", "-", yellow("遗留锁"), yellow(fmt_dur(remain)),
            lk.get("holder", "?"), lk.get("purpose") or "-"))
    print("\n持有人标识是你自己吗？当前记录为: " + cyan(who_am_i()))


def cmd_acquire(args):
    ttl = parse_ttl(args.ttl)
    if ttl > MAX_TTL:
        raise SystemExit("单次最长占用 24h，长任务请用 extend 续期")
    holder = args.by or who_am_i()
    try:
        took_over = acquire_lock(args.serial, holder, args.purpose, ttl)
    except Busy as e:
        lk = e.lk
        remain = lk["expires_at"] - time.time()
        print(red("✗ 设备 {} 正被 {} 占用，剩余 {}".format(args.serial, lk.get("holder"), fmt_dur(remain))))
        if lk.get("purpose"):
            print("  用途: " + lk["purpose"])
        print("  可 dev extend/找对方 release，或等锁过期（{} 到期）".format(fmt_ts(lk["expires_at"])))
        sys.exit(1)
    tag = "（接管了已过期的锁）" if took_over else ""
    print(green("✓ 设备 {} 已由 {} 占用，{} 后到期{}。用完请 dev release {}".format(
        args.serial, holder, fmt_dur(ttl), tag, args.serial)))
    if args.shell_hint:
        print("  提示: export DEV_USER=\"{}\" 可省去每次 --by".format(holder))


def cmd_release(args):
    path = lock_path(args.serial)
    lk = read_lock(path)
    if lk is None:
        if os.path.exists(path):  # 文件在但内容损坏：无法确认持有者，直接清理
            os.unlink(path)
            print(yellow("锁文件损坏，已清理: " + args.serial))
        else:
            print("设备 {} 当前没有锁记录（可能已过期自动失效）".format(args.serial))
        return
    if lk.get("expires_at", 0) <= time.time():  # 过期锁对所有人无效，任何人都可清理
        os.unlink(path)
        print(green("设备 {} 的锁已于 {} 过期，已清理".format(args.serial, fmt_ts(lk["expires_at"]))))
        return
    check_holder(lk, args.serial, args.by, args.force)
    os.unlink(path)
    print(green("✓ 设备 {} 已释放".format(args.serial)))


def cmd_extend(args):
    ttl = parse_ttl(args.ttl)
    lk = read_lock(lock_path(args.serial))
    if lk is None:
        raise SystemExit("设备 {} 没有活动锁".format(args.serial))
    check_holder(lk, args.serial, args.by, args.force)
    now = time.time()
    base = max(lk.get("expires_at", 0), now)  # 已过期则从现在起算
    lk["expires_at"] = min(base + ttl, now + MAX_TTL)
    write_lock(lock_path(args.serial), lk["holder"], lk.get("purpose", ""), lk["expires_at"] - now)
    print(green("✓ 设备 {} 已续期至 {}".format(args.serial, fmt_ts(lk["expires_at"]))))


def cmd_exec(args):
    holder = args.by or who_am_i()
    owned = False  # 锁是否为本次 exec 临时创建（复用他人长锁时不能释放）
    try:
        acquire_lock(args.serial, holder, args.purpose or "exec", EXEC_TTL)
        owned = True
    except Busy as e:
        lk = e.lk
        if lk.get("holder") != holder:
            print(red("✗ 设备 {} 正被 {} 占用（剩余 {}），命令未执行".format(
                args.serial, lk.get("holder"), fmt_dur(lk["expires_at"] - time.time()))))
            sys.exit(1)
        print("（你已持有该设备的锁，exec 复用，到期时间不变）")
    if not args.cmd or args.cmd == ["--"]:
        raise SystemExit("用法: dev exec <serial> [--] <命令...>（命令以 adb 开头时自动加 -s）")
    if args.cmd[0] == "--":
        args.cmd = args.cmd[1:]
    if os.path.basename(args.cmd[0]) == "adb":  # 自动锁定目标设备
        args.cmd = args.cmd[:1] + ["-s", args.serial] + args.cmd[1:]
    try:
        env = dict(os.environ)
        env["PATH"] = os.path.dirname(adb_path()) + os.pathsep + env.get("PATH", "")
        env["DEV_SERIAL"] = env["DEV_LOCKED_SERIAL"] = args.serial
        rc = subprocess.call(["/bin/bash", "-c", " ".join(shlex.quote(a) for a in args.cmd)], env=env)
    finally:
        if owned:
            lk = read_lock(lock_path(args.serial))
            if lk and lk.get("holder") == holder:
                os.unlink(lock_path(args.serial))
    sys.exit(rc)


def cmd_whoami(_args):
    print(who_am_i())


def main():
    ap = argparse.ArgumentParser(prog="dev", description="ADB 设备占用锁工具（mac mini 共享服务器）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="所有设备 + 锁状态一览")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("acquire", help="占用设备")
    p.add_argument("serial")
    p.add_argument("--by", help="持有人名字（默认 DEV_USER 或 SSH 来源 IP）")
    p.add_argument("-p", "--purpose", help="用途说明")
    p.add_argument("--ttl", default="4h", help="占用时长，如 90m / 4h / 2d（默认 4h）")
    p.add_argument("--shell-hint", action="store_true", help="附带 DEV_USER 设置提示")
    p.set_defaults(fn=cmd_acquire)

    p = sub.add_parser("release", help="释放设备")
    p.add_argument("serial")
    p.add_argument("--by", help="持有人名字")
    p.add_argument("--force", action="store_true", help="强制释放别人的锁")
    p.set_defaults(fn=cmd_release)

    p = sub.add_parser("extend", help="续期")
    p.add_argument("serial")
    p.add_argument("--by", help="持有人名字")
    p.add_argument("--ttl", default="2h", help="续时长（默认 2h）")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_extend)

    p = sub.add_parser("exec", help="临时锁住执行一条命令，结束自动释放")
    p.add_argument("serial")
    p.add_argument("cmd", nargs="+")
    p.add_argument("--by", help="持有人名字")
    p.add_argument("-p", "--purpose", help="用途说明")
    p.set_defaults(fn=cmd_exec)

    p = sub.add_parser("whoami", help="显示当前会被记录的持有人标识")
    p.set_defaults(fn=cmd_whoami)

    args = ap.parse_args()
    if not hasattr(args, "fn"):
        ap.print_help()
        sys.exit(1)
    args.fn(args)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:  # 输出被 head 等截断时静默退出，别打印难看的提示
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(0)
