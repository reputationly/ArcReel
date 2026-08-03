import { useState, useEffect } from "react";
import {
  Package,
  History,
  Clapperboard,
  Waypoints,
  ArrowLeft,
  Loader2,
  PackageCheck,
} from "lucide-react";
import { GlassPopover } from "@/components/ui/GlassPopover";
import { PrimaryButton } from "@/components/ui/PrimaryButton";
import { useTranslation } from "react-i18next";
import type { RefObject, ReactNode } from "react";
import type { EpisodeMeta } from "@/types/project";
import { WARM_TONE } from "@/utils/severity-tone";

export type ExportScope = "current" | "full" | "jianying-draft" | "chatcut-handoff";

const DRAFT_PATH_STORAGE_KEY = "arcreel_jianying_draft_path";

interface ExportScopeDialogProps {
  open: boolean;
  onClose: () => void;
  onSelect: (scope: ExportScope) => void;
  anchorRef: RefObject<HTMLElement | null>;
  episodes?: EpisodeMeta[];
  onJianyingExport?: (episode: number, draftPath: string, jianyingVersion: string) => void;
  jianyingExporting?: boolean;
  onChatcutExport?: (episode: number) => void;
  chatcutExporting?: boolean;
}

export function ExportScopeDialog({
  open,
  onClose,
  onSelect,
  anchorRef,
  episodes = [],
  onJianyingExport,
  jianyingExporting = false,
  onChatcutExport,
  chatcutExporting = false,
}: ExportScopeDialogProps) {
  const { t } = useTranslation(["dashboard", "common"]);
  const [mode, setMode] = useState<"select" | "jianying-form" | "chatcut-form">("select");
  const [selectedEpisode, setSelectedEpisode] = useState<number>(
    episodes.length > 0 ? episodes[0].episode : 1,
  );
  const isWindows =
    typeof navigator !== "undefined" && navigator.userAgent.includes("Windows");
  const defaultDraftPath = isWindows
    ? t("dashboard:draft_path_default_windows")
    : t("dashboard:draft_path_default_mac");
  const [draftPath, setDraftPath] = useState<string>(
    () => localStorage.getItem(DRAFT_PATH_STORAGE_KEY) || defaultDraftPath,
  );
  const [jianyingVersion, setJianyingVersion] = useState("6");

  useEffect(() => {
    if (!open) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- 弹窗关闭时重置到初始选择界面，是有意的 UI 状态重置
      setMode("select");
    }
  }, [open]);

  useEffect(() => {
    if (episodes.length > 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- episodes prop 变化时同步表单默认值，受控拷贝是有意设计
      setSelectedEpisode(episodes[0].episode);
    }
  }, [episodes]);

  const handleJianyingSubmit = () => {
    if (!draftPath.trim() || !onJianyingExport) return;
    localStorage.setItem(DRAFT_PATH_STORAGE_KEY, draftPath.trim());
    onJianyingExport(selectedEpisode, draftPath.trim(), jianyingVersion);
  };

  // 交接包只需要集数，而单集模式（广告 / MV）根本没得选——弹一个只有一个选项的下拉，
  // 是在问一个只有一个答案的问题。多集才进选集页，单集直接导出。
  const handleChatcutClick = () => {
    if (!onChatcutExport) return;
    if (episodes.length > 1) {
      setMode("chatcut-form");
      return;
    }
    onChatcutExport(episodes[0]?.episode ?? 1);
  };

  return (
    <GlassPopover
      open={open}
      onClose={onClose}
      anchorRef={anchorRef}
      sideOffset={8}
      width="w-[22rem]"
    >
      {mode === "select" ? (
        <div className="px-4 pb-3 pt-3.5">
          <div className="mb-2.5 flex items-center gap-2">
            <span
              aria-hidden
              className="grid h-7 w-7 place-items-center rounded-lg"
              style={{
                background:
                  "linear-gradient(135deg, var(--color-accent-dim), oklch(0.76 0.09 295 / 0.05))",
                border: "1px solid var(--color-accent-soft)",
                color: "var(--color-accent-2)",
                boxShadow: "0 8px 18px -8px var(--color-accent-glow)",
              }}
            >
              <PackageCheck className="h-3.5 w-3.5" />
            </span>
            <div className="min-w-0">
              <div
                className="display-serif text-[14px] font-semibold tracking-tight"
                style={{ color: "var(--color-text)" }}
              >
                {t("dashboard:export_scope_title")}
              </div>
              <div
                className="num text-[10px] uppercase"
                style={{
                  color: "var(--color-text-4)",
                  letterSpacing: "1.0px",
                }}
              >
                {t("dashboard:eyebrow_export_scope")}
              </div>
            </div>
          </div>

          <div className="flex flex-col gap-2">
            <SectionLabel>{t("dashboard:export_group_archive")}</SectionLabel>
            <ScopeOption
              icon={<Package className="h-4 w-4" />}
              title={
                <span className="inline-flex items-center gap-1.5">
                  <span>{t("dashboard:current_version_only")}</span>
                  <span
                    className="num rounded-[3px] px-1.5 py-px text-[9.5px] uppercase"
                    style={{
                      letterSpacing: "0.6px",
                      color: "var(--color-accent-2)",
                      background: "var(--color-accent-dim)",
                      border: "1px solid var(--color-accent-soft)",
                    }}
                  >
                    {t("dashboard:recommended")}
                  </span>
                </span>
              }
              hint={t("dashboard:small_size_hint")}
              tone="accent"
              onClick={() => onSelect("current")}
            />
            <ScopeOption
              icon={<History className="h-4 w-4" />}
              title={t("dashboard:all_data")}
              hint={t("dashboard:full_history_hint")}
              tone="neutral"
              onClick={() => onSelect("full")}
            />
            <SectionLabel>{t("dashboard:export_group_handoff")}</SectionLabel>
            <ScopeOption
              icon={<Clapperboard className="h-4 w-4" />}
              title={t("dashboard:export_jianying_draft")}
              hint={t("dashboard:generate_jianying_zip_hint")}
              tone="warm"
              onClick={() => setMode("jianying-form")}
            />
            <ScopeOption
              icon={<Waypoints className="h-4 w-4" />}
              title={t("dashboard:export_chatcut_handoff")}
              hint={t("dashboard:chatcut_handoff_hint")}
              tone="warm"
              onClick={handleChatcutClick}
            />
          </div>
        </div>
      ) : (
        <div className="px-4 pb-4 pt-3.5">
          <div className="mb-3 flex items-center gap-2">
            <button
              type="button"
              onClick={() => setMode("select")}
              className="arc-close-btn focus-ring grid h-6 w-6 place-items-center rounded-md"
              aria-label={t("common:back")}
            >
              <ArrowLeft className="h-3.5 w-3.5" />
            </button>
            <span
              aria-hidden
              className="grid h-7 w-7 place-items-center rounded-lg"
              style={{
                background:
                  "linear-gradient(135deg, var(--color-warm-tint), var(--color-warm-tint-faint))",
                border: `1px solid ${WARM_TONE.ring}`,
                color: WARM_TONE.color,
                boxShadow: `0 8px 18px -8px ${WARM_TONE.glow}`,
              }}
            >
              {mode === "jianying-form" ? (
                <Clapperboard className="h-3.5 w-3.5" />
              ) : (
                <Waypoints className="h-3.5 w-3.5" />
              )}
            </span>
            <div
              className="display-serif text-[14px] font-semibold tracking-tight"
              style={{ color: "var(--color-text)" }}
            >
              {mode === "jianying-form"
                ? t("dashboard:export_jianying_draft")
                : t("dashboard:export_chatcut_handoff")}
            </div>
          </div>
          <div className="flex flex-col gap-3">
            {episodes.length > 1 && (
              <FormField
                htmlFor="export-episode-select"
                label={t("dashboard:select_episode")}
              >
                <select
                  id="export-episode-select"
                  value={selectedEpisode}
                  onChange={(e) => setSelectedEpisode(Number(e.target.value))}
                  className="focus-ring w-full rounded-md px-2.5 py-1.5 text-[13px] outline-none"
                  style={{
                    background: "oklch(0.16 0.010 265 / 0.6)",
                    border: "1px solid var(--color-hairline)",
                    color: "var(--color-text)",
                  }}
                >
                  {episodes.map((ep) => (
                    <option key={ep.episode} value={ep.episode}>
                      {t("dashboard:episode_with_title", {
                        episode: ep.episode,
                        title: ep.title,
                      })}
                    </option>
                  ))}
                </select>
              </FormField>
            )}

            {mode === "chatcut-form" ? (
              <PrimaryButton
                tone="warm"
                size="sm"
                onClick={() => onChatcutExport?.(selectedEpisode)}
                disabled={chatcutExporting}
                leadingIcon={
                  chatcutExporting ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : undefined
                }
              >
                {chatcutExporting
                  ? t("dashboard:exporting")
                  : t("dashboard:export_handoff")}
              </PrimaryButton>
            ) : (
              <>
            <FormField
              htmlFor="jianying-version-select"
              label={t("dashboard:jianying_version")}
            >
              <select
                id="jianying-version-select"
                value={jianyingVersion}
                onChange={(e) => setJianyingVersion(e.target.value)}
                className="focus-ring w-full rounded-md px-2.5 py-1.5 text-[13px] outline-none"
                style={{
                  background: "oklch(0.16 0.010 265 / 0.6)",
                  border: "1px solid var(--color-hairline)",
                  color: "var(--color-text)",
                }}
              >
                <option value="6">{t("dashboard:jianying_v6_plus")}</option>
                <option value="5">{t("dashboard:jianying_v5_x")}</option>
              </select>
            </FormField>

            <FormField
              htmlFor="jianying-draft-path"
              label={t("dashboard:draft_path")}
              hint={t("dashboard:draft_path_hint")}
            >
              <input
                id="jianying-draft-path"
                type="text"
                value={draftPath}
                onChange={(e) => setDraftPath(e.target.value)}
                placeholder={t("dashboard:draft_path_placeholder")}
                className="focus-ring w-full rounded-md px-2.5 py-1.5 text-[13px] outline-none"
                style={{
                  background: "oklch(0.16 0.010 265 / 0.6)",
                  border: "1px solid var(--color-hairline)",
                  color: "var(--color-text)",
                  fontFamily: "var(--font-mono)",
                }}
              />
            </FormField>

            <PrimaryButton
              tone="warm"
              size="sm"
              onClick={handleJianyingSubmit}
              disabled={!draftPath.trim() || jianyingExporting}
              leadingIcon={
                jianyingExporting ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : undefined
              }
            >
              {jianyingExporting
                ? t("dashboard:exporting")
                : t("dashboard:export_draft")}
            </PrimaryButton>
              </>
            )}
          </div>
        </div>
      )}
    </GlassPopover>
  );
}

