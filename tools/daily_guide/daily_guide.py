"""《高性价比人生指南》每日一条：按书中顺序取固定条数，拼成一条视频的提示词。

数据来自 https://github.com/eternity4719/HowToLiveBetter （Unlicense，公共领域）。
这里只做三件事：同步书稿、解析条目、按游标生成当天提示词。
生成视频仍走项目现有的自动链路；本脚本不调用模型。
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BOOK_DIR = ROOT / "book"
CURSOR = ROOT / "cursor.json"
PROMPTS = ROOT / "prompts"
ANGLES = ROOT / "angles.json"
TARBALL = "https://codeload.github.com/eternity4719/HowToLiveBetter/tar.gz/refs/heads/main"
SOURCE = "https://github.com/eternity4719/HowToLiveBetter"

FIELDS = {"cost": "成本", "plain": "说人话", "gain": "收益",
          "level": "证据等级", "source": "来源", "note": "备注"}


def fetch_book(force: bool = False) -> None:
    if BOOK_DIR.exists() and any(BOOK_DIR.glob("*.md")) and not force:
        print(f"书稿已存在：{BOOK_DIR}（--force 可重新下载）")
        return
    print("下载书稿……", flush=True)
    req = urllib.request.Request(TARBALL, headers={"User-Agent": "daily-guide"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = resp.read()
    BOOK_DIR.mkdir(parents=True, exist_ok=True)
    kept = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            if "/book/" in name and name.endswith(".md"):
                f = tar.extractfile(member)
                if f is None:
                    continue
                target = BOOK_DIR / Path(name).name
                target.write_bytes(f.read())
                kept += 1
            elif name.endswith("/LICENSE") and name.count("/") == 1:
                f = tar.extractfile(member)
                if f is not None:
                    (ROOT / "LICENSE-book").write_bytes(f.read())
    print(f"已保存 {kept} 章到 {BOOK_DIR}")


def _field(body: str, name: str) -> str:
    m = re.search(rf"^-\s*{name}：(.*?)(?=^-\s*[^\n：]+：|^###|\Z)", body, re.M | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def parse_book() -> list[dict]:
    if not BOOK_DIR.exists():
        raise SystemExit(f"没有书稿：{BOOK_DIR}，先运行 `python {Path(__file__).name} fetch`")
    entries: list[dict] = []
    for path in sorted(BOOK_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        m = re.search(r"^#\s+(.+)$", text, re.M)
        chapter = m.group(1).strip() if m else path.stem
        blocks = re.split(r"^###\s+(\d+)\.\s*", text, flags=re.M)
        for i in range(1, len(blocks), 2):
            body = blocks[i + 1]
            tag = re.search(r"<!--\s*成本标签:\s*(.*?)\s*-->", body)
            entries.append({
                "id": f"{len(entries) + 1:03d}",
                "chapter": chapter,
                "num": int(blocks[i]),
                "title": body.splitlines()[0].strip(),
                "tag": tag.group(1) if tag else "",
                **{key: _field(body, label) for key, label in FIELDS.items()},
                "file": path.name,
            })
    return entries


def load_cursor() -> dict:
    if CURSOR.exists():
        return json.loads(CURSOR.read_text(encoding="utf-8"))
    return {"next": 0, "episode": 0, "last_date": "", "last_entries": []}


def save_cursor(cursor: dict) -> None:
    CURSOR.write_text(json.dumps(cursor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


#: 演示版多出来的「效果编排」。日常版不写这一段：一镜两种动效、逐条指定画法，
#: 适合拿来做能力展示，不适合每天出片——它会明显拉长写码与评审时间。
#: 这里只写「要什么效果、落在哪一条」，不点名共享组件，也不写「不照做就打回」。
SHOWCASE_RULES = """\
演示版效果编排（这一版用来展示能力，日常版不用照做）：
17. 转场按段落走：整片 `transition: dissolve`、`transition_sec: 0.4`；第 2 条之后 `fadeblack`（暗场换夜），第 3 条之后 `wipeleft`，第 5 条之后 `slideup`，第 6 条之后 `circleclose`，第 7 条之后 `zoomin`，第 8 条之后 `radial`（涟漪荡开），第 9 条之后 `hblur`，第 10 条之后 `smoothleft`；第 4 条之后用 `circleopen`；时长统一 0.4 秒，最后一镜不写转场。不要每条接缝都换花样。
18. 整片入场 `entrance: rise`、出场 `exit: zoom`；这两项是整片一档，不是逐镜设置。
19. 每一镜至少两处看得见的动效，其中至少一处锚在这一镜旁白的具体词上（`motion` 的 anchors 写 `subtitle:<第几个词>`）；数字镜再加一处锚在数字字段上。动效必须服务这一镜的内容，宁少勿滥。
20. 下面这些画法至少各出现一次，按条目分配；一镜最多一个主效果、一个辅助效果：
    - 真三维：第 1 条的安全带卡扣、第 7 条的血压计，各做一件器物；有厚度、有金属/橡胶材质、有接触阴影，慢推近或轻绕，不做群像。这是整片的底子，其余镜头也都在同一个三维台面上。
    - 手绘线稿逐笔生长：第 2 条头盔的受力剖面、第 6 条楼道的逃生路线；线从起点长到终点（描边逐段显现），画在实物之上、不遮住实物，不要整张图淡入。
    - 遮罩揭示：第 4 条软管剖面、第 5 条蘑菇的菌褶、第 10 条并排碰撞对比；用遮罩一层层露出实物内部（像手绘一笔笔完成），不要拿淡入代替。
    - 漂浮粒子：第 3 条煤炉上方看不见的气体、第 6 条的烟尘飘散、第 1 条安全带缝里的微尘；一堆微弱光点慢慢飘，粒子亮度低于主体，不抢戏。
    - 爆炸/纸屑粒子：第 9 条撞击瞬间炸开一层碎屑（玻璃渣、塑料片、纸屑），只炸一下、两秒内收干净，不要满屏噪点。
    - 手绘标注：第 5 条把「土办法鉴别」划掉、第 7 条圈住 44.7%、第 8 条给 7.2% 画下划线；像人手画的，线条压在实物上，不要规整的 PPT 框。
    - 镜头运动：第 1 条绕卡扣缓慢旋转、第 9 条撞击时短促摇摆再推近、第 10 条横向平移做对比；其余镜头只做 1.02→1.05 的慢推或轻横移，不要甩镜。
    - 生长：第 5 条蘑菇从土里长出来、第 8 条坐标轴和曲线一起长；沿路径长，不要缩放整张图冒充生长。
    - 纯代码场景：第 3 条夜里的煤炉与窗、第 6 条楼道纵深、第 10 条的车内视角，都用几何、材质、灯光和雾画成三维场景（墙、地面、窗格、仪表盘），不要照片、贴图。
    - 剪纸质感：第 5、9 条画面里的纸制品（说明书、警示标签、旧报纸）用真实纸材质——撕边、纸纹、层间投影；这是纸这件道具的质感，不是把整片做成剪纸风。
    - 元素弹出与错落淡入：物件按旁白次序一个个出现（先主体、再道具、最后数字装置），每个都落在台面上、有它自己的接触阴影，不要整屏一起淡入。
