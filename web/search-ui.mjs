import { errorText } from './i18n.mjs';
import { initializeReviews,reviewMetric } from './reviews-ui.mjs';

const $ = (id) => document.getElementById(id);

export function familyLabel(family,t) {
  const key={'rules.v1':'rulesFamily','python.v1':'pythonFamily','prompt.v1':'promptFamily','hybrid.v1':'hybridFamily','sklearn.classifier.v1':'mlFamily','workflow.v1':'workflowFamily','agent.tools.v1':'agentFamily'}[family];
  return key?t(key):family;
}

export function inferenceEvidence(execution,calls=[]) {
  const ids=[...new Set([...(execution.runtime?.model_call_ids||[]),...(execution.call_id?[execution.call_id]:[])])];
  const known=new Map(calls.map(call=>[call.id,call]));
  return {ids,complete:ids.every(id=>known.has(id)),ceilingCny:ids.reduce((sum,id)=>sum+(known.get(id)?.charged_micro??known.get(id)?.reserved_micro??0),0)/1e6};
}

export function invocationSummary(row,calls,t) {
  const evidence=inferenceEvidence(row,calls);
  const status=row.runtime?.status==='cancelled'?'cancelled':row.error?'failed':'succeeded';
  return `${t(status)} · ${t('modelCallsLabel')}: ${evidence.ids.length} · ${t('inferenceCostLabel')}: ${evidence.complete?'¥'+evidence.ceilingCny.toFixed(6):t('costUnattributed')} · ${t('latencyLabel')}: ${Number(row.latency_ms||0).toFixed(1)} ms`;
}

export function criteriaSummary(receipt,t) {
  if(receipt?.status==='checked') return t(receipt.passed?'criteriaPassed':'criteriaFailed');
  return t('criteriaUnavailable');
}

function criteriaView(bundle,t) {
  const fragment=document.createDocumentFragment();
  const status=document.createElement('p');status.textContent=t(bundle.status==='preparing'?'criteriaPreparing':bundle.status==='proposed'?'criteriaUnreviewed':'criteriaUnavailable');fragment.append(status);
  for(const [field,key] of [['covered','criteriaCovered'],['uncovered','criteriaUncovered']]) {
    if(!bundle[field]?.length)continue;
    const heading=document.createElement('p');heading.textContent=t(key);fragment.append(heading);
    const list=document.createElement('ul');for(const text of bundle[field]){const item=document.createElement('li');item.textContent=text;list.append(item);}fragment.append(list);
  }
  const details=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');
  summary.textContent=t('criteriaRaw');pre.textContent=JSON.stringify(bundle,null,2);details.append(summary,pre);fragment.append(details);
  return fragment;
}

function executionView(row,calls,t) {
  const article=document.createElement('article');article.className='execution-record';
  const heading=document.createElement('h4');heading.textContent=`${t('sampleLabel')} ${row.sample_index+1}`;
  const metrics=document.createElement('p');metrics.className='field-help';metrics.textContent=invocationSummary(row,calls,t);
  article.append(heading,metrics);
  if(row.criteria){const checks=document.createElement('p');checks.className='field-help';checks.textContent=criteriaSummary(row.criteria,t);article.append(checks);
    if(row.criteria.errors?.length){const pre=document.createElement('pre');pre.textContent=JSON.stringify(row.criteria.errors,null,2);article.append(pre);}}
  if(row.error){const message=document.createElement('p');message.textContent=errorText(t,row.error);article.append(message);}
  else {
    if(row.label_agreement===0||row.label_agreement===1){const label=document.createElement('p');label.className='field-help';label.textContent=t(row.label_agreement?'labelMatch':'labelMismatch');article.append(label);}
    const output=document.createElement('pre');output.setAttribute('aria-label',t('outputLabel'));output.textContent=JSON.stringify(row.output,null,2);article.append(output);
  }
  for(const [index,step] of (row.runtime?.steps||[]).entries()) {
    const details=document.createElement('details'),summary=document.createElement('summary');
    summary.textContent=`${t('stepLabel')} ${index+1} · ${familyLabel(step.family,t)}${step.tool?` · ${t('selectedTool')}: ${step.tool}`:step.action==='final'?` · ${t('finalAnswer')}`:''} · ${invocationSummary({runtime:{model_call_ids:step.model_call_ids,status:step.status},error:step.status==='failed',latency_ms:step.latency_ms},calls,t)}`;
    details.append(summary);
    if(step.error){const message=document.createElement('p');message.textContent=errorText(t,step.error);details.append(message);}
    else {const output=document.createElement('pre');output.textContent=step.output_omitted?t('stepOutputOmitted'):JSON.stringify(step.output,null,2);details.append(output);}
    article.append(details);
  }
  const raw=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');
  summary.textContent=t('rawExecutionEvidence');pre.textContent=JSON.stringify(row,null,2);raw.append(summary,pre);article.append(raw);
  return article;
}

