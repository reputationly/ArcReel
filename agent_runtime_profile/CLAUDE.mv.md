# AI 视频生成工作空间
<!-- mode: mv -->

---

## 重要总则

以下规则适用于整个项目的所有操作：

### 视频规格
- **视频比例**：由项目 `aspect_ratio` 配置决定（MV 默认 16:9 横屏），无需在 prompt 中指定
- **单镜头时长**：MV 项目**没有** `default_duration` 偏好——镜头时长由**歌曲段落**决定（副歌快切、间奏可长），
  且必须取所选视频模型 `supported_durations` 中的值；subagent 运行时通过 `mcp__arcreel__get_video_capabilities` 自查真值
- **图片分辨率**：1K
- **视频分辨率**：1080p
- **生成方式**：按 `generation_mode` 分两路——storyboard 模式每个镜头独立生成、以分镜图作起始帧；reference_video 模式按派生分组（video_unit）直出、跳过分镜（见下文「生成模式」）

> **关于 extend 功能**：Veo 3.1 extend 功能仅用于延长单个镜头，
> 每次固定 +7 秒，不适合用于串联不同镜头。不同镜头之间使用 ffmpeg 拼接。

### 音频规范
- **BGM 自动禁止**：生成端已在视频 prompt 末尾自动追加「禁止出现：BGM、文字字幕、水印」。MV 的音乐是**独立音轨**（`music/main.wav`），
  不能让视频模型再生成一层——`video_prompt.ambiance_audio` 只写画面内的环境音（脚步、风声），不描述音乐本身

### 工具调用

- **业务入队 / 文本生成 / 能力查询**：统一走 `mcp__arcreel__*` 系列 SDK in-process MCP tool（角色/场景/道具/分镜/视频/宫格/集脚本/规范化剧本/视频能力查询）。它们跑在 server 主进程，不受 sandbox 网络白名单约束，agent 直接以 tool 形式调用。
- **编辑项目 JSON**：修改剧本（`scripts/*.json`）或角色/场景/道具（`project.json`）**一律走 `mcp__arcreel__*` 编辑工具**——剧本改**镜头**字段用 `patch_episode_script`（batch-native：传 `{分镜id: {字段路径: 值}}` 映射，单次调用改多分镜 × 多字段，单条编辑写成长度 1 的 map；all-or-nothing 原子，任一编辑非法则整批不落盘、错误会指出出错的分镜 id 与字段（结构校验类错误按字段路径报告）；批量编辑前先 Read 该剧本确认现状），改分集标题用 `patch_episode_meta`，增/删/拆分镜用 `insert_segment` / `remove_segment` / `split_segment`，角色/场景/道具用 `patch_project`。**MV 的 `song` / `lyrics` 是剧本顶层字段，只能用 `patch_song`**——`patch_episode_script` 按分镜 id 定位、改不了顶层字段。**严禁**用 Write / Edit / Bash 直改这两类文件（已被 sandbox `denyWrite` 与 PreToolUse hook 双层拒绝）。**改 prompt 必重生**：用 `patch_episode_script` 改了某些分镜的 `image_prompt` / `video_prompt` 后，工具不会自动作废旧图/视频，必须紧接着调对应生成工具重新生成这些分镜，否则会留下「新 prompt + 旧画面」的陈旧。
- **Bash 用途**：仅供通用排查与文件浏览（`ls / cat / jq / python / curl` 等），以及 `manage-project` / `compose-video` 这两个 skill 内还保留的 Python 脚本。
- **敏感文件保护**：`.env` / `vertex_keys/` / `.system_config.json*` / `.arcreel.db*` / `.claude/settings.json` 由 sandbox profile（`filesystem.denyRead`）内核级拒绝读取，并由 PreToolUse 文件访问 hook 双重防御；代码文件（.py/.js/.ts/.tsx/.sh/.yaml/.yml/.toml）受运行时 hook 阻止写入。

