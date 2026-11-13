#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制台部署：一次调用走完全程。

为什么有这个脚本
----------------
2026-11-01 那次部署开了 4 次独立 SSH（备份 / 上传 / py_compile / 覆盖重启），
多花约 3 分钟纯等模型思考与往返。`AGENTS.md` R11 要求「同一目标的多步操作必须
合并成一次脚本或一次 SSH 会话」—— 本脚本就是这条规则的落地。

做的事（一个 SSH 连接内全部完成）
-------------------------------
1. 对比本地与远端每个 .py 的 md5，算出真正要传的文件
2. 备份远端 app/ 与数据库、.env
3. 上传变更文件 → 逐个校验 md5 → py_compile 预检
4. 原子覆盖 → 重启服务
5. 健康检查（内网 + 公网 + journal 错误扫描）
6. **任一步失败自动回滚**并重启到旧版本

不碰的东西
---------
- `data/`（数据库、.env 是命根子）
- `venv/`
- `content/`（含服务器自己产生的 `.history/`，本地没有，绝不能删）

用法
----
    # 只看看会传哪些文件，不改动服务器
    python deploy_control.py --dry-run

    # 真部署（密码从环境变量读，不写进任何文件 —— AGENTS.md R5）
    set XIYUAN_SSH_PASSWORD=...
    python deploy_control.py

退出码：0 成功 / 1 失败（已自动回滚）/ 2 用法错误
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import posixpath
import sys
from pathlib import Path

LOCAL_ROOT = Path(__file__).resolve().parent
APP_DIR = "/opt/xiyuan-control"
SERVICE = "xiyuan-control"
PORT = 8100
PUBLIC_HEALTH = "https://con.wxxydouge.icu/healthz"

# 只同步这些子树里的 .py；data / venv / content 永远不碰
SYNC_SUBTREES = ("app", "tools")
EXTRA_FILES = ("requirements.txt",)


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def collect_local() -> dict[str, bytes]:
    """本地要部署的文件 → {相对路径: 内容}"""
    out: dict[str, bytes] = {}
    for sub in SYNC_SUBTREES:
        base = LOCAL_ROOT / sub
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            rel = p.relative_to(LOCAL_ROOT).as_posix()
            if "__pycache__" in rel:
                continue
            out[rel] = p.read_bytes()
    for name in EXTRA_FILES:
        p = LOCAL_ROOT / name
        if p.exists():
            out[name] = p.read_bytes()
    return out