/**
 * 选项分组的小标题。
 *
 * 四个导出项其实是两类东西：上两项是 ArcReel 自己的归档包（能再导回 ArcReel），下两项是
 * 交给外部剪辑软件的单向交付。扁平列到第四项就分不清了，这条分界把关系写明。
 *
 * 第一组之前不画横线——它紧跟标题，再加一条线只是噪音。
 */
function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <div
      className="num mt-1.5 flex items-center gap-2 text-[9.5px] uppercase first:mt-0"
      style={{ color: "var(--color-text-4)", letterSpacing: "1.0px" }}
    >
      <span className="shrink-0">{children}</span>
      <span aria-hidden className="h-px flex-1" style={{ background: "var(--color-hairline)" }} />
    </div>
  );
}

type ScopeTone = "accent" | "neutral" | "warm";

const SCOPE_PALETTE: Record<
  ScopeTone,
  { color: string; ring: string; hoverBg: string; hoverBorder: string }
> = {
  accent: {
    color: "var(--color-accent-2)",
    ring: "var(--color-accent-soft)",
    hoverBg: "var(--color-accent-dim)",
    hoverBorder: "var(--color-accent-soft)",
  },
  warm: {
    color: WARM_TONE.color,
    ring: WARM_TONE.ring,
    hoverBg: WARM_TONE.soft,
    hoverBorder: WARM_TONE.ring,
  },
  neutral: {
    color: "var(--color-text-3)",
    ring: "var(--color-hairline)",
    hoverBg: "oklch(1 0 0 / 0.04)",
    hoverBorder: "var(--color-hairline-strong)",
  },
};

