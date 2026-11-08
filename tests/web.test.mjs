import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { messages, normalizeLocale, translate,errorText,fieldText } from '../web/i18n.mjs';
import { funnelStages,familyLabel,inferenceEvidence,invocationSummary } from '../web/search-ui.mjs';

test('English default is independent of browser locale; explicit Chinese is supported', () => {
  for (const locale of [null, undefined, '', 'zh', 'fr-FR']) assert.equal(normalizeLocale(locale), 'en-US');
  assert.equal(normalizeLocale('zh-CN'), 'zh-CN');
  assert.equal(translate('zh-CN', 'settings'), '设置');
});

test('competition keeps earlier pauses visible at later fidelity stages', () => {
  const record = {candidates:[{id:'a'},{id:'b'},{id:'c'}],decisions:[
    {candidate_id:'a',stage:'L1',action:'paused'},{candidate_id:'b',stage:'L1',action:'active'},
    {candidate_id:'c',stage:'L1',action:'active'},{candidate_id:'b',stage:'L2',action:'pruned'},
    {candidate_id:'c',stage:'L2',action:'active'},
  ]};
  assert.deepEqual(funnelStages(record),[
    {stage:'built',active:3,paused:0,pruned:0},{stage:'L1',active:2,paused:1,pruned:0},{stage:'L2',active:1,paused:1,pruned:1},
  ]);
});

test('all displayed static text and persisted states have both translations', () => {
  assert.deepEqual(Object.keys(messages['en-US']).sort(), Object.keys(messages['zh-CN']).sort());
  const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
  const keys = [...html.matchAll(/data-i18n="([^"]+)"/g)].map((m) => m[1]);
  for(const file of ['app.mjs','task-ui.mjs','search-ui.mjs']) {
    const source=readFileSync(new URL('../web/'+file,import.meta.url),'utf8');
    keys.push(...[...source.matchAll(/\bt\('([^']+)'\)/g)].map(m=>m[1]));
  }
  keys.push('queued', 'running', 'succeeded', 'failed', 'cancelled', 'foundation_check', 'error', 'created');
  for (const locale of ['en-US', 'zh-CN']) for (const key of keys) {
    assert.ok(messages[locale][key], `${locale}: ${key}`);
  }
});

test('workflow summaries localize states, families, costs and errors in Chinese and English',()=>{
  const calls=[{id:'first',charged_micro:260,reserved_micro:2000000},{id:'last',charged_micro:null,reserved_micro:2105344}];
  const row={call_id:'last',runtime:{model_call_ids:['first','last','first'],status:'failed'},error:'provider_usage_unverified',latency_ms:12.5};
  assert.deepEqual(inferenceEvidence(row,calls),{ids:['first','last'],complete:true,ceilingCny:2.105604});
  const zh=key=>translate('zh-CN',key),en=key=>translate('en-US',key);
  assert.match(invocationSummary(row,calls,zh),/失败.*模型调用次数: 2.*推理费用上界: ¥2\.105604.*耗时: 12\.5 ms/);
  assert.match(invocationSummary(row,calls,en),/Failed.*Model calls: 2.*Inference cost ceiling/);
  assert.equal(familyLabel('workflow.v1',zh),'多步骤方案');
  assert.equal(familyLabel('future.adapter',zh),'future.adapter');
  assert.match(errorText(zh,'provider_usage_unverified'),/预留费用继续保留/);
  assert.equal(errorText(zh,'provider_http_429'),'模型服务请求失败 (HTTP 429)');
  assert.equal(errorText(zh,'unknown_internal_english_code'),messages['zh-CN'].error);
  assert.equal(fieldText(zh,'samples'),'输入样本');
  assert.equal(fieldText(zh,'unknown_fields'),'');
});

test('missing call attribution is explicit and old single-call evidence remains readable',()=>{
  const zh=key=>translate('zh-CN',key);
  assert.match(invocationSummary({call_id:'not-in-ledger',latency_ms:1},[],zh),/费用归因尚不完整/);
  assert.equal(inferenceEvidence({call_id:'legacy'},[{id:'legacy',charged_micro:0,reserved_micro:2000000}]).ceilingCny,0);
  assert.deepEqual(inferenceEvidence({call_id:null,runtime:null},[]),{ids:[],complete:true,ceilingCny:0});
  assert.match(errorText(zh,'model_backed_serving_runtime_required'),/最终测试数据保持封存/);
  assert.match(translate('zh-CN','modelServingReady'),/源码包不会携带密钥/);
  assert.equal(translate('zh-CN','serviceAllowance'),'独立服务预留额度');
  assert.match(errorText(zh,'service_request_not_replayable'),/不会自动重跑/);
});
