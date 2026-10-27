"""四处**本仓自持**的解析器/加密件：金样回归（R10-T49-② / Q66）。

这四处（JWT HS256 + PBKDF2、YAML 子集、frontmatter、CJK token 计数）经逐条定价后
**决定不换库**，理由写在开发指南 §3.6：换 pyjwt 会打开**算法协商面**（现实现只认
HS256）、换 bcrypt 要重做历史口令兼容、换 tiktoken 会让 `65536` 这条**跨模型**预算
变成按模型分词器各算一套。

**frontmatter 是唯一的例外**（R13）：它改成优先 `yaml.safe_load`（`requirements.txt`
已声明 PyYAML），极简 `key: value` 只作无依赖兜底；MCP 配置那份 YAML 子集仍是自写
解析器。所以 [3] 一节现在把**两条路径**都钉住。

不换库的代价是「谁改坏了没人知道」，所以这里的判据是 **不改行为、只锁行为**：
每个输入都带一条**当前实现的真实输出**（金样），改了就会红。金样里刻意包含边界
（Q66 点名的四类）：无 `Authorization` 头 / 过期 / 错签名、`${ENV}` 未定义、
多行 `description`、纯 CJK 与中英混排。

**金样不是"正确性证明"**：其中几条记的是当前实现的**已知粗糙处**（比如无 PyYAML 的
回退路径上，多行 `description` 只留第一行、没带 `:` 的续行会被丢掉）。把它们如实
锁住，是为了让「想改它的人」看得见自己在改什么——而不是假装它们不存在。

运行：  python tests/test_handrolled_goldens.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.auth import (bearer_token, hash_password, jwt_issue, jwt_verify,
                                  verify_password)
from agent_framework.compress import default_token_counter, estimate_tokens
from agent_framework.skill import _parse_frontmatter
from agent_framework.tools.mcp import parse_simple_yaml

_checks = 0
_fails = 0
_MISSING = object()


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def eq(got: object, want: object, label: str) -> None:
    check(got == want, f"{label}（实得 {got!r}）")


# --------------------------------------------------------------------------
# ① JWT（HS256）+ PBKDF2 —— auth.py
# --------------------------------------------------------------------------

def part1_jwt_and_password() -> None:
    print("\n[1] auth.py：JWT HS256 + PBKDF2")
    secret = b"golden-secret"

    # 无 Authorization 头 / 非 Bearer 方案 —— 都读成「没带 token」，不是抛错
    eq(bearer_token(None), "", "Authorization 头缺失 → 空串（不是异常）")
    eq(bearer_token(""), "", "空头 → 空串")
    eq(bearer_token("Basic dXNlcjpwYXNz"), "", "Basic 方案 → 空串（只认 Bearer）")
    eq(bearer_token("Bearer abc.def.ghi"), "abc.def.ghi", "Bearer 头 → 明文 token")
    eq(bearer_token("  bearer   tok  "), "tok", "大小写不敏感 + 两端空白剥掉")

    token = jwt_issue("u_gold", secret, ttl_sec=3600)
    eq(token.count("."), 2, "JWT 是三段（header.payload.signature）")
    eq(jwt_verify(token, secret), "u_gold", "正常签发/验证往返")
    eq(jwt_verify(token, b"other-secret"), None, "错签名 → None")
    eq(jwt_verify(jwt_issue("u_gold", secret, ttl_sec=-1), secret), None,
       "已过期（exp 在过去）→ None")
    head, payload, sig = token.split(".")
    eq(jwt_verify(f"{head}.{payload}.{sig[:-2]}xx", secret), None, "签名被改坏 → None")
    eq(jwt_verify("not-a-jwt", secret), None, "不是三段 → None（不许抛）")
    eq(jwt_verify("", secret), None, "空串 → None")
    # alg 只在 header 里声明，验签走的是固定 HS256——**没有算法协商面**（Q66 的定价依据）
    eq(jwt_verify(f"{head}.{payload}.{sig}", b"golden-secret"), "u_gold",
       "同一密钥的等价写法（bytes）照常通过")

    stored = hash_password("pw-golden")
    eq(stored.split("$")[0], "pbkdf2", "口令散列方案名固定为 pbkdf2")
    eq(stored.split("$")[1], "300000", "迭代次数 300000（改小即降安全，金样在这里拦）")
    check(len(stored.split("$")[2]) == 32, "盐 16 字节 → 32 位十六进制")
    eq(verify_password("pw-golden", stored), True, "正确口令 → True")
    eq(verify_password("pw-golden2", stored), False, "错口令 → False")
    eq(verify_password("pw-golden", "bcrypt$xx$yy$zz"), False, "别的方案 → False（不抛）")
    eq(verify_password("pw-golden", ""), False, "空散列 → False")
    check(hash_password("pw-golden") != stored, "同一口令两次散列不同（盐每次新取）")


# --------------------------------------------------------------------------
# ② YAML 子集 —— tools/mcp.py:parse_simple_yaml
# --------------------------------------------------------------------------

_YAML_GOLDEN = """\
# MCP 配置：注释整行跳过
servers:
  local:
    type: stdio
    command: python
    args: ["-m", "stub", "--x=1"]
    tool_timeout: 12
    note: "带 # 的引号串"
  remote:
    type: streamableHttp
    url: https://${GOLDEN_HOST}/mcp
    token: ${GOLDEN_UNSET_VAR}
    empty:
