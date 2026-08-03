# Docker 部署升级与回滚

面向已用 Docker Compose 跑起 ArcReel 的部署，说明如何升级到新镜像以及升级失败时如何退回。
首次部署见 [`getting-started.md`](getting-started.md)，沙箱依赖与 `.env` 约束见 [`deployment.md`](deployment.md)。

## 先确认要升到哪个 tag

CI 推送的 tag 分两类，**选错 tag 是升级最常见的失败原因**：

| 触发方式 | 产出的 tag | 说明 |
|---|---|---|
| 打 git tag `v1.2.3` | `1.2.3`、`1.2`、`1`、`latest`、`sha-<短sha>` | 正式发布 |
| 推分支（含 main） | `<分支名>-<8位sha>`、`<分支名>`、`sha-<短sha>` | 未发布构建 |

`latest` **只在正式发布时移动**——分支构建不会更新它。所以跟着 main 走的部署要 pin
`main-<8位sha>`（不可变，可精确回滚）或 `main`（每次推送都变）；pin `latest` 会一直停在
上一个正式版本。

分支名中的 `/` 会被规范化为 `-`（`agent/foo` → `agent-foo`）。

镜像仓库：

- GHCR：`ghcr.io/<owner>/arcreel`
- 阿里云（配了 `ACR_*` secrets 时）：`<ACR域名>/<owner>/arcreel`

取新 tag 最直接的方式是看构建日志里的 `MOVING_TAGS` 与 `tags:` 行：

```bash
gh run list --workflow=docker.yml --limit 1
gh run view <run-id> --log | grep -E "MOVING_TAGS|tags:"
```

## 收集现有部署信息

在**部署机**上执行，确认部署目录、当前 tag、挂载与架构：

```bash
echo "=== 架构 ===" && uname -m && \
echo && echo "=== ArcReel 容器 ===" && \
docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' | grep -iE 'arcreel|NAMES' && \
echo && echo "=== 部署目录与挂载 ===" && \
docker inspect arcreel-arcreel-1 --format '镜像: {{.Config.Image}}
compose目录: {{index .Config.Labels "com.docker.compose.project.working_dir"}}
挂载:{{range .Mounts}}
  {{.Source}} -> {{.Destination}}{{end}}'
```

架构要与镜像 manifest 对得上（CI 同时构建 amd64 与 arm64，多架构 manifest 会自动选）。

## 升级

以下命令假定部署目录 `/opt/arcreel`、compose 文件 `docker-compose.yml`、应用服务名 `arcreel`，
按上一步的实际输出替换。`<新tag>` 与 `<旧tag>` 替换为实际值。

### 1. 备份

`projects/` 是项目数据（剧本、分镜图、视频、音频），PostgreSQL 存任务队列与用量记录，
两者都要备。**升级前必做**——回滚只能退镜像，退不回被新版本改写过的数据。

数据库用户名从容器环境里取，不要写死 `postgres`——compose 里通常自定义过，写死会让
`pg_dumpall` 报 `role "postgres" does not exist`，而 `&&` 链会就此中断，**前面几步看似成功、
备份实则不完整**。

```bash
cd /opt/arcreel && \
BK=/opt/arcreel-backup-$(date +%Y%m%d-%H%M) && mkdir -p $BK && \
PGUSER=$(docker exec arcreel-postgres-1 printenv POSTGRES_USER) && \
echo "数据库用户: ${PGUSER:?取不到 POSTGRES_USER，先确认 postgres 容器名与环境变量}" && \
cp -a .env docker-compose.yml $BK/ && \
tar czf $BK/projects.tgz projects && \
docker exec arcreel-postgres-1 pg_dumpall -U "$PGUSER" > $BK/db.sql && \
echo "旧镜像 tag: <旧tag>" > $BK/ROLLBACK.txt && \
ls -lah $BK && echo "✅ 备份完成: $BK"
```

跑完核对 `ls` 输出里 `db.sql` **不是 0 字节**——空文件说明 dump 失败但重定向已建好文件，
看起来像备份成功。

用 SQLite（开发部署，无 postgres 容器）时把 `PGUSER=` 与 `pg_dumpall` 两行去掉——数据库文件在
`projects/.arcreel.db`，已随 `projects.tgz` 一并备份。

### 2. 换 tag 并拉起

