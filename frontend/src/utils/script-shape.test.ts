import { describe, expect, it } from "vitest";
import type { AdShot, DramaScene, MVShot, NarrationSegment } from "@/types";
import {
  charactersFieldFor,
  getScriptItemCharacters,
  getScriptItemId,
  getScriptItems,
  type EditorContentMode,
} from "./script-shape";

const MODES: EditorContentMode[] = ["narration", "drama", "ad", "mv"];

describe("script-shape", () => {
  it("maps every editor content mode to a characters field", () => {
    // 漏一个模式的后果不是编译错误，而是运行时读到 undefined → 空列表
    for (const mode of MODES) {
      expect(charactersFieldFor(mode)).toBeTruthy();
    }
    expect(charactersFieldFor("mv")).toBe("characters_in_shot");
  });

  it("reads characters through the same field it would write back", () => {
    // 引用区读列表与存回 patch 必须同源：字段名分叉会让弹窗空列表开局，一存就清空已有角色
    const items: Record<EditorContentMode, Record<string, unknown>> = {
      narration: { segment_id: "E1S01", characters_in_segment: ["旁白者"] },
      drama: { scene_id: "E1S01", characters_in_scene: ["姜月茴"] },
      ad: { shot_id: "E1S01", characters_in_shot: ["主播"] },
      mv: { shot_id: "E1S01", characters_in_shot: ["主唱", "吉他手"] },
    };
    for (const mode of MODES) {
      const item = items[mode];
      expect(getScriptItemCharacters(item as never, mode)).toEqual(item[charactersFieldFor(mode)]);
    }
  });

  it("falls back to an empty list when the field is absent", () => {
    expect(getScriptItemCharacters({ shot_id: "E1S01" } as MVShot, "mv")).toEqual([]);
  });

  it("reads mv items and ids from the flat shots array", () => {
    const shot = { shot_id: "E1S03" } as MVShot;
    expect(getScriptItems({ shots: [shot] } as never, "mv")).toEqual([shot]);
    expect(getScriptItemId(shot, "mv")).toBe("E1S03");
  });

  it("keeps the other modes' id fields distinct", () => {
    expect(getScriptItemId({ segment_id: "S1" } as NarrationSegment, "narration")).toBe("S1");
    expect(getScriptItemId({ scene_id: "S2" } as DramaScene, "drama")).toBe("S2");
    expect(getScriptItemId({ shot_id: "S3" } as AdShot, "ad")).toBe("S3");
  });
});
