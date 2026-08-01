/**
 * 剧本形状分派（与后端 SCRIPT_SHAPES 同口径）：content_mode → 条目 ID / 角色引用字段。
 *
 * 时间线编辑器各组件（列表 / 分屏 / 详情 / 引用区）共用，避免三元映射在多处漂移。
 */

import type {
  AdEpisodeScript,
  AdShot,
  DramaEpisodeScript,
  DramaScene,
  EpisodeScript,
  MVEpisodeScript,
  MVShot,
  NarrationEpisodeScript,
  NarrationSegment,
} from "@/types";

export type EditorContentMode = "narration" | "drama" | "ad" | "mv";
export type ScriptItem = NarrationSegment | DramaScene | AdShot | MVShot;

export type CharactersField =
  | "characters_in_segment"
  | "characters_in_scene"
  | "characters_in_shot";

/** 取条目 ID（segment_id / scene_id / shot_id）。 */
export function getScriptItemId(item: ScriptItem, mode: EditorContentMode): string {
  if (mode === "narration") return (item as NarrationSegment).segment_id;
  // mv 与 ad 同为平铺 shots[]，id 字段同名——形状相同、字段不同（mv 另有 start_seconds 等）
  if (mode === "ad" || mode === "mv") return (item as AdShot | MVShot).shot_id;
  return (item as DramaScene).scene_id;
}

/**
 * 取剧集剧本在该模式下的条目数组（segments / shots / scenes）。
 *
 * 未注册的模式返回空数组：以其他模式的形状渲染会让保存分派到错误端点。
 */
export function getScriptItems(
  script: EpisodeScript | null | undefined,
  mode: EditorContentMode,
): ScriptItem[] {
  if (!script) return [];
  if (mode === "narration") return (script as NarrationEpisodeScript).segments ?? [];
  if (mode === "ad") return (script as AdEpisodeScript).shots ?? [];
  if (mode === "mv") return (script as MVEpisodeScript).shots ?? [];
  if (mode === "drama") return (script as DramaEpisodeScript).scenes ?? [];
  return [];
}

/** 取该模式下条目的角色引用字段名。 */
export function charactersFieldFor(mode: EditorContentMode): CharactersField {
  if (mode === "drama") return "characters_in_scene";
  if (mode === "ad" || mode === "mv") return "characters_in_shot";
  return "characters_in_segment";
}

/**
 * 取条目上的角色引用列表。
 *
 * 必须与 `charactersFieldFor` 同源：引用区读列表、存回时按 `charactersFieldFor` 拼 patch，
 * 两处字段名一旦分叉，编辑弹窗会以空列表开局并把已有角色一并存没。
 */
export function getScriptItemCharacters(item: ScriptItem, mode: EditorContentMode): string[] {
  return (item as unknown as Record<string, string[] | undefined>)[charactersFieldFor(mode)] ?? [];
}

/**
 * 该模式下「镜头级一等文案」的字段名；没有这种字段的模式返回 null。
 *
 * 与后端 `lib/script_models.py::VOICEOVER_TEXT_FIELDS` 同一张表的编辑侧投影：ad 的是口播
 * 文案、mv 的是该镜对应的歌词行。narration 的 novel_text 由切片阶段产出、不在镜头详情里
 * 编辑，故不登记——登记会让详情面板多出一个改了也不该生效的输入框。
 */
export function shotTextFieldFor(mode: EditorContentMode): "voiceover_text" | "lyrics_line" | null {
  if (mode === "ad") return "voiceover_text";
  if (mode === "mv") return "lyrics_line";
  return null;
}