export function funnelStages(record) {
  const states = new Map(record.candidates.map((candidate) => [candidate.id, 'active']));
  const stages = [{stage:'built',active:states.size,paused:0,pruned:0}];
  for (const stage of [...new Set((record.decisions || []).map((decision) => decision.stage))]) {
    for (const decision of record.decisions.filter((row) => row.stage === stage)) states.set(decision.candidate_id, decision.action);
    stages.push({stage,...Object.fromEntries(['active','paused','pruned'].map((state) => [state,[...states.values()].filter((value) => value === state).length]))});
  }
  return stages;
}

export function initializeSearch({ api, t, taskUI, renderRoot }) {
  let searches = [], selected = null, budget = null, pending = false, message = '', listKey = '', resultKey = '', followActive = false, lineage = null, graphKey = '', criteriaKey = '', delivery = null;
  const reviews=initializeReviews({api,t,onChange:refresh});
  function render() {
    const task = taskUI.selected();
    $('search-section').hidden = !task;
    const settings = task?.contract.definition.budget;
    if (selected?.task_id !== task?.task_id) delivery = null;
    const valid = task && !delivery && (settings.mode === 'inherit_work' ? budget?.limit_cny > 0 : (settings.max_cost_cny ?? 0) > 0 && settings.max_candidate_executions >= 2);
    $('start-search').disabled = pending || !valid;
    $('search-hint').textContent = t(delivery ? 'finalConsumed' : valid ? 'searchScope' : 'searchBudgetHint');
    $('search-message').textContent = errorText(t,message);
    $('work-budget').textContent = budget ? `${t('workBudget')}: ¥${budget.committed_ceiling_cny.toFixed(6)} / ¥${budget.limit_cny} ${t('ceilingEstimate')}${budget.delegated_ceiling_cny?` · ${t('serviceAllowance')}: ¥${budget.delegated_ceiling_cny.toFixed(2)}`:''}` : '';
    const visible = selected && selected.task_id === task?.task_id;
    $('search-detail').hidden = !visible;
    $('cancel-search').hidden = !visible || !['queued', 'running'].includes(selected.state);
    $('rollback-search').hidden = !!delivery || !visible || !selected.selection?.candidate_id || !['succeeded','failed','interrupted','cancelled'].includes(selected.state);
    $('rollback-search').disabled = pending;
    if (visible) {
      $('search-summary').textContent = `${t(selected.state)} · ${selected.candidates.length} ${t('candidateCount')} · ${selected.executions.length} ${t('executionCount')}`;
      $('search-error').textContent = selected.error ? `${t('searchStoppedError')} ${errorText(t,selected.error)}` : '';
      $('selection-summary').textContent = selected.selection ? `${t(selected.selection.evidence.status)} · ${t(selected.selection.evidence.basis)} · ${t(delivery ? 'finalConsumed' : 'finalSealed')}` : '';
      $('criteria-evidence').hidden=!selected.criteria_bundle;
      const nextCriteriaKey=JSON.stringify([selected.criteria_bundle,t('criteriaTitle')]);
      if(selected.criteria_bundle&&criteriaKey!==nextCriteriaKey){criteriaKey=nextCriteriaKey;$('criteria-summary').replaceChildren(criteriaView(selected.criteria_bundle,t));}
      const winner = selected.candidates.find((row) => row.id === selected.selection?.candidate_id);
      const needsModelServing=['prompt.v1','hybrid.v1','agent.tools.v1'].includes(winner?.family)||(winner?.family==='workflow.v1' && winner.artifact.steps.some(step=>step.family==='prompt.v1'));
      $('delivery-section').hidden = selected.state !== 'succeeded' || !['rules.v1','python.v1','sklearn.classifier.v1','workflow.v1','prompt.v1','hybrid.v1','agent.tools.v1'].includes(winner?.family);
      const finalIndices=selected.partitions.filter(r=>r.split==='final_test').map(r=>r.sample_index), labelled=new Set(task.contract.definition.labels.map(r=>r.sample_index));
      const needsOperator=finalIndices.some(i=>!labelled.has(i));
      $('prepare-delivery').hidden=needsOperator;
      $('prepare-review-delivery').hidden=!needsOperator||!!delivery;
      $('prepare-review-delivery').disabled=pending;
      $('prepare-delivery').disabled = pending || !!delivery;
      $('delivery-status').textContent = delivery ? t(delivery.export?.archive_integrity_verified ? 'local_export_ready' : delivery.state) : needsModelServing?t('modelServingReady'):'';
      $('delivery-metrics').textContent = delivery?.report ? delivery.report.review_state==='pending'?`${t('finalExamplesRecorded')}: ${delivery.report.count}`:`${reviewMetric(delivery.report,t)}. ${t(delivery.draft_integrity_verified && delivery.proof?.status === 'passed' ? 'serviceVerified' : 'serviceNotVerified')}` : '';
      $('export-service').hidden = !delivery?.draft_integrity_verified || delivery?.proof?.status !== 'passed' || !!delivery?.export;
      $('export-service').disabled = pending;
      $('download-service').hidden = !delivery?.export?.archive_integrity_verified;
      $('download-service').href = `/api/searches/${selected.id}/delivery/export`;
      $('delivery-json').textContent = delivery ? JSON.stringify(delivery,null,2) : '';
      const nextResultKey = JSON.stringify([selected, t('searchEvidence'), pending]);
      if (resultKey !== nextResultKey) {
        resultKey = nextResultKey;
        $('search-json').textContent = JSON.stringify(selected, null, 2);
        $('candidate-results').replaceChildren(...selected.candidates.map((candidate) => {
          const li = document.createElement('li'), heading = document.createElement('h3'), rationale = document.createElement('p');
          heading.textContent = `${familyLabel(candidate.family,t)} · ${candidate.name}`;
          rationale.textContent = `${t('plannerHypothesis')}: ${candidate.rationale}`;
          const executions = selected.executions.filter((row) => row.candidate_id === candidate.id);
          const stats = document.createElement('p'); stats.className = 'field-help';
          stats.textContent = `${executions.length} ${t('executionCount')} · ${executions.filter((row) => row.error).length} ${t('failureCount')}`;
          const decision = selected.decisions?.filter((row) => row.candidate_id === candidate.id).at(-1);
          if (decision) stats.textContent += ` · ${decision.stage}: ${t(decision.action)}`;
          if (selected.selection?.candidate_id === candidate.id) stats.textContent += ` · ${t('provisionalLeader')}`;
          const details = document.createElement('details'), summary = document.createElement('summary');
          summary.textContent = t('candidateOutputs');
          details.append(summary,...executions.map(row=>executionView(row,selected.budget?.calls,t))); li.append(heading, rationale, stats, details);
          if (['succeeded', 'failed', 'interrupted', 'cancelled'].includes(selected.state)) {
            const revive = document.createElement('button'); revive.textContent = t('reactivateCandidate'); revive.disabled = pending || !!delivery;
            revive.onclick = async () => {
              if (pending) return;
              pending = true; renderRoot();
              try { selected = await api(`/api/searches/${selected.id}/candidates/${candidate.id}/reactivate`, {request_id: crypto.randomUUID().replaceAll('-', '')}); }
              catch (error) { message = error.code || 'error'; }
              finally { pending = false; await refresh(); }
            };
            li.append(revive);
            const improve = document.createElement('button'); improve.textContent = t('improveCandidate'); improve.disabled = pending || !!delivery;
            improve.onclick = () => act(`/api/searches/${selected.id}/candidates/${candidate.id}/evolve`);
            li.append(improve);
          }
          return li;
        }));
        $('mutation-diffs').replaceChildren(...(selected.mutations || []).map((mutation) => {
          const details = document.createElement('details'), summary = document.createElement('summary'), pre = document.createElement('pre'), reason = document.createElement('p');
          summary.textContent = t('mutationDiff'); reason.textContent = `${t('plannerHypothesis')}: ${mutation.reason}`; pre.textContent = mutation.diff;
          details.append(summary, reason, pre); return details;
        }));
        $('search-funnel').replaceChildren(...funnelStages(selected).map((stage) => {
          const li = document.createElement('li');
          li.textContent = `${t(stage.stage)} → ${stage.active} ${t('active')} · ${stage.paused} ${t('paused')} · ${stage.pruned} ${t('pruned')}`;
          return li;
        }));
      }
      const nextGraphKey = JSON.stringify([lineage, t('evolutionGraph')]);
      if (graphKey !== nextGraphKey) {
        graphKey = nextGraphKey;
        const nodes = lineage?.nodes || [], mapping = new Map(nodes.map((node) => [node.id,node]));
        $('evolution-graph').replaceChildren(...nodes.map((node) => {
          const box = document.createElement('div'); box.className = 'evolution-node';
          const edge = lineage.edges.find((edge) => edge.child === node.id);
          box.dataset.kind = edge?.kind || 'initial';
          const parent = mapping.get(node.parent_candidate_id), label = document.createElement('p');
          label.className = 'field-help'; label.textContent = parent ? `${t(edge?.kind || 'initial')}: ${parent.name} →` : t('initial');
          const button = document.createElement('button'); button.textContent = `${familyLabel(node.family,t)} · ${node.name} · r${node.revision} · ${node.artifact_sha256.slice(0,8)}`;
          button.onclick = async () => {
            followActive = false;
            try { selected = await api(`/api/searches/${node.search_id}`); await refresh(); }
            catch { message = 'error'; renderRoot(); }
          };
          box.append(label,button); return box;
        }));
      }
    }
    reviews.render(visible?selected:null,delivery);
    const filtered = searches.filter((row) => row.task_id === task?.task_id);
    const nextListKey = JSON.stringify([filtered, t('succeeded')]);
    if (listKey !== nextListKey) {
      listKey = nextListKey;
      $('searches').replaceChildren(...filtered.map((row) => {
        const li = document.createElement('li'), button = document.createElement('button');
        button.textContent = `${t('revisionLabel')} ${row.revision} · ${t(row.state)} · ${row.id.slice(0, 8)}`;
        button.onclick = async () => {
          followActive = false;
          try { selected = await api(`/api/searches/${row.id}`); lineage = await api(`/api/searches/${row.id}/lineage`); delivery = (await api(`/api/searches/${row.id}/delivery`)).delivery; renderRoot(); }
          catch { message = 'error'; renderRoot(); }
        };
        li.append(button); return li;
      }));
    }
  }
  async function refresh() {
    try {
      searches = (await api('/api/searches')).searches;
      budget = await api('/api/budget');
      if (selected) selected = await api(`/api/searches/${selected.id}`);
      if (selected && followActive) {
        const next = selected.events.filter((event) => ['evolution_scheduled','generation_repair_scheduled'].includes(event.type)).at(-1)?.detail.child_search_id;
        if (next) selected = await api(`/api/searches/${next}`);
      }
      if (selected) {
        lineage = await api(`/api/searches/${selected.id}/lineage`);
        delivery = (await api(`/api/searches/${selected.id}/delivery`)).delivery;
      }
      if (message === 'error') message = '';
    } catch { message = 'error'; }
    renderRoot();
  }
  $('start-search').onclick = async () => {
    const task = taskUI.selected();
    if (!task || pending) return;
    pending = true; message = ''; renderRoot();
    followActive = true;
    try { selected = await api('/api/searches', { task_id: task.task_id, revision: task.revision, request_id: crypto.randomUUID().replaceAll('-', '') }); }
    catch (error) { message = error.code || 'error'; }
    finally { pending = false; await refresh(); }
  };
  $('cancel-search').onclick = async () => {
    if (!selected) return;
    try { selected = await api(`/api/searches/${selected.id}/cancel`, {}); }
    catch { message = 'error'; }
    await refresh();
  };
  async function act(path) {
    if (pending) return;
    pending = true; message = ''; followActive = true; renderRoot();
    try { selected = await api(path, {request_id: crypto.randomUUID().replaceAll('-', '')}); }
    catch (error) { message = error.code || 'error'; }
    finally { pending = false; await refresh(); }
  }
  $('export-service').onclick = async () => {
    if (pending || !selected) return;
    pending = true; message = ''; renderRoot();
    try { await api(`/api/searches/${selected.id}/delivery/export`,{}); }
    catch (error) { message = error.code || 'error'; }
    finally { pending = false; await refresh(); }
  };
  async function prepareDelivery(operator=false) {
    if (pending || !selected || delivery) return;
    pending = true; message = ''; renderRoot();
    try { delivery = await api(`/api/searches/${selected.id}/delivery`,{request_id:crypto.randomUUID().replaceAll('-',''),...(operator?{quality_mode:'operator_review'}:{})}); }
    catch (error) { message = error.code || 'error'; }
    finally { pending = false; await refresh();await reviews.refresh(); }
  }
  $('prepare-delivery').onclick=()=>prepareDelivery();
  $('prepare-review-delivery').onclick=()=>prepareDelivery(true);
  $('rollback-search').onclick = () => act(`/api/searches/${selected.id}/rollback`);
  return { render, refresh };
}
