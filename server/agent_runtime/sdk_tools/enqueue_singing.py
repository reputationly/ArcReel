"""SDK MCP tool for singing synthesis (SoulX-Singer svs).

工具返回文本是 agent-facing（免 i18n）；显示名在 ``ARCREEL_MCP_TOOL_IDS`` 注册、补三语。
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from lib.generation_queue_client import TaskSpec, batch_enqueue_and_wait
from lib.resource_paths import resource_relative_path
from server.agent_runtime.sdk_tools._context import ToolContext, tool_error

#: 主唱人声轨的固定 resource_id。与作曲的 "main" 同名不同前缀（music/vocal_main.wav
#: vs music/main.wav），一支 MV 一条主唱轨，重复生成覆盖同一份。
_MAIN_VOCAL_ID = "main"


def generate_singing_tool(ctx: ToolContext):
    @tool(
        "generate_singing",
        "歌声合成（SoulX-Singer）：用指定音色唱指定的曲子，入队并等待完成。"
        "voice_reference 是音色参考音频的项目内相对路径（通常取角色的 reference_audio）；"
        "target_song 是目标曲/伴奏的相对路径（通常是作曲产物 music/main.wav）。"
        "两者都必填、语义不同——前者决定用谁的嗓子，后者决定唱什么旋律。"
        "产物落 music/vocal_main.wav，可作为口型驱动（演唱镜头）的驱动音频。",
        {
            "type": "object",
            "properties": {
                "voice_reference": {
                    "type": "string",
                    "description": "音色参考音频的项目内相对路径（谁来唱）",
                },
                "target_song": {
                    "type": "string",
                    "description": "目标曲/伴奏的项目内相对路径（唱什么旋律），如 music/main.wav",
                },
            },
            "required": ["voice_reference", "target_song"],
        },
    )
    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            voice_ref = args.get("voice_reference")
            target = args.get("target_song")
            for label, value in (("voice_reference", voice_ref), ("target_song", target)):
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{label} 必填：缺任一方引擎会产出一段无关音频并照常计费")

            spec = TaskSpec.from_request(
                task_type="singing",
                media_type="audio",
                resource_id=_MAIN_VOCAL_ID,
                extra_payload={
                    "voice_reference": str(voice_ref).strip(),
                    "target_song": str(target).strip(),
                },
            )
            successes, failures = await batch_enqueue_and_wait(
                project_name=ctx.project_name,
                specs=[spec],
            )

            if failures:
                return {
                    "content": [{"type": "text", "text": f"❌ 歌声合成失败: {failures[0].error}"}],
                    "is_error": True,
                }

            result = (successes[0].result or {}) if successes else {}
            rel = result.get("file_path") or resource_relative_path("singing", _MAIN_VOCAL_ID)
            actual = result.get("duration_seconds")
            note = f"，时长 {actual:.1f}s" if isinstance(actual, (int, float)) else ""
            return {"content": [{"type": "text", "text": f"✓ 歌声已合成 → {rel}{note}"}]}
        except Exception as exc:  # noqa: BLE001
            return tool_error("generate_singing", exc)

    return _handler


__all__ = ["generate_singing_tool"]
