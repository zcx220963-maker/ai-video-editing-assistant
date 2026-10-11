# -*- coding: utf-8 -*-
"""零素材图形科普片的 motion 包：代码分镜契约 + Remotion 逐镜渲染 + 母带共用件 + 补丁装配。

模块分工（一条链，四个出处）：`code_spec` 是契约与归一/缓存键的唯一口径，**闸⑦ 也判在这一层**
（读代码字节里的 import，烧像素之前），`code_render` 是执行层（八道闸里属于它的七道，
其中闸⑧ 只记账不设红），
`render` 是与像素来源无关的母带共用件（配音、词时间轴、缓存协议、
质感、BGM 闪避），`code_patch` 是「选区改」的装配器，`hitmap` 只管指针与指纹，
`remotion_client` 是与 creation-remotion 容器之间那一层 HTTP。
"""
