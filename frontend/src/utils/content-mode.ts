/**
 * 内容模式的项目级事实（与后端 `lib/project_manager.py` 的同名常量同口径）。
 *
 * 与 `script-shape.ts` 分工：那边是「剧本长什么形状」（条目数组键、ID 字段、角色字段），
 * 这边是「项目在这个模式下怎么运作」（是否分集、开放哪些生成方式、单镜时长偏好是否成立）。
 *
 * 收在一处的理由与后端相同：这些判断散落在侧栏、概览、设置页、创建向导四五个组件里，
 * 各写一次 `content_mode === "ad"` 的结果是新增模式时只改到其中几处——界面一半按单集
 * 呈现、一半还在显示「第 1 集」，而 typecheck 全绿。
 */

import type { GenerationMode } from "./generation-mode";

export type ContentMode = "narration" | "drama" | "ad" | "mv";

/** 恒单集的模式：一支短片 / 一支 MV 都是单件成品，界面隐藏「集」语义。 */
const SINGLE_EPISODE_MODES: ReadonlySet<string> = new Set<ContentMode>(["ad", "mv"]);

/** 不持有项目级单镜时长偏好的模式：ad 按目标总时长逐镜规划，mv 由歌曲段落决定。 */
const NO_DEFAULT_DURATION_MODES: ReadonlySet<string> = new Set<ContentMode>(["ad", "mv"]);

/** content_mode → 不开放的 generation_mode。mv 另禁参考直出：口型驱动要拿分镜图作人物首帧。 */
const UNSUPPORTED_GENERATION_MODES: Readonly<Record<string, readonly GenerationMode[]>> = {
  ad: ["grid"],
  mv: ["grid", "reference_video"],
};

/**
 * 该模式是否有源文预处理阶段（切片 / step1 中间稿）。与后端
 * `lib/episode_paths.py::has_step1` 同口径。
 *
 * ad 的创作输入是 brief + 产品信息、mv 的是剧本顶层的 song + lyrics，两者都不导入源文、
 * 也没有切片可审阅。判错的表现是新建项目一进工作台就停在「源文审阅」页——那一页对这两个
 * 模式永远是空的，而真正该走的下一步（填 brief / 写歌）在别处。
 */
export function hasSourceReview(mode: string | null | undefined): boolean {
  return !NO_SOURCE_REVIEW_MODES.has(mode ?? "");
}

const NO_SOURCE_REVIEW_MODES: ReadonlySet<string> = new Set<ContentMode>(["ad", "mv"]);

export function isSingleEpisodeMode(mode: string | null | undefined): boolean {
  return SINGLE_EPISODE_MODES.has(mode ?? "");
}

export function hasDefaultDuration(mode: string | null | undefined): boolean {
  return !NO_DEFAULT_DURATION_MODES.has(mode ?? "");
}

/** 该模式下应被禁用的生成方式（供 GenerationModeSelector 的 disabledModes）。 */
export function disabledGenerationModes(mode: string | null | undefined): GenerationMode[] | undefined {
  const disabled = UNSUPPORTED_GENERATION_MODES[mode ?? ""];
  return disabled ? [...disabled] : undefined;
}

/** 模式标签的 i18n key。未知模式回落 narration，与既有兜底一致。 */
export function contentModeLabelKey(mode: string | null | undefined): string {
  if (mode === "drama") return "drama_animation_mode";
  if (mode === "ad") return "ad_short_video_mode";
  if (mode === "mv") return "mv_mode";
  return "narration_visuals_mode";
}

/** 创建向导里模式说明文案的 i18n key。与 contentModeLabelKey 同表、同一处维护。 */
export function contentModeDescKey(mode: string | null | undefined): string {
  if (mode === "drama") return "content_mode_drama_desc";
  if (mode === "ad") return "content_mode_ad_desc";
  if (mode === "mv") return "content_mode_mv_desc";
  return "content_mode_narration_desc";
}

/** 顶栏的短标签（大写代号，不走 i18n——四个词在三语下形态一致）。 */
export function contentModeTag(mode: string | null | undefined): string {
  if (mode === "drama") return "DRAMA";
  if (mode === "ad") return "AD";
  if (mode === "mv") return "MV";
  return "NARRATION";
}
