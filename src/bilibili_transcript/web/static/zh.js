/* Presentation-only translations: API values and saved user content stay unchanged. */
const statusNames = {
  queued: "排队中",
  running: "运行中",
  pending: "待处理",
  completed: "已完成",
  failed: "失败",
  paused: "已暂停",
  pausing: "正在暂停",
  cancelled: "已取消",
  cancelling: "正在取消",
};
const actionNames = {
  pause: "暂停",
  resume: "继续",
  cancel: "取消",
  retry: "重试",
};
const optionNames = {
  auto: "自动",
  default: "默认",
  cpu: "CPU（处理器）",
  cuda: "CUDA（显卡）",
};
const configLabels = {
  downloader: "下载器",
  timeout: "超时（秒）",
  retries: "重试次数",
  parser: "解析器",
  model: "模型",
  device: "计算设备",
  compute_type: "计算精度",
  language: "识别语言",
  beam_size: "束搜索宽度",
  vad_filter: "跳过静音片段",
  output: "输出",
  formats: "文件格式",
  timestamps: "文本时间戳",
  collection: "合集",
  url: "链接",
  name: "名称",
};
function collectionName(collection) {
  return collection?.id === 1 ? "通用" : collection?.name || "";
}
function configZh(value) {
  if (Array.isArray(value)) return value;
  if (typeof value === "boolean") return value ? "启用" : "关闭";
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        configLabels[key] || key,
        configZh(item),
      ]),
    );
  return value;
}
const messagesZh = {
  Complete: "已完成",
  Paused: "已暂停",
  Cancelled: "已取消",
  "Dry run complete — no resources changed": "试运行完成，未修改任何资源",
  "Interrupted; resume to retry": "任务已中断，请继续任务以重试",
  "Cancelled by user": "已由用户取消",
  "Worker started.": "工作进程已启动。",
  "Job queued with a snapshot of current settings.":
    "任务已加入队列，并保存当前配置快照。",
  "Pause requested.": "已请求暂停。",
  "Resume requested.": "已请求继续。",
  "Cancel requested.": "已请求取消。",
  "Retry requested.": "已请求重试。",
  "Fetching collection pages…": "正在获取合集页面…",
  "Converting audio…": "正在转换音频…",
  "Loading model (first use downloads model weights)…":
    "正在加载模型（首次使用需下载模型权重）…",
  "reuse audio": "复用已有音频",
  "download audio": "下载音频",
  "reuse transcript": "复用已有转录数据",
  "transcribe audio": "将音频转录为文字",
  "blocked: download audio first": "无法执行：请先下载音频",
  "blocked: parse audio first": "无法执行：请先解析音频",
  "Fetch collection pages (network required on actual run)":
    "获取合集页面（实际执行时需要联网）",
  "Deduplicate video URLs and add collection membership":
    "去除重复的视频链接，并将资源加入合集",
  "Download this resource first, or choose Full pipeline.":
    "请先下载此资源，或选择完整流程。",
  "Parse this resource before exporting.": "请先解析此资源，再运行导出。",
  "Job not found": "任务不存在",
  "Resource not found": "资源不存在",
  "File not found": "文件不存在",
  "Unknown action": "未知操作",
  "Unknown file category": "未知文件类型",
  "File missing; rerun the appropriate stage": "文件缺失，请重新运行对应阶段",
  "No worker log yet.": "暂无工作进程日志。",
  "Failed to fetch": "网络连接失败，请检查本地服务是否运行",
  "Cross-origin writes are not allowed": "不允许来自其他网站的修改请求",
  "Use a full Bilibili video URL (https://www.bilibili.com/video/BV…).":
    "请填写完整的哔哩哔哩视频链接（https://www.bilibili.com/video/BV…）。",
  "Use a Bilibili collection URL: https://space.bilibili.com/USER/lists/ID?type=series (or season).":
    "请填写哔哩哔哩合集链接：https://space.bilibili.com/用户ID/lists/合集ID?type=series（或 season）。",
  "Collection type must be series or season.":
    "合集类型必须为 series 或 season。",
  "Use auto or a language code such as zh, en, ja.":
    "请填写 auto 或语言代码，例如 zh、en、ja。",
};
function messageZh(value) {
  if (!value) return "";
  const text = String(value).replace(/^Value error, /, "");
  if (messagesZh[text]) return messagesZh[text];
  const stage = (key) =>
    ({ download: "下载", parse: "解析", export: "导出", discover: "合集解析" })[
      key.toLowerCase()
    ] || key;
  const patterns = [
    [/^Starting (\w+)…$/, (_, s) => `正在开始${stage(s)}…`],
    [/^(Download|Parse|Export) complete$/, (_, s) => `${stage(s)}完成`],
    [/^Resolved (\d+) videos$/, (_, n) => `已解析 ${n} 个视频`],
    [
      /^Downloaded ([\d.]+) MB(?: of ([\d.]+) MB)?$/,
      (_, n, total) => `已下载 ${n} MB${total ? `，共 ${total} MB` : ""}`,
    ],
    [
      /^Part (\d+)\/(\d+) · (\d+)s transcribed$/,
      (_, part, total, seconds) =>
        `第 ${part}/${total} 个分段 · 已转录 ${seconds} 秒`,
    ],
    [
      /^Exported part (\d+)\/(\d+)$/,
      (_, part, total) => `已导出第 ${part}/${total} 个分段`,
    ],
    [
      /^Finished with (\d+) failed item\(s\)\.$/,
      (_, n) => `处理结束，${n} 项失败。`,
    ],
    [
      /^Worker exited with code (.+)\. See item log\.$/,
      (_, code) => `工作进程已退出，退出码 ${code}。请查看日志。`,
    ],
    [/^Resource (\d+) no longer exists\.$/, (_, id) => `资源 ${id} 已不存在。`],
    [
      /^Cannot (\w+) a (\w+) job\.$/,
      (_, action, state) =>
        `无法${actionNames[action] || action}状态为“${statusNames[state] || state}”的任务。`,
    ],
    [
      /^Item (\d+) failed: (.*)$/s,
      (_, id, error) => `第 ${id} 项失败：${messageZh(error)}`,
    ],
    [
      /^Worker error: (.*)$/s,
      (_, error) => `工作进程错误：${messageZh(error)}`,
    ],
    [
      /^(.+): (download|parse|export)$/,
      (_, title, s) => `${title}：${stage(s)}`,
    ],
    [/^export (.+)$/, (_, formats) => `导出 ${formats.toUpperCase()}`],
  ];
  for (const [pattern, translate] of patterns)
    if (pattern.test(text)) return text.replace(pattern, translate);
  return text;
}
function validationZh(error) {
  const field = [...(error.loc || [])]
    .reverse()
    .find((x) => typeof x === "string" && x !== "body");
  const labels = {
    ...configLabels,
    urls: "视频链接",
    resource_ids: "所选资源",
    kind: "任务类型",
  };
  const label = labels[field] || "输入内容";
  const ctx = error.ctx || {};
  const messages = {
    missing: "不能为空",
    too_short: `至少需要 ${ctx.min_length || 1} 项`,
    too_long: `最多支持 ${ctx.max_length} 项`,
    string_too_short: `至少需要 ${ctx.min_length} 个字符`,
    string_too_long: `最多支持 ${ctx.max_length} 个字符`,
    greater_than_equal: `不能小于 ${ctx.ge}`,
    less_than_equal: `不能大于 ${ctx.le}`,
    int_parsing: "请填写整数",
    int_type: "请填写整数",
    literal_error: "请选择有效选项",
  };
  return messages[error.type]
    ? `${label}：${messages[error.type]}`
    : messageZh(error.msg);
}