"""


def part2_yaml_subset() -> None:
    print("\n[2] mcp.py：YAML 子集解析（含 ${ENV} 展开）")
    os.environ["GOLDEN_HOST"] = "example.test"
    os.environ.pop("GOLDEN_UNSET_VAR", None)
    try:
        data = parse_simple_yaml(_YAML_GOLDEN)
    finally:
        os.environ.pop("GOLDEN_HOST", None)

    # 形状按**生产用那一种**：`MCPToolsConfig.from_dict` 对 servers 调 `.items()`，
    # 所以它是「名字 → 配置」的嵌套映射，不是列表（列表形状它会 AttributeError）。
    servers = data["servers"]
    eq(sorted(servers), ["local", "remote"], "嵌套映射：两台 server 都在")
    eq(servers["local"]["type"], "stdio", "第二层的键值对")
    eq(servers["local"]["args"], ["-m", "stub", "--x=1"], "行内列表 [a, b]（引号与 = 都保留）")
    eq(servers["local"]["tool_timeout"], 12, "整数 → int")
    eq(servers["local"]["note"], "带 # 的引号串", "引号内的 # 不是注释、引号被剥掉")
    eq(servers["remote"]["url"], "https://example.test/mcp", "${ENV} 已定义 → 展开成值")
    # **这条是金样里最重要的一个**：未定义变量展开成**空串**，既不保留字面量也不抛错。
    # 保留字面量会把一个没设的环境变量名当成密钥发出去；抛错会让整份配置用不了。
    eq(servers["remote"]["token"], "", "${ENV} 未定义 → 空串（不保留字面量、不抛错）")
    eq(servers["remote"]["empty"], None, "空值的 key → None")
    eq(parse_simple_yaml(""), {}, "空文本 → 空映射")
    eq(parse_simple_yaml("# 只有注释\n"), {}, "只有注释 → 空映射")
    eq(parse_simple_yaml("a: null\nb: ~\nc: \n"), {"a": None, "b": None, "c": None},
       "null / ~ / 空值 三种写法都读成 None")
    eq(parse_simple_yaml("xs: [1, 2, 3]\n"), {"xs": [1, 2, 3]}, "顶层行内列表")
    eq(parse_simple_yaml("xs: []\n"), {"xs": []}, "空的行内列表")

    # ---- 两条**当前实现的边界**，如实锁住（Q66：不改行为，只锁行为）----
    # ① `{}` 不进行内列表分支（那只认 `[`），会被当成字符串 "{}"。而 `from_dict`
    #    对 headers 调 `dict(...)`——写 YAML 时用 `headers: {}` 会在建配置时炸掉。
    #    空映射的正确写法是**省略这个键**（默认值本来就是空 dict）。
    eq(parse_simple_yaml("headers: {}\n"), {"headers": "{}"},
       "YAML 里 `{}` 读成字符串 \"{}\"（空映射请省略该键，别写 {}）")
    # ② 列表里「一项占多行」的形状不支持：`- name: x` 之后的续行会漏到**上一层**。
    #    生产用的 servers 形状是嵌套映射（见上），所以这条路线上没有配置依赖它；
    #    真要写列表就用 JSON——`mcp.json` 走的就是 JSON 分支。
    leaky = parse_simple_yaml("servers:\n  - name: a\n    type: stdio\n")
    eq(list(leaky), ["servers", "type"],
       "「- 多行映射」的列表形状不支持：续行 `type` 漏到了顶层（改用嵌套映射或 JSON）")
    eq(leaky["servers"], [{"name": "a"}], "列表项里只读到第一个键")


# --------------------------------------------------------------------------
# ③ frontmatter —— skill.py:_parse_frontmatter
# --------------------------------------------------------------------------

def part3_frontmatter() -> None:
    print("\n[3] skill.py：frontmatter 解析")
    meta, body = _parse_frontmatter(
        '---\nname: demo\ndescription: "一句话说明"\nalways: true\n'
        'requires: CLI: curl, ENV: KEY\n---\n\n正文第一行\n正文第二行\n')
    eq(meta["name"], "demo", "普通键值")
    eq(meta["description"], "一句话说明", "双引号被剥掉")
    eq(meta["always"], "true", "**值是字符串**（布尔转换在 _is_true，不在解析器里）")
    eq(meta["requires"], "CLI: curl, ENV: KEY", "值里的冒号不被当分隔符切开（按第一个 : 切）")
    eq(body, "正文第一行\n正文第二行", "正文 = 收尾 --- 之后的全部内容（两端 strip）")

    # 多行 description：**两条路径都要钉**。R13 起 frontmatter 优先 PyYAML
    #（`requirements.txt` 已声明），续行按 YAML 折成一行；没有 PyYAML 时回退的极简
    # `key: value` 解析只留第一行（续行静默丢弃）。形状不同，但都不许把续行变成
    # meta 里的一个**键**——那是两条路共同的下限。
    src = "---\nname: multi\ndescription: 第一行\n  第二行\n---\n正文\n"
    meta2, body2 = _parse_frontmatter(src)
    eq(meta2["description"], "第一行 第二行",
       "多行 description 按 YAML 折叠成一行（PyYAML 路径）")
    eq(body2, "正文", "正文不受续行影响")
    check("第二行" not in meta2, "续行没有变成任何键（它落不进 meta）")

    # 无 PyYAML 的回退路径：续行丢弃，只留第一行（这是 `_parse_frontmatter` 的
    # 兜底语义，容器里装了 PyYAML 也要能钉住它）。把 sys.modules["yaml"] 置 None
    # 即可让 `import yaml` 抛 ImportError，无需卸载依赖。
    saved = sys.modules.get("yaml", _MISSING)
    sys.modules["yaml"] = None
    try:
        meta_fb, body_fb = _parse_frontmatter(src)
    finally:
        if saved is _MISSING:
            del sys.modules["yaml"]
        else:
            sys.modules["yaml"] = saved
    eq(meta_fb["description"], "第一行",
       "无 PyYAML 回退路径只留第一行（续行丢弃——已知且锁住）")
    eq(body_fb, "正文", "回退路径的正文同样不受续行影响")

    meta3, body3 = _parse_frontmatter("没有 frontmatter 的正文\n第二行")
    eq(meta3, {}, "不以 --- 开头 → 空 meta")
    eq(body3, "没有 frontmatter 的正文\n第二行", "整段都是正文")

    meta4, body4 = _parse_frontmatter("---\nname: x")
    eq(meta4.get("name"), "x", "没有收尾 --- 时键照样读进来（宽松）")
    eq(body4, "", "没有收尾 --- → 正文为空")
    eq(_parse_frontmatter("---\n---\n正文")[1], "正文", "空 frontmatter → meta 空、正文照旧")


# --------------------------------------------------------------------------
# ④ CJK token 计数 —— compress.py:estimate_tokens / default_token_counter
# --------------------------------------------------------------------------

def part4_cjk_counting() -> None:
    print("\n[4] compress.py：CJK 感知的 token 估算")
    eq(estimate_tokens(""), 0, "空串 → 0")
    eq(estimate_tokens("你好世界"), 4, "纯 CJK：一字 1 token（旧实现 4 字算 1，低 4 倍）")
    eq(estimate_tokens("，。！？"), 4, "全角标点（0x3000–0x303F）也算 1 字")
    eq(estimate_tokens("한국어"), 3, "谚文（0xAC00–0xD7AF）也算 1 字")
    eq(estimate_tokens("abcdefgh"), 2, "纯 ASCII：4 字符 1 token")
    eq(estimate_tokens("ab"), 1, "不足 4 字符也至少算 1（max(1, …)）")
    eq(estimate_tokens("你好a"), 3, "混排：2（CJK）+ max(1, 1//4) = 3")
    eq(estimate_tokens("你好abcd"), 3, "混排：2 + max(1, 1) = 3")
    eq(estimate_tokens("你好abcdefgh"), 4, "混排：2 + 8//4 = 4")
    # 计数器 = 估算 + 4（每条消息的固定开销），tool_calls 也进估算
    eq(default_token_counter({"role": "user", "content": "你好"}), 6, "消息计数 = 估算 2 + 固定 4")
    eq(default_token_counter({"role": "user", "content": ""}), 4, "空正文只留固定开销")
    eq(default_token_counter({"role": "assistant", "content": "",
                              "tool_calls": [{"id": "c1", "function": {"name": "n",
                                                                       "arguments": "{}"}}]}) > 4,
       True, "tool_calls 的 JSON 也进估算（只发 content 会漏掉一大块）")


def main() -> None:
    part1_jwt_and_password()
    part2_yaml_subset()
    part3_frontmatter()
    part4_cjk_counting()
    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
