import { normalizeLocale, translate,errorText } from './i18n.mjs';
import { initializeTasks } from './task-ui.mjs';
import { initializeSearch } from './search-ui.mjs';

const $ = (id) => document.getElementById(id);
let locale = 'en-US';
try { locale = normalizeLocale(localStorage.getItem('sts.language')); } catch { /* Storage is optional. */ }
let jobs = [];
let selected = null;
let message = '';
let pending = false;
let lastJobsRenderKey = '';
const t = (key) => translate(locale, key);
let taskUI;
let searchUI;

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-STS-Request': '1' }, body: JSON.stringify(body),
  };
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error('Request failed'); error.code = payload.error; error.field = payload.field;
    throw error;
  }
  return payload;
}

function render() {
  document.documentElement.lang = locale === 'zh-CN' ? 'zh-CN' : 'en';
  document.querySelectorAll('[data-i18n]').forEach((node) => {
    const text = t(node.dataset.i18n);
    if (node.textContent !== text) node.textContent = text;
  });
  $('language').value = locale;
  $('message').textContent = errorText(t,message);
  $('create').disabled = pending;
  $('empty').hidden = jobs.length > 0;
  const jobsRenderKey = JSON.stringify([locale, selected?.id, jobs]);
  if (jobsRenderKey !== lastJobsRenderKey) {
    lastJobsRenderKey = jobsRenderKey;
    $('jobs').replaceChildren(...jobs.map((job) => {
    const li = document.createElement('li');
    const button = document.createElement('button');
    button.className = 'job';
    button.setAttribute('aria-pressed', String(selected?.id === job.id));
    const name = document.createElement('span');
    name.textContent = t(job.kind);
    const date = document.createElement('time');
    date.dateTime = new Date(job.created_at * 1000).toISOString();
    date.textContent = new Date(job.created_at * 1000).toLocaleString(locale);
    const state = document.createElement('span');
    state.className = `state ${job.state}`;
    state.textContent = t(job.state);
    button.append(name, date, state);
    button.onclick = async () => {
      try { selected = await api(`/api/jobs/${job.id}`); render(); }
      catch { message = 'error'; render(); }
    };
    li.append(button);
    return li;
    }));
  }
  $('detail-panel').hidden = !selected;
  if (selected) {
    $('selected-id').textContent = selected.id;
    $('detail').textContent = JSON.stringify(selected, null, 2);
    $('cancel').hidden = !['queued', 'running'].includes(selected.state);
  }
  taskUI?.render();
  searchUI?.render();
}

async function refresh() {
  try {
    jobs = (await api('/api/jobs')).jobs;
    if (selected) selected = await api(`/api/jobs/${selected.id}`);
    if (message === 'created' && selected && !['queued', 'running'].includes(selected.state)) message = '';
    if (message === 'error') message = '';
  } catch { message = 'error'; }
  render();
}

$('settings').onclick = () => {
  $('settings-panel').hidden = !$('settings-panel').hidden;
  $('settings').setAttribute('aria-expanded', String(!$('settings-panel').hidden));
};
$('language').onchange = (event) => {
  locale = normalizeLocale(event.target.value);
  try { localStorage.setItem('sts.language', locale); } catch { /* Session-only selection. */ }
  render();
};
$('create').onclick = async () => {
  pending = true;
  render();
  try { selected = await api('/api/jobs', { kind: 'foundation_check' }); message = 'created'; }
  catch { message = 'error'; }
  finally { pending = false; }
  await refresh();
};
$('cancel').onclick = async () => {
  try { selected = await api(`/api/jobs/${selected.id}/cancel`, {}); }
  catch { message = 'error'; }
  await refresh();
};
taskUI = initializeTasks({ api, t, renderRoot: render });
searchUI = initializeSearch({ api, t, taskUI, renderRoot: render });
render();
await taskUI.refresh();
await searchUI.refresh();
await refresh();
// Schedule after completion so slow requests cannot create overlapping polling loops.
async function poll() { await refresh(); await searchUI.refresh(); setTimeout(poll, 1500); }
setTimeout(poll, 1500);
