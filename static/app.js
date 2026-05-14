const v = window.APP_VERSION ? `?v=${window.APP_VERSION}` : '';

function showApp(loadStats, loadEmbedder, loadMediaEmbedder, loadLogs) {
  document.getElementById('login').style.display = 'none';
  document.getElementById('app').style.display = 'flex';
  loadStats();
  loadEmbedder();
  loadMediaEmbedder();
  loadLogs();
}

async function onTelegramAuth(apiFetch, showAppFn, loadStats, loadEmbedder, loadMediaEmbedder, loadLogs, user) {
  const r = await apiFetch('/auth/telegram', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(user),
  });
  if (r.ok) showAppFn(loadStats, loadEmbedder, loadMediaEmbedder, loadLogs);
  else alert('Доступ запрещён');
}

function initTabs() {
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
      if (tab.dataset.tab === 'dashboard') { loadStats(); loadEmbedder(); loadMediaEmbedder(); loadLogs(); }
      if (tab.dataset.tab === 'chats') { loadChats(); pollSync(); }
      if (tab.dataset.tab === 'settings') { loadSettings(); loadTokens(); }
      if (tab.dataset.tab === 'improvements') { loadImprovements(); }
    });
  });
}

async function bootstrap() {
  const { apiFetch } = await import(`./api.js${v}`);
  const dashboard = await import(`./pages/dashboard.js${v}`);
  const chats = await import(`./pages/chats.js${v}`);
  const settings = await import(`./pages/settings.js${v}`);
  const improvements = await import(`./pages/improvements.js${v}`);

  initTabs();
  chats.initChatsPage();
  settings.initSettingsPage();

  window.onTelegramAuth = (user) => onTelegramAuth(
    apiFetch,
    showApp,
    dashboard.loadStats,
    dashboard.loadEmbedder,
    dashboard.loadMediaEmbedder,
    dashboard.loadLogs,
    user,
  );

  window.clearChunks = dashboard.clearChunks;
  window.toggleEmbedder = dashboard.toggleEmbedder;
  window.loadEmbedder = dashboard.loadEmbedder;
  window.loadLogs = dashboard.loadLogs;
  window.clearMediaChunks = dashboard.clearMediaChunks;
  window.toggleMediaEmbedder = dashboard.toggleMediaEmbedder;
  window.loadMediaEmbedder = dashboard.loadMediaEmbedder;

  window.loadChats = chats.loadChats;
  window.syncFolders = chats.syncFolders;
  window.syncTopics = chats.syncTopics;
  window.onSearch = chats.onSearch;
  window.setFilter = chats.setFilter;
  window.sortBy = chats.sortBy;
  window.onFolderFilter = chats.onFolderFilter;
  window.renderChats = chats.renderChats;
  window.showAvatar = chats.showAvatar;
  window.toggleSync = chats.toggleSync;
  window.pollSync = chats.pollSync;

  window.loadSettings = settings.loadSettings;
  window.saveSettings = settings.saveSettings;
  window.toggleType = settings.toggleType;
  window.addBlacklist = settings.addBlacklist;
  window.removeBlacklist = settings.removeBlacklist;

  window.loadTokens = settings.loadTokens;
  window.addToken = settings.addToken;
  window.deleteToken = settings.deleteToken;
  window.toggleToken = settings.toggleToken;
  window.toggleTokenCapsEditor = settings.toggleTokenCapsEditor;
  window.saveTokenCaps = settings.saveTokenCaps;

  window.loadImprovements = improvements.loadImprovements;
  window.onImprovementStatus = improvements.onImprovementStatus;
  window.approveImprovement = improvements.approveImprovement;
  window.rejectImprovement = improvements.rejectImprovement;

  apiFetch('/api/admin/stats').then(r => {
    if (!r.ok) return;
    r.json().then(() => showApp(dashboard.loadStats, dashboard.loadEmbedder, dashboard.loadMediaEmbedder, dashboard.loadLogs));
  });
}

bootstrap();
