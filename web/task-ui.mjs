import {errorText,fieldText} from './i18n.mjs';

const $ = (id) => document.getElementById(id);
const lines = (id) => $(id).value.split('\n').map((line) => line.trim()).filter(Boolean);

export function initializeTasks({ api, t, renderRoot }) {
  let tasks = [];
  let preview = null;
  let detail = null;
  let history = [];
  let editing = null;
  let pending = false;
  let message = '';
  let errorField = '';
  let listKey = '';
  let detailKey = '';

  function showError(error) { message = error.code || 'error'; errorField = error.field || ''; }
  function definition() {
    const value = {
      samples: { format: $('sample-format').value, content: $('task-samples').value },
      output_specification: $('task-goal').value,
      labels: { format: 'json', content: $('task-labels').value.trim() || '[]' },
      constraints: { hard: lines('task-hard'), soft: lines('task-soft') },
      technical_preferences: lines('task-tech'), resources: lines('task-resources'),
      budget: $('inherit-budget').checked ? {mode: 'inherit_work'} : { mode: 'explicit', max_cost_cny: Number($('task-cost').value), max_candidate_executions: Number($('task-executions').value) },
    };
    if ($('task-title').value.trim()) value.title = $('task-title').value;
    return value;
  }

  function render() {
    $('task-fields').disabled = pending;
    $('task-form').hidden = !!detail && !editing && !preview;
    $('budget-fields').hidden = $('inherit-budget').checked;
    $('save-task').disabled = pending;
    $('edit-task').disabled = pending;
    $('new-task').disabled = pending;
    $('refresh-tasks').disabled = pending;
    const field=fieldText(t,errorField);
    $('task-message').textContent = message ? errorText(t,message) + (field ? ` (${field})` : '') : '';
    $('new-task').hidden = !editing && !detail;
    $('tasks-empty').hidden = tasks.length > 0;
    const nextListKey = JSON.stringify([tasks, detail?.task_id, t('savedTasks')]);
    if (nextListKey !== listKey) {
      listKey = nextListKey;
      $('tasks').replaceChildren(...tasks.map((task) => {
        const li = document.createElement('li');
        const button = document.createElement('button');
        button.className = 'job';
        const name = document.createElement('span'); name.textContent = task.title;
        const count = document.createElement('span'); count.className = 'field-help'; count.textContent = `${task.sample_count} ${t('sampleCount')}`;
        const revision = document.createElement('span'); revision.className = 'state'; revision.textContent = `${t('revisionLabel')} ${task.revision}`;
        button.append(name, count, revision);
        button.onclick = async () => {
          if (pending) return;
          try {
            detail = await api(`/api/tasks/${task.task_id}`); history = detail.revisions;
            preview = null; message = ''; errorField = ''; renderRoot();
          } catch (error) { showError(error); renderRoot(); }
        };
        li.append(button); return li;
      }));
    }
    const record = preview || detail;
    $('task-review').hidden = !record;
    $('save-task').hidden = !preview;
    $('edit-task').hidden = !detail || !!preview || detail.revision !== history[0]?.revision;
    $('revision-picker').hidden = !detail || !!preview;
    if (!record) return;
    const contract = record.contract;
    $('review-heading').textContent = preview ? t('reviewHeading') : contract.definition.title;
    $('contract-summary').textContent = `${contract.dataset.sample_count} ${t('sampleCount')} · ${contract.evaluation.label_count} ${t('labelCount')} · ${t(contract.evaluation.supervision_mode)}`;
    $('contract-goal').textContent = contract.definition.output_specification;
    const nextDetailKey = JSON.stringify([record, history, t('revisionLabel')]);
    if (nextDetailKey !== detailKey) {
      detailKey = nextDetailKey;
      $('contract-unknowns').replaceChildren(...contract.unresolved.map((code) => {
        const li = document.createElement('li'); li.textContent = t(code); return li;
      }));
      $('task-revision').replaceChildren(...history.map((row) => {
        const option = document.createElement('option'); option.value = row.revision;
        option.textContent = `${t('revisionLabel')} ${row.revision}`; return option;
      }));
      if (detail) $('task-revision').value = String(detail.revision);
      $('contract-json').textContent = JSON.stringify(preview ? { contract, content_sha256: record.content_sha256 } : record, null, 2);
    }
  }

  async function refresh() {
    try { tasks = (await api('/api/tasks')).tasks; }
    catch (error) { showError(error); }
    renderRoot();
  }

  $('task-form').oninput = () => {
    if (preview) { preview = null; message = 'reviewChanged'; errorField = ''; renderRoot(); }
  };
  $('sample-file').onchange = async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    if (file.size > 262144) { message = 'task_too_large'; renderRoot(); return; }
    try {
      $('task-samples').value = await file.text();
      $('sample-format').value = /\.(jsonl|ndjson)$/i.test(file.name) ? 'jsonl' : 'json';
      preview = null; message = ''; errorField = ''; renderRoot();
    } catch { message = 'file_error'; renderRoot(); }
  };
  $('inherit-budget').onchange = () => { $('budget-fields').hidden = $('inherit-budget').checked; };
  $('task-form').onsubmit = async (event) => {
    event.preventDefault();
    if (pending) return;
    pending = true; message = ''; errorField = ''; renderRoot();
    try {
      const input = definition();
      const compiled = await api('/api/tasks/preview', { definition: input });
      preview = { ...compiled, submittedDefinition: input };
      detail = null; history = [];
    } catch (error) { preview = null; showError(error); }
    finally {
      pending = false; renderRoot();
      if (preview) $('review-heading').focus();
    }
  };
  $('save-task').onclick = async () => {
    if (!preview || pending) return;
    pending = true; message = ''; errorField = ''; renderRoot();
    const payload = { definition: preview.submittedDefinition, reviewed_sha256: preview.content_sha256 };
    const path = editing ? `/api/tasks/${editing.id}/revisions` : '/api/tasks';
    if (editing) payload.base_revision = editing.base;
    try {
      const saved = await api(path, payload);
      detail = await api(`/api/tasks/${saved.task_id}`); history = detail.revisions;
      preview = null; editing = null; message = 'taskSaved';
    } catch (error) { showError(error); }
    finally { pending = false; await refresh(); }
  };
  $('edit-task').onclick = () => {
    if (!detail || pending) return;
    const value = detail.contract.definition;
    editing = { id: detail.task_id, base: detail.revision };
    $('task-title').value = value.title;
    $('task-samples').value = JSON.stringify(value.samples, null, 2);
    $('sample-format').value = 'json';
    $('task-goal').value = value.output_specification;
    $('task-labels').value = JSON.stringify(value.labels, null, 2);
    for (const [id, items] of [['task-hard', value.constraints.hard], ['task-soft', value.constraints.soft],
      ['task-tech', value.technical_preferences], ['task-resources', value.resources]]) $(id).value = items.join('\n');
    $('task-cost').value = value.budget.max_cost_cny ?? 0;
    $('task-executions').value = value.budget.max_candidate_executions;
    $('inherit-budget').checked = value.budget.mode === 'inherit_work';
    message = 'editingRevision'; errorField = ''; preview = null; renderRoot();
    $('task-goal').focus();
  };
  $('new-task').onclick = () => {
    editing = null; preview = null; detail = null; history = []; message = ''; errorField = '';
    $('task-form').reset(); renderRoot();
  };
  $('task-revision').onchange = async (event) => {
    if (!detail) return;
    try {
      detail = await api(`/api/tasks/${detail.task_id}/revisions/${event.target.value}`);
      renderRoot();
    } catch (error) { showError(error); renderRoot(); }
  };
  $('refresh-tasks').onclick = refresh;
  return { render, refresh, selected: () => preview ? null : detail };
}
