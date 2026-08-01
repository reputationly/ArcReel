import { describe, expect, it } from "vitest";
import zh from "@/i18n/zh/dashboard";
import {
  contentModeDescKey,
  hasSourceReview,
  contentModeLabelKey,
  contentModeTag,
  disabledGenerationModes,
  hasDefaultDuration,
  isSingleEpisodeMode,
  type ContentMode,
} from "./content-mode";
import {
  charactersFieldFor,
  getScriptItemId,
  shotTextFieldFor,
  type EditorContentMode,
} from "./script-shape";

const MODES: ContentMode[] = ["narration", "drama", "ad", "mv"];

/**
 * 与后端 `tests/test_mode_dispatch_exhaustiveness.py` 对称：断言每张按 content_mode 键控的
 * 表都覆盖到全部取值。漏掉一个模式不会有 typecheck 错误（这些函数都接受 string），
 * 表现是界面上一部分按新模式渲染、另一部分还按旧模式，而全套测试照样绿。
 */
describe("content-mode dispatch coverage", () => {
  it("gives every mode a distinct label key and tag", () => {
    const keys = MODES.map(contentModeLabelKey);
    const tags = MODES.map(contentModeTag);
    expect(new Set(keys).size).toBe(MODES.length);
    expect(new Set(tags).size).toBe(MODES.length);
  });

  it("gives every mode its own description key, resolved to a translation", () => {
    // 说明文案曾靠三元链拼、mv 落进 else 分支，把 MV 解释成广告短片——第一步就把人带偏
    const keys = MODES.map(contentModeDescKey);
    expect(new Set(keys).size).toBe(MODES.length);
    for (const key of keys) {
      expect(zh[key as keyof typeof zh]).toBeTruthy();
    }
  });

  it("resolves every label key to an actual translation", () => {
    // 模式标签曾靠三元链拼，新模式落进 else 分支显示成别的模式名——这里逐个核对到 i18n
    for (const mode of MODES) {
      expect(zh[contentModeLabelKey(mode) as keyof typeof zh]).toBeTruthy();
    }
  });

  it("keeps single-episode separate from ad-specific", () => {
    // 两个概念曾合成一个 isAd：给 mv 补上单集语义时，顺手把广告专属的产品/brief 初始化页
    // 和产品资产入口也开给了 MV。谓词分开，消费方才能各取所需。
    expect(isSingleEpisodeMode("mv")).toBe(true);
    expect(isSingleEpisodeMode("ad")).toBe(true);
    // 「是否广告片」不由本模块提供——它是字面量比较，刻意不抽象，避免再被误用成「恒单集」
    expect(MODES.filter(isSingleEpisodeMode)).not.toEqual(["ad"]);
  });

  it("agrees with the backend on which modes have no source review", () => {
    // 与后端 lib/episode_paths.py::NO_STEP1_CONTENT_MODES 同表：判错的表现是新建的
    // ad / MV 项目一进工作台就停在一张永远为空的源文审阅页上
    expect(MODES.filter((m) => !hasSourceReview(m))).toEqual(["ad", "mv"]);
  });

  it("agrees with the backend on which modes are single-episode", () => {
    // 后端 ProjectManager.SINGLE_EPISODE_MODES = {"ad", "mv"}
    expect(MODES.filter(isSingleEpisodeMode)).toEqual(["ad", "mv"]);
  });

  it("agrees with the backend on which modes hold a per-shot duration preference", () => {
    // 后端 ProjectManager.NO_DEFAULT_DURATION_MODES = {"ad", "mv"}
    expect(MODES.filter((m) => !hasDefaultDuration(m))).toEqual(["ad", "mv"]);
  });

  it("agrees with the backend on unsupported generation modes", () => {
    // 后端 ProjectManager.UNSUPPORTED_GENERATION_MODES；前端灰掉、后端放行（或反过来）
    // 会让同一个约束在不同入口给出不同答案
    expect(disabledGenerationModes("narration")).toBeUndefined();
    expect(disabledGenerationModes("drama")).toBeUndefined();
    expect(disabledGenerationModes("ad")).toEqual(["grid"]);
    expect(disabledGenerationModes("mv")).toEqual(["grid", "reference_video"]);
  });

  it("keeps every editor mode wired through the script-shape helpers", () => {
    const editorModes: EditorContentMode[] = ["narration", "drama", "ad", "mv"];
    for (const mode of editorModes) {
      expect(charactersFieldFor(mode)).toBeTruthy();
      // id 字段：漏掉的模式会拿到 undefined，列表选中与保存定位一起失效
      expect(getScriptItemId({ shot_id: "E1S01", scene_id: "E1S01", segment_id: "E1S01" } as never, mode)).toBe(
        "E1S01",
      );
    }
  });

  it("names the shot-level text field for exactly the shot-skeleton modes", () => {
    // ad 的口播、mv 的歌词行；narration 的 novel_text 由切片阶段产出、不在详情里编辑
    expect(shotTextFieldFor("ad")).toBe("voiceover_text");
    expect(shotTextFieldFor("mv")).toBe("lyrics_line");
    expect(shotTextFieldFor("narration")).toBeNull();
    expect(shotTextFieldFor("drama")).toBeNull();
  });
});
