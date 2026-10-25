[服务端核对：这条 run 结束了，但没有任何成片产出。零素材出片只有一条路：调用 `make_short_video`（一次调用完成分镜、逐镜画面、逐帧质检与渲染，回一条能播的链接）。有素材的剪辑走 load_media → … → plan_timeline → render_video。不要用一句「已完成」收尾。]
