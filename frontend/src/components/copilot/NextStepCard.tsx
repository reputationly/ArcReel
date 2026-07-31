import { useTranslation } from "react-i18next";
import { ArrowRight, Compass } from "lucide-react";
import { useProjectsStore } from "@/stores/projects-store";
import { useAssistantStore } from "@/stores/assistant-store";
import { deriveNextStep } from "@/lib/next-step";

/**
 * 智能体面板空态里的「建议下一步」卡片。
 *
 * 整条流水线由对话驱动，而空态此前只有「输入消息开始对话」——用户知道要说话，不知道该说
 * 什么，只能自己猜措辞或去问人。这张卡片把当前该做的事直接写出来，可自动化的那步还附一条
 * 现成指令。
 *
 * 指令是**填入输入框**而非直接发送：生成类动作耗时且产生真实费用，误点一次的代价远大于
 * 多按一次回车。填入后聚焦输入框（复用 assistant-store 的 input 投递通路，与分集空态
 * CTA 同一机制），用户可直接回车，也可以先改。
 *
 * 派生不出可靠建议时整块不渲染 —— 指向一个不存在的步骤比不给建议更糟。
 */
export function NextStepCard() {
  const { t } = useTranslation("dashboard");
  const project = useProjectsStore((s) => s.currentProjectData);
  const step = deriveNextStep(project);

  if (!step) return null;

  const label = t(`next_step_${step.id}_label`);
  const desc = t(`next_step_${step.id}_desc`);
  // manual 步骤（上传源文件、填产品表单）没有指令：智能体代劳不了表单操作，
  // 给一条它执行不了的指令只会把用户带进死胡同。
  const prompt = step.manual ? null : t(`next_step_${step.id}_prompt`);

  return (
    <div
      className="mt-5 w-full max-w-[280px] rounded-xl px-3.5 py-3 text-left"
      style={{
        background: "oklch(0.20 0.012 265 / 0.7)",
        border: "1px solid var(--color-hairline-soft)",
      }}
    >
      <div className="flex items-center gap-1.5">
        <Compass className="h-3 w-3" style={{ color: "var(--color-accent-2)" }} aria-hidden />
        <span
          className="font-mono text-[10px] font-bold uppercase tracking-[0.14em]"
          style={{ color: "var(--color-accent-2)" }}
        >
          {t("next_step_title")}
        </span>
      </div>

      <p className="mt-2 text-[13px] font-semibold" style={{ color: "var(--color-text)" }}>
        {label}
      </p>
      <p className="mt-1 text-[11.5px] leading-[1.55]" style={{ color: "var(--color-text-3)" }}>
        {desc}
      </p>

      {prompt && (
        <button
          type="button"
          onClick={() => useAssistantStore.getState().setInput(prompt)}
          className="focus-ring mt-2.5 inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11.5px] font-medium transition-colors hover:bg-[oklch(1_0_0_/_0.05)]"
          style={{ color: "var(--color-accent-2)" }}
        >
          {t("next_step_fill_label")}
          <ArrowRight className="h-3 w-3" aria-hidden />
        </button>
      )}
    </div>
  );
}
