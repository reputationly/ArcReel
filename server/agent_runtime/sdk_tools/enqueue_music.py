"""SDK MCP tool for music generation (ACE-Step t2m).

工具返回文本是 agent-facing（免 i18n）；显示名在 ``ARCREEL_MCP_TOOL_IDS`` 注册、补三语。
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from lib.generation_queue_client import TaskSpec, batch_enqueue_and_wait
from lib.resource_paths import resource_relative_path
from server.agent_runtime.sdk_tools._context import ToolContext, tool_error

#: 项目级单曲的固定 resource_id。音乐不像分镜那样按条目产出——一支片子一首曲，
#: 用固定 id 让重新生成天然覆盖同一份产物，也让剧本只需引用一个稳定路径。
_MAIN_TRACK_ID = "main"


def generate_music_tool(ctx: ToolContext):
    @tool(
        "generate_music",
        "生成一首曲子（ACE-Step），入队并等待完成。"
        "prompt 是曲风描述（风格+情绪+乐器+节奏，如「舒缓的钢琴独奏，忧郁，60BPM」）。"
        "lyrics 给了就按词唱、留空则引擎自动作词（sample 模式）——MV 项目务必传定稿歌词，"
        "否则引擎会自己编一版，与剧本里排好的 lyrics_line 对不上。"
        "duration_seconds / bpm / vocal_language 可选，留空由引擎决定。"
        "产物落在项目 music/ 目录，同一项目重复调用会覆盖上一首。"
        "MV 项目应先出歌再写剧本——镜头时长要按歌曲段落分配。",
        {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "曲风描述：风格、情绪、乐器、节奏",
                },
                "duration_seconds": {
                    "type": "integer",
                    "description": "目标时长（秒）；留空由引擎决定",
                },
                "lyrics": {
                    "type": "string",
                    "description": "歌词；留空则引擎按描述自动作词。MV 项目传定稿歌词",
                },
                "bpm": {
                    "type": "integer",
                    "description": "速度（BPM）；留空由引擎决定",
                },
                "vocal_language": {
                    "type": "string",
                    "description": "演唱语言（如 chinese / english）；留空由引擎决定",
                },
            },
            "required": ["prompt"],
        },
    )
    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            prompt = args.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("prompt 必填：空描述会让引擎自由发挥，产出与项目无关的曲子且照常计费")

            duration = args.get("duration_seconds")
            if duration is not None and (not isinstance(duration, int) or duration <= 0):
                raise ValueError(f"duration_seconds 必须是正整数秒，收到: {duration!r}")

            extra: dict[str, Any] = {}
            if duration is not None:
                extra["duration_seconds"] = duration
            for key in ("lyrics", "bpm", "vocal_language"):
                value = args.get(key)
                if value not in (None, ""):
                    extra[key] = value

            spec = TaskSpec.from_request(
                task_type="music",
                media_type="audio",
                resource_id=_MAIN_TRACK_ID,
                prompt=prompt.strip(),
                extra_payload=extra or None,
            )
            successes, failures = await batch_enqueue_and_wait(
                project_name=ctx.project_name,
                specs=[spec],
            )

            if failures:
                f = failures[0]
                return {
                    "content": [{"type": "text", "text": f"❌ 音乐生成失败: {f.error}"}],
                    "is_error": True,
                }

            result = (successes[0].result or {}) if successes else {}
            rel = result.get("file_path") or resource_relative_path("music", _MAIN_TRACK_ID)
            actual = result.get("duration_seconds")
            # 实测时长是 MV 排布镜头的时间轴依据：引擎产出未必等于申请值，报出来让
            # 调用方按真实长度分配镜头，而不是拿申请值去凑。
            duration_note = f"，实测时长 {actual:.1f}s" if isinstance(actual, (int, float)) else ""
            return {
                "content": [{"type": "text", "text": f"✓ 音乐已生成 → {rel}{duration_note}"}],
            }
        except Exception as exc:  # noqa: BLE001
            return tool_error("generate_music", exc)

    return _handler


__all__ = ["generate_music_tool"]
