#!/bin/sh
# 跑受"技能搬家 + 工具注册 + 计划卡白名单"影响的所有套件，逐行报结果。
cd /app || exit 1
for f in test_simple_boundary test_simple_film test_short_video \
         test_video_editing test_plan_gate test_plan_style_auto test_motion_channel \
         test_caption_layer test_code_transition test_layout_subject test_motif_share \
         test_shot_index test_motion_judge_channel test_theme_single_source \
         test_cost_caption_fix test_code_forage test_code_spec test_display_name_sources; do
  out=$(python "tests/$f.py" 2>&1)
  line=$(printf '%s\n' "$out" | grep -E '通过$|通过 [0-9]+/' | tail -1)
  bad=$(printf '%s\n' "$out" | grep -cE '^\s*(XX|✗|FAIL)')
  printf '%-30s %-22s bad=%s\n' "$f" "${line:-（无汇总行）}" "$bad"
done