class Deployer:
    def __init__(self, host: str, user: str, password: str, port: int = 22):
        import paramiko  # 延迟导入，方便 --help

        self.paramiko = paramiko
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    # ---------- 基础 ----------
    def connect(self) -> None:
        self.client.connect(
            self.host, port=self.port, username=self.user,
            password=self.password, timeout=25, banner_timeout=25, auth_timeout=25,
        )

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass

    def run(self, cmd: str, sudo: bool = False, timeout: int = 120) -> tuple[int, str, str]:
        if sudo:
            cmd = "sudo -S -p '' bash -lc %s" % _shquote(cmd)
        stdin, stdout, stderr = self.client.exec_command(cmd, timeout=timeout, get_pty=False)
        if sudo:
            stdin.write(self.password + "\n")
            stdin.flush()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err

    def put(self, remote: str, data: bytes) -> None:
        sftp = self.client.open_sftp()
        try:
            with sftp.file(remote, "wb") as f:
                f.write(data)
        finally:
            sftp.close()

    def sftp_read(self, remote: str) -> bytes:
        sftp = self.client.open_sftp()
        try:
            with sftp.file(remote, "rb") as f:
                return f.read()
        finally:
            sftp.close()

    # ---------- 步骤 ----------
    def remote_md5(self, rel_paths: list[str]) -> dict[str, str]:
        """一次 SSH 批量取远端 md5；返回 {相对路径: md5 或 '' 表示不存在}"""
        if not rel_paths:
            return {}
        # 用一条命令打包，避免每个文件一次往返
        script = "; ".join(
            'f=%s; if [ -f "$f" ]; then echo "%s $(md5sum "$f" | cut -d" " -f1)"; '
            'else echo "%s -"; fi' % (
                _shquote(posixpath.join(APP_DIR, p)), p.replace('"', ''), p.replace('"', '')
            )
            for p in rel_paths
        )
        code, out, err = self.run(script)
        if code != 0:
            raise RuntimeError("读取远端 md5 失败：%s" % err.strip()[:300])
        result: dict[str, str] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                result[parts[0]] = "" if parts[1] == "-" else parts[1]
        return result

    def backup(self, stamp: str) -> str:
        """备份 app/ 与数据库、.env；返回远端备份目录"""
        bak = "%s.app.bak.%s" % (APP_DIR, stamp)
        db_bak = "%s/data/backup/before-deploy-%s.tar.gz" % (APP_DIR, stamp)
        cmd = (
            "set -e; "
            "sudo -S -p '' rm -rf {bak}; "
            "sudo -S -p '' cp -a {app}/app {bak}; "
            "sudo -S -p '' mkdir -p {app}/data/backup; "
            "sudo -S -p '' tar czf {dbbak} -C {app}/data control.db control.db-wal control.db-shm .env 2>/dev/null || "
            "sudo -S -p '' tar czf {dbbak} -C {app}/data control.db .env; "
            "echo BACKUP_OK"
        ).format(bak=_shquote(bak), app=APP_DIR, dbbak=_shquote(db_bak))
        code, out, err = self.run(cmd, sudo=True, timeout=180)
        if "BACKUP_OK" not in out:
            raise RuntimeError("备份失败：%s" % (err.strip() or out.strip())[:400])
        return bak

    def upload(self, files: dict[str, bytes], stamp: str) -> str:
        """上传到临时目录并校验 md5"""
        tmp = "/tmp/deploy_%s" % stamp
        code, _, err = self.run("mkdir -p %s && echo OK" % _shquote(tmp))
        if code != 0:
            raise RuntimeError("建临时目录失败：%s" % err.strip()[:300])
        for rel in files:
            dst = posixpath.join(tmp, rel.replace("/", "__"))
            self.put(dst, files[rel])
        # 校验
        checks = "; ".join(
            'echo "%s $(md5sum %s | cut -d" " -f1)"' % (
                rel, _shquote(posixpath.join(tmp, rel.replace("/", "__")))
            )
            for rel in files
        )
        code, out, err = self.run(checks)
        if code != 0:
            raise RuntimeError("上传后校验失败：%s" % err.strip()[:300])
        remote = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                remote[parts[0]] = parts[1]
        bad = [r for r in files if remote.get(r) != md5_bytes(files[r])]
        if bad:
            raise RuntimeError("md5 不一致：%s" % ", ".join(bad[:5]))
        return tmp

    def precompile(self, files: dict[str, bytes], tmp: str) -> None:
        """落盘前 py_compile 预检（用临时目录里的副本）。

        🔴 只编译 .py：`EXTRA_FILES` 里还有 `requirements.txt` 这类非 Python 文件，
           把它们一并丢给 py_compile 会以 `SyntaxError: invalid syntax` 失败，
           而报错指向的是 requirements.txt 第 4 行 —— 看起来像代码问题，实际是清单写错。
           2026-11-10 首次改动 requirements.txt 时踩到（此前它从没变更过，所以一直没暴露）。
        """
        py_files = [r for r in files if r.endswith('.py')]
        if not py_files:
            return
        paths = " ".join(_shquote(posixpath.join(tmp, rel.replace("/", "__"))) for rel in py_files)
        cmd = (
            "cd %s && PYTHONPYCACHEPREFIX=/tmp/pyc_ctrl %s/venv/bin/python -m py_compile %s "
            "&& echo PY_COMPILE_OK"
        ) % (APP_DIR, APP_DIR, paths)
        code, out, err = self.run(cmd, timeout=180)
        if "PY_COMPILE_OK" not in out:
            raise RuntimeError("py_compile 预检失败，未改服务器：%s" % (err.strip() or out.strip())[:500])

    def install(self, files: dict[str, bytes], tmp: str) -> None:
        """原子覆盖到目标位置"""
        cmds = ["set -e"]
        for rel in files:
            src = posixpath.join(tmp, rel.replace("/", "__"))
            dst = posixpath.join(APP_DIR, rel)
            parent = posixpath.dirname(dst)
            cmds.append("sudo -S -p '' mkdir -p %s" % _shquote(parent))
            cmds.append("sudo -S -p '' cp %s %s" % (_shquote(src), _shquote(dst)))
        cmds.append("sudo -S -p '' chown -R ubuntu:ubuntu %s/app %s/tools 2>/dev/null || true"
                    % (APP_DIR, APP_DIR))
        code, out, err = self.run("; ".join(cmds), sudo=True, timeout=180)
        if code != 0:
            raise RuntimeError("覆盖文件失败：%s" % err.strip()[:400])

    def restart(self) -> str:
        """重启并等到真的能服务；返回重启时刻（用于切 journal 日志，避开上一轮的旧错误）。

        systemd 的 Restart=on-failure + RestartSec=5 会让崩掉的进程在 is-active 里显示
        "activating"，只 sleep 2 秒判一次会误判成功（2026-11-02 实测踩过）。
        """
        code, out, _ = self.run("date -Iseconds")
        mark = out.strip() or "1 minute ago"
        self.run("systemctl reset-failed %s; systemctl restart %s" % (SERVICE, SERVICE),
                 sudo=True, timeout=120)
        deadline = 25
        last = ""
        for _ in range(deadline):
            code, out, _ = self.run("systemctl is-active %s" % SERVICE)
            last = out.strip()
            if last == "active":
                code, out2, _ = self.run(
                    "curl -s -o /dev/null -w '%%{http_code}' --max-time 5 "
                    "http://127.0.0.1:%d/healthz" % PORT)
                if out2.strip() == "200":
                    return mark
            __import__("time").sleep(1)
        raise RuntimeError("服务 %d 秒内未就绪（最后状态 %s）" % (deadline, last or "?"))

    def health(self, since: str) -> list[str]:
        """返回问题列表；空列表表示全绿。since = 重启时刻，只看它之后的日志。"""
        problems = []
        code, out, _ = self.run("curl -s -o /dev/null -w '%%{http_code}' --max-time 10 "
                                "http://127.0.0.1:%d/healthz" % PORT)
        if out.strip() != "200":
            problems.append("内网 /healthz = %s" % (out.strip() or "无响应"))
        code, out, _ = self.run("curl -s -o /dev/null -w '%%{http_code}' --max-time 15 "
                                "%s" % PUBLIC_HEALTH)
        if out.strip() != "200":
            problems.append("公网 /healthz = %s" % (out.strip() or "无响应"))
        code, out, _ = self.run(
            "journalctl -u %s --since %s --no-pager 2>/dev/null "
            "| grep -icE 'traceback|error' || true" % (SERVICE, _shquote(since)))
        n = out.strip() or "0"
        if n.isdigit() and int(n) > 0:
            problems.append("重启后 journal 有 %s 条 error/traceback" % n)
        return problems

    def rollback(self, bak: str) -> None:
        print("!! 回滚中……")
        cmd = "sudo -S -p '' cp -a %s %s/app/" % (_shquote(bak + "/."), APP_DIR)
        code, out, err = self.run(cmd, sudo=True, timeout=180)
        if code != 0:
            print("   拷贝备份失败：%s" % err.strip()[:200])
        ok = False
        try:
            self.restart()
            ok = True
        except Exception as e:
            print("   回滚重启失败：%s" % e)
        print("   回滚结果：%s" % ("服务已恢复 active" if ok else "仍未起来"))
        if ok:
            print("   已回到旧版本（备份保留在 %s）" % bak)
        else:
            print("   ⚠️ 回滚后服务仍未起来，请人工检查：sudo journalctl -u %s -n 80" % SERVICE)


