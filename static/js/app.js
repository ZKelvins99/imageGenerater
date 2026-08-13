/* global Alpine */

const TERMINAL_JOB_STATES = new Set(["succeeded", "failed", "cancelled"]);
const RUNNING_JOB_STATES = new Set([
  "queued",
  "preparing",
  "running",
  "streaming",
  "saving",
  "cancel_requested",
]);

async function apiRequest(path, options = {}) {
  const response = await fetch(path, options);
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json")
    ? await response.json().catch(() => ({}))
    : await response.text().catch(() => "");
  if (!response.ok) {
    const message =
      data?.error?.message ||
      data?.detail ||
      (typeof data === "string" ? data : "") ||
      `请求失败（HTTP ${response.status}）`;
    const error = new Error(message);
    error.status = response.status;
    error.code = data?.error?.code || "REQUEST_FAILED";
    error.payload = data;
    throw error;
  }
  return data;
}

function defaultDistributor() {
  return {
    base_url: "",
    path: "/token",
    method: "POST",
    auth_mode: "bearer",
    auth_header_name: "X-Client-Secret",
    request_body: {},
    token_path: "access_token",
    expires_in_path: "expires_in",
    expires_at_path: null,
    token_type_path: "token_type",
    timeout_seconds: 15,
  };
}

function emptyProviderDraft() {
  return {
    id: "",
    name: "",
    provider_type: "openai_compatible_images",
    base_url: "",
    auth_type: "static_bearer",
    default_model: "gpt-image-2",
    enabled: true,
    verify_tls: true,
    timeout_seconds: 300,
    extra_headers: {},
    capability_overrides: {},
    responses_enabled: false,
    responses_model: "",
    token_distributor: defaultDistributor(),
    api_key: "",
    api_key_set: false,
    distributor_client_id: "",
    distributor_client_id_set: false,
    distributor_client_secret: "",
    distributor_client_secret_set: false,
    is_active: false,
  };
}