### 路径规范

agent session 的当前工作目录（cwd）已绑定到当前项目根，**所有工具参数中的路径必须遵循以下规则**：

- **Read / Edit / Write / Glob / Grep**：`file_path` 使用**绝对路径**
- **Bash 调用 skill 脚本**：使用**相对项目根 cwd** 的路径，例如：
  - ✅ `scripts/episode_1.json`、`storyboards/E1S01.png`
  - ❌ `projects/{项目名}/scripts/episode_1.json`（双前缀，占位符替换或拼接出错就会落到 projects 根）
- **严禁**在工具参数中出现 `projects/{...}/` 前缀；该前缀仅用于文档说明项目目录结构，**不可直接作为参数传给任何工具**
- skill 脚本内部已加 cwd 校验，cwd 漂离当前项目目录时会直接拒绝执行
- **关于 agent.md / SKILL.md 中的相对形式**：subagent 指引（如「读取 `project.json`」）里出现的相对路径是**项目内位置说明**，并非可直接传给工具的 `file_path` 值。调用 Read/Edit/Write/Glob/Grep 时仍按本节规则用 session cwd 拼成绝对路径再传参

---

## 内容模式

本项目为 **MV 模式**（mv），产出**单支**与歌曲同长的音乐视频：

- 剧本数据结构为平铺 `shots[]`，`shot_id` 格式 `E1S{n}`；每个镜头携带 `section`（歌曲段落名）、
  **`start_seconds`（该镜在歌曲时间轴上的绝对入点）**、`lyrics_line`（对应歌词行）与
  `is_performance`（是否人物出镜演唱）
- 项目**恒单集**：`episodes` 恒为第 1 集单条，剧本即 `scripts/episode_1.json`；不做分集规划
- 剧本顶层另有 `song`（歌曲元数据：实测时长、BPM、段落表、音频路径）与 `lyrics`（完整歌词）

### 为什么 start_seconds 是一等字段

其余模式的镜头顺次排布、时长累加即可；**MV 不行**。镜头必须钉在歌曲的绝对时间点上：

- 口型要对上歌声，卡点要对上鼓点，段落切换要对上编曲
- 视频时长按供应商档位取整，实际产出与规划值有偏差是常态；累加式排布下，一镜偏 0.5 秒，
  后面全部顺移，越到片尾错得越多

所以排镜头时**先定 start_seconds，再定 duration_seconds**，相邻镜头首尾相接、铺满整首歌。

---

## 生成模式

MV 模式**只开放 `storyboard`（图生视频）**：

- 演唱镜头要用分镜图作人物首帧再驱动口型，参考直出没有这一步
- `reference_video` 与 `grid` 对 MV 不开放

---

## 工作流程概览

**顺序与其余模式相反：先有歌，再有剧本。** 歌曲决定时间轴，剧本是按歌排的镜头表——
顺序反了只能推倒重排。

1. **确认创作输入**：Read `project.json` 与剧本。与用户确认曲风、情绪、歌词来源
   （用户提供歌词，还是让你按主题创作后交用户确认）
2. **生成音乐**：`generate-music` skill → `mcp__arcreel__generate_music({"prompt": "曲风描述"})`。
   产物落 `music/main.wav`，**以工具回报的实测时长为准**，不要用申请值
3. **回写歌曲元数据**：把实测时长、段落划分、音频路径经 `patch_song` 写进剧本顶层 `song`；
   歌词同样经 `patch_song` 写进 `lyrics`。段落表是下一步排镜头的硬约束
4. **资产设计**：歌手（`characters`）、场景、道具先定义再 dispatch `generate-assets` 出设计图。
   **歌手的 `reference_audio` 要单独确认**——它是歌声合成的音色参考，缺了就只能用默认音色
5. **生成剧本**：`mcp__arcreel__generate_episode_script({"episode": 1})`，按段落表与实测时长排镜头。
   生成后核对：相邻镜头首尾相接、最后一镜结束时间等于歌曲总时长、副歌至少有一个演唱镜头
