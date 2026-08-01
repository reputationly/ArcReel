"""口型驱动（MV 演唱镜）的判定——provider 派生与执行层的单一真相源。

MV 的演唱镜与普通镜头**同为 ``task_type="video"``**，但走不同的模型：演唱镜用
``default_lip_sync_backend``（s2v），其余走项目配置的常规视频模型。这个差异必须在三处
读同一份判据：

1. 入队时的 provider 派生（``generation_queue._derive_provider_id_for_enqueue``）——决定任务
   落进哪个供应商的并发池，也是重启后 resume 认领时锁定的 provider；
2. worker 认领时的限流槽路由（``generation_worker._extract_provider``）；
3. 执行层选模型（``generation_tasks.execute_video_task``）。

三处分叉的后果不是报错而是**错配**：任务在常规视频供应商的额度里排队与限流，实际请求却打到
口型驱动供应商——并发账目对不上、超发打爆自建网关；重启后 resume 还会按错的 provider 去锁
一个已经提交给另一家的任务。这类错配没有任何一条报错指向它。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["item_is_lip_sync", "task_is_lip_sync"]


def item_is_lip_sync(project: dict[str, Any] | None, item: object) -> bool:
    """该镜头是否走口型驱动：仅 ``content_mode=mv`` 且镜头 ``is_performance=true``。

    非演唱镜（氛围镜、空镜）传驱动音频反而会让画面主体被强行对口型，故不能只看 content_mode。
    """
    if not project or project.get("content_mode") != "mv":
        return False
    return isinstance(item, dict) and bool(item.get("is_performance"))


def task_is_lip_sync(
    project_name: str | None,
    project: dict[str, Any] | None,
    payload: dict[str, Any] | None,
    resource_id: str | None,
) -> bool:
    """按 ``(project, payload.script_file, resource_id)`` 判定该视频任务是否走口型驱动模型。

    供拿不到剧本条目的 provider 派生侧使用（入队与 worker 认领）；执行层已持有条目，直接用
    ``item_is_lip_sync``。

    任何一步取不到（非 MV、缺 script_file、剧本读不出、条目未命中）都返回 False——派生侧的
    契约是「拿不准就别硬塞」（见 ``_derive_provider_id_for_enqueue`` docstring），按常规视频
    模型走比按口型模型走更接近默认行为。
    """
    if not project_name or not project or project.get("content_mode") != "mv" or not resource_id:
        return False
    script_file = (payload or {}).get("script_file")
    if not isinstance(script_file, str) or not script_file:
        return False
    try:
        from lib.project_manager import get_project_manager
        from lib.storyboard_sequence import find_storyboard_item, get_storyboard_items

        script = get_project_manager().load_script(project_name, script_file)
        items, id_field, *_ = get_storyboard_items(script)
        resolved = find_storyboard_item(items, id_field, resource_id)
    except Exception:
        logger.debug("口型驱动判定读剧本失败，按常规视频模型派生 provider", exc_info=True)
        return False
    return item_is_lip_sync(project, resolved[0] if resolved else None)
