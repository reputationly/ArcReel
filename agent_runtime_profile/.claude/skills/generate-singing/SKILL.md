---
name: generate-singing
description: 歌声合成（SoulX-Singer）——用指定音色唱指定的曲子。当用户说"合成歌声"、"让 XX 唱"、"生成人声"，或 MV 项目做完伴奏需要人声时使用。
---

# 歌声合成

用**音色参考**唱**目标曲**，产出人声轨 `music/vocal_main.wav`。
这条轨可以直接作为演唱镜头的口型驱动音频。

## 两个输入语义不同，都必填

| 参数 | 是什么 | 通常取自 |
|---|---|---|
| `voice_reference` | **谁来唱**（音色样本） | 角色的 `reference_audio` |
| `target_song` | **唱什么旋律**（目标曲/伴奏） | 作曲产物 `music/main.wav` |

缺任一方引擎会产出一段无关音频并照常计费，故工具直接拒绝空值。

## 工具调用

**重要：歌声合成必须调用下列 MCP 工具入队。此 skill 不提供任何 Python/Shell 脚本。**

```
mcp__arcreel__generate_singing({
  "voice_reference": "characters/refs/歌手音色.wav",
  "target_song": "music/main.wav"
})
```

> **前置**：先有伴奏（`generate-music`）、先有音色参考（角色资产的 `reference_audio`，
> 由用户上传——agent 不能代传音频文件）。缺音色参考时引导用户到角色资产页上传，
> 不要拿默认音色顶替：用户要的是特定的嗓子。
>
> **依赖**：音乐模型需具备歌声合成能力（如 `soulx-singer`）。作曲用的 ACE-Step
> 与歌声合成是**两个不同的模型**，若设置页配的是纯作曲模型，工具会提示换模型。

## 在 MV 流程中的位置

```
generate-music（伴奏）→ generate-singing（人声）→ 演唱镜头的口型驱动
                                    ↓
                          music/vocal_main.wav
```

演唱镜头（剧本 `is_performance=true`）生成视频时以这条人声轨作驱动音频，
口型跟着唱词动。非演唱镜头走常规图生视频，不需要它。

## 错误处理

- 音色参考或目标曲路径不存在 → 工具报错，检查路径是否为**项目内相对路径**
- 模型不具备歌声合成能力 → 引导用户到设置页把音乐模型换成 svs 模型
- 合成失败不自动重试：通常是模型未部署或输入音频格式不受支持，重试无用
