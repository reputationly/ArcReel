import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { ExportScopeDialog } from "@/components/layout/ExportScopeDialog";
import type { EpisodeMeta } from "@/types/project";

const EPISODE = (episode: number, title: string): EpisodeMeta =>
  ({ episode, title }) as EpisodeMeta;

function renderDialog(props: Partial<Parameters<typeof ExportScopeDialog>[0]> = {}) {
  const onSelect = vi.fn();
  const onJianyingExport = vi.fn();
  const onChatcutExport = vi.fn();
  render(
    <ExportScopeDialog
      open
      onClose={vi.fn()}
      onSelect={onSelect}
      anchorRef={createRef<HTMLElement>()}
      episodes={[EPISODE(1, "第一集")]}
      onJianyingExport={onJianyingExport}
      onChatcutExport={onChatcutExport}
      {...props}
    />,
  );
  return { onSelect, onJianyingExport, onChatcutExport };
}

describe("ExportScopeDialog · OpenChatCut 交接包", () => {
  it("单集项目点选后直接导出，不弹只有一个选项的选集页", async () => {
    // 广告 / MV 都是单集模式，也正是交接包的主场景：此时问「导出哪一集」是在问一个
    // 只有一个答案的问题
    const { onChatcutExport } = renderDialog({ episodes: [EPISODE(1, "MV")] });

    await userEvent.click(screen.getByRole("button", { name: /OpenChatCut/ }));

    expect(onChatcutExport).toHaveBeenCalledWith(1);
  });

  it("多集项目先进选集页，选定后才导出", async () => {
    const { onChatcutExport } = renderDialog({
      episodes: [EPISODE(1, "第一集"), EPISODE(2, "第二集")],
    });

    await userEvent.click(screen.getByRole("button", { name: /OpenChatCut/ }));
    expect(onChatcutExport).not.toHaveBeenCalled();

    await userEvent.selectOptions(screen.getByLabelText("选择集数"), "2");
    await userEvent.click(screen.getByRole("button", { name: "导出工程" }));

    expect(onChatcutExport).toHaveBeenCalledWith(2);
  });

  it("交接包不要求填本地路径——那是剪映草稿才需要的", async () => {
    renderDialog({ episodes: [EPISODE(1, "一"), EPISODE(2, "二")] });

    await userEvent.click(screen.getByRole("button", { name: /OpenChatCut/ }));

    expect(screen.queryByLabelText("草稿目录路径")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("剪映版本")).not.toBeInTheDocument();
  });

  it("选集页可退回选项列表", async () => {
    renderDialog({ episodes: [EPISODE(1, "一"), EPISODE(2, "二")] });

    await userEvent.click(screen.getByRole("button", { name: /OpenChatCut/ }));
    await userEvent.click(screen.getByRole("button", { name: "返回" }));

    expect(screen.getByRole("button", { name: /剪映草稿/ })).toBeInTheDocument();
  });

  it("导出中禁用按钮，避免重复触发下载", async () => {
    renderDialog({
      episodes: [EPISODE(1, "一"), EPISODE(2, "二")],
      chatcutExporting: true,
    });

    await userEvent.click(screen.getByRole("button", { name: /OpenChatCut/ }));

    expect(screen.getByRole("button", { name: /导出中/ })).toBeDisabled();
  });

  it("剪映草稿仍走原有的二级表单，没被交接包的分支改坏", async () => {
    const { onJianyingExport } = renderDialog({ episodes: [EPISODE(1, "第一集")] });

    await userEvent.click(screen.getByRole("button", { name: /剪映草稿/ }));

    expect(screen.getByLabelText("剪映版本")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "导出草稿" }));
    expect(onJianyingExport).toHaveBeenCalled();
  });

  it("归档类选项仍经 onSelect 分发", async () => {
    const { onSelect } = renderDialog();

    await userEvent.click(screen.getByRole("button", { name: /仅当前版本/ }));

    expect(onSelect).toHaveBeenCalledWith("current");
  });
});
