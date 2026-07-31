import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import type { ProjectData } from "@/types";
import { useProjectsStore } from "@/stores/projects-store";
import { useAssistantStore } from "@/stores/assistant-store";
import { NextStepCard } from "./NextStepCard";

function projectData(overrides: Partial<ProjectData> = {}): ProjectData {
  return {
    title: "测试项目",
    content_mode: "narration",
    style: "",
    episodes: [],
    characters: {},
    status: {
      current_phase: "worldbuilding",
      phase_progress: 0,
      characters: { total: 0, completed: 0 },
      scenes: { total: 0, completed: 0 },
      props: { total: 0, completed: 0 },
      episodes_summary: { total: 0, scripted: 0, in_production: 0, completed: 0 },
    },
    ...overrides,
  } as ProjectData;
}

describe("NextStepCard", () => {
  beforeEach(() => {
    useProjectsStore.setState({ currentProjectData: null });
    useAssistantStore.setState({ input: "" });
  });

  it("renders nothing when no project is loaded", () => {
    const { container } = render(<NextStepCard />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when no reliable step can be derived", () => {
    // 指向一个不存在的步骤比不给建议更糟，故派生不出时整块不渲染。
    useProjectsStore.setState({
      currentProjectData: projectData({
        status: { ...projectData().status!, current_phase: "scripting" },
      }),
    });
    const { container } = render(<NextStepCard />);
    expect(container).toBeEmptyDOMElement();
  });

  it("puts the prompt into the composer instead of sending it", () => {
    // 生成类动作耗时且产生真实费用，误点一次的代价远大于多按一次回车。
    useProjectsStore.setState({ currentProjectData: projectData() });
    render(<NextStepCard />);

    expect(screen.getByText("生成剧本")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /填入输入框/ }));

    expect(useAssistantStore.getState().input).toBe("生成剧本，按项目已有的素材与设定来。");
  });

  it("offers no prompt button for steps the agent cannot perform", () => {
    // 上传源文件是表单动作，给一条智能体执行不了的指令会把用户带进死胡同。
    useProjectsStore.setState({
      currentProjectData: projectData({
        status: { ...projectData().status!, current_phase: "setup" },
      }),
    });
    render(<NextStepCard />);

    expect(screen.getByText("导入小说原文")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("points an ad project at the product form rather than a source file", () => {
    useProjectsStore.setState({
      currentProjectData: projectData({
        content_mode: "ad",
        status: { ...projectData().status!, current_phase: "setup" },
      }),
    });
    render(<NextStepCard />);

    expect(screen.getByText("填写产品信息与创作 Brief")).toBeInTheDocument();
  });
});
