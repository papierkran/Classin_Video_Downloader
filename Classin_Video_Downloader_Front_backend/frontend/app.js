/* ClassIn 视频下载管理系统 - 前端逻辑
 * 技术栈：Vue3 (Composition API in Options-api wrapper) + Element Plus
 * 数据流：所有请求走 api() 统一封装（自动带 token、统一错误弹窗、401 跳登录）
 * 轮询策略：任务详情/日志在抽屉打开且任务活跃时 2s 轮询一次（不用 WebSocket，
 *           避免引入连接管理复杂度；2s 对人眼进度条已足够流畅）
 */
const { createApp, ref, reactive, computed, watch, nextTick, onMounted, onBeforeUnmount } = Vue;

/* ---------- 统一 API 客户端 ---------- */
async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  const token = localStorage.getItem('token');
  if (token) headers['Authorization'] = 'Bearer ' + token;
  if (options.body instanceof FormData) delete headers['Content-Type']; // 让浏览器带 boundary
  let resp;
  try {
    resp = await fetch(path, { ...options, headers });
  } catch (e) {
    ElementPlus.ElMessage.error('网络错误：无法连接服务器');
    throw e;
  }
  if (resp.status === 401) {
    localStorage.removeItem('token');
    location.reload();
    throw new Error('未登录');
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    // 后端统一错误格式 {code, message}，直接把 message 抛给调用方
    const msg = data.message || `请求失败(${resp.status})`;
    ElementPlus.ElMessage.error(msg);
    throw new Error(msg);
  }
  return data;
}

/* ---------- 状态中文映射（前后端共享词表，集中一处便于维护） ---------- */
const TASK_STATUS = { created: '待启动', running: '运行中', paused: '已暂停', stopping: '停止中', success: '已完成', failed: '已失败', cancelled: '已取消' };
const LESSON_STATUS = { pending: '待下载', downloading: '下载中', success: '成功', failed: '失败' };
const VIDEO_STATUS = { pending: '待下载', downloading: '下载中', success: '已下载', skipped: '已存在', failed: '失败' };

