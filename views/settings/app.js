const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const byId = (id) => document.getElementById(id);
const form = byId("settings-form");
const fields = byId("fields");
const timeInput = byId("inform-time");
const adminInput = byId("admin-users");
const intervalInput = byId("check-interval");
const testForm = byId("test-push-form");
const testGroupInput = byId("test-group");
const testButton = byId("test-push");
let savedSettings = null;
let busy = false;
let testing = false;
let testGroups = new Map();

function groupLabel(group) {
  return `${group.group_name || "未知群名"}:${group.group_id}`;
}

function updateTestState() {
  testGroupInput.disabled = testing || testGroups.size === 0;
  testButton.disabled = testing || !savedSettings || !testGroups.get(testGroupInput.value)?.available;
}

function parseAdmins() {
  return [...new Set(adminInput.value.split(/[\s,，;；]+/u).filter(Boolean))];
}

function formSettings() {
  const [hour, minute] = timeInput.value.split(":").map(Number);
  return {
    inform_hour: hour,
    inform_minute: minute,
    check_interval_seconds: intervalInput.valueAsNumber,
    admin_users: parseAdmins(),
  };
}

function sameSettings(left, right) {
  return left && right && left.inform_hour === right.inform_hour &&
    left.inform_minute === right.inform_minute &&
    left.check_interval_seconds === right.check_interval_seconds &&
    JSON.stringify([...left.admin_users].sort()) === JSON.stringify([...right.admin_users].sort());
}

function updateFormState() {
  byId("time-preview").textContent = timeInput.value || "--:--";
  byId("admin-count").textContent = `${parseAdmins().length} 人`;
  const dirty = savedSettings && !sameSettings(formSettings(), savedSettings);
  byId("save").disabled = busy || !dirty;
  byId("reset").disabled = busy || !dirty;
  byId("save-state").textContent = busy ? "正在保存…" : dirty ? "有未保存的修改" : savedSettings ? "配置已同步" : "尚未连接";
}

function feedback(message, error = false) {
  const element = byId("feedback");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.hidden = !message;
}

function setForm(settings) {
  savedSettings = { ...settings, admin_users: [...settings.admin_users] };
  timeInput.value = `${String(settings.inform_hour).padStart(2, "0")}:${String(settings.inform_minute).padStart(2, "0")}`;
  adminInput.value = settings.admin_users.join("\n");
  intervalInput.value = settings.check_interval_seconds;
  updateFormState();
}

function serverDate(value) {
  // Preserve the server's wall clock instead of converting to the viewer's timezone.
  return value ? value.slice(0, 19).replace("T", " ") : "—";
}

function renderRuntime(runtime) {
  byId("monitor-status").textContent = runtime.monitor_running ? "定时任务运行中" : "定时任务未运行";
  byId("monitor-dot").classList.toggle("active", runtime.monitor_running);
  byId("next-run").textContent = serverDate(runtime.next_run);
  byId("server-time").textContent = serverDate(runtime.server_time);
  const offset = runtime.server_time?.match(/([+-]\d{2}:\d{2}|Z)$/)?.[0] || "";
  byId("server-timezone").textContent = `${runtime.timezone || "本地时区"}${offset ? ` · UTC${offset === "Z" ? "+00:00" : offset}` : ""}`;
  const groups = runtime.subscribed_groups;
  const selectedGroup = testGroupInput.value;
  testGroups = new Map((runtime.test_groups || []).map((group) => [group.group_id, group]));
  byId("group-count").textContent = String(groups.length);
  byId("empty-groups").hidden = groups.length > 0;
  byId("group-list").replaceChildren(...groups.map((id) => {
    const item = document.createElement("li");
    item.textContent = groupLabel(testGroups.get(String(id)) || { group_id: id });
    return item;
  }));
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = testGroups.size ? "请选择目标群组" : "暂无群组，请先在群内发送 /lc列表";
  placeholder.disabled = true;
  testGroupInput.replaceChildren(placeholder, ...[...testGroups.values()].map((group) => {
    const option = document.createElement("option");
    option.value = group.group_id;
    option.textContent = `${groupLabel(group)}${group.available ? "" : "（会话待更新）"}`;
    option.disabled = !group.available;
    return option;
  }));
  testGroupInput.value = testGroups.get(selectedGroup)?.available ? selectedGroup : "";
  updateTestState();
}

form.addEventListener("input", (event) => {
  if (event.target.form !== form) return;
  feedback("");
  updateFormState();
});

function testFeedback(message, error = false) {
  const element = byId("test-result");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.hidden = !message;
}

testGroupInput.addEventListener("change", () => {
  testFeedback("");
  updateTestState();
});

testForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (testing || !savedSettings || !testForm.reportValidity()) return;
  const groupId = testGroupInput.value.trim();
  if (!testGroups.get(groupId)?.available) {
    testFeedback("请先选择已记录且可测试的群组。", true);
    testGroupInput.focus();
    return;
  }
  testing = true;
  testButton.disabled = true;
  testGroupInput.disabled = true;
  testButton.textContent = "正在发送…";
  testFeedback("正在获取今日题目并发送，请稍候…");
  try {
    const result = await bridge.apiPost("test-push", { group_id: groupId });
    testFeedback(`已向 ${groupLabel(result)} 提交测试推送：${result.question_title}`);
  } catch (error) {
    testFeedback(error.message || "测试推送失败，请稍后重试。", true);
  } finally {
    testing = false;
    testButton.textContent = "发送测试题目";
    updateTestState();
  }
});

form.addEventListener("invalid", (event) => {
  // Keep validation errors reachable even when advanced settings were collapsed.
  const details = event.target.closest("details");
  if (details) details.open = true;
}, true);

byId("reset").addEventListener("click", () => {
  setForm(savedSettings);
  feedback("");
});

byId("refresh").addEventListener("click", async () => {
  byId("refresh").disabled = true;
  try {
    const result = await bridge.apiGet("settings");
    renderRuntime(result.runtime);
    feedback("运行状态已刷新，表单中的修改已保留。");
  } catch (error) {
    feedback(`刷新失败：${error.message || "请稍后重试"}`, true);
  } finally {
    byId("refresh").disabled = false;
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!savedSettings || busy || !form.reportValidity()) return;
  const settings = formSettings();
  if (settings.admin_users.some((id) => id.length > 128)) {
    feedback("管理员 ID 不能超过 128 个字符。", true);
    adminInput.focus();
    return;
  }
  busy = true;
  fields.disabled = true;
  feedback("");
  updateFormState();
  try {
    const result = await bridge.apiPost("settings", settings);
    setForm(result.settings);
    renderRuntime(result.runtime);
    feedback("配置已保存并立即生效。");
  } catch (error) {
    feedback(`保存失败：${error.message || "请稍后重试"}。修改内容已保留。`, true);
  } finally {
    busy = false;
    fields.disabled = false;
    updateFormState();
  }
});

try {
  if (!bridge) throw new Error("请从 AstrBot 插件详情中打开「每日一题设置」视图");
  await bridge.ready();
  const result = await bridge.apiGet("settings");
  setForm(result.settings);
  renderRuntime(result.runtime);
  fields.disabled = false;
  updateTestState();
  byId("connection").textContent = "已连接 AstrBot";
} catch (error) {
  byId("connection").textContent = "连接失败";
  byId("save-state").textContent = "配置加载失败";
  byId("save-hint").textContent = "请检查 AstrBot 日志，处理后重新打开此视图。";
  feedback(error.message || "无法读取配置，请重新打开此视图。", true);
}
