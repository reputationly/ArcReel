import { describe, expect, it } from "vitest";
import { deriveNextStep } from "./next-step";
import type { ProjectData } from "@/types";

function project(overrides: Partial<ProjectData> = {}): ProjectData {
  return {
    title: "测试项目",
    content_mode: "narration",
    style: "",
    episodes: [],
    characters: {},
    status: {
      current_phase: "scripting",
      phase_progress: 0,
      characters: { total: 0, completed: 0 },
      scenes: { total: 0, completed: 0 },
      props: { total: 0, completed: 0 },
      episodes_summary: { total: 0, scripted: 0, in_production: 0, completed: 0 },
    },
    ...overrides,
  } as ProjectData;
}

describe("deriveNextStep", () => {
  it("returns null without status so the UI shows nothing rather than a guess", () => {
    expect(deriveNextStep(null)).toBeNull();
    expect(deriveNextStep(project({ status: undefined }))).toBeNull();
  });

  it("points a fresh narration project at the source file", () => {
    const step = deriveNextStep(
      project({ status: { ...project().status!, current_phase: "setup" } }),
    );
    expect(step).toEqual({ id: "upload_source", manual: true });
  });

  it("points a fresh ad project at the product form instead", () => {
    // 广告项目不导入源文件——指向源文件等于把用户送去一个它根本不用的入口。
    const step = deriveNextStep(
      project({
        content_mode: "ad",
        status: { ...project().status!, current_phase: "setup" },
      }),
    );
    expect(step).toEqual({ id: "fill_product", manual: true });
  });

  it("suggests generating the script during worldbuilding", () => {
    const step = deriveNextStep(
      project({ status: { ...project().status!, current_phase: "worldbuilding" } }),
    );
    expect(step).toEqual({ id: "generate_script", manual: false });
  });

  it("puts asset sheets before anything downstream", () => {
    // 资产图没齐就往下走，每个镜头里的人与场景都会长得不一样。
    const step = deriveNextStep(
      project({
        status: {
          ...project().status!,
          characters: { total: 3, completed: 1 },
        },
        episodes: [{ episode: 1, title: "E1", script_file: "e1.json", videos: { total: 4, completed: 0 } }],
      } as Partial<ProjectData>),
    );
    expect(step).toEqual({ id: "complete_assets", manual: false });
  });

  it("suggests storyboards when the storyboard path still has gaps", () => {
    const step = deriveNextStep(
      project({
        generation_mode: "storyboard",
        episodes: [
          {
            episode: 1,
            title: "E1",
            script_file: "e1.json",
            storyboards: { total: 6, completed: 2 },
            videos: { total: 6, completed: 0 },
          },
        ],
      } as Partial<ProjectData>),
    );
    expect(step).toEqual({ id: "generate_storyboards", manual: false });
  });

  it("skips storyboards entirely on the reference-video path", () => {
    // 参考直出用资产图直接驱动视频，没有分镜这一步；建议生成分镜就是指向不存在的步骤。
    const step = deriveNextStep(
      project({
        generation_mode: "reference_video",
        episodes: [
          {
            episode: 1,
            title: "E1",
            script_file: "e1.json",
            storyboards: { total: 6, completed: 0 },
            videos: { total: 6, completed: 0 },
          },
        ],
      } as Partial<ProjectData>),
    );
    expect(step).toEqual({ id: "generate_videos", manual: false });
  });

  it("lets an episode-level generation_mode override the project default", () => {
    const step = deriveNextStep(
      project({
        generation_mode: "storyboard",
        episodes: [
          {
            episode: 1,
            title: "E1",
            script_file: "e1.json",
            generation_mode: "reference_video",
            storyboards: { total: 6, completed: 0 },
            videos: { total: 6, completed: 0 },
          },
        ],
      } as Partial<ProjectData>),
    );
    expect(step).toEqual({ id: "generate_videos", manual: false });
  });

  it("suggests export once everything is complete", () => {
    const step = deriveNextStep(
      project({ status: { ...project().status!, current_phase: "completed" } }),
    );
    expect(step).toEqual({ id: "export", manual: true });
  });

  it("returns null when nothing is pending but the project is not complete", () => {
    // 产物尚未登记（剧本刚出、镜头还没建）时给不出可靠建议，不显示好过猜错。
    expect(deriveNextStep(project())).toBeNull();
  });
});
