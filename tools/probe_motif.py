"""验「模型会不会真的写 MOTIF_AT」——只写 3 镜、不渲染。

    docker cp tools/probe_motif.py creation-app:/app/tools_probe_motif.py
    docker exec creation-app python /app/tools_probe_motif.py [user_id]

为什么单独验：整片跑一遍 30 分钟，而"跨镜接力"能不能成立，只取决于一件事——
模型有没有在场景体里声明 `const MOTIF_AT = {…}`。没有那一行，`shared_hops` 就是空的，
外层一次都不接。这个探针把那一件事单独拉出来看。
"""
import asyncio
import sys

sys.path.insert(0, "/app")

from agent_framework.identity import use_identity
from agent_framework.storage import build_storage
from storyline_server.simple import kernel, morph, story

USER = sys.argv[1] if len(sys.argv) > 1 else "u-ebbfe7487e9a"
MOTIF = "一枚信号灯"
SCENES = [
    {"text": "长老把一枚信号灯塞进你手里。", "hi": "信号灯",
     "plate": "一只枯瘦的手把一枚发光的小灯递过来，背景是暗红的天与远处的高塔剪影"},
    {"text": "你把它举过头顶。", "hi": "举过头顶",
     "plate": "一个人站在荒原上把小小的灯举过头顶，灯是他身上唯一的光源"},
    {"text": "两百年后，那盏灯还在亮。", "hi": "还在亮",
     "plate": "宇宙里一个极小的光点，旁边是巨大的行星边缘，光点很小但很清楚"},
]


async def main() -> int:
    st = build_storage("pg_minio")
    await st.start()
    await kernel.ensure_storage()
    anchors = []
    with use_identity(USER, "probe-motif"):
        for i, sc in enumerate(SCENES):
            body = await story.write_scene_story_code(
                sc, index=i + 1, total=6, seconds=6.0, aspect="landscape",
                user_id=USER, conversation_id="probe-motif", motif=MOTIF)
            at = morph.parse_motif_at(body)
            anchors.append(at)
            head = [ln for ln in body.splitlines() if "MOTIF_AT" in ln]
            print(f"  s{i + 1:02d} 锚点 {at}  |  {head[0].strip() if head else '（没写 MOTIF_AT）'}")

    got = sum(1 for a in anchors if a)
    print(f"\n{got}/3 镜声明了锚点")
    if got >= 2:
        tl = {"cuts": [180, 360]}
        hops = morph.shared_hops(tl, anchors, 3)
        print("能接起来", len(hops), "跳：", [(h["at"], h["from"], h["to"]) for h in hops])
    else:
        print("XX 锚点太少，接不起来——提示词要再强调一次")
    return 0


raise SystemExit(asyncio.run(main()))