def _shquote(s: str) -> str:
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="控制台部署（一次调用走完全程）")
    ap.add_argument("--host", default="42.192.52.117")
    ap.add_argument("--user", default="ubuntu")
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--dry-run", action="store_true", help="只对比，不上传不重启")
    ap.add_argument("--force", action="store_true", help="即使没有文件变化也重启一次")
    args = ap.parse_args(argv)

    password = os.environ.get("XIYUAN_SSH_PASSWORD", "")
    if not password:
        print("缺少环境变量 XIYUAN_SSH_PASSWORD（密码不入库，见 AGENTS.md R5）")
        print("  PowerShell:  $env:XIYUAN_SSH_PASSWORD='...'  ")
        print("  bash:        export XIYUAN_SSH_PASSWORD='...'")
        return 2

    local = collect_local()
    print("本地待检查：%d 个文件" % len(local))

    d = Deployer(args.host, args.user, password, args.port)
    bak = None        # 备份成功后才赋值；一旦有值，异常就要回滚
    deployed = False  # 是否已经动过服务器上的文件
    try:
        d.connect()
        print("已连接 %s@%s" % (args.user, args.host))

        remote = d.remote_md5(list(local))
        changed = {r: b for r, b in local.items() if remote.get(r) != md5_bytes(b)}
        missing = [r for r in changed if not remote.get(r)]

        print("远端已一致：%d 个" % (len(local) - len(changed)))
        if missing:
            print("远端不存在：%s" % ", ".join(missing))
        if changed:
            print("待更新：%d 个" % len(changed))
            for r in sorted(changed):
                print("  - %s" % r)
        else:
            print("没有需要更新的文件。")

        if args.dry_run:
            print("\n--dry-run：到此为止，未改动服务器。")
            return 0

        if not changed and not args.force:
            print("\n无需部署。")
            return 0

        stamp = _now_stamp()
        bak = d.backup(stamp)
        print("已备份 app → %s" % bak)

        if changed:
            tmp = d.upload(changed, stamp)
            print("已上传并校验 md5 → %s" % tmp)
            d.precompile(changed, tmp)
            print("py_compile 预检通过")
            d.install(changed, tmp)
            deployed = True
            print("已覆盖到 %s" % APP_DIR)

        mark = d.restart()
        print("服务已重启并响应健康检查：active（自 %s 起算日志）" % mark)

        problems = d.health(mark)
        if problems:
            print("\n健康检查未通过：")
            for p in problems:
                print("  - %s" % p)
            d.rollback(bak)
            return 1

        print("\n健康检查全绿：内网 200 / 公网 200 / journal 无 error")
        if changed:
            print("\n下一步：")
            print("  python C:/build_ws/xiyuan_ops/verify.py")
            print("  git -C C:/build_ws/xiyuan_control status --porcelain")
        return 0
    except Exception as e:
        print("\n部署失败：%s" % e)
        if deployed and bak:
            d.rollback(bak)
        elif bak:
            print("（未改动服务器文件，无需回滚；备份 %s 保留）" % bak)
        return 1
    finally:
        d.close()


def _now_stamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d-%H%M%S")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
