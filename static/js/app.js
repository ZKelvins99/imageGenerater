function app() {
  return {
    mode: "text",
    prompt: "",
    model: "",
    size: "1024x1024",
    quality: "medium",
    n: 1,
    models: [],
    showAllModels: false,
    history: [],
    selectedId: null,
    selected: null,
    previewUrls: [],
    showSettings: false,
    settings: {
      base_url: "",
      api_key: "",
      api_key_set: false,
      api_key_masked: "",
      default_model: "",
      default_size: "1024x1024",
      default_quality: "medium",
      default_n: 1,
      model_filter_keywords: [],
    },
    filterKeywordsText: "",
    settingsMsg: "",
    refFile: null,
    refFileName: "",
    refPath: null,
    refPreview: null,
    taskBusy: false,
    taskProgress: 0,
    taskMessage: "",
    taskError: "",
    activeTaskId: null,
    ws: null,
    wsState: "closed",
    _wsHeartbeat: null,
    _wsReconnectTimer: null,
    lightbox: null,

    async init() {
      await this.loadSettings();
      await this.loadModels();
      await this.loadHistory();
      this.connectWs();
    },

    statusLabel(s) {
      return ({ pending: "排队", running: "生成中", done: "完成", error: "失败" }[s] || s);
    },

    _clearWsTimers() {
      if (this._wsHeartbeat) {
        clearInterval(this._wsHeartbeat);
        this._wsHeartbeat = null;
      }
      if (this._wsReconnectTimer) {
        clearTimeout(this._wsReconnectTimer);
        this._wsReconnectTimer = null;
      }
    },

    connectWs() {
      // Prevent stacked heartbeats / reconnect timers across reconnects.
      this._clearWsTimers();
      if (this.ws) {
        this.ws.onclose = null;
        this.ws.onerror = null;
        this.ws.onmessage = null;
        this.ws.onopen = null;
        try {
          this.ws.close();
        } catch (_) {
          /* ignore */
        }
        this.ws = null;
      }

      const proto = location.protocol === "https:" ? "wss" : "ws";
      const url = `${proto}://${location.host}/ws`;
      try {
        this.ws = new WebSocket(url);
      } catch (e) {
        this.wsState = "error";
        return;
      }
      this.ws.onopen = () => {
        this.wsState = "open";
      };
      this.ws.onclose = () => {
        this.wsState = "closed";
        this._clearWsTimers();
        this._wsReconnectTimer = setTimeout(() => this.connectWs(), 2000);
      };
      this.ws.onerror = () => {
        this.wsState = "error";
      };
      this.ws.onmessage = (ev) => {
        let msg;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg.type === "task") {
          this.onTaskEvent(msg.payload);
        }
      };
      this._wsHeartbeat = setInterval(() => {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
          this.ws.send("ping");
        }
      }, 25000);
    },

    onTaskEvent(payload) {
      if (this.activeTaskId && payload.task_id !== this.activeTaskId) {
        // still refresh history for other tasks
        if (payload.status === "done" || payload.status === "error") {
          this.loadHistory();
        }
        return;
      }
      this.taskProgress = payload.progress || 0;
      this.taskMessage = payload.message || "";
      if (payload.status === "running" || payload.status === "pending") {
        this.taskBusy = true;
        this.taskError = "";
      }
      if (payload.status === "done") {
        this.taskBusy = false;
        this.taskError = "";
        this.previewUrls = payload.output_urls || [];
        this.activeTaskId = null;
        this.loadHistory().then(() => {
          if (payload.history_id) {
            const item = this.history.find((h) => h.id === payload.history_id);
            if (item) this.selectHistory(item);
          }
        });
      }
      if (payload.status === "error") {
        this.taskBusy = false;
        this.taskError = payload.error || payload.message || "生成失败";
        this.activeTaskId = null;
        this.loadHistory();
      }
    },

    async loadSettings() {
      const res = await fetch("/api/settings");
      const data = await res.json();
      this.settings = { ...data, api_key: "" };
      this.filterKeywordsText = (data.model_filter_keywords || []).join(",");
      this.model = data.default_model || "";
      this.size = data.default_size || this.size;
      this.quality = data.default_quality || this.quality;
      this.n = data.default_n || this.n;
      if (!data.base_url || !data.api_key_set || !data.default_model) {
        this.showSettings = true;
      }
    },

    async saveSettings() {
      this.settingsMsg = "";
      const body = {
        base_url: this.settings.base_url,
        default_model: this.settings.default_model,
        default_size: this.settings.default_size,
        default_quality: this.settings.default_quality,
        default_n: this.settings.default_n,
        model_filter_keywords: this.filterKeywordsText
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
      };
      if (this.settings.api_key && this.settings.api_key.trim()) {
        body.api_key = this.settings.api_key.trim();
      }
      const res = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        this.settingsMsg = err.detail || "保存失败";
        return;
      }
      const data = await res.json();
      this.settings = { ...data, api_key: "" };
      this.filterKeywordsText = (data.model_filter_keywords || []).join(",");
      this.settingsMsg = "已保存";
      this.model = data.default_model;
      await this.loadModels();
    },

    async loadModels() {
      try {
        const res = await fetch(`/api/models?show_all=${this.showAllModels ? "true" : "false"}`);
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          console.warn("models failed", err);
          this.models = [{ id: this.model || "（请先配置）" }];
          return;
        }
        const data = await res.json();
        this.models = data.data || [];
        if (this.model && !this.models.find((m) => m.id === this.model) && this.models.length) {
          // keep configured default even if not in filtered list
        } else if (!this.model && this.models.length) {
          this.model = this.models[0].id;
        }
      } catch (e) {
        this.models = [{ id: this.model || "（请先配置）" }];
      }
    },

    async loadHistory() {
      const res = await fetch("/api/history");
      const data = await res.json();
      this.history = data.items || [];
    },

    selectHistory(item) {
      this.selectedId = item.id;
      this.selected = item;
      this.previewUrls = item.output_urls || [];
      this.taskError = item.status === "error" ? item.error || "生成失败" : "";
    },

    newSession() {
      this.selectedId = null;
      this.selected = null;
      this.previewUrls = [];
      this.taskError = "";
      this.prompt = "";
      this.clearRef();
    },

    onFile(ev) {
      const file = ev.target.files && ev.target.files[0];
      if (!file) return;
      this.refFile = file;
      this.refFileName = file.name;
      this.refPath = null;
      this.refPreview = URL.createObjectURL(file);
    },

    clearRef() {
      this.refFile = null;
      this.refFileName = "";
      this.refPath = null;
      this.refPreview = null;
    },

    reuseSelected() {
      if (!this.selected) return;
      this.prompt = this.selected.prompt || "";
      this.model = this.selected.model || this.model;
      this.size = this.selected.size || this.size;
      this.quality = this.selected.quality || this.quality;
      this.n = this.selected.n || this.n;
      this.mode = this.selected.mode || "text";
      if (this.selected.output_paths && this.selected.output_paths.length) {
        this.mode = "image";
        this.refFile = null;
        this.refFileName = "";
        this.refPath = this.selected.output_paths[0];
        this.refPreview = this.selected.output_urls[0];
      }
    },

    async deleteSelected() {
      if (!this.selected) return;
      if (!confirm("确认删除这条记录及本地图片？")) return;
      const id = this.selected.id;
      const res = await fetch(`/api/history/${id}`, { method: "DELETE" });
      if (!res.ok) return;
      if (this.selectedId === id) this.newSession();
      await this.loadHistory();
    },

    async submit() {
      if (this.taskBusy) return;
      if (!this.prompt.trim()) return;
      if (this.mode === "image" && !this.refFile && !this.refPath) {
        this.taskError = "图生图需要参考图";
        return;
      }
      this.taskBusy = true;
      this.taskProgress = 0.05;
      this.taskMessage = "提交任务…";
      this.taskError = "";
      this.previewUrls = [];

      const fd = new FormData();
      fd.append("mode", this.mode);
      fd.append("prompt", this.prompt.trim());
      fd.append("model", this.model);
      fd.append("size", this.size);
      fd.append("quality", this.quality);
      fd.append("n", String(this.n));
      if (this.mode === "image") {
        if (this.refFile) {
          fd.append("image", this.refFile);
        } else if (this.refPath) {
          fd.append("reference_path", this.refPath);
        }
      }

      try {
        const res = await fetch("/api/generate", { method: "POST", body: fd });
        const data = await res.json();
        if (!res.ok) {
          this.taskBusy = false;
          this.taskError = data.detail || "提交失败";
          return;
        }
        this.activeTaskId = data.task_id;
        this.taskMessage = data.message || "任务已创建";
        this.taskProgress = data.progress || 0.1;
        await this.loadHistory();
      } catch (e) {
        this.taskBusy = false;
        this.taskError = String(e);
      }
    },
  };
}
