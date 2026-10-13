/**
 * webfont 注册表 —— 第六根因（静默回落）的正面机制。
 *
 * 病是怎么来的：镜像里只装了 Noto CJK 那几个字族，而 demo/镜头代码里硬写着
 * `fontFamily: 'SF Pro Display'` 这类名字。浏览器找不到那个字族时**不报错**，
 * 直接回落成默认字族——代码里写的和画面上出现的不是一回事，而整条链上没有任何
 * 一处会说话。`font-family-whitelist` 只拦得住「写了白名单外的字族」，
 * 拦不住「这个字族根本没人加载」。
 *
 * 这里补的是后半句：**要么这个字族装在图里（fc-list 白名单），要么它由本文件
 * 当场加载进来，没有第三条路。**
 *
 * 加载用官方 `@remotion/fonts` 的 `loadFont`（§3.1 决定，不抄官方 skill 的源码）。
 * 它内部走 `delayRender`，所以「字体没落地就开始截图」这件事在结构上不可能发生；
 * 加载失败时那次 delayRender 永不 continue，渲染以超时收场——**响亮地失败**，
 * 而不是悄悄换一张脸。
 *
 * 片数是 101，不是 1：CJK 网络字体没有单文件形态，上游按 unicode-range 切片下发。
 * 见 `scripts/fetch_fonts.mjs` 与 §9-Q75。每一片都带自己的 unicode-range，
 * 浏览器仍然只下它真用到的那几片；这里是**注册**，不是下载。
 */
import {loadFont} from '@remotion/fonts';
import {staticFile} from 'remotion';

import {
  NOTO_SANS_SC,
  NOTO_SANS_SC_SLICES,
  NOTO_SANS_SC_WEIGHT,
} from './fonts.notosanssc.generated';

export {NOTO_SANS_SC, NOTO_SANS_SC_WEIGHT};

/**
 * 由本文件负责加载、因而镜头代码可以放心写进 `fontFamily` 的字族。
 *
 * `lint-rules.mjs` 的 `font-family-whitelist` 会**从这份常量里正则读名字**
 * （那边是纯 Node，没有打包器，读不了 import）。所以：新增一个 webfont 时，
 * 这里不加名字，镜头里写它就是红的——两份名单天然同步，不靠人记得。
 */
export const WEB_FONT_FAMILIES = ['Noto Sans SC'] as const;

for (const slice of NOTO_SANS_SC_SLICES) {
  // display:'block'：字形没到之前**不**先用回落字族顶着画。
  // 'swap' 是给网页看的（宁可先难看也不留白），我们是要一张确定的图。
  loadFont({
    family: NOTO_SANS_SC,
    url: staticFile(`fonts/${slice.file}`),
    format: 'woff2',
    weight: NOTO_SANS_SC_WEIGHT,
    display: 'block',
    unicodeRange: slice.range,
  }).catch((err: unknown) => {
    // 不去 continueRender：让这次渲染超时失败，比出一张字体不对的片子好。
    console.error(`[fonts] 切片加载失败 ${slice.file}：${String(err)}`);
  });
}

/** 便于镜头代码声明式地「我要 this 字族」并顺带把本模块拉进依赖图。 */
export const SANS = `"${NOTO_SANS_SC}", "Noto Sans CJK SC", sans-serif`;