21. 效果和旁白对齐：说到「扣好」卡扣才合上，数字出现的那一帧数字装置才亮。不要先炫技再配词。
22. 效果只在画面内部发生：底色、台面、光位、字幕位置不动；不要为了塞效果把主体缩小、把字幕挪走或加第二条字幕。
23. 配乐用《We Never》，开 `beat_sync`，镜头切点尽量落在拍上；曲库没有这首就换同气质低频氛围乐。
24. 每个数字镜把关键数字提成一个可调字段（`fields`），写清合理范围；演示时当场改这个值，画面跟着变，不必改代码。
25. 这一版不做、也不要写进分镜：真实地图与地理数据（没有外部服务）、透明底输出（成片是 H.264 整帧）、AI 生成图片与照片。所有画面都自己画，不用外部素材；需要地图感时画一张示意地图（纯代码的色块、河道、路线，摆在台面上当一件道具）。
26. 字幕要短：每一镜的文案控制在 12–24 个汉字（最多不超过 30 个），一句话读完；长句拆到下一镜去说，不要靠压小字号或堆到三行硬塞进一帧。英文那行同样短些，别把中文整句逐字翻。
"""


def build_prompt(entries: list[dict], episode: int, style: str,
                 angles: dict[str, str] | None = None,
                 showcase: bool = False) -> str:
    chapters = []
    for e in entries:
        if e["chapter"] not in chapters:
            chapters.append(e["chapter"])
    topic = "、".join(re.sub(r"^\d+\.\s*", "", c) for c in chapters)
    n = len(entries)
    total_sec = min(300, max(180, round(30 * n)))
    per_sec = round(total_sec / n)
    per_chars = max(70, round((total_sec * 4.2 - 40) / n))
    angles = angles or {}
    lines = [
        f"做一条横屏短视频。《高性价比人生指南》第 {episode:03d} 期，主题：{topic}。",
        "",
        f"这一期只讲下面 {n} 条，按书里的顺序讲，不增不减：",
        "",
    ]
    for i, e in enumerate(entries, 1):
        lines += [
            f"【第 {i} 条】{e['title']}",
            f"成本：{e['cost']}",
            f"说人话：{e['plain']}",
            f"收益（数字只能从这里取）：{e['gain']}",
            f"证据等级：{e['level']}",
            f"备注：{e['note']}",
        ]
        if e["id"] in angles:
            lines.append(f"参考角度（可换）：{angles[e['id']]}")
        lines.append("")
    lines += [
        "旁白要求：",
        f"1. 按顺序讲这 {n} 条，每条 {per_chars - 18}–{per_chars + 18} 字：故事 → 概念 → 动作 → 数字。总旁白约 {int(per_chars * n * 0.85)}–{int(per_chars * n * 1.05)} 字（含开场和结尾）。",
        f"2. 开场一句本期论点（15–25 字），把这 {n} 条串成一个共同逻辑；结尾一句回扣，不喊口号、不说教。",
        "3. 数字只能来自上面的「收益」或「说人话」，不改写、不四舍五入、不补书里没有的数字；讲清口径（哪个国家、哪一年、什么人群），不要只说「大幅」「很多」；证据等级 A/B/C 用一行小字写在右上角期号下面（右上角只放这两行，画面别处不要重复）。",
        "4. 口语，像跟朋友说话；不用「震惊」「必看」「你一定要知道」这类词；不吓唬人，不做诊断。",
        "5. 旁白说完一句再换下一句，字幕跟旁白同步；不要一句话没说完就切镜。成片是中英双字幕：每一镜除了中文文案，还要写一行英文 `text_en`，与中文逐句对应、语气一致、意思到位即可，不逐字硬译。英文那行要短——不超过 60 个字符（约 10 个英文词），两行同号，写长了会折成两行、把字幕堆成三行。",
        "",
        "内容深度（每条都要有故事、概念、数字，缺一不可）：",
        "6. 每条先讲一个具体的故事、实验或典故：谁、什么时候、发生了什么，2–3 句，30–50 字。不要只报概念名，也不要用「有研究表明」糊过去。",
        "7. 故事之后给出一个真实存在的概念，解释「人为什么忽略它」或「它为什么有效」；概念一句话说清，紧接着落到具体动作。可用方向：",
        "   心理学：正常化偏误、现状偏误、损失厌恶、双曲贴现、风险补偿、幸存者偏差、可得性偏差、控制错觉、沉没成本；",
        "   经济学：边际效用、激励相容、路径依赖、预防原则、公地悲剧、外部性；",
        "   哲学：休谟的归纳问题、塞内卡的「我们受苦多于必要」、塔勒布的「反脆弱」与「沉默证据」、奥卡姆剃刀；",
        "   中国典故：扁鹊见蔡桓公、曲突徙薪、亡羊补牢、讳疾忌医。",
        "8. 每条至少一个数字，数字只能来自上面的「收益」或「说人话」，讲清口径，不要换算、不要四舍五入。",
        "9. 不许编造人名、年份、地点、名言、实验或数据；记不准就用书里已有的案例和数字讲，不要补细节。概念只做解释，不替代证据等级。",
        # 这一条是给「查证」踩刹车的：书稿里的数字本来就是核对过的，模型却会为每个数
        # 去网上找出处链接；真机 run `b8ace0b2ffbc`（2026-10-21）为一个 USFA 的百分比
        # 连打 15 次 web_search，一句提示词都没换，最后被原地打转守卫停成 failed、
        # 白烧 ¥1.06。出处写机构名 + 年份就够交差，不必每条都有可点链接。
        "数字与出处：上面「收益」「说人话」里的数字、年份、人群口径都已经核对过，直接引用；不要为了补链接去联网检索出处，出处栏写材料里的机构名和年份（例如「USFA，2018–2020」）即可。同一个说法搜一次没有命中，就按材料照写，不要换措辞反复搜。",
        "",
        "画面要求：",
        f"10. 横屏 1920×1080，30fps，总长 {total_sec - 10}–{total_sec + 15} 秒；每条约 {per_sec - 2}–{per_sec + 3} 秒，每条 3–5 镜，每镜 5–8 秒。",
        "11. 每一帧都必须有一个和当前旁白关键词对应的实体物件，而且它是画面主体；先列出这一句里的实体名词，再画那个物件。没有实体物件的句子不单独成镜，合并到前后镜头里。不允许只有渐变、色块、线条或文字的空镜，不允许用抽象图形代替物件。讲概念时，把概念落到一个具体物件上（例如「预防原则」落到一盏一直没响过的报警器），不要用问号、大脑图标或抽象符号代替。",
        f"12. 整片选风格包「暗房微距」，不套纸片壳，画面一直画到四边（`bleed: true`，底色 `#07080a`，`texture: film`，`caption_style: outlined`，`highlight_style: plain`）；期号与主题作为一行小字直接压在画面右上角（「第 {episode:03d} 期 · {topic}」），全片位置不变，字高约画面高 1.6%，不要做成标题卡。每一镜要自己画完整的**场景**：远景是被压暗的墙或门洞，中景是台面与它周围的器物，近景是这一镜的主角实物；不要只画一个物件漂在空底上。",
        "13. 需要人时只出现手、手臂或身体局部，不画脸、不画全身人物。",
        "",
        "画面风格：",
        style.strip(),
        "",
        "片头片尾：",
        f"14. 片头不单独做标题卡：第一镜直接是第一个物件，画面右上角一行小字「第 {episode:03d} 期 · {topic}」，全片都在。",
        "15. 片尾最后一镜停 3 秒，画面里放一行小字「内容整理自《高性价比人生指南》，不替代医生、律师或专业人士」。",
        "",
        "音乐：",
        "16. 背景音乐用《We Never》，循环铺底，音量明显低于旁白，不抢人声；如果素材库里没有这首，就换同气质的低频氛围乐，不要强鼓点。",
        "",
    ]
    if showcase:
        lines += SHOWCASE_RULES.strip().splitlines() + [""]
    return "\n".join(lines)


def cmd_fetch(args: argparse.Namespace) -> None:
    fetch_book(force=args.force)


def cmd_stats(_: argparse.Namespace) -> None:
    entries = parse_book()
    print(f"条目：{len(entries)}")
    print(f"章节：{len({e['chapter'] for e in entries})}")
    for level in sorted({e["level"] for e in entries}):
        print(f"  证据等级 {level}：{sum(1 for e in entries if e['level'] == level)}")
    print("\n前 5 条：")
    for e in entries[:5]:
        print(f"  {e['id']} [{e['chapter']}] {e['title']}（{e['level']}）")


def cmd_prompt(args: argparse.Namespace) -> None:
    entries = parse_book()
    cursor = load_cursor() if not args.from_id else {"next": 0, "episode": 0}
    start = int(args.from_id.lstrip("0") or 0) - 1 if args.from_id else cursor["next"]
    if start >= len(entries):
        raise SystemExit(f"已经讲完：全书 {len(entries)} 条")
    batch = entries[start:start + args.count]
    episode = args.episode or cursor.get("episode", 0) + 1
    style = Path(args.style).read_text(encoding="utf-8") if args.style else DEFAULT_STYLE
    angles = {}
    if args.angles:
        angles = json.loads(Path(args.angles).read_text(encoding="utf-8"))
    elif ANGLES.exists():
        angles = json.loads(ANGLES.read_text(encoding="utf-8"))
    prompt = build_prompt(batch, episode, style, angles,
                          showcase=bool(getattr(args, "showcase", False)))

    day = args.date or dt.date.today().isoformat()
    PROMPTS.mkdir(parents=True, exist_ok=True)
    suffix = "-演示版" if getattr(args, "showcase", False) else ""
    out = PROMPTS / f"{day}-第{episode:03d}期{suffix}.md"
    out.write_text(prompt, encoding="utf-8")
    print(f"提示词：{out}")
    print(f"条目：{batch[0]['id']}–{batch[-1]['id']}（{len(batch)} 条）")
    if args.advance:
        cursor.update({
            "next": start + len(batch),
            "episode": episode,
            "last_date": day,
            "last_entries": [e["id"] for e in batch],
        })
        save_cursor(cursor)
        print(f"游标已推进到第 {cursor['next'] + 1} 条")
    if args.print:
        print("\n" + prompt)


DEFAULT_STYLE = """\
夜里的微距静物摄影（暗房微距）：底色近黑（#07080a），台面是深色木头或深石，
背后是一块被压暗的旧墙。整片只用一个光位——一束从后上方来的暖光（#ffc994），
再配一圈冷色边光（#5f92ff）；明暗比约 4:1，最亮的地方只留给光源和金属高光。
环境与边缘是冷的，暖色只落在受光面上。

