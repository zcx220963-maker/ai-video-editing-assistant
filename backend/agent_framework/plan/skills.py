"""技能预注入：执行轮起步把各步 skills_hint 声明的技能正文直接拼进上下文。

不赌模型自己点 ``load_skill``——``always=true`` 的技能已由常驻上下文给过，这里去重。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .support import clean


async def preload_skills(loader: Any, names: Sequence[str], *,
                         resident: Iterable[str] = ()) -> list[str]:
    """执行轮起步预注入：各步声明的技能正文直接拼进上下文（不赌模型点 load_skill）。

    ``always=true`` 的技能已由 ``SkillManifestContextSource`` 常驻注入，这里不重复给。
    """
    if loader is None or not names:
        return []
    skip = set(resident)
    sections: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = clean(name)
        if not key or key in seen or key in skip:
            continue
        seen.add(key)
        try:
            skill = await loader.get(key)
        except Exception:  # noqa: BLE001 - 预注入是增益，读不到就照常跑
            continue
        if skill is None or not getattr(skill, "available", True) or not skill.body:
            continue
        sections.append(f'<skill_instructions name="{skill.name}">\n{skill.body}\n'
                        f'</skill_instructions>')
    return sections
