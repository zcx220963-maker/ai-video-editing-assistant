# -*- coding: utf-8 -*-
"""账号密码登录 + JWT 鉴权的离线验证。

运行：  python tests/test_auth_login.py   # 内存替身,不联网

钉住八件事：
① 密码哈希往返与错密码拒绝;② 账号注册与用户名唯一;③ 登录成功/失败;
④ JWT 签发→解析往返;⑤ 过期与篡改拒绝;⑥ 双凭证解析(JWT/旧 token);
⑦ 跨实例密钥一致(密钥落库,两个 Authenticator 互通);⑧ 匿名旧 token 兼容。
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.auth import (
    Authenticator, hash_password, jwt_issue, jwt_verify, verify_password,
)
from agent_framework.storage import build_storage

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


async def part1_password() -> None:
    print("\n[1] 密码哈希:往返与错密码拒绝")
    stored = hash_password("s3cret!密码")
    check(verify_password("s3cret!密码", stored), "正确密码通过")
    check(not verify_password("wrong", stored), "错密码拒绝")
    check(stored.startswith("pbkdf2$") and "s3cret" not in stored,
          "存储形态是哈希,不含明文")


async def part2_register_login() -> None:
    print("\n[2] 账号注册与登录")
    st = build_storage("memory")
    await st.users.provision("default")     # JWT 密钥落库的归属身份
    auth = Authenticator(st)

    uid = await auth.register_user("小明", "pw123456")
    check(uid.startswith("u-"), f"注册返回 user_id({uid[:8]}…)")
    try:
        await auth.register_user("小明", "other")
        check(False, "用户名唯一(竟未拒)")
    except Exception as e:
        check("已被使用" in str(e), f"重复用户名拒收({e})")

    tok = await auth.login("小明", "pw123456")
    check(bool(tok) and tok.count(".") == 2, "登录成功签发 JWT(三段)")
    check(await auth.login("小明", "wrong!") is None, "错密码 None")
    check(await auth.login("不存在的人", "pw123456") is None, "不存在用户 None")
    return st, auth, uid, tok


async def part3_jwt(st, auth, uid, tok) -> None:
    print("\n[3] JWT:往返、过期、篡改、双凭证")
    check(await auth.resolve_credential(tok) == uid, "JWT 解析回 user_id")

    expired = jwt_issue(uid, b"k", ttl_sec=-10)
    check(await auth.resolve_credential(expired) is None, "过期 JWT 拒绝")

    tampered = tok[:-3] + ("aaa" if not tok.endswith("aaa") else "bbb")
    check(await auth.resolve_credential(tampered) is None, "篡改签名拒绝")

    # 旧随机 token(匿名/CLI 通道)仍然可用
    old_uid, old_token = await auth.register("legacy")
    check(await auth.resolve_credential(old_token) == old_uid,
          "旧随机 token 兼容(双凭证解析)")

    # 跨实例:另一个 Authenticator 连同一个库,密钥落库 → JWT 互通
    auth2 = Authenticator(st)
    check(await auth2.resolve_credential(tok) == uid,
          "密钥落库:重启/换实例后 JWT 仍有效")


async def part4_garbage(st) -> None:
    print("\n[4] 杂项凭证:一律 None 不抛")
    auth = Authenticator(st)
    for cred in ("", "garbage", "a.b", "a.b.c.d", "null.null.null"):
        check(await auth.resolve_credential(cred) is None, f"无效凭证 {cred!r} → None")


async def main() -> None:
    st, auth, uid, tok = await part2_register_login()
    await part1_password()
    await part3_jwt(st, auth, uid, tok)
    await part4_garbage(st)
    print(f"\n==== {_checks} 项检查，{_fails} 项失败 ====")
    if _fails:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