每一镜只放一件真实的主角实物：瓷碗、玻璃瓶、金属器械、木头、一撮盐、一片药、
一截软管。它有厚度、有圆角、有材质——玻璃透、金属有反射、陶瓷半哑、木有纹理、
颗粒物有堆积感；粗糙度要有变化，不要一整套塑料高光。主角占画面宽 35–55%、
高 35–55%，落在画面中部偏一侧，另一侧留出虚化的背景；台面在画面下方约 0.55H
处，主体落地的位置压一片接触阴影——差这一片，东西就像浮着。不要平涂图标，
不要只画一个物件漂在空底上，也不要把主体缩成 10% 宽的小图标。

背景与前景掠过的物件是虚的，主角锐利；靠雾、暗角、细颗粒和暖冷分离把主体
从背景里剥出来。整片同一张台面、同一束光、同一圈冷边光；每一镜只换主角与它
被照到的方式，不换光位、不换台面。暗部要能看出层，不要糊成一个黑块；不要过曝
的死白高光。

讲数字时不要画两根并排的灰柱子：做一件有材质、有投影的实体装置（刻度盘、
计数轮、铜牌、玻璃量筒里的液面、机械计数器），数字是实物上的一部分，
字高不小于画面高的 8%。

横屏构图：画面**铺满整帧**（`bleed: true`，底色 `#07080a`），墙、台面、光一直画到
四边，不要镶在一条居中的带子里。主体落在画面中部偏一侧，另一侧留出场景纵深；
最下面那 0.26H 留给台面和投影，字幕压在这里，别把主体或要读的字放进去。不要因为
横屏把物件拉扁。运镜只有缓慢推近、横移或轻绕（一镜一个动作，中间不切），
不要甩镜、不要手持晃动。