6. **分镜图生成**：逐镜出图。**演唱镜头（`is_performance=true`）要重点审**——它们会作为口型驱动的
   人物首帧，人物主体过小或被遮挡会让口型驱动失败
7. **视频生成**：非演唱镜头逐镜图生视频；演唱镜头走口型驱动（见下文）
8. **导出剪映草稿**：视频齐全后在 Web 端导出，草稿含视频轨 + 歌词字幕轨 + **独立音乐轨**

工作流支持**灵活入口**：从剧本 `song` 是否已回写、镜头是否齐全判断进行到哪一步。

### 演唱镜头与口型驱动

`is_performance` 在剧本层就分流，不留给生成端猜：

- `true`：画面主体是歌手在唱这句词。这类镜头需要清晰的正面/侧面人物构图，
  主体过小、背对镜头、被遮挡都会让口型驱动失败
- `false`：氛围镜、空镜、意象镜。`lyrics_line` 可留空

副歌通常需要至少一个演唱镜头——那是观众记住这首歌的地方。纯器乐段（intro/outro/bridge 常见）
的 `lyrics_line` 填空串、`is_performance` 一律 false。

### 歌词与字幕

`lyrics_line` 是**字幕来源**，不是配音来源——歌词要唱不要念。MV 不走旁白 TTS
（`generate_narration_audio` 对 MV 不适用），人声由歌声合成产出。

---

## 职责边界

- **禁止编写代码**：不得创建或修改任何代码文件（.py/.js/.sh 等），数据处理走 `mcp__arcreel__*` 工具或 `manage-project` / `compose-video` 的现有脚本
- **代码 bug 上报**：如果明确判断 MCP 工具或 skill 脚本出现的是代码 bug（而非参数或环境问题），向用户报告错误并建议反馈给开发者

## 项目目录结构

> 下面的目录树仅为说明用途，agent session 的 cwd 已在项目根。**Bash 调用 skill 脚本**时使用相对 cwd 的路径（如 `scripts/`）；**Read / Edit / Write / Glob / Grep** 的 `file_path` 仍按上文「路径规范」要求使用**绝对路径**。无论哪种工具都不可带 `projects/{项目名}/` 前缀。

```text
projects/{项目名}/      # ← session cwd 已在此，下面均为 cwd 内的相对路径
├── project.json       # 项目元数据（角色、场景、道具、风格）
├── scripts/           # 剧本 (JSON)，恒为 episode_1.json
├── music/             # 歌曲产物（main.wav）
├── characters/        # 角色设计图
├── scenes/            # 场景设计图
├── props/             # 道具设计图
├── storyboards/       # 分镜图片（storyboard 模式）
├── videos/            # 生成的视频片段（storyboard 模式）
├── reference_videos/  # 生成的 video_unit（reference_video 模式）
├── thumbnails/        # 首帧缩略图
└── output/            # 最终输出
```

### project.json 核心字段

- `schema_version`：项目数据格式版本
- `title`、`content_mode`（固定 `mv`）、`generation_mode`（固定 `storyboard`）、`style`、`style_description`
- `episodes`：恒为第 1 集单条（episode、title、script_file）
- `characters` / `scenes` / `props`：资产完整定义（歌手走 `characters`，其 `reference_audio` 是歌声合成的音色参考）

### 数据分层原则

- 角色/场景/道具的完整定义**只存储在 project.json**，剧本中仅引用名称
- 歌曲元数据（`song`：实测时长、段落表、音频路径）存在**剧本顶层**而非 project.json——它是这一集镜头表的时间轴依据，与剧本同生共死
- `scenes_count`、`status`、`progress` 等统计字段由 StatusCalculator **读时计算**，不存储
- 剧集元数据（episode/title/script_file）在剧本保存时**写时同步**
