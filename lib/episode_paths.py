"""step1 中间态文件名与 episode 剧本路径的单一真相源。

这些路径原本散落在审核 gate、状态计算、web 草稿读写层、剧本生成器与 SDK 文本工具中各自
硬编码同名字面量。审核 gate 找不到 step1 文件时按 ``no_step1`` 放行 step2（文件不存在不等于
故障），因此任一写盘侧文件名 / 目录漂移都会让 gate 静默绕过且无报错。本模块把这些映射收敛到
一处：新增走结构化两段式的 content_mode 只需在 ``STEP1_FILENAMES`` 登记结构化文件名，gate、
状态计算、web 读取与写盘自动一致。

保留既有语义差异（收敛时不许抹平）：

- 审核 gate 只认结构化 ``.json``（``STEP1_FILENAMES`` / ``step1_filename``）；
- 状态计算与 web 读取层额外兼认旧版 ``.md`` 别名（``STEP1_LEGACY_FILENAMES`` /
  ``step1_read_candidates``），令存量在制品仍被识别 / 可浏览——「是否分过段」与「格式迁移」
  是两回事。
"""

from __future__ import annotations

from pathlib import Path

#: 结构化 step1 中间态文件名（按 content_mode）。审核 gate 仅认这两类。
#: 新增走结构化两段式的 content_mode 在此登记一处即可让 gate / 状态计算 / web 读取 / 写盘一致。
STEP1_FILENAMES: dict[str, str] = {
    "drama": "step1_normalized_script.json",
    "narration": "step1_segments.json",
}

#: 旧版非结构化 step1 别名（按 content_mode）。仅供状态计算 / web 读取层兼认存量在制品，
#: 审核 gate 与写盘侧不认。新增 content_mode 无历史遗留，无需登记于此。
STEP1_LEGACY_FILENAMES: dict[str, tuple[str, ...]] = {
    "drama": ("step1_normalized_script.md",),
    "narration": ("step1_segments.md",),
}

#: reference_video 的结构化 step1 中间态文件名。reference_video 是 generation_mode 维度、
#: 跨 content_mode（narration / drama 均可），不进按 content_mode 键控的 ``STEP1_FILENAMES``；
#: 审核 gate 按 step1 变体单独纳入本文件名（见 ``lib.script_review.step1_kind``）。
REFERENCE_VIDEO_STEP1_FILENAME = "step1_reference_units.json"

#: reference_video 旧版自由文本 step1 别名。仅供读取 / 浏览层兼认存量在制品；
#: 写盘与生成侧不认——仅存在旧 ``.md`` 时生成侧给出重跑拆分的明确提示。
REFERENCE_VIDEO_STEP1_LEGACY_FILENAME = "step1_reference_units.md"

#: 一键生成、无 step1 中间态的 content_mode。创作输入不在 ``drafts/``：ad 取 project.json 的
#: brief + 产品信息，mv 取剧本顶层的 song + lyrics（歌曲先于剧本存在）。
#:
#: 单列一张表而非让各消费方各写一次 ``content_mode == "ad"``：这个事实至少有四个消费方
#: （MCP 生成前置检查、状态计算的 step1 探测、web 的 step1 文件名映射、剧本生成的分派），
#: 名单分叉时新模式会被其中若干处按「该有 step1」对待，报出的却是「未找到 Step 1 文件」——
#: 错误信息指向的是缺文件，真正原因是这个模式压根不该有那个文件，排查方向被带偏。
NO_STEP1_CONTENT_MODES: frozenset[str] = frozenset({"ad", "mv"})

if NO_STEP1_CONTENT_MODES & STEP1_FILENAMES.keys():
    # import 期自洽：同一个模式不可能既登记了结构化 step1 文件名、又声明无 step1。
    raise RuntimeError(
        f"NO_STEP1_CONTENT_MODES 与 STEP1_FILENAMES 重叠: {NO_STEP1_CONTENT_MODES & STEP1_FILENAMES.keys()}"
    )


def has_step1(content_mode: str | None) -> bool:
    """该 content_mode 是否存在 step1 中间态（未知 / 缺失按「有」处理，与既有兜底一致）。"""
    return content_mode not in NO_STEP1_CONTENT_MODES


def step1_filename(content_mode: str) -> str | None:
    """该 content_mode 的结构化 step1 文件名；不走结构化 step1（如 ad / mv）时返回 None。"""
    return STEP1_FILENAMES.get(content_mode)


def step1_read_candidates(content_mode: str) -> tuple[str, ...]:
    """结构化 step1 文件名 + 旧版 ``.md`` 别名（读取 / 浏览侧候选，主文件缺失时回落探测）。

    不走结构化 step1 的模式返回空元组。审核 gate 不用此函数（只认结构化 ``.json``）。
    """
    primary = STEP1_FILENAMES.get(content_mode)
    if primary is None:
        return ()
    return (primary, *STEP1_LEGACY_FILENAMES.get(content_mode, ()))


def episode_drafts_dir(project_path: Path, episode: int) -> Path:
    """该集 step1 草稿目录 ``{project}/drafts/episode_N``。"""
    return project_path / "drafts" / f"episode_{episode}"


def episode_script_filename(episode: int) -> str:
    """该集剧本文件名 ``episode_N.json``（不含 ``scripts/`` 目录前缀）。"""
    return f"episode_{episode}.json"


def episode_script_relpath(episode: int) -> str:
    """该集剧本相对项目根的默认路径 ``scripts/episode_N.json``。"""
    return f"scripts/{episode_script_filename(episode)}"
