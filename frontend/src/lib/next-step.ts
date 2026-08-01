/**
 * 「下一步该做什么」的派生：项目状态 → 一条可执行建议。
 *
 * 存在的理由：整条流水线由与智能体的对话驱动，而界面此前只显示「在第几步」（顶部阶段条），
 * 不显示「这一步该做什么」。用户要么自己猜措辞，要么去问人——本函数把这份知识从人脑挪进
 * 产品，产出一条带现成指令的建议，用户点一下即可填进输入框。
 *
 * 判定顺序与 `StatusCalculator.calculate_current_phase` 同构：按实际产物倒序判定，先看
 * 最靠后的产物是否缺失。阶段本身不足以决定动作——同一阶段下，参考直出没有分镜这一步，
 * 广告模式的起点是产品信息而非源文件，故还要读 `generation_mode` / `content_mode`。
 * 猜错的代价是把用户指向一个不存在的步骤（正是本函数要消灭的体验），故信息不足时
 * 一律返回 null，不给建议好过给错建议。
 */

import type { ProjectData } from "@/types";

/** 建议的动作 id；同时是 i18n key 的后缀（`next_step_<id>_label` / `next_step_<id>_prompt`）。 */
export type NextStepId =
  | "upload_source"
  | "fill_product"
  | "write_song"
  | "generate_script"
  | "complete_assets"
  | "generate_storyboards"
  | "generate_videos"
  | "export";

export interface NextStep {
  id: NextStepId;
  /**
   * 该建议是否需要用户离开对话去界面上操作。为 true 时只展示说明、不投递指令——
   * 上传源文件、填写产品信息都是表单动作，智能体代劳不了，给一条指令反而误导。
   */
  manual: boolean;
}

/** 资产三类是否还有未完成的设计图。总数为 0 表示剧本没声明该类资产，不算缺口。 */
function hasPendingAssets(project: ProjectData): boolean {
  const status = project.status;
  if (!status) return false;
  return [status.characters, status.scenes, status.props].some(
    (c) => c && c.total > 0 && c.completed < c.total,
  );
}

/** 剧集级 generation_mode 覆盖项目级；两处都缺时按 storyboard（后端同口径的默认值）。 */
function effectiveGenerationMode(project: ProjectData): string {
  const episodeMode = project.episodes?.find((e) => e.generation_mode)?.generation_mode;
  return episodeMode ?? project.generation_mode ?? "storyboard";
}

function hasPendingStoryboards(project: ProjectData): boolean {
  return (project.episodes ?? []).some(
    (e) => e.storyboards && e.storyboards.total > 0 && e.storyboards.completed < e.storyboards.total,
  );
}

function hasPendingVideos(project: ProjectData): boolean {
  return (project.episodes ?? []).some(
    (e) => e.videos && e.videos.total > 0 && e.videos.completed < e.videos.total,
  );
}

export function deriveNextStep(project: ProjectData | null | undefined): NextStep | null {
  if (!project?.status) return null;

  const phase = project.status.current_phase;

  if (phase === "completed") return { id: "export", manual: true };

  // 起点因内容模式而异：广告项目不导入源文件，它的起点是产品信息与创作 brief（筹备页表单）；
  // MV 的起点是写歌——song 与 lyrics 存在剧本顶层，而镜头表要按实测曲长来排，没有歌就排不了镜头。
  if (phase === "setup") {
    if (project.content_mode === "ad") return { id: "fill_product", manual: true };
    if (project.content_mode === "mv") return { id: "write_song", manual: true };
    return { id: "upload_source", manual: true };
  }

  if (phase === "worldbuilding") return { id: "generate_script", manual: false };

  // scripting / production 共用同一套产物判定：资产图 → 分镜图 → 视频，缺哪补哪。
  // 顺序不能反——资产图没齐就生成视频，每个镜头里的人和场景都会长得不一样。
  if (hasPendingAssets(project)) return { id: "complete_assets", manual: false };

  // 参考直出跳过分镜（资产图直接驱动视频），此时建议生成分镜等于把用户指向不存在的步骤。
  if (effectiveGenerationMode(project) === "storyboard" && hasPendingStoryboards(project)) {
    return { id: "generate_storyboards", manual: false };
  }

  if (hasPendingVideos(project)) return { id: "generate_videos", manual: false };

  // 阶段未到 completed 但三类产物都没有缺口：产物尚未登记（如剧本刚生成、镜头还没建），
  // 此时给不出可靠建议。宁可不显示，也不猜一个可能错的下一步。
  return null;
}
