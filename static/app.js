import { apiFetch } from './api.js';
import { clearChunks, loadEmbedder, loadLogs, loadStats, toggleEmbedder } from './pages/dashboard.js';
import { initChatsPage, loadChats, onFolderFilter, onSearch, pollSync, renderChats, setFilter, showAvatar, sortBy, syncFolders, syncTopics, toggleSync } from './pages/chats.js';
import { addBlacklist, addToken, deleteToken, initSettingsPage, loadSettings, loadTokens, removeBlacklist, saveSettings, toggleToken, toggleType } from './pages/settings.js';

function showApp() {
  document.getElementById('login').style.display = 'none';
  document.getElementById('app').style.display = 'flex';
  loadStats();
  loadEmbedder();
  loadLogs();
}

async function onTelegramAuth(user) {
  const r = await apiFetch('/auth/telegram', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(user),
  });
  if (r.ok) showApp();
  else alert('Доступ запрещён');
}

function initTabs() {
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
      if (tab.dataset.tab === 'dashboard') { loadStats(); loadEmbedder(); loadLogs(); }
      if (tab.dataset.tab === 'chats') { loadChats(); pollSync(); }
      if (tab.dataset.tab === 'settings') { loadSettings(); loadTokens(); }
    });
  });
}

async function bootstrap() {
  initTabs();
  initChatsPage();
  initSettingsPage();

  window.onTelegramAuth = onTelegramAuth;
  window.clearChunks = clearChunks;
  window.toggleEmbedder = toggleEmbedder;
  window.loadEmbedder = loadEmbedder;
  window.loadLogs = loadLogs;

  window.loadChats = loadChats;
  window.syncFolders = syncFolders;
  window.syncTopics = syncTopics;
  window.onSearch = onSearch;
  window.setFilter = setFilter;
  window.sortBy = sortBy;
  window.onFolderFilter = onFolderFilter;
  window.renderChats = renderChats;
  window.showAvatar = showAvatar;
  window.toggleSync = toggleSync;
  window.pollSync = pollSync;

  window.loadSettings = loadSettings;
  window.saveSettings = saveSettings;
  window.toggleType = toggleType;
  window.addBlacklist = addBlacklist;
  window.removeBlacklist = removeBlacklist;

  window.loadTokens = loadTokens;
  window.addToken = addToken;
  window.deleteToken = deleteToken;
  window.toggleToken = toggleToken;

  apiFetch('/api/admin/stats').then(r => {
    if (!r.ok) return;
    r.json().then(() => showApp());
  });
}

bootstrap();