const app = createApp({
  setup() {
    /* ===== 认证态 ===== */
    const authed = ref(!!localStorage.getItem('token'));
    const user = reactive({ id: 0, username: '', role: '' });
    const loginForm = reactive({ username: '', password: '' });
    const loginLoading = ref(false);
    const view = ref('dashboard');

    const viewTitle = computed(() => ({ dashboard: '仪表盘', lessons: '课堂管理', tasks: '下载任务', videos: '视频库', settings: '系统设置' }[view.value]));

    async function doLogin() {
      if (!loginForm.username || !loginForm.password) return ElementPlus.ElMessage.warning('请输入账号密码');
      loginLoading.value = true;
      try {
        const data = await api('/api/auth/login', { method: 'POST', body: JSON.stringify(loginForm) });
        localStorage.setItem('token', data.token);
        Object.assign(user, data.user);
        authed.value = true;
        bootstrap();
      } finally {
        loginLoading.value = false;
      }
    }

    async function fetchMe() {
      try { Object.assign(user, await api('/api/auth/me')); } catch (e) { /* 401 已在 api 处理 */ }
    }

    /* ===== 仪表盘 ===== */
    const stats = reactive({ lessons_total: 0, lessons_pending: 0, lessons_success: 0, lessons_failed: 0, tasks_total: 0, tasks_running: 0, videos_total: 0, videos_downloaded: 0, videos_failed: 0, videos_downloading: 0, recent_tasks: [] });
    async function loadStats() {
      try { Object.assign(stats, await api('/api/dashboard/stats')); } catch (e) { /* 已提示 */ }
    }

    /* ===== 课堂 ===== */
    const lessons = ref([]);
    const selectedLessons = ref([]);
    const lessonLoading = ref(false);
    const lessonQuery = reactive({ page: 1, page_size: 20, total: 0, keyword: '', status: '' });
    const lessonDialog = reactive({ visible: false, form: {} });
    const importVisible = ref(false);
    const importFile = ref(null);
    const importing = ref(false);
    const lessonDownloadId = ref(null);

    async function loadLessons(page) {
      if (page) lessonQuery.page = page;
      lessonLoading.value = true;
      try {
        const q = new URLSearchParams({ page: lessonQuery.page, page_size: lessonQuery.page_size, keyword: lessonQuery.keyword, status: lessonQuery.status });
        const data = await api('/api/lessons?' + q);
        lessons.value = data.items;
        lessonQuery.total = data.total;
      } finally { lessonLoading.value = false; }
    }

    function openLessonDialog(row) {
      // 编辑/新增共用一个表单（row=null 为新增），减少一个组件文件
      lessonDialog.form = row ? { ...row } : { lesson_id: '', class_id: '', class_name: '', lesson_name: '', start_time: '', creation_time: '' };
      lessonDialog.visible = true;
    }

    async function saveLesson() {
      const f = lessonDialog.form;
      if (!f.lesson_id) return ElementPlus.ElMessage.warning('课堂ID必填');
      if (f.id) {
        await api('/api/lessons/' + f.id, { method: 'PUT', body: JSON.stringify({ lesson_name: f.lesson_name, class_name: f.class_name, start_time: f.start_time }) });
      } else {
        await api('/api/lessons', { method: 'POST', body: JSON.stringify(f) });
      }
      ElementPlus.ElMessage.success('已保存');
      lessonDialog.visible = false;
      loadLessons();
    }

    async function downloadLesson(row) {
      lessonDownloadId.value = row.id;
      let task;
      try {
        task = await api('/api/tasks', { method: 'POST', body: JSON.stringify({
          name: `下载：${row.lesson_name || row.lesson_id}`,
          lesson_ids: [row.id],
          save_dir: null,
        }) });
        const started = await api(`/api/tasks/${task.id}/start`, { method: 'POST' });
        ElementPlus.ElMessage.success(`课堂下载任务 #${started.id} 已启动`);
        view.value = 'tasks';
        await loadTasks(1);
        loadLessons(lessonQuery.page);
        loadStats();
        await openTaskDetail(started);
      } catch (e) {
        if (task) loadTasks(1); // 启动失败时保留已创建任务，用户可在任务页查看/重试。
      } finally { lessonDownloadId.value = null; }
    }

    async function deleteLesson(row) {
      await ElementPlus.ElMessageBox.confirm(`删除课堂「${row.lesson_name || row.lesson_id}」？`, '确认', { type: 'warning' });
      await api('/api/lessons/' + row.id, { method: 'DELETE' });
      ElementPlus.ElMessage.success('已删除');
      loadLessons(lessonQuery.page);
    }

    async function batchDeleteLessons() {
      await ElementPlus.ElMessageBox.confirm(`删除所选 ${selectedLessons.value.length} 个课堂？`, '确认', { type: 'warning' });
      await api('/api/lessons/batch-delete', { method: 'POST', body: JSON.stringify({ ids: selectedLessons.value.map(l => l.id) }) });
      ElementPlus.ElMessage.success('已删除');
      loadLessons(1);
    }

    function onImportFile(file) { importFile.value = file.raw; }

    async function doImport() {
      if (!importFile.value) return;
      importing.value = true;
      try {
        const fd = new FormData();
        fd.append('file', importFile.value);
        const r = await api('/api/lessons/import', { method: 'POST', body: fd });
        ElementPlus.ElMessage.success(`导入完成：新增 ${r.created}，更新 ${r.updated}，跳过 ${r.skipped}`);
        importVisible.value = false;
        importFile.value = null;
        loadLessons(1);
        loadStats();
      } finally { importing.value = false; }
    }

    /* ===== 视频库 ===== */
    const videos = ref([]);
    const videoClasses = ref([]);
    const classLessons = reactive({});
    const classLessonLoading = reactive({});
    const lessonVideos = reactive({});
    const lessonVideoLoading = reactive({});
    const videoLoading = ref(false);
    const scanningVideos = ref(false);
    const videoQuery = reactive({ page: 1, page_size: 20, total: 0, keyword: '', status: '' });
    const videoPlayer = reactive({ visible: false, title: '', url: '', id: 0 });
    let playbackTimer = null;

    async function loadVideos(page) {
      if (page) videoQuery.page = page;
      videoLoading.value = true;
      try {
        const q = new URLSearchParams({ page: videoQuery.page, page_size: videoQuery.page_size, keyword: videoQuery.keyword, status: videoQuery.status });
        const data = await api('/api/videos/classes?' + q);
        videoClasses.value = data.items;
        videoQuery.total = data.total;
      } finally { videoLoading.value = false; }
    }

    async function onClassExpand(row, expandedRows) {
      const expanded = expandedRows.some(item => item.class_key === row.class_key);
      if (!expanded || Object.prototype.hasOwnProperty.call(classLessons, row.class_key)) return;
      classLessonLoading[row.class_key] = true;
      try {
        classLessons[row.class_key] = await api(`/api/videos/classes/${encodeURIComponent(row.class_key)}/lessons`);
      } finally { classLessonLoading[row.class_key] = false; }
    }

    async function loadLessonVideos(lessonId) {
      lessonVideoLoading[lessonId] = true;
      try {
        const q = new URLSearchParams({ page: 1, page_size: 200, lesson_id: lessonId, status: videoQuery.status });
        const data = await api('/api/videos?' + q);
        lessonVideos[lessonId] = data.items;
      } finally { lessonVideoLoading[lessonId] = false; }
    }

    async function onLessonExpand(row, expandedRows) {
      const expanded = expandedRows.some(item => item.lesson_id === row.lesson_id);
      if (!expanded || Object.prototype.hasOwnProperty.call(lessonVideos, row.lesson_id)) return;
      try { await loadLessonVideos(row.lesson_id); }
      catch (e) { /* api() 已提示错误；再次展开时可重试 */ }
    }

    function onVideoStatusChange() {
      const expandedLessonIds = Object.keys(lessonVideos);
      expandedLessonIds.forEach(key => delete lessonVideos[key]);
      loadVideos(1);
      expandedLessonIds.forEach(key => loadLessonVideos(key).catch(() => {}));
    }

    async function playVideo(row) {
      const ticket = await api(`/api/videos/${row.id}/ticket`, { method: 'POST' });
      videoPlayer.title = `${row.lesson_name || row.lesson_id} · ${row.title}`;
      videoPlayer.id = row.id;
      videoPlayer.url = ticket.url;
      videoPlayer.visible = true;
      if (playbackTimer) clearInterval(playbackTimer);
      playbackTimer = setInterval(() => {
        if (videoPlayer.visible && videoPlayer.id) api(`/api/videos/${videoPlayer.id}/ticket`, { method: 'POST' }).catch(() => closeVideoPlayer());
      }, 10 * 60 * 1000);
    }

    function closeVideoPlayer() {
      videoPlayer.url = '';
      videoPlayer.id = 0;
      if (playbackTimer) { clearInterval(playbackTimer); playbackTimer = null; }
    }

    async function scanVideos() {
      scanningVideos.value = true;
      try {
        const result = await api('/api/videos/scan', { method: 'POST' });
        ElementPlus.ElMessage.success(`扫描完成：新增 ${result.indexed} 个，未匹配 ${result.unmatched} 个`);
        loadVideos(1);
      } finally { scanningVideos.value = false; }
    }

    async function downloadVideo(row) {
      const ticket = await api(`/api/videos/${row.id}/ticket`, { method: 'POST' });
      const link = document.createElement('a');
      link.href = ticket.url + '?download=1';
      link.download = row.title || 'classin-video';
      document.body.appendChild(link);
      link.click();
      link.remove();
    }

    async function retryVideo(row) {
      const task = await api(`/api/videos/${row.id}/retry`, { method: 'POST' });
      ElementPlus.ElMessage.success(`已启动课堂重试任务 #${task.id}`);
      loadVideos(videoQuery.page);
      loadLessonVideos(row.lesson_id);
      loadTasks(1);
      loadStats();
    }

    /* ===== 任务 ===== */
    const tasks = ref([]);
    const taskLoading = ref(false);
    const taskQuery = reactive({ page: 1, page_size: 20, total: 0, status: '' });
    const taskDialog = reactive({ visible: false, name: '', scope: 'all', save_dir: '' });
    const detail = reactive({ visible: false, task: { id: 0, items: [], status: '' }, logs: [], tab: 'items', lastLogId: 0 });
    let pollTimer = null;

    async function loadTasks(page) {
      if (page) taskQuery.page = page;
      taskLoading.value = true;
      try {
        const q = new URLSearchParams({ page: taskQuery.page, page_size: taskQuery.page_size, status: taskQuery.status });
        const data = await api('/api/tasks?' + q);
        tasks.value = data.items;
        taskQuery.total = data.total;
      } finally { taskLoading.value = false; }
    }

    function openTaskDialog() {
      taskDialog.name = `任务 ${new Date().toLocaleString()}`;
      taskDialog.scope = selectedLessons.value.length ? 'selected' : 'all';
      taskDialog.save_dir = '';
      taskDialog.visible = true;
    }

    async function createTask() {
      if (!taskDialog.name.trim()) return ElementPlus.ElMessage.warning('请填写任务名称');
      const body = { name: taskDialog.name, save_dir: taskDialog.save_dir || null,
        lesson_ids: taskDialog.scope === 'selected' ? selectedLessons.value.map(l => l.id) : null };
      const t = await api('/api/tasks', { method: 'POST', body: JSON.stringify(body) });
      ElementPlus.ElMessage.success(`任务 #${t.id} 已创建`);
      taskDialog.visible = false;
      loadTasks(1);
      openTaskDetail(t); // 创建后直接进详情，引导用户点启动
    }

    async function taskAction(row, action) {
      const zh = { start: '启动', pause: '暂停', resume: '继续', stop: '停止' }[action];
      try {
        const t = await api(`/api/tasks/${row.id}/${action}`, { method: 'POST' });
        Object.assign(row, t);
        if (detail.task.id === row.id) Object.assign(detail.task, t);
        ElementPlus.ElMessage.success(`已${zh}`);
        if (action === 'start') { loadLessons(); loadStats(); }
        if (action === 'stop') loadStats();
        schedulePoll();
      } catch (e) { /* api() 已提示业务错误（如状态冲突） */ }
    }

    async function deleteTask(row) {
      await ElementPlus.ElMessageBox.confirm(`删除任务「${row.name}」？其日志与明细将一并删除。`, '确认', { type: 'warning' });
      await api('/api/tasks/' + row.id, { method: 'DELETE' });
      if (detail.task.id === row.id) { detail.visible = false; stopPoll(); }
      ElementPlus.ElMessage.success('已删除');
      loadTasks(taskQuery.page);
      loadStats();
    }

    async function openTaskDetail(row) {
      detail.task = { id: row.id, name: row.name || '', status: row.status || 'created', items: [], percent: 0, speed_mbps: 0, error: '' };
      detail.visible = true;
      detail.tab = 'items';
      detail.lastLogId = 0;
      detail.logs = [];
      await refreshDetail();
      schedulePoll();
    }

    async function refreshDetail() {
      if (!detail.visible || !detail.task.id) return;
      try {
        const t = await api('/api/tasks/' + detail.task.id);
        detail.task = t;
        // 增量拉日志：只传 last_id，避免重复渲染与全量传输
        const lg = await api(`/api/tasks/${detail.task.id}/logs?after_id=${detail.lastLogId}&limit=500`);
        if (lg.items.length) {
          detail.logs.push(...lg.items);
          detail.lastLogId = lg.last_id;
          if (detail.tab === 'logs') await nextTick(() => { const el = document.querySelector('.log-box'); if (el) el.scrollTop = el.scrollHeight; });
        }
      } catch (e) { stopPoll(); }
    }

    const isActive = (s) => ['running', 'paused', 'stopping'].includes(s);
    function schedulePoll() { stopPoll(); pollTimer = setInterval(async () => { await refreshDetail(); loadTasks(); if (view.value === 'videos') loadVideos(); }, 2000); }
    function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

    watch(() => detail.visible, (v) => { if (!v) stopPoll(); });

    /* ===== 设置 ===== */
    const settingsSaved = reactive({ cookie_masked: '', has_cookie: false, save_dir: '', driver_path: '', max_workers: 4, speed_limit: 0, skip_existing: true, headless: false });
    const settingsForm = reactive({ cookie: '', save_dir: '', driver_path: '', max_workers: 4, speed_limit: 0, skip_existing: true, headless: false });
    const engineInfo = reactive({ browser: '', driver_path: '', driver_ready: false });
    const verifying = ref(false);
    const cookieCheckMsg = ref('');
    const cookieCheckClass = ref('hint');
    const dirPicker = reactive({ visible: false, current_path: '', parent_path: null, directories: [], target: 'settings', loading: false });

    async function browseDirectory(path = '') {
      dirPicker.loading = true;
      try {
        const query = new URLSearchParams();
        if (path) query.set('path', path);
        const result = await api('/api/settings/directories' + (query.size ? '?' + query.toString() : ''));
        Object.assign(dirPicker, result);
      } finally { dirPicker.loading = false; }
    }

    async function openDirectoryPicker(target) {
      dirPicker.target = target;
      dirPicker.visible = true;
      const current = target === 'task' ? taskDialog.save_dir : settingsForm.save_dir;
      try { await browseDirectory(current || ''); }
      catch (e) {
        if (current) {
          try { await browseDirectory(''); } catch (_) { /* API 已提示错误 */ }
        }
      }
    }

    function chooseDirectory() {
      if (!dirPicker.current_path) return;
      if (dirPicker.target === 'task') taskDialog.save_dir = dirPicker.current_path;
      else settingsForm.save_dir = dirPicker.current_path;
      dirPicker.visible = false;
    }

    async function loadSettings() {
      try {
        const s = await api('/api/settings');
        Object.assign(settingsSaved, s);
        // cookie 不回显明文：表单只留空，保存时传了才覆盖
        settingsForm.cookie = '';
        Object.assign(settingsForm, { save_dir: s.save_dir, driver_path: s.driver_path, max_workers: s.max_workers, speed_limit: s.speed_limit, skip_existing: s.skip_existing, headless: s.headless });
      } catch (e) { /* 403 非管理员 */ }
    }

    async function saveSettings() {
      const body = { save_dir: settingsForm.save_dir, driver_path: settingsForm.driver_path, max_workers: settingsForm.max_workers,
        speed_limit: settingsForm.speed_limit, skip_existing: settingsForm.skip_existing, headless: settingsForm.headless };
      if (settingsForm.cookie.trim()) body.cookie = settingsForm.cookie.trim(); // 只有填写了才提交，避免空串清空
      const s = await api('/api/settings', { method: 'PUT', body: JSON.stringify(body) });
      Object.assign(settingsSaved, s);
      settingsForm.cookie = '';
      ElementPlus.ElMessage.success('配置已保存');
    }

    async function verifyCookie() {
      verifying.value = true;
      cookieCheckMsg.value = '';
      try {
        const r = await api('/api/settings/verify-cookie', { method: 'POST', body: JSON.stringify({ cookie: settingsForm.cookie.trim() || null }) });
        cookieCheckMsg.value = (r.valid ? '✅ ' : '❌ ') + r.message;
        cookieCheckClass.value = r.valid ? 'hint ok' : 'hint bad';
      } finally { verifying.value = false; }
    }

    async function loadEngine() {
      try { Object.assign(engineInfo, await api('/api/settings/engine')); } catch (e) { /* 已提示 */ }
    }

    /* ===== 用户 ===== */
    const pwdDialog = reactive({ visible: false, old: '', new1: '', new2: '' });
    async function changePassword() {
      if (!pwdDialog.new1 || pwdDialog.new1 !== pwdDialog.new2) return ElementPlus.ElMessage.warning('两次输入的新密码不一致');
      await api('/api/auth/password', { method: 'PUT', body: JSON.stringify({ old_password: pwdDialog.old, new_password: pwdDialog.new1 }) });
      ElementPlus.ElMessage.success('密码已修改，下次登录生效');
      pwdDialog.visible = false;
      Object.assign(pwdDialog, { old: '', new1: '', new2: '' });
    }

    function onUserCmd(cmd) {
      if (cmd === 'logout') { localStorage.removeItem('token'); location.reload(); }
      if (cmd === 'password') { Object.assign(pwdDialog, { visible: true, old: '', new1: '', new2: '' }); }
    }

    /* ===== 视图切换（顺带按需拉数据） ===== */
    function switchLessons() { view.value = 'lessons'; loadLessons(1); }
    function switchTasks() { view.value = 'tasks'; loadTasks(1); }
    function switchVideos() { view.value = 'videos'; loadVideos(1); }

    function statusZh(s) { return TASK_STATUS[s] || s; }
    function statusTag(s) { return { created: 'info', running: '', paused: 'warning', stopping: 'warning', success: 'success', failed: 'danger', cancelled: 'info' }[s] || 'info'; }
    function lessonStatusZh(s) { return LESSON_STATUS[s] || s || '待下载'; }
    function lessonStatusTag(s) { return { pending: 'info', downloading: '', success: 'success', failed: 'danger' }[s] || 'info'; }
    function fmtTime(iso) { return iso ? iso.replace('T', ' ').slice(0, 19) : ''; }
    function videoStatusZh(s) { return VIDEO_STATUS[s] || s || '待下载'; }
    function videoStatusTag(s) { return { pending: 'info', downloading: '', success: 'success', skipped: 'success', failed: 'danger' }[s] || 'info'; }
    function formatBytes(n) { if (!n) return '-'; const units = ['B', 'KB', 'MB', 'GB', 'TB']; let v = n, i = 0; while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; } return `${v.toFixed(i ? 1 : 0)} ${units[i]}`; }

    async function bootstrap() {
      await fetchMe();
      loadStats();
      loadLessons(1);
      loadTasks(1);
      if (user.role === 'admin') loadSettings();
    }

    onMounted(() => { if (authed.value) bootstrap(); });
    onBeforeUnmount(() => { stopPoll(); if (playbackTimer) clearInterval(playbackTimer); });

    return {
      authed, user, loginForm, loginLoading, doLogin, view, viewTitle,
      stats, lessons, selectedLessons, lessonLoading, lessonQuery, lessonDialog,
      lessonDownloadId, downloadLesson,
      importVisible, importFile, importing, onImportFile, doImport,
      openLessonDialog, saveLesson, deleteLesson, batchDeleteLessons, loadLessons,
      tasks, taskLoading, taskQuery, taskDialog, detail, TASK_STATUS,
      loadTasks, openTaskDialog, createTask, taskAction, deleteTask, openTaskDetail,
      videos, videoClasses, classLessons, classLessonLoading, lessonVideos, lessonVideoLoading,
      videoLoading, videoQuery, videoPlayer, VIDEO_STATUS, loadVideos, onClassExpand, onLessonExpand, onVideoStatusChange,
      playVideo, closeVideoPlayer, downloadVideo, retryVideo, scanVideos, scanningVideos,
      settingsSaved, settingsForm, engineInfo, verifying, cookieCheckMsg, cookieCheckClass,
      loadSettings, saveSettings, verifyCookie, loadEngine,
      dirPicker, openDirectoryPicker, browseDirectory, chooseDirectory,
      pwdDialog, changePassword, onUserCmd,
      switchLessons, switchTasks, switchVideos, createTaskFromSelection: openTaskDialog,
      statusZh, statusTag, lessonStatusZh, lessonStatusTag, videoStatusZh, videoStatusTag, formatBytes, fmtTime,
    };
  },
});

/* 注册 Element Plus 图标（v-for 动态组件用不到，只兜底几个模板内图标） */
for (const [name, comp] of Object.entries(ElementPlusIconsVue || {})) app.component(name, comp);
app.use(ElementPlus);
app.mount('#app');
