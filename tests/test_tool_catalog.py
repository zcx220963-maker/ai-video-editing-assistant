"""出口替换器验证（不联网）：ToolCatalog 的双轨规则与流式挂起缓冲。

三件必备用例（收口判据点名的那三件）：
  ① 前缀遮蔽 —— 长名必须先于短名试，否则 plan_timeline 会把 plan_timeline_pro 咬断；
  ② token 切断 —— ``split_sho`` + ``ts`` 分两帧到达，出口不许有英文残片；
  ③ 键位双轨 —— 机器名留在结构化键上并挂 *_display，自由文本才就地换中文。

运行：  python tests/test_tool_catalog.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.catalog import (  # noqa: E402
    PARAM_LABELS,
    StreamRewriter,
    ToolCatalog,
    get_catalog,
    set_catalog,
)

DISPLAYS = {
    "split_shots": "镜头切分",
    "plan_timeline": "时间线编排",
    "plan_timeline_pro": "时间线编排·专业版",
    "plan_timeline_ai_transition": "时间线编排·AI转场",
    "render_video": "成片渲染",
    "render_status": "渲染进度查询",
    "select_BGM": "背景音乐选择",
    "asr": "语音转写",
    "grep": "检索文件",
    "load_skill": "加载技能",
    "highlight_extraction_skill": "高光片段提取",
}

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 0 if cond else 1


def cat() -> ToolCatalog:
    return ToolCatalog(DISPLAYS)


# ---- ① 前缀遮蔽 ----------------------------------------------------------

def case_prefix() -> None:
    c = cat()
    check(c.rewrite_text("先 plan_timeline_pro 再 plan_timeline")
          == "先 时间线编排·专业版 再 时间线编排", "长名优先，短名不咬长名")
    check(c.rewrite_text("plan_timeline_ai_transition 与 plan_timeline 是两条路径")
          == "时间线编排·AI转场 与 时间线编排 是两条路径", "三个同前缀名字各归各位")
    check(c.rewrite_text("render_video 之后要 render_status")
          == "成片渲染 之后要 渲染进度查询", "两个不同长度名字一起换")
    check(c.rewrite_text("select_BGM 区分大小写")
          == "背景音乐选择 区分大小写", "名字里的大写字母照样匹配")
    check(c.rewrite_text("SELECT_BGM select_bgm")
          == "SELECT_BGM select_bgm", "大小写敏感：正文里的别拼法不误伤")
    check(c.rewrite_text("ASR 只应跑在含人声的素材上")
          == "ASR 只应跑在含人声的素材上", "三字母名 asr 不吃正文里的 ASR")


# ---- ② 流式挂起缓冲 ------------------------------------------------------

def case_stream() -> None:
    c = cat()
    s = StreamRewriter(c)
    out1 = s.feed("接下来调用 split_sho")
    out2 = s.feed("ts 把镜头切开")
    tail = s.flush()
    whole = out1 + out2 + tail
    check("split_sho" not in whole and "ts 把" not in whole,
          f"残片不出门（实得 {whole!r}）")
    check(whole == "接下来调用 镜头切分 把镜头切开", "两帧拼回来是一句完整中文")

    s2 = StreamRewriter(c)
    w2 = s2.feed("调用 render_video") + s2.feed("") + s2.flush()
    check(w2 == "调用 成片渲染", "帧尾正好停在完整名字上：扣到 flush 才发")

    s3 = StreamRewriter(c)
    w3 = "".join(s3.feed(p) for p in ["先", "计", "划", "调", "用", " plan_", "timeline",
                                      "_pro，", "然后", "渲染"]) + s3.flush()
    check(w3 == "先计划调用 时间线编排·专业版，然后渲染", "逐字符切片仍能拼出长名")

    s4 = StreamRewriter(c)
    w4 = "".join([s4.feed("看这段 `split_sho"), s4.feed("ts` 原样保留"), s4.flush()])
    check(w4 == "看这段 `split_shots` 原样保留",
          f"代码段跨帧也不替换、反引号不丢（实得 {w4!r}）")

    s5 = StreamRewriter(c)
    w5 = "".join([s5.feed("```python\nrender_video("), s5.feed(")\n```\n完了")]) + s5.flush()
    check(w5 == "```python\nrender_video()\n```\n完了",
          f"围栏代码块跨帧原样、闭合后正文照旧替换（实得 {w5!r}）")

    s6 = StreamRewriter(c)
    check(s6.feed("纯中文没有工具名") == "纯中文没有工具名", "无关增量不扣字")

    s7 = StreamRewriter(ToolCatalog())
    check(s7.feed("空词表 ") == "空词表 " and s7.feed("原样") == "原样",
          "词表为空时流式完全直通")


# ---- ③ 键位双轨 ----------------------------------------------------------

def case_dual_track() -> None:
    c = cat()
    frame = {"type": "tool", "tool": "render_video", "session_id": "u:1:c:1",
             "args": {"node": "split_shots"}, "result": "render_video 已完成",
             "rerun_nodes": ["plan_timeline", "asr"]}
    out = c.rewrite_obj(frame)
    check(out["tool"] == "render_video" and out["tool_display"] == "成片渲染",
          "机器名留在 tool 上，另挂 tool_display")
    check(out["args"] == {"node": "split_shots", "node_display": "镜头切分"},
          "嵌套层同样双轨")
    check(out["result"] == "成片渲染 已完成", "自由文本就地换中文")
    check(out["session_id"] == "u:1:c:1", "会话标识不碰")
    check(out["rerun_nodes"] == ["plan_timeline", "asr"]
          and out["rerun_nodes_display"] == ["时间线编排", "语音转写"],
          "机器名列表保留原值并挂等长中文列表")
    check(frame["result"] == "render_video 已完成", "输入对象没被就地改掉")

    media = {"file_name": "render_video.mp4", "object_key": "materials/split_shots/x.mp4",
             "label": "已加载 render_video.mp4", "media_url": "http://h/a/render_video.mp4"}
    o2 = c.rewrite_obj(media)
    check(o2["file_name"] == "render_video.mp4" and o2["object_key"] == "materials/split_shots/x.mp4",
          "文件名与对象键是用户内容，一字不改")
    check(o2["media_url"] == media["media_url"], "链接不替")

    lst = c.rewrite_obj([{"tool": "asr"}, {"tools": ["grep", "load_skill"]}])
    check(lst[0]["tool_display"] == "语音转写"
          and lst[1]["tools_display"] == ["检索文件", "加载技能"],
          "顶层是列表时也逐个走双轨")

    check(c.rewrite_obj({"unknown_key": "plan_timeline 步骤"})
          == {"unknown_key": "时间线编排 步骤"}, "表外的键按自由文本处理")
    check(c.rewrite_obj({"note_display": "已有同名字段"}) == {"note_display": "已有同名字段"},
          "已存在的 *_display 不被覆盖")


# ---- 边界：URL / 路径 / 代码 / 未声明 ------------------------------------

def case_boundaries() -> None:
    c = cat()
    check(c.rewrite_text("产物在 store:split_shots 里") == "产物在 store:split_shots 里",
          "Store 键里的机器名不动（它是键）")
    check(c.rewrite_text("见 docs/split_shots/plan.md") == "见 docs/split_shots/plan.md",
          "路径分段里的机器名不动")
    check(c.rewrite_text("详情 https://x.dev/render_video/a?b=plan_timeline 结尾")
          == "详情 https://x.dev/render_video/a?b=plan_timeline 结尾", "URL 整体不替")
    check(c.rewrite_text("日志在 D:\\work\\render_video.log 下")
          == "日志在 D:\\work\\render_video.log 下", "Windows 路径不替")
    check(c.rewrite_text("`grep` 与 grep.log 和 grep 命令")
          == "`grep` 与 grep.log 和 检索文件 命令", "反引号内不替、带后缀不替、独立词替")
    check(c.rewrite_text("C:\\Users\\xu'zhi'cheng\\Desktop")
          == "C:\\Users\\xu'zhi'cheng\\Desktop", "含引号的路径不替")
    check(c.rewrite_text("split_shots，然后render_video。")
          == "镜头切分，然后成片渲染。", "紧贴中文标点也替换（中文不是标识符字符）")
    check(c.rewrite_text("not_a_tool 保持原样") == "not_a_tool 保持原样",
          "词表外的名字原样送出（宁可露真名，不编造）")
    check(c.rewrite_text("") == "" and c.rewrite_text("没有名字") == "没有名字",
          "空串与无命中零开销直通")
    check(ToolCatalog().rewrite_text("split_shots 未装配词表") == "split_shots 未装配词表",
          "空词表全直通（Storyline 没连上时不编中文名）")
    check(c.rewrite_text("镜头切分 是中文") == "镜头切分 是中文", "中文名不会被再换一次")


# ---- 取数口：标签、反查、进程级单例 --------------------------------------

def case_lookup() -> None:
    c = cat()
    check(c.label("split_shots") == "镜头切分" and c.label("mystery") == "mystery",
          "label() 查不到就原样给回")
    check(c.display("mystery") == "", "display() 查不到给空串")
    check(c.machine_name("镜头切分") == "split_shots", "反查：中文 → 机器名")
    c.update({"other_tool": "镜头切分"})
    check(c.machine_name("镜头切分") == "", "两个机器名撞同一个中文时反查直接不作答")
    check(c.labeled(["split_shots", "unknown"]) ==
          [{"key": "split_shots", "label": "镜头切分"},
           {"key": "unknown", "label": "unknown"}], "labeled() 给计划卡用的形状")
    c2 = ToolCatalog({"a_tool": "甲"})
    c2.update({"b_tool": "乙"})
    check(c2.label("b_tool") == "乙", "update 增量并入")
    c2.update({"a_tool": "甲"})
    check(c2.label("a_tool") == "甲", "同值重复并入幂等")

    old = get_catalog()
    try:
        set_catalog(c)
        check(get_catalog() is c and get_catalog().label("asr") == "语音转写",
              "进程级单例可被装配期填上、出口处读到")
        set_catalog(ToolCatalog())
        check(not get_catalog(), "空表是可用的降级态")
    finally:
        set_catalog(old)


def case_params() -> None:
    c = cat()
    check(c.param_label("material_ids") == "素材", "参数名查单源表（不在 _displays 里）")
    check(c.param_label("wait_sec") == "等待时长（秒）",
          "渲染的等待参数在册（此前只有前端那张表里有）")
    check(c.param_label("totally_unknown") == "totally_unknown",
          "没声明过的参数原样给回，不编一个")
    check(len(c.params_display()) >= 60 and c.params_display()["timeline"] == "时间线",
          f"整表可导出给 /tools（{len(c.params_display())} 条）")
    check(c.arg_labels({"material_ids": ["m1"], "totally_unknown": 1}) == {"material_ids": "素材"},
          "一次调用的标签只收查得到的键，未声明的不占位")
    check(c.arg_labels('{"query": "x", "wait_sec": 3}')
          == {"query": "关键词", "wait_sec": "等待时长（秒）"},
          "工具帧里的 arguments 是 JSON 文本也认")
    check(c.arg_labels("not json") is None and c.arg_labels({"nope": 1}) is None
          and c.arg_labels(None) is None,
          "坏文本 / 一个都查不到 / 没有入参 → None，出口不挂空位")
    check(all(k not in c.names for k in ("name", "path", "key", "task", "mode")),
          "参数名不进修辞替换候选：普通词进表会把正文咬坏")
    check(c.rewrite_text("按 name 排序，取 path 下的 grep 结果")
          == "按 name 排序，取 path 下的 检索文件 结果",
          "参数位与工具名各走各的：grep 换中文，name/path 原样")
    check(PARAM_LABELS.keys() >= {"session_id", "artifact_id", "user_request"},
          "剪辑节点的公共参数（作用域四件套）在单源表里")


async def main() -> None:
    print("=== ① 前缀遮蔽（长名优先） ===")
    case_prefix()
    print("=== ② 流式挂起缓冲（token 切断） ===")
    case_stream()
    print("=== ③ 键位双轨（机器名留键、中文挂旁路） ===")
    case_dual_track()
    print("=== 边界：URL / 路径 / 代码段 / 未声明 ===")
    case_boundaries()
    print("=== 取数口：标签 / 反查 / 单例 ===")
    case_lookup()
    print("=== 参数标签：单源表 + arg_labels ===")
    case_params()
    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