function imageStudio() {
  return {
    providers: [],
    providerId: "",
    activeProvider: null,
    modelOptions: [],
    model: "",
    capabilities: null,
    loadingModels: false,

    mode: "generate",
    prompt: "",
    promptFocused: false,
    inputAssets: [],
    maskAsset: null,
    showMaskEditor: false,
    maskTool: "paint",
    maskBrushSize: 64,
    maskUndoStack: [],
    maskRedoStack: [],
    maskDrawing: false,
    maskLastPoint: null,
    dragAssetIndex: null,
    uploadingInputs: false,
    uploadingMask: false,

    sizePreset: "1024x1024",
    customWidth: 1024,
    customHeight: 1024,
    quality: "auto",
    n: 1,
    outputFormat: "png",
    outputCompression: 90,
    background: "",
    moderation: "auto",
    partialImages: 0,
    advancedOpen: false,

    jobs: [],
    loadingJobs: false,
    selectedJob: null,
    activeJob: null,
    activeTaskId: "",
    previewUrls: [],
    partialPreviewUrls: [],
    historyQuery: "",
    historyFilter: "all",
    historyModeFilter: "",
    historyProviderFilter: "",
    historyVisible: 30,
    favorites: [],
    jobTags: {},

    taskBusy: false,
    taskProgress: 0,
    taskMessage: "",
    taskError: "",

    ws: null,
    wsState: "closed",
    _wsHeartbeat: null,
    _wsReconnectTimer: null,
    _pollTimer: null,
    _keyHandler: null,

    mobileNavOpen: false,
    mobileStudioOpen: false,
    showProviderSettings: false,
    isDesktop: false,
    isMaximized: false,
    providerDraft: emptyProviderDraft(),
    connectionTest: null,
    providerMessage: "",
    providerMessageType: "",
    testingProvider: false,
    savingProvider: false,

    showConversation: false,
    conversation: null,
    conversationSourceJob: null,
    conversationPrompt: "",
    conversationBusy: false,
    conversationError: "",

    lightboxUrl: "",
    lightboxIndex: 0,
    toasts: [],

    async init() {
      this.isDesktop = typeof window.pywebview !== "undefined";
      if (this.isDesktop) document.body.classList.add("desktop");
      // pywebview injects window.pywebview *after* the page has finished
      // loading, so re-check when it signals readiness (race otherwise).
      window.addEventListener("pywebviewready", () => {
        this.isDesktop = true;
        document.body.classList.add("desktop");
      });
      this.favorites = this.readLocalJson("ig:favorites", []);
      this.jobTags = this.readLocalJson("ig:jobTags", {});
      this.bindKeyboard();
      await this.loadProviders();
      if (!this.providers.length) this.openProviderSettings();
      await Promise.all([this.loadJobs(), this.loadModels()]);
      await this.loadCapabilities();
      this.restoreActiveJob();
      this.connectWs();
    },

    winMinimize() {
      window.pywebview?.api?.minimize();
    },

    async winToggleMaximize() {
      if (!window.pywebview?.api) return;
      this.isMaximized = await window.pywebview.api.toggle_maximize();
    },

    winClose() {
      window.pywebview?.api?.close();
    },

    winStartDrag(event) {
      const api = window.pywebview?.api;
      if (!api) return;
      event.preventDefault();
      try {
        event.currentTarget.setPointerCapture?.(event.pointerId);
      } catch (_) {}
      this._dragState = { lastX: event.screenX, lastY: event.screenY };
      const onMove = (ev) => {
        if (!this._dragState) return;
        const dx = ev.screenX - this._dragState.lastX;
        const dy = ev.screenY - this._dragState.lastY;
        this._dragState = { lastX: ev.screenX, lastY: ev.screenY };
        api.move_by(dx, dy);
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        this._dragState = null;
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },

    winStartResize(event, direction) {
      const api = window.pywebview?.api;
      if (!api) return;
      event.preventDefault();
      try {
        event.currentTarget.setPointerCapture?.(event.pointerId);
      } catch (_) {}
      this._resizeState = { lastX: event.screenX, lastY: event.screenY, edge: direction };
      const onMove = (ev) => {
        if (!this._resizeState) return;
        const dx = ev.screenX - this._resizeState.lastX;
        const dy = ev.screenY - this._resizeState.lastY;
        this._resizeState = { ...this._resizeState, lastX: ev.screenX, lastY: ev.screenY };
        api.resize_by(dx, dy, this._resizeState.edge);
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        this._resizeState = null;
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },

    dispose() {
      this._clearWsTimers();
      if (this.ws) {
        try {
          this.ws.close();
        } catch (_) {
          // Ignore shutdown races.
        }
      }
      if (this._keyHandler) window.removeEventListener("keydown", this._keyHandler);
      this.inputAssets.forEach((asset) => this.revokeObjectUrl(asset));
    },

    bindKeyboard() {
      this._keyHandler = (event) => {
        if (this.showMaskEditor && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
          event.preventDefault();
          if (event.shiftKey) this.redoMask();
          else this.undoMask();
          return;
        }
        const target = event.target;
        const typing =
          target instanceof HTMLInputElement ||
          target instanceof HTMLTextAreaElement ||
          target instanceof HTMLSelectElement ||
          target?.isContentEditable;
        if (!typing && event.key.toLowerCase() === "n") {
          event.preventDefault();
          this.newGeneration();
        }
      };
      window.addEventListener("keydown", this._keyHandler);
    },

    readLocalJson(key, fallback) {
      try {
        return JSON.parse(localStorage.getItem(key) || "") || fallback;
      } catch (_) {
        return fallback;
      }
    },

    notify(message, type = "success") {
      const toast = { id: `${Date.now()}-${Math.random()}`, message, type };
      this.toasts.push(toast);
      setTimeout(() => {
        this.toasts = this.toasts.filter((item) => item.id !== toast.id);
      }, 3500);
    },

    closeOverlays() {
      this.mobileNavOpen = false;
      this.mobileStudioOpen = false;
      this.showProviderSettings = false;
      this.showConversation = false;
      this.lightboxUrl = "";
    },

    truncate(text, max) {
      const value = String(text || "");
      return value.length > max ? `${value.slice(0, max)}…` : value;
    },

    modeLabel(mode) {
      return (
        {
          generate: "文生图",
          text: "文生图",
          reference: "参考合成",
          image: "参考合成",
          edit_mask: "局部编辑",
        }[mode] || "图像生成"
      );
    },

    modeIcon(mode) {
      return { generate: "✦", text: "✦", reference: "▧", image: "▧", edit_mask: "◐" }[
        mode
      ] || "◇";
    },

    statusLabel(status) {
      return (
        {
          queued: "排队中",
          preparing: "准备中",
          pending: "排队中",
          running: "生成中",
          streaming: "生成预览",
          saving: "保存中",
          succeeded: "已完成",
          done: "已完成",
          failed: "生成失败",
          error: "生成失败",
          cancel_requested: "正在取消",
          cancelled: "已取消",
        }[status] || status || "未知"
      );
    },

    qualityLabel(value) {
      return { auto: "自动", low: "快速草稿", medium: "标准", high: "高质量" }[value] || value;
    },

    backgroundLabel(value) {
      return { auto: "自动", opaque: "不透明", transparent: "透明" }[value] || value;
    },

    formatRelativeTime(value) {
      if (!value) return "";
      const time = new Date(value).getTime();
      if (!Number.isFinite(time)) return value;
      const delta = Math.max(0, Date.now() - time);
      const minute = 60 * 1000;
      const hour = 60 * minute;
      const day = 24 * hour;
      if (delta < minute) return "刚刚";
      if (delta < hour) return `${Math.floor(delta / minute)} 分钟前`;
      if (delta < day) return `${Math.floor(delta / hour)} 小时前`;
      if (delta < 7 * day) return `${Math.floor(delta / day)} 天前`;
      return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric" }).format(
        new Date(value),
      );
    },

    get matchingJobs() {
      const query = this.historyQuery.trim().toLowerCase();
      return this.jobs.filter((job) => {
        if (this.historyFilter === "running" && !RUNNING_JOB_STATES.has(job.status)) return false;
        if (this.historyFilter === "favorite" && !this.isFavorite(job.id)) return false;
        const snap = job.request_snapshot || {};
        const normalizedMode =
          snap.mode === "text" ? "generate" : snap.mode === "image" ? "reference" : snap.mode;
        if (this.historyModeFilter && normalizedMode !== this.historyModeFilter) return false;
        if (this.historyProviderFilter && job.provider_id !== this.historyProviderFilter) return false;
        if (!query) return true;
        return [
          snap.prompt,
          snap.model,
          job.provider_id,
          this.modeLabel(snap.mode),
          ...(this.jobTags[job.id] || []),
        ]
          .filter(Boolean)
          .some((value) => String(value).toLowerCase().includes(query));
      });
    },

    get filteredJobs() {
      return this.matchingJobs.slice(0, this.historyVisible);
    },

    get filteredJobCount() {
      return this.matchingJobs.length;
    },

    get runningJobsCount() {
      return this.jobs.filter((job) => RUNNING_JOB_STATES.has(job.status)).length;
    },

    get favoriteCount() {
      return this.jobs.filter((job) => this.isFavorite(job.id)).length;
    },

    setHistoryFilter(value) {
      this.historyFilter = value;
      this.historyVisible = 30;
      this.mobileNavOpen = false;
    },

    isFavorite(id) {
      return Boolean(id && this.favorites.includes(id));
    },

    toggleFavorite(id) {
      if (!id) return;
      this.favorites = this.isFavorite(id)
        ? this.favorites.filter((item) => item !== id)
        : [...this.favorites, id];
      localStorage.setItem("ig:favorites", JSON.stringify(this.favorites));
    },

    jobTagText(id) {
      return (this.jobTags[id] || []).join("、");
    },

    addTag(id) {
      if (!id) return;
      const value = prompt("输入标签名称（例如：产品、角色、海报）");
      const tag = String(value || "").trim().slice(0, 20);
      if (!tag) return;
      const current = this.jobTags[id] || [];
      if (!current.includes(tag)) this.jobTags = { ...this.jobTags, [id]: [...current, tag] };
      localStorage.setItem("ig:jobTags", JSON.stringify(this.jobTags));
    },

    async loadProviders() {
      try {
        const list = await apiRequest("/api/v1/providers");
        const publicItems = await Promise.all(
          (list.items || []).map(async (item) => {
            try {
              return await apiRequest(`/api/v1/providers/${encodeURIComponent(item.id)}`);
            } catch (_) {
              return item;
            }
          }),
        );
        this.providers = publicItems.filter((item) => !item.deleted);
        const active =
          this.providers.find((item) => item.is_active) ||
          this.providers.find((item) => item.enabled) ||
          this.providers[0];
        if (active) {
          if (!this.providerId || !this.providers.some((p) => p.id === this.providerId)) {
            this.providerId = active.id;
          }
          this.activeProvider = this.providers.find((p) => p.id === this.providerId) || active;
          if (!this.model) this.model = this.activeProvider.default_model || "gpt-image-2";
        } else {
          this.providerId = "";
          this.activeProvider = null;
        }
      } catch (error) {
        this.taskError = `无法读取连接配置：${error.message}`;
      }
    },

    async changeProvider() {
      this.activeProvider = this.providers.find((item) => item.id === this.providerId) || null;
      this.model = this.activeProvider?.default_model || "";
      try {
        await apiRequest(`/api/v1/providers/${encodeURIComponent(this.providerId)}/activate`, {
          method: "POST",
        });
        this.providers = this.providers.map((item) => ({
          ...item,
          is_active: item.id === this.providerId,
        }));
      } catch (error) {
        this.notify(error.message, "error");
      }
      await this.loadModels();
      await this.loadCapabilities();
    },

    async loadModels() {
      if (!this.providerId) {
        this.modelOptions = this.model ? [{ id: this.model }] : [];
        return;
      }
      this.loadingModels = true;
      try {
        const data = await apiRequest(
          `/api/v1/providers/${encodeURIComponent(this.providerId)}/models`,
        );
        this.modelOptions = (data.data || []).filter((item) => item?.id);
        if (this.model && !this.modelOptions.some((item) => item.id === this.model)) {
          this.modelOptions.unshift({ id: this.model, owned_by: "" });
        }
        if (!this.model && this.modelOptions.length) this.model = this.modelOptions[0].id;
      } catch (error) {
        const fallback = this.model || this.activeProvider?.default_model || "gpt-image-2";
        this.model = fallback;
        this.modelOptions = [{ id: fallback, owned_by: "" }];
        this.notify(`模型列表暂不可用：${error.message}`, "error");
      } finally {
        this.loadingModels = false;
      }
    },

    async loadCapabilities() {
      if (!this.model) {
        this.capabilities = null;
        return;
      }
      try {
        const query = this.providerId
          ? `?provider_id=${encodeURIComponent(this.providerId)}`
          : "";
        this.capabilities = await apiRequest(
          `/api/v1/models/${encodeURIComponent(this.model)}/capabilities${query}`,
        );
        this.normalizeOptionsToCapabilities();
      } catch (error) {
        this.capabilities = null;
        this.notify(`读取模型能力失败：${error.message}`, "error");
      }
    },

    normalizeOptionsToCapabilities() {
      if (!this.capabilities) return;
      if (!this.qualityOptions.includes(this.quality)) this.quality = this.qualityOptions[0] || "auto";
      if (!this.formatOptions.includes(this.outputFormat)) this.outputFormat = this.formatOptions[0] || "png";
      if (this.n > this.maxN) this.n = this.maxN;
      if (this.background && !this.backgroundOptions.includes(this.background)) this.background = "";
      if (!this.moderationOptions.includes(this.moderation)) {
        this.moderation = this.moderationOptions[0] || "auto";
      }
      if (!this.capabilities.supports_partial_images) this.partialImages = 0;
      if (this.partialImages > (this.capabilities.max_partial_images || 0)) {
        this.partialImages = this.capabilities.max_partial_images || 0;
      }
      if (this.mode === "reference" && !this.capabilities.multi_image_reference) {
        this.mode = this.capabilities.image_edit ? "reference" : "generate";
      }
      if (this.mode === "edit_mask" && !this.capabilities.mask_edit) this.mode = "generate";
    },

    get maxInputImages() {
      return Math.max(1, this.capabilities?.max_input_images || 1);
    },

    get maxN() {
      return Math.max(1, this.capabilities?.max_n || 1);
    },

    get nOptions() {
      return Array.from({ length: Math.min(this.maxN, 10) }, (_, index) => index + 1);
    },

    get partialImageOptions() {
      return Array.from(
        { length: this.capabilities?.max_partial_images || 0 },
        (_, index) => index + 1,
      );
    },

    get conversationAvailable() {
      return Boolean(
        this.activeProvider?.responses_enabled && this.activeProvider?.responses_model,
      );
    },

    get qualityOptions() {
      return this.capabilities?.qualities?.length
        ? this.capabilities.qualities
        : ["auto", "low", "medium", "high"];
    },

    get formatOptions() {
      return this.capabilities?.output_formats?.length
        ? this.capabilities.output_formats
        : ["png", "jpeg", "webp"];
    },

    get backgroundOptions() {
      return this.capabilities?.background_modes || [];
    },

    get moderationOptions() {
      return this.capabilities?.moderation_modes?.length
        ? this.capabilities.moderation_modes
        : ["auto"];
    },

    get inputHint() {
      if (this.mode === "edit_mask") return "第一张是被编辑主图，其余图片作为视觉参考";
      return "第一张为主参考，可拖动改变顺序";
    },

    get promptPlaceholder() {
      if (this.mode === "reference") return "例如：保留第一张图中的人物，使用第二张图的服装和第三张图的场景，合成一张自然的电影剧照…";
      if (this.mode === "edit_mask") return "例如：只把蒙版区域改成一扇通向森林的窗，保持其他区域完全不变…";
      return "例如：清晨薄雾中的现代玻璃住宅，室内暖光，建筑摄影，细腻真实的材质…";
    },

    get sizeValue() {
      if (this.sizePreset === "custom") return `${this.customWidth}x${this.customHeight}`;
      return this.sizePreset;
    },

    get sizeLabel() {
      return this.sizeValue === "auto" ? "自动尺寸" : this.sizeValue.replace("x", " × ");
    },

    syncSizePreset() {
      if (this.sizePreset !== "custom" && this.sizePreset !== "auto") {
        const [width, height] = this.sizePreset.split("x").map(Number);
        this.customWidth = width;
        this.customHeight = height;
      }
    },

    get validationMessage() {
      if (!this.providerId) return "请先在连接设置中创建并启用一个 Provider";
      if (!this.model) return "请选择生图模型";
      if (!this.prompt.trim()) return "请输入画面描述";
      if (this.mode !== "generate" && !this.inputAssets.length) return "请至少添加一张参考图片";
      if (this.mode === "edit_mask" && !this.maskAsset) return "局部编辑需要 PNG 蒙版";
      if (this.sizePreset === "custom") {
        const width = Number(this.customWidth);
        const height = Number(this.customHeight);
        if (!width || !height || width % 16 || height % 16) return "自定义宽高必须是 16 的倍数";
        if (Math.max(width, height) > 3840) return "最长边不能超过 3840 px";
        if (Math.max(width, height) / Math.min(width, height) > 3) return "长短边比例不能超过 3:1";
        const pixels = width * height;
        if (pixels < 655360 || pixels > 8294400) return "总像素必须在 655,360 到 8,294,400 之间";
      }
      return "";
    },

    get canSubmit() {
      return !this.taskBusy && !this.uploadingInputs && !this.uploadingMask && !this.validationMessage;
    },

    setMode(value) {
      if (value === "reference" && this.capabilities && !this.capabilities.multi_image_reference) {
        this.notify("当前模型不支持多图参考", "error");
        return;
      }
      if (value === "edit_mask" && this.capabilities && !this.capabilities.mask_edit) {
        this.notify("当前模型不支持蒙版编辑", "error");
        return;
      }
      this.mode = value;
      if (value !== "edit_mask") this.maskAsset = null;
    },

    usePrompt(value) {
      this.prompt = value;
      this.mobileStudioOpen = true;
    },

    appendPrompt(value) {
      this.prompt = this.prompt.trim() ? `${this.prompt.trim()}，${value}` : value;
    },

    clearPrompt() {
      this.prompt = "";
    },

    async copyPrompt() {
      try {
        await navigator.clipboard.writeText(this.prompt);
        this.notify("提示词已复制");
      } catch (_) {
        this.notify("浏览器未允许复制", "error");
      }
    },

    async uploadInputFiles(event) {
      const files = Array.from(event.target.files || []);
      event.target.value = "";
      const available = Math.max(0, this.maxInputImages - this.inputAssets.length);
      if (!files.length || !available) return;
      if (files.length > available) this.notify(`当前模型最多允许 ${this.maxInputImages} 张输入图`, "error");
      const selected = files.slice(0, available);
      this.uploadingInputs = true;
      try {
        const form = new FormData();
        selected.forEach((file) => form.append("files", file));
        const data = await apiRequest("/api/v1/assets?category=input", {
          method: "POST",
          body: form,
        });
        this.inputAssets = [...this.inputAssets, ...(data.items || [])];
        this.notify(`已添加 ${data.items?.length || 0} 张参考图片`);
      } catch (error) {
        this.notify(error.message, "error");
      } finally {
        this.uploadingInputs = false;
      }
    },

    async uploadMaskFile(event) {
      const file = event.target.files?.[0];
      event.target.value = "";
      if (!file || !this.inputAssets.length) return;
      this.uploadingMask = true;
      try {
        await this.uploadMaskBlob(file);
        this.notify("蒙版校验通过");
      } catch (error) {
        this.maskAsset = null;
        this.notify(error.message, "error");
      } finally {
        this.uploadingMask = false;
      }
    },

    async uploadMaskBlob(blob) {
      const primary = this.inputAssets[0];
      if (!primary) throw new Error("请先添加主图");
      const file =
        blob instanceof File
          ? blob
          : new File([blob], `mask-${Date.now()}.png`, { type: "image/png" });
      const validateForm = new FormData();
      validateForm.append("file", file);
      await apiRequest(
        `/api/v1/assets/validate-mask?width=${primary.width}&height=${primary.height}`,
        { method: "POST", body: validateForm },
      );
      const uploadForm = new FormData();
      uploadForm.append("files", file);
      const data = await apiRequest("/api/v1/assets?category=mask", {
        method: "POST",
        body: uploadForm,
      });
      this.maskAsset = data.items?.[0] || null;
      return this.maskAsset;
    },

    async openMaskCanvas() {
      if (!this.inputAssets.length) {
        this.notify("请先添加主图", "error");
        return;
      }
      this.showMaskEditor = true;
      this.maskTool = "paint";
      this.maskUndoStack = [];
      this.maskRedoStack = [];
      await this.$nextTick();
      const canvas = document.getElementById("mask-paint-canvas");
      const primary = this.inputAssets[0];
      canvas.width = primary.width;
      canvas.height = primary.height;
      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      if (this.maskAsset?.content_url) {
        try {
          const source = await this.loadCanvasImage(this.maskAsset.content_url);
          const scratch = document.createElement("canvas");
          scratch.width = canvas.width;
          scratch.height = canvas.height;
          const scratchCtx = scratch.getContext("2d", { willReadFrequently: true });
          scratchCtx.drawImage(source, 0, 0, canvas.width, canvas.height);
          const pixels = scratchCtx.getImageData(0, 0, canvas.width, canvas.height);
          for (let i = 0; i < pixels.data.length; i += 4) {
            pixels.data[i] = 215;
            pixels.data[i + 1] = 60;
            pixels.data[i + 2] = 82;
            pixels.data[i + 3] = 255 - pixels.data[i + 3];
          }
          ctx.putImageData(pixels, 0, 0);
        } catch (_) {
          this.notify("已有蒙版预览读取失败，已打开空白画布", "error");
        }
      }
      this.pushMaskHistory();
    },

    loadCanvasImage(url) {
      return new Promise((resolve, reject) => {
        const image = new Image();
        image.onload = () => resolve(image);
        image.onerror = reject;
        image.src = url;
      });
    },

    maskPoint(event) {
      const canvas = event.currentTarget;
      const rect = canvas.getBoundingClientRect();
      return {
        x: ((event.clientX - rect.left) / rect.width) * canvas.width,
        y: ((event.clientY - rect.top) / rect.height) * canvas.height,
      };
    },

    beginMaskStroke(event) {
      event.currentTarget.setPointerCapture(event.pointerId);
      this.maskDrawing = true;
      this.maskLastPoint = this.maskPoint(event);
      this.drawMaskStroke(event, true);
    },

    drawMaskStroke(event, dot = false) {
      if (!this.maskDrawing) return;
      const canvas = event.currentTarget;
      const point = this.maskPoint(event);
      const ctx = canvas.getContext("2d");
      ctx.save();
      ctx.globalCompositeOperation = this.maskTool === "erase" ? "destination-out" : "source-over";
      ctx.strokeStyle = "#d73c52";
      ctx.fillStyle = "#d73c52";
      ctx.lineWidth = Number(this.maskBrushSize);
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      if (dot) {
        ctx.beginPath();
        ctx.arc(point.x, point.y, Number(this.maskBrushSize) / 2, 0, Math.PI * 2);
        ctx.fill();
      } else {
        ctx.beginPath();
        ctx.moveTo(this.maskLastPoint.x, this.maskLastPoint.y);
        ctx.lineTo(point.x, point.y);
        ctx.stroke();
      }
      ctx.restore();
      this.maskLastPoint = point;
    },

    endMaskStroke(event) {
      if (!this.maskDrawing) return;
      this.drawMaskStroke(event);
      this.maskDrawing = false;
      this.maskLastPoint = null;
      this.pushMaskHistory();
    },

    pushMaskHistory() {
      const canvas = document.getElementById("mask-paint-canvas");
      if (!canvas) return;
      const snapshot = canvas.toDataURL("image/png");
      if (this.maskUndoStack.at(-1) === snapshot) return;
      this.maskUndoStack = [...this.maskUndoStack.slice(-19), snapshot];
      this.maskRedoStack = [];
    },

    async restoreMaskSnapshot(snapshot) {
      const canvas = document.getElementById("mask-paint-canvas");
      if (!canvas || !snapshot) return;
      const image = await this.loadCanvasImage(snapshot);
      const ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(image, 0, 0);
    },

    undoMask() {
      if (this.maskUndoStack.length <= 1) return;
      const current = this.maskUndoStack.at(-1);
      this.maskUndoStack = this.maskUndoStack.slice(0, -1);
      this.maskRedoStack = [...this.maskRedoStack, current];
      this.restoreMaskSnapshot(this.maskUndoStack.at(-1));
    },

    redoMask() {
      if (!this.maskRedoStack.length) return;
      const snapshot = this.maskRedoStack.at(-1);
      this.maskRedoStack = this.maskRedoStack.slice(0, -1);
      this.maskUndoStack = [...this.maskUndoStack, snapshot];
      this.restoreMaskSnapshot(snapshot);
    },

    clearMaskCanvas() {
      const canvas = document.getElementById("mask-paint-canvas");
      if (!canvas) return;
      canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
      this.pushMaskHistory();
    },

    invertMaskCanvas() {
      const canvas = document.getElementById("mask-paint-canvas");
      if (!canvas) return;
      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height);
      for (let i = 0; i < pixels.data.length; i += 4) {
        pixels.data[i] = 215;
        pixels.data[i + 1] = 60;
        pixels.data[i + 2] = 82;
        pixels.data[i + 3] = 255 - pixels.data[i + 3];
      }
      ctx.putImageData(pixels, 0, 0);
      this.pushMaskHistory();
    },

    async saveMaskCanvas() {
      const canvas = document.getElementById("mask-paint-canvas");
      if (!canvas) return;
      this.uploadingMask = true;
      try {
        const output = document.createElement("canvas");
        output.width = canvas.width;
        output.height = canvas.height;
        const ctx = output.getContext("2d");
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, output.width, output.height);
        ctx.globalCompositeOperation = "destination-out";
        ctx.drawImage(canvas, 0, 0);
        const blob = await new Promise((resolve, reject) =>
          output.toBlob((value) => (value ? resolve(value) : reject(new Error("蒙版导出失败"))), "image/png"),
        );
        await this.uploadMaskBlob(blob);
        this.showMaskEditor = false;
        this.notify("绘制蒙版已保存");
      } catch (error) {
        this.notify(error.message, "error");
      } finally {
        this.uploadingMask = false;
      }
    },

    revokeObjectUrl(asset) {
      if (asset?._objectUrl) URL.revokeObjectURL(asset._objectUrl);
    },

    removeInputAsset(index) {
      const [removed] = this.inputAssets.splice(index, 1);
      this.revokeObjectUrl(removed);
      this.inputAssets = [...this.inputAssets];
      if (index === 0 && this.maskAsset) {
        this.maskAsset = null;
        this.notify("主图已改变，请重新上传对应尺寸的蒙版", "error");
      }
    },

    removeMask() {
      this.maskAsset = null;
    },

    moveAsset(index, direction) {
      const next = index + direction;
      if (next < 0 || next >= this.inputAssets.length) return;
      const items = [...this.inputAssets];
      [items[index], items[next]] = [items[next], items[index]];
      this.inputAssets = items;
      if ((index === 0 || next === 0) && this.maskAsset) {
        this.maskAsset = null;
        this.notify("主图顺序已改变，请重新上传蒙版", "error");
      }
    },

    dropAsset(index) {
      if (this.dragAssetIndex === null || this.dragAssetIndex === index) return;
      const items = [...this.inputAssets];
      const [moved] = items.splice(this.dragAssetIndex, 1);
      items.splice(index, 0, moved);
      const primaryChanged = this.dragAssetIndex === 0 || index === 0;
      this.inputAssets = items;
      this.dragAssetIndex = null;
      if (primaryChanged && this.maskAsset) {
        this.maskAsset = null;
        this.notify("主图顺序已改变，请重新上传蒙版", "error");
      }
    },

    async submitGeneration() {
      if (!this.canSubmit) {
        if (this.validationMessage) this.notify(this.validationMessage, "error");
        return;
      }
      this.taskBusy = true;
      this.taskError = "";
      this.taskProgress = 0.04;
      this.taskMessage = "正在提交任务…";
      this.previewUrls = [];
      this.partialPreviewUrls = [];
      const body = {
        provider_id: this.providerId,
        mode: this.mode,
        prompt: this.prompt.trim(),
        model: this.model,
        input_asset_ids: this.mode === "generate" ? [] : this.inputAssets.map((asset) => asset.id),
        primary_asset_id: this.mode === "generate" ? null : this.inputAssets[0]?.id || null,
        mask_asset_id: this.mode === "edit_mask" ? this.maskAsset?.id || null : null,
        size: this.sizeValue,
        quality: this.quality,
        n: Number(this.n),
        output_format: this.outputFormat,
        output_compression:
          this.outputFormat === "jpeg" || this.outputFormat === "webp"
            ? Number(this.outputCompression)
            : null,
        background: this.background || null,
        moderation: this.moderation,
        partial_images: this.capabilities?.supports_partial_images ? Number(this.partialImages) : null,
      };
      try {
        const data = await apiRequest("/api/v1/generations", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        this.activeTaskId = data.task_id;
        this.taskMessage = data.message || "任务已排队";
        this.taskProgress = data.progress || 0.05;
        localStorage.setItem("ig:activeJob", this.activeTaskId);
        await this.refreshActiveJob();
        await this.loadJobs();
        this.mobileStudioOpen = false;
      } catch (error) {
        this.taskBusy = false;
        this.taskError = error.message;
      }
    },

    _clearWsTimers() {
      if (this._wsHeartbeat) clearInterval(this._wsHeartbeat);
      if (this._wsReconnectTimer) clearTimeout(this._wsReconnectTimer);
      if (this._pollTimer) clearInterval(this._pollTimer);
      this._wsHeartbeat = null;
      this._wsReconnectTimer = null;
      this._pollTimer = null;
    },

    connectWs() {
      this._clearWsTimers();
      if (this.ws) {
        this.ws.onclose = null;
        try {
          this.ws.close();
        } catch (_) {
          // Ignore stale socket.
        }
      }
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      try {
        this.ws = new WebSocket(`${protocol}://${location.host}/ws`);
      } catch (_) {
        this.startPolling();
        return;
      }
      this.ws.onopen = () => {
        this.wsState = "open";
        this._wsHeartbeat = setInterval(() => {
          if (this.ws?.readyState === WebSocket.OPEN) this.ws.send("ping");
        }, 25000);
      };
      this.ws.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data);
          if (message.type === "task") this.handleTaskEvent(message.payload);
          if (message.type === "job.partial_image") this.handlePartialImage(message.payload);
        } catch (_) {
          // Ignore malformed event.
        }
      };
      this.ws.onerror = () => {
        this.wsState = "error";
      };
      this.ws.onclose = () => {
        this.wsState = "closed";
        if (this._wsHeartbeat) clearInterval(this._wsHeartbeat);
        this._wsHeartbeat = null;
        this.startPolling();
        this._wsReconnectTimer = setTimeout(() => this.connectWs(), 2500);
      };
    },

    startPolling() {
      if (this._pollTimer) return;
      this._pollTimer = setInterval(() => {
        if (this.activeTaskId) this.refreshActiveJob();
      }, 2500);
    },

    async handleTaskEvent(payload) {
      if (!payload?.task_id) return;
      const relevant = payload.task_id === this.activeTaskId;
      if (relevant) {
        this.taskProgress = payload.progress || 0;
        this.taskMessage = payload.message || "";
        if (payload.status === "done" || payload.status === "error") {
          await this.refreshActiveJob();
        }
      }
      if (payload.status === "done" || payload.status === "error") await this.loadJobs();
    },

    handlePartialImage(payload) {
      if (!payload?.job_id || payload.job_id !== this.activeTaskId || !payload.url) return;
      const items = [...this.partialPreviewUrls];
      items[Number(payload.partial_image_index) || 0] = payload.url;
      this.partialPreviewUrls = items.filter(Boolean);
      this.taskMessage = `已收到 ${this.partialPreviewUrls.length} 张渐进预览`;
    },

    async refreshActiveJob() {
      if (!this.activeTaskId) return;
      try {
        const job = await apiRequest(`/api/v1/jobs/${encodeURIComponent(this.activeTaskId)}`);
        this.activeJob = job;
        this.taskProgress = job.progress || 0;
        this.taskMessage = job.message || "";
        this.taskBusy = RUNNING_JOB_STATES.has(job.status);
        if (this.taskBusy) this.partialPreviewUrls = job.partial_urls || [];
        if (job.status === "succeeded") {
          this.previewUrls = job.output_urls || [];
          this.partialPreviewUrls = [];
          this.selectedJob = job;
          this.taskError = "";
          this.finishActiveJob();
          this.notify("图像生成完成");
        } else if (job.status === "failed" || job.status === "cancelled") {
          this.partialPreviewUrls = [];
          this.taskError = job.error || job.message || this.statusLabel(job.status);
          this.finishActiveJob();
        }
      } catch (error) {
        if (error.status === 404) this.finishActiveJob();
      }
    },

    finishActiveJob() {
      this.taskBusy = false;
      this.activeTaskId = "";
      localStorage.removeItem("ig:activeJob");
      this.loadJobs();
    },

    restoreActiveJob() {
      const stored = localStorage.getItem("ig:activeJob");
      const running = this.jobs.find((job) => job.id === stored && RUNNING_JOB_STATES.has(job.status));
      const fallback = this.jobs.find((job) => RUNNING_JOB_STATES.has(job.status));
      const job = running || fallback;
      if (job) {
        this.activeTaskId = job.id;
        this.activeJob = job;
        this.taskBusy = true;
        this.taskProgress = job.progress || 0;
        this.taskMessage = job.message || "";
        this.partialPreviewUrls = job.partial_urls || [];
        localStorage.setItem("ig:activeJob", job.id);
      } else {
        localStorage.removeItem("ig:activeJob");
      }
    },

    async cancelActiveJob() {
      if (!this.activeTaskId) return;
      try {
        await apiRequest(`/api/v1/jobs/${encodeURIComponent(this.activeTaskId)}/cancel`, {
          method: "POST",
        });
        this.taskMessage = "取消请求已发送…";
        await this.refreshActiveJob();
      } catch (error) {
        this.notify(error.message, "error");
      }
    },

    async loadJobs() {
      this.loadingJobs = true;
      try {
        const data = await apiRequest("/api/v1/jobs?limit=200");
        this.jobs = data.items || [];
        if (this.selectedJob) {
          const updated = this.jobs.find((job) => job.id === this.selectedJob.id);
          if (updated) this.selectedJob = updated;
        }
      } catch (error) {
        this.notify(`读取历史失败：${error.message}`, "error");
      } finally {
        this.loadingJobs = false;
      }
    },

    selectJob(job) {
      this.selectedJob = job;
      this.previewUrls = job.output_urls || [];
      this.partialPreviewUrls = [];
      this.taskError = job.status === "failed" ? job.error || "生成失败" : "";
      this.mobileNavOpen = false;
    },

    newGeneration() {
      this.selectedJob = null;
      this.previewUrls = [];
      this.partialPreviewUrls = [];
      this.taskError = "";
      this.prompt = "";
      this.mode = "generate";
      this.inputAssets = [];
      this.maskAsset = null;
      this.mobileNavOpen = false;
      if (window.innerWidth < 980) this.mobileStudioOpen = true;
    },

    async continueFromResult(url) {
      try {
        this.mobileStudioOpen = true;
        this.mode = "reference";
        const response = await fetch(url);
        if (!response.ok) throw new Error("读取结果图片失败");
        const blob = await response.blob();
        const extension = blob.type === "image/jpeg" ? "jpg" : blob.type === "image/webp" ? "webp" : "png";
        const file = new File([blob], `result-reference.${extension}`, { type: blob.type });
        const form = new FormData();
        form.append("files", file);
        const data = await apiRequest("/api/v1/assets?category=input", {
          method: "POST",
          body: form,
        });
        this.inputAssets = data.items?.length ? [data.items[0]] : [];
        this.maskAsset = null;
        this.prompt = "";
        this.selectedJob = null;
        this.previewUrls = [];
        this.notify("结果已加入为参考图");
      } catch (error) {
        this.notify(error.message, "error");
      }
    },

    async openConversation(job = this.selectedJob) {
      if (!job || !job.output_urls?.length) {
        this.notify("请选择一条包含图片的历史结果", "error");
        return;
      }
      const conversationId = job.request_snapshot?.conversation_id;
      this.showConversation = true;
      this.conversationSourceJob = job;
      this.conversationPrompt = "";
      this.conversationError = "";
      this.conversation = null;
      if (conversationId) {
        try {
          this.conversation = await apiRequest(
            `/api/v1/conversations/${encodeURIComponent(conversationId)}`,
          );
        } catch (error) {
          this.conversationError = error.message;
        }
      } else if (!this.conversationAvailable) {
        this.conversationError = "当前 Provider 未启用 Responses API，请先在连接设置中配置";
      }
    },

    async submitConversationTurn() {
      const prompt = this.conversationPrompt.trim();
      if (!prompt || this.conversationBusy || this.conversationError) return;
      this.conversationBusy = true;
      try {
        const continuing = Boolean(this.conversation?.id);
        const path = continuing
          ? `/api/v1/conversations/${encodeURIComponent(this.conversation.id)}/turns`
          : "/api/v1/conversations";
        const body = continuing
          ? { prompt, action: "edit" }
          : {
              provider_id: this.providerId,
              responses_model: this.activeProvider?.responses_model,
              source_job_id: this.conversationSourceJob?.id,
              prompt,
              action: "edit",
            };
        this.conversation = await apiRequest(path, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        this.conversationPrompt = "";
        const latest = this.conversation.turns?.at(-1);
        if (latest?.output_urls?.length) {
          this.previewUrls = latest.output_urls;
          await this.loadJobs();
          this.selectedJob = this.jobs.find((job) => job.id === latest.job_id) || this.selectedJob;
        }
        this.notify("多轮编辑完成");
      } catch (error) {
        this.conversationError = error.message;
      } finally {
        this.conversationBusy = false;
      }
    },

    conversationUsage(turn) {
      const usage = turn?.usage || {};
      const input = usage.input_tokens ?? usage.input_tokens_details?.total_tokens;
      const output = usage.output_tokens ?? usage.output_tokens_details?.total_tokens;
      if (input == null && output == null) return "用量由上游 Provider 结算";
      return `主模型 Token：输入 ${input || 0} · 输出 ${output || 0}`;
    },

    openLightbox(url, index) {
      this.lightboxUrl = url;
      this.lightboxIndex = index;
    },

    downloadAll() {
      this.previewUrls.forEach((url, index) => {
        setTimeout(() => {
          const link = document.createElement("a");
          link.href = url;
          link.download = `image-${index + 1}`;
          document.body.appendChild(link);
          link.click();
          link.remove();
        }, index * 250);
      });
    },

    async deleteSelectedJob() {
      if (!this.selectedJob) return;
      if (!confirm("确认删除这条记录及其本地输出图片？此操作不可撤销。")) return;
      try {
        await apiRequest(`/api/v1/jobs/${encodeURIComponent(this.selectedJob.id)}`, {
          method: "DELETE",
        });
        this.toggleFavoriteIfPresent(this.selectedJob.id);
        if (this.jobTags[this.selectedJob.id]) {
          const nextTags = { ...this.jobTags };
          delete nextTags[this.selectedJob.id];
          this.jobTags = nextTags;
          localStorage.setItem("ig:jobTags", JSON.stringify(this.jobTags));
        }
        this.selectedJob = null;
        this.previewUrls = [];
        await this.loadJobs();
        this.notify("记录已删除");
      } catch (error) {
        this.notify(error.message, "error");
      }
    },

    toggleFavoriteIfPresent(id) {
      if (!this.isFavorite(id)) return;
      this.favorites = this.favorites.filter((item) => item !== id);
      localStorage.setItem("ig:favorites", JSON.stringify(this.favorites));
    },

    async retrySelectedJob() {
      if (!this.selectedJob) return;
      try {
        const data = await apiRequest(
          `/api/v1/jobs/${encodeURIComponent(this.selectedJob.id)}/retry`,
          { method: "POST" },
        );
        this.activeTaskId = data.task_id;
        this.taskBusy = true;
        this.taskError = "";
        this.taskMessage = data.message || "任务已重新排队";
        this.taskProgress = data.progress || 0.05;
        localStorage.setItem("ig:activeJob", this.activeTaskId);
        await this.loadJobs();
      } catch (error) {
        this.notify(error.message, "error");
      }
    },

    openProviderSettings() {
      this.showProviderSettings = true;
      this.connectionTest = null;
      this.providerMessage = "";
      const current = this.providers.find((item) => item.id === this.providerId);
      if (current) this.editProvider(current);
      else this.newProviderDraft();
    },

    async editProvider(provider) {
      try {
        const detail = await apiRequest(`/api/v1/providers/${encodeURIComponent(provider.id)}`);
        this.providerDraft = {
          ...emptyProviderDraft(),
          ...detail,
          token_distributor: { ...defaultDistributor(), ...(detail.token_distributor || {}) },
          api_key: "",
          distributor_client_id: "",
          distributor_client_secret: "",
        };
        this.connectionTest = null;
        this.providerMessage = "";
      } catch (error) {
        this.providerMessage = error.message;
        this.providerMessageType = "error";
      }
    },

    newProviderDraft() {
      this.providerDraft = emptyProviderDraft();
      this.connectionTest = null;
      this.providerMessage = "";
    },

    providerPayload() {
      const draft = this.providerDraft;
      const body = {
        name: draft.name.trim(),
        provider_type: draft.provider_type,
        base_url: draft.base_url.trim().replace(/\/+$/, ""),
        auth_type: draft.auth_type,
        default_model: draft.default_model.trim(),
        enabled: Boolean(draft.enabled),
        verify_tls: Boolean(draft.verify_tls),
        timeout_seconds: Number(draft.timeout_seconds || 300),
        extra_headers: draft.extra_headers || {},
        capability_overrides: draft.capability_overrides || {},
        responses_enabled: Boolean(draft.responses_enabled),
        responses_model: draft.responses_enabled ? draft.responses_model.trim() : "",
        token_distributor:
          draft.auth_type === "token_distributor"
            ? { ...defaultDistributor(), ...(draft.token_distributor || {}) }
            : null,
      };
      for (const key of ["api_key", "distributor_client_id", "distributor_client_secret"]) {
        if (draft[key]?.trim()) body[key] = draft[key].trim();
      }
      return body;
    },

    async saveProvider() {
      this.savingProvider = true;
      this.providerMessage = "";
      this.providerMessageType = "";
      try {
        const isNew = !this.providerDraft.id;
        const body = this.providerPayload();
        const saved = isNew
          ? await apiRequest("/api/v1/providers", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
            })
          : await apiRequest(
              `/api/v1/providers/${encodeURIComponent(this.providerDraft.id)}`,
              {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
              },
            );
        await this.loadProviders();
        const targetId = saved.id || this.providerDraft.id;
        const target = this.providers.find((item) => item.id === targetId);
        if (target) await this.editProvider(target);
        if (isNew) {
          // 新建连接后立即切换并激活，避免仍停留在空的 Default 上。
          this.providerId = targetId;
          await this.changeProvider();
        } else if (this.providerId === targetId) {
          // 编辑的是当前激活连接：改了默认模型则同步主界面的模型选择，
          // 再刷新模型列表与能力。
          if (target?.default_model && target.default_model !== this.model) {
            this.model = target.default_model;
          }
          await this.loadModels();
          await this.loadCapabilities();
        }
        this.providerMessage = "连接已保存";
        this.notify("连接配置已保存");
      } catch (error) {
        this.providerMessage = error.message;
        this.providerMessageType = "error";
      } finally {
        this.savingProvider = false;
      }
    },

    async testProvider() {
      if (!this.providerDraft.id) {
        this.providerMessage = "请先保存连接，再测试";
        this.providerMessageType = "error";
        return;
      }
      this.testingProvider = true;
      this.connectionTest = null;
      this.providerMessage = "";
      try {
        this.connectionTest = await apiRequest(
          `/api/v1/providers/${encodeURIComponent(this.providerDraft.id)}/test`,
          { method: "POST" },
        );
        this.providerMessage = this.connectionTest.ok ? "连接测试通过" : "部分测试未通过";
        this.providerMessageType = this.connectionTest.ok ? "" : "error";
      } catch (error) {
        this.providerMessage = error.message;
        this.providerMessageType = "error";
      } finally {
        this.testingProvider = false;
      }
    },

    async deleteProvider() {
      if (!this.providerDraft.id) return;
      if (!confirm(`确认删除连接“${this.providerDraft.name}”？历史任务不会被删除。`)) return;
      try {
        await apiRequest(`/api/v1/providers/${encodeURIComponent(this.providerDraft.id)}`, {
          method: "DELETE",
        });
        await this.loadProviders();
        if (this.providers.length) await this.editProvider(this.providers[0]);
        else this.newProviderDraft();
        await this.changeProvider();
        this.notify("连接已删除");
      } catch (error) {
        this.providerMessage = error.message;
        this.providerMessageType = "error";
      }
    },
  };
}
