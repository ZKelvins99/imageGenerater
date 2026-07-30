# ImageGenerater

本地 AI 图像工作台：文生图、多图参考合成、蒙版局部编辑与 Responses API 对话式连续编辑，对接 OpenAI 及兼容 API（如 `gpt-image-2`）。

## 快速开始（uv）

```powershell
cd imageGenerater
uv venv
.\.venv\Scripts\Activate.ps1
uv pip install -r requirements.txt
python run.py
```

浏览器打开：http://127.0.0.1:27183

**首次必须在页面「设置」中自行填写：**

- Base URL（你的 API 网关地址）
- API Key（你自己的 token）
- 默认模型（如 `gpt-image-2`）

## 配置说明（可上传 GitHub）

| 文件 | 是否进 git | 说明 |
|------|------------|------|
| `config/settings.example.json` | 是 | 空模板，无地址/密钥/模型 |
| `config/secrets.example.json` | 是 | Key 模板，值为空 |
| `config/settings.json` | **否** | 本地非敏感配置 |
| `config/secrets.json` | **否** | 本地 API Key |

仓库内不包含任何网关地址、模型名或密钥默认值；每人自行配置。

## 功能

- 文生图：`POST /v1/images/generations`
- 参考合成：`POST /v1/images/edits`（支持多张参考图和拖动排序）
- 局部编辑：主图 + 可选参考图；支持上传 PNG Alpha 蒙版或在浏览器内画笔绘制
- 模型列表：`GET /v1/models`
- 参数：灵活尺寸 / quality / n / 输出格式 / 压缩 / 背景 / moderation
- 完整工作台：能力驱动参数、任务取消与恢复、历史搜索/筛选/收藏/标签、批量下载、继续编辑
- 本地历史：SQLite + 受管理的本地图片资产；旧 `history.jsonl` 可幂等迁移
- 任务进度：持久化 Job + WebSocket `/ws`，断线时自动轮询
- 渐进预览：GPT Image `partial_images` 0–3、SSE 实时预览、刷新恢复与最终图自动替换
- 对话式编辑：从历史结果开启 Responses API 多轮会话，持久化 response 关联、revised prompt 与 token usage
- 多 Provider：`/api/v1/providers`（CRUD、激活、连接测试、按 Provider 拉模型）
- 认证：静态 Bearer，或可配置的公司 Token Distributor（需自行填写 endpoint/字段映射；**真实联调未完成**，见 `docs/IMPLEMENTATION_PLAN.md` §22）

旧设置页 `/api/settings` 仍可用，会自动迁移为 `default` Provider。

Responses API 能力默认关闭。仅在 Provider 设置中明确启用并填写 Responses 主模型后，页面才显示“对话式编辑”；该流程会产生主模型 token 成本，并要求上游兼容 OpenAI Responses API。

## 目录

```
app/           # FastAPI 与业务模块
config/        # example 进 git；settings.json / secrets.json 仅本地
data/          # 历史与图片
docs/          # 产品与实施计划
static/        # 前端静态资源
templates/     # 页面
tests/         # 回归测试（mock，无真实网络）
run.py         # 启动入口
```

## 开发与测试

```powershell
uv pip install -r requirements-dev.txt
.\.venv\Scripts\ruff.exe check app tests
.\.venv\Scripts\ruff.exe format app tests
.\.venv\Scripts\mypy.exe app
.\.venv\Scripts\pytest.exe -q
```

CI 见 `.github/workflows/ci.yml`。测试全程使用 mock，不调用真实收费 API。

进度见 [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) §0（当前 Phase 0–6 已完成）。
