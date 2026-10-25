"""算一下"几分钟的片子"在当前参数下能不能表达（不改任何东西，只算）。

回答"能不能做 3 分钟"这种问题时，先把**结构上限**算出来——
不然会给出"改个常量就行"的错误结论。
"""
import sys

sys.path.insert(0, "/app")

from storyline_server.simple import film, kernel

print(f"kernel.MAX_SECONDS = {kernel.MAX_SECONDS}")
print(f"film.MAX_SHOTS = {film.MAX_SHOTS}, MAX_SHOT_SEC = {film.MAX_SHOT_SEC}")
print(f"结构上限 = {film.MAX_SHOTS} 镜 × {film.MAX_SHOT_SEC}s = "
      f"{film.MAX_SHOTS * film.MAX_SHOT_SEC:.0f}s\n")

for total in (60, 120, 180, 240, 300):
    n = film.shot_count(total)
    secs = film.split_seconds(total, n)
    over = [s for s in secs if s > film.MAX_SHOT_SEC + 1e-6]
    print(f"{total:>4}s → {n:>2} 镜，每镜 {secs[0]:.2f}s，"
          f"合计 {sum(secs):.1f}s，超 {film.MAX_SHOT_SEC}s 的镜数 {len(over)}"
          + ("  ← 静默违约" if over else ""))