function ScopeOption({
  icon,
  title,
  hint,
  tone,
  onClick,
}: {
  icon: ReactNode;
  title: ReactNode;
  hint: string;
  tone: ScopeTone;
  onClick: () => void;
}) {
  const palette = SCOPE_PALETTE[tone];

  return (
    <button
      type="button"
      onClick={onClick}
      className="focus-ring group flex items-start gap-3 rounded-lg px-3 py-2.5 text-left transition-colors"
      style={{
        border: "1px solid var(--color-hairline)",
        background: "oklch(0.20 0.011 265 / 0.4)",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.background = palette.hoverBg;
        e.currentTarget.style.borderColor = palette.hoverBorder;
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.background = "oklch(0.20 0.011 265 / 0.4)";
        e.currentTarget.style.borderColor = "var(--color-hairline)";
      }}
    >
      <span
        aria-hidden
        className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-md"
        style={{
          background: "oklch(0.16 0.010 265 / 0.6)",
          border: `1px solid ${palette.ring}`,
          color: palette.color,
        }}
      >
        {icon}
      </span>
      <div className="min-w-0 flex-1">
        <div
          className="text-[13px] font-medium leading-tight"
          style={{ color: "var(--color-text)" }}
        >
          {title}
        </div>
        <p
          className="mt-1 text-[11.5px] leading-[1.5]"
          style={{ color: "var(--color-text-4)" }}
        >
          {hint}
        </p>
      </div>
    </button>
  );
}

function FormField({
  htmlFor,
  label,
  hint,
  children,
}: {
  htmlFor: string;
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div>
      <label
        htmlFor={htmlFor}
        className="num mb-1 block text-[10px] uppercase"
        style={{
          color: "var(--color-text-4)",
          letterSpacing: "1.0px",
        }}
      >
        {label}
      </label>
      {children}
      {hint && (
        <p
          className="mt-1.5 text-[11px] leading-[1.55]"
          style={{ color: "var(--color-text-4)" }}
        >
          {hint}
        </p>
      )}
    </div>
  );
}
