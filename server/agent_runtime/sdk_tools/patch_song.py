"""SDK MCP tool for MV song metadata & lyrics (剧本顶层字段).

工具返回文本是 agent-facing（免 i18n）；显示名在 ``ARCREEL_MCP_TOOL_IDS`` 注册、补三语。

与 ``patch_episode_script`` 分开的原因：那个工具按分镜 id 定位、只改镜头字段，而 ``song``
与 ``lyrics`` 是剧本**顶层**字段，没有 id 可定位。两者语义也不同——镜头字段是创作内容，
歌曲元数据是排镜头的时间轴依据，改错的后果是全片错位而非单镜画面不对。
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from lib.script_editor import ScriptEditError
from server.agent_runtime.sdk_tools._context import ToolContext, tool_error, validate_script_filename

#: song 下允许写入的字段。engine 回包与人工划分各占一半：duration_seconds / audio_path
#: 来自作曲步骤，style / bpm / sections 由 agent 按曲子填。不开放整体替换——逐字段合并
#: 才能让「先回写时长、再补段落表」这种分两次写入的常规操作不互相冲掉。
_SONG_FIELDS = frozenset({"style", "duration_seconds", "bpm", "audio_path", "sections"})


def _ensure_mv_script_exists(ctx: ToolContext, script_filename: str) -> None:
    """剧本不存在时落一个最小 MV 骨架，让「先写歌」这条路走得通。

    MV 的 song / lyrics 存在**剧本顶层**，而剧本生成又要靠 song 的实测时长排镜头——
    两者互为前置。新建的 MV 项目没有剧本文件时，若这里直接报错，用户建完项目就卡死：
    写不了歌（没剧本），也生成不了剧本（没歌）。

    骨架只含结构必需的字段（空 shots + 空 song/lyrics），能过结构校验；镜头由后续的
    剧本生成填充，不在此臆造内容。项目已有剧本时原样返回，绝不覆盖既有镜头。
    """
    try:
        ctx.pm.load_script(ctx.project_name, script_filename)
        return
    except FileNotFoundError:
        pass

    project = ctx.pm.load_project(ctx.project_name)
    if project.get("content_mode") != "mv":
        # 非 MV 项目缺剧本是另一回事（走各自的生成流程），不在这里代建。
        raise ScriptEditError(f"patch_song 仅适用 MV 模式项目，当前 content_mode={project.get('content_mode')!r}")

    ctx.pm.save_script(
        ctx.project_name,
        {
            "content_mode": "mv",
            "title": project.get("title") or "",
            "shots": [],
            "song": {},
            "lyrics": "",
        },
        script_filename,
        validate=True,
    )


def patch_song_tool(ctx: ToolContext):
    @tool(
        "patch_song",
        "写入 MV 剧本的歌曲元数据与歌词（剧本顶层字段，非分镜字段）。"
        "song 传 {字段: 值} 部分更新（style/duration_seconds/bpm/audio_path/sections），"
        "逐字段合并、不覆盖未传字段；lyrics 传完整歌词字符串。"
        "duration_seconds 必须填作曲工具回报的**实测时长**而非申请值——按申请值排镜头会让全片逐渐错位。"
        "sections 是段落表 [{name, start_seconds, duration_seconds}]，它是排镜头的硬约束。"
        "歌词定稿后再生成剧本：剧本生成只读 lyrics 不写它，重新生成镜头表不会冲掉已定稿的歌词。",
        {
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "剧本文件名（纯文件名，如 episode_1.json）",
                },
                "song": {
                    "type": "object",
                    "description": "歌曲元数据部分更新：style / duration_seconds / bpm / audio_path / sections",
                },
                "lyrics": {
                    "type": "string",
                    "description": "完整歌词；分行写，与段落表对应",
                },
            },
            "required": ["script"],
        },
    )
    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            script_filename = validate_script_filename(args["script"])
            song_patch = args.get("song")
            lyrics = args.get("lyrics")

            if song_patch is None and lyrics is None:
                raise ScriptEditError("至少要传 song 或 lyrics 之一")
            if song_patch is not None and not isinstance(song_patch, dict):
                raise ScriptEditError(f"song 必须是 {{字段: 值}} 映射，收到: {type(song_patch).__name__}")
            if lyrics is not None and not isinstance(lyrics, str):
                raise ScriptEditError(f"lyrics 必须是字符串，收到: {type(lyrics).__name__}")

            if song_patch:
                unknown = sorted(set(song_patch) - _SONG_FIELDS)
                if unknown:
                    raise ScriptEditError(f"song 含未知字段: {unknown}；允许: {sorted(_SONG_FIELDS)}")

            _ensure_mv_script_exists(ctx, script_filename)

            changed: list[str] = []
            with ctx.pm.locked_script(ctx.project_name, script_filename) as script:
                if script.get("content_mode") != "mv":
                    raise ScriptEditError(
                        f"patch_song 仅适用 MV 模式剧本，当前 content_mode={script.get('content_mode')!r}"
                    )
                if song_patch:
                    # 逐字段合并而非整体替换：常规流程是先回写实测时长、再补段落表，
                    # 整体替换会让后一次写入把前一次的字段清掉。
                    song = script.get("song")
                    if not isinstance(song, dict):
                        song = {}
                    song.update(song_patch)
                    script["song"] = song
                    changed.extend(f"song.{k}" for k in sorted(song_patch))
                if lyrics is not None:
                    script["lyrics"] = lyrics
                    changed.append("lyrics")

            return {"content": [{"type": "text", "text": f"✓ 已更新: {', '.join(changed)}"}]}
        except Exception as exc:  # noqa: BLE001
            return tool_error("patch_song", exc)

    return _handler


__all__ = ["patch_song_tool"]
