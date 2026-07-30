# ImageGenerater

本地单人生图工具：文生图 / 图生图，对接 OpenAI 兼容的 Images API（如 `gpt-image-2`）。

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
- 图生图：`POST /v1/images/edits`（1 张参考图）
- 模型列表：`GET /v1/models`
- 参数：size / quality / n
- 本地历史：`data/history.jsonl` + `data/outputs/` + `data/uploads/`
- 任务进度：WebSocket `/ws`

## 目录

```
app/           # FastAPI 与业务模块
config/        # example 进 git；settings.json / secrets.json 仅本地
data/          # 历史与图片
static/        # 前端静态资源
templates/     # 页面
run.py         # 启动入口
```
