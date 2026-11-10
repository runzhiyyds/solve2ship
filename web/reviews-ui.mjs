import { errorText } from './i18n.mjs';

export function reviewMetric(report,t) {
  if(report.quality_mode==='operator_review'&&report.review_state==='pending')return t('awaiting_operator_review');
  return report.quality_mode==='operator_review'
    ? `${t('operatorAccepted')}: ${report.accepted_review_count||0}/${report.count} · ${t('operatorUnverified')}`
    : `${t('deliveryChecks')}: ${report.provided_label_matches}/${report.count}`;
}

export function initializeReviews({api,t,onChange}) {
  const $=id=>document.getElementById(id);
  let search=null,delivery=null,validation=null,finalPacket=null,key='',pending=false,message='';
  const drafts=new Map();
  async function load() {
    if(!search)return;
    const id=search.id,mode=delivery?.report?.quality_mode;
    try {
      const nextValidation=await api(`/api/searches/${id}/quality-reviews`);
      const nextFinal=mode==='operator_review'?await api(`/api/searches/${id}/delivery/reviews`):null;
      if(search?.id!==id)return;
      validation=nextValidation;finalPacket=nextFinal;
    } catch(error){message=error.code||'error';}
    draw();
  }
  async function submit(path,body) {
    if(pending)return;
    const reviewer=$('quality-reviewer').value.trim();
    if(!reviewer||!$('quality-attested').checked){message='explicit_operator_attestation_required';draw();return;}
    pending=true;message='';draw();
    try {await api(path,{...body,request_id:crypto.randomUUID().replaceAll('-',''),reviewer,attested:true});drafts.delete(body.snapshot_sha256);}
    catch(error){message=error.code||'review_submission_uncertain';}
    finally{pending=false;await load();await onChange();}
  }
  function examples(packet,final=false) {
    const result=document.createDocumentFragment();
    for(const target of packet.targets) {
      const card=document.createElement('article'),heading=document.createElement('h4'),pre=document.createElement('pre');
      card.className='execution-record';heading.textContent=`${t('sampleLabel')} ${target.sample_index+1} · ${target.candidate_id.slice(0,8)}`;
      pre.textContent=JSON.stringify({input:target.input,output:target.output,error:target.error},null,2);card.append(heading,pre);
      if(target.latest_review){const previous=document.createElement('p');previous.textContent=`${t('recordedReview')}: ${t('review_'+target.latest_review.review.verdict)}`;card.append(previous);}
      if(!packet.closed){
        const comment=document.createElement('input');comment.maxLength=2000;comment.placeholder=t(target.latest_review?'reviewAmendReason':'reviewComment');
        comment.value=drafts.get(target.snapshot_sha256)||'';comment.oninput=()=>drafts.set(target.snapshot_sha256,comment.value);
        comment.setAttribute('aria-label',t('reviewComment'));card.append(comment);
        for(const verdict of ['accept','reject','unsure']) {
          const button=document.createElement('button');button.textContent=t('review_'+verdict);button.disabled=pending||(target.error&&verdict==='accept');
          button.onclick=()=>submit(`/api/searches/${search.id}/${final?'delivery/reviews':'quality-reviews'}`,{
            [final?'sample_index':'execution_id']:final?target.sample_index:target.execution_id,
            snapshot_sha256:target.snapshot_sha256,previous_review_id:target.latest_review?.id||null,verdict,comment:comment.value});
          card.append(button);
        }
      }
      result.append(card);
    }
    return result;
  }
  function draw() {
    $('quality-message').textContent=message?errorText(t,message):'';
    const next=JSON.stringify([validation,finalPacket,t('qualityReviewTitle'),pending]);
    if(key===next)return;key=next;
    $('validation-review-cases').replaceChildren();$('final-review-cases').replaceChildren();
    if(validation){
      const s=validation.summary,j=s.judge_agreement;
      $('validation-review-summary').textContent=`${t('reviewedCount')}: ${s.reviewed}/${s.total} · ${t('judgeOperatorAgreement')}: ${j.matches}/${j.count} (${t('descriptiveOnly')})`;
      $('validation-review-cases').append(examples(validation));
    }
    if(finalPacket){
      $('final-review-cases').append(examples(finalPacket,true));
      $('final-review-summary').textContent=t(finalPacket.closed?'qualityClosed':'reviewFinalHelp');
    }
    $('complete-quality-review').disabled=pending||!finalPacket||finalPacket.closed||finalPacket.targets.some(r=>!r.latest_review||r.latest_review.review.verdict==='unsure');
  }
  $('quality-review-panel').addEventListener('toggle',()=>{if($('quality-review-panel').open)load();});
  $('complete-quality-review').onclick=()=>finalPacket&&submit(`/api/searches/${search.id}/delivery/quality-decision`,{
    review_evidence_sha256:finalPacket.review_evidence_sha256,comment:''});
  return {
    render(nextSearch,nextDelivery){
      if(search?.id!==nextSearch?.id){validation=null;finalPacket=null;key='';message='';drafts.clear();}
      const changed=delivery?.report?.review_state!==nextDelivery?.report?.review_state;
      search=nextSearch;delivery=nextDelivery;
      $('quality-review-panel').hidden=!search||!search.executions?.length;
      $('final-review-section').hidden=delivery?.report?.quality_mode!=='operator_review';
      if(changed&&$('quality-review-panel').open)load();
      draw();
    }, refresh:load,
  };
}
