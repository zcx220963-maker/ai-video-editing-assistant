# -*- coding: utf-8 -*-
"""图形科普链**退役后**留在 `motion/` 里的东西：只剩"产品面还要用"的四件。

2026-10-25 删掉了一万行的生成链（`shot_writer` / `art_director` / `design_review` /
`judge_channel` / `recipe_index` / `review_loop` / `review_reconcile` / `code_patch` /
`hitmap` / `style_sections` / `simple_motion`，以及 `nodes/` 下三个节点模块）。
留下的每一个都**有活调用方**，不是"以防万一"：

    code_spec        契约与归一化 + 界面「文字库」要的 `library_props()` /
                     `RATE_CHOICES` / `palettes()` / `CAPTION_STYLES` /
                     `HIGHLIGHT_STYLES` / `norm_highlight_color()` /
                     `sample_shot_code()`；`selfcheck.check_film` 还要
                     `whole_film_gate_results` / `style_facts` / `validate_code_spec`
    code_render      执行层；`caption_preview` 与 `selfcheck.preview_shot` 要
                     `shot_input_props`
    render           与像素来源无关的母带共用件（配音、词时间轴、缓存协议、BGM 闪避）
    remotion_client  与 creation-remotion 容器之间那一层 HTTP —— **简洁线也在用**
                     （`simple/kernel.py`、`simple/film.py`、`simple/story.py`）
    selfcheck        `check_film` / `check_shot_code` / `preview_shot` 三个工具
    caption_preview  `/motion/caption-preview` 字幕样张
    components       `code_spec` 的组件清单
    beats            节拍

下一步真正要做的不是再删文件，而是**把 `code_spec` 里那 4 个产品面函数抽出来**
（`library_props` / `RATE_CHOICES` / `palettes` / `FILM_GEOMETRY` + 字幕样式常量）。
抽完之后 `code_spec`（6,978 行）只剩 `whole_film_gate_results` 一个用途，
再决定 `check_film` 这个工具还留不留——那时才能把它砍到几百行。
"""