字幕**压在画面上**（不是画面外面的一条黑带）：贴画面底部，白色字、黑色描边，
字号适中；**英文一行在上、中文一行在下，两行一样大**，整条字同一个白色，
不要关键词换色、不要逐词点亮、不要底色块。文案里只写这句话本身——不要「」『』⌈⌉⌊⌋
这类装饰括号，也不要引号包住整句；**句尾不写句号**，一句话里有两句时中间用两个空格
隔开（不画那个圈）。不要字幕条、不要第二条字幕。

颗粒是整片质感的一部分（细颗粒、轻暗角、极淡的暖雾），由风格统一加一次；
镜头代码里不要再叠整屏的 `<Grain>`；非要加，不透明度不超过 0.12，叠层用
overlay——一层中灰压上去会把整帧的对比度洗掉。

这一版要的是写实：宁可少一件东西，也不要把画面做成色块拼贴、PPT 卡片或平涂
插画。需要示意时（地图、流程、对比），把示意做成台面上的一件真实道具。
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch", help="下载/更新书稿")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_fetch)
    p = sub.add_parser("stats", help="条目统计")
    p.set_defaults(func=cmd_stats)
    p = sub.add_parser("prompt", help="生成某一期的提示词")
    p.add_argument("--count", type=int, default=10, help="每期条数，默认 10（约 4 分半）")
    p.add_argument("--date", help="日期，默认今天")
    p.add_argument("--episode", type=int, help="期号，默认游标 +1")
    p.add_argument("--from-id", help="从某个条目 id 开始，例如 031")
    p.add_argument("--style", help="风格说明文件，默认内置")
    p.add_argument("--angles", help="逐条思路文件（JSON：条目 id → 一句话角度）")
    p.add_argument("--advance", action="store_true", help="成功后推进游标")
    p.add_argument("--showcase", action="store_true",
                   help="演示版：加一段效果编排（转场/动效/三维/线描/粒子）")
    p.add_argument("--print", action="store_true", help="同时打印提示词")
    p.set_defaults(func=cmd_prompt)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