`docker compose pull` 只下载不切换，运行中的容器不受影响；真正的替换发生在 `up -d`。

```bash
cd /opt/arcreel && \
sed -i 's#arcreel:<旧tag>#arcreel:<新tag>#' docker-compose.yml && \
grep -n "image:" docker-compose.yml && \
docker compose pull arcreel && \
docker compose up -d arcreel && \
sleep 15 && docker compose ps
```

拉取报未授权时先登录对应仓库，再重跑本步：

```bash
docker login <镜像仓库域名>
```

### 3. 数据库迁移

只有当次升级**跨越了含 `alembic/versions/` 新文件的提交**时才需要——容器启动不会自动迁移。

```bash
# 先确认区间内有没有迁移文件
git log --oneline --name-only <旧sha>..<新sha> -- alembic/versions/

# 有才执行
docker exec arcreel-arcreel-1 uv run --no-sync alembic upgrade head
```

### 4. 验证

```bash
curl -s localhost:1241/health && echo && \
docker compose logs --tail=30 arcreel | grep -iE "error|traceback|Uvicorn running"
```

健康检查通过、日志无 traceback 即可。要确认某个具体特性是否真的上了，在容器内导入对应模块——
比导航界面更快、也不依赖前端缓存是否刷新：

```bash
docker exec arcreel-arcreel-1 uv run --no-sync python -c "from lib.profile_manifest import VALID_CONTENT_MODES as M; print('内容模式:', sorted(M))"
```

两处容易踩空：容器里的裸 `python` 是系统解释器、装不到依赖（报 `No module named 'pydantic'`），
要走 `uv run --no-sync`（与 Dockerfile 的 CMD 同一环境）；`python -c` 写成单行，多行形式经
终端粘贴常被带上缩进而报 `IndentationError`——两种报错都像功能缺失，实际只是没进对环境。

浏览器侧若仍是旧界面，强制刷新（Ctrl/Cmd + Shift + R）清掉前端静态资源缓存。

## 回滚

镜像回滚是幂等的，改回旧 tag 重新 `up -d` 即可：

```bash
cd /opt/arcreel && \
sed -i 's#arcreel:<新tag>#arcreel:<旧tag>#' docker-compose.yml && \
docker compose up -d arcreel && \
sleep 15 && docker compose ps
```

**跑过数据库迁移就不能只退镜像**——旧版本的代码读不懂新 schema。需要连数据一起退：

```bash
cd /opt/arcreel && \
docker compose down && \
cat /opt/arcreel-backup-<时间戳>/db.sql | docker exec -i arcreel-postgres-1 psql -U "$(docker exec arcreel-postgres-1 printenv POSTGRES_USER)" && \
sed -i 's#arcreel:<新tag>#arcreel:<旧tag>#' docker-compose.yml && \
docker compose up -d
```

项目数据被新版本改写过（如生成了新格式的剧本字段）时，一并还原：

```bash
cd /opt/arcreel && mv projects projects.broken && \
tar xzf /opt/arcreel-backup-<时间戳>/projects.tgz && \
docker compose restart arcreel
```

## 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| `pull` 提示 manifest unknown | tag 拼错，或该分支构建未推成功 | 回到「先确认要升到哪个 tag」核对构建日志 |
| `pull` 提示 unauthorized | 未登录私有仓库 | `docker login <域名>` |
| 拉到的还是旧版本 | pin 了 `latest` 而本次是分支构建 | 改 pin 到 `<分支名>-<8位sha>` |
| 容器起来但立刻退出 | `.env` 残留 provider 密钥被启动检查拒绝 | 见 [`deployment.md`](deployment.md) 的 .env 迁移说明 |
| 启动报 sandbox 工具缺失 | 宿主机 seccomp/apparmor 或 cap 配置丢失 | 对照 `deploy/docker-compose.yml` 的 `security_opt` / `cap_add` |
| 界面功能没变 | 前端静态资源缓存 | 强制刷新浏览器 |
| `pg_dumpall` 报 role 不存在 | 数据库用户名不是 `postgres` | 用 `printenv POSTGRES_USER` 取，并核对 `db.sql` 非空 |
| 容器内 `python` 报 No module named | 裸 `python` 是系统解释器，依赖在 uv 虚拟环境 | 命令前加 `uv run --no-sync` |
