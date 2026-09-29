'use strict';
const $ = (selector) => document.querySelector(selector);
const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clockText = (seconds) => `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
const draftKey = stage => `nce2-lesson1-draft-${stage}`;
const owner = crypto.randomUUID();
let lesson, snapshot, current, noteBase = '', dirty = false, saveTimeout, clipEnd = null;
let queue = Promise.resolve();
const audio = $('#audio');

function error(message) { $('#error').hidden = false; $('#error').textContent = message; }
function clearError() { $('#error').hidden = true; }
async function request(url, options) {
  let response;
  try { response = await fetch(url, {cache:'no-store', ...options, signal:AbortSignal.timeout(8000)}); }
  catch (_) { throw new Error('暂时连不上本地助手。请保持启动终端打开；未保存的笔记会保留在当前浏览器。'); }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '操作未保存，请稍后重试。');
  return data;
}
function send(event) {
  const body = {id:crypto.randomUUID(), ...event};
  const operation = queue.catch(() => {}).then(async () => {
    const result = await request('/api/event', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    snapshot = result;
    updateChrome();
    return result;
  });
  queue = operation;
  return operation;
}
function stageInfo() { return lesson.stages.find(s => s.id === current); }
function stageSeconds(key) {
  const s = snapshot.state.stages[key];
  const timer = snapshot.state.timer;
  let seconds = s.seconds;
  if (timer && timer.stage === key) {
    const now = Date.now()/1000;
    const end = now - timer.heartbeat <= 45 ? now : timer.heartbeat;
    seconds += Math.max(0, end-timer.start);
  }
  return Math.max(seconds, s.reportedMinutes*60);
}
function tick() {
  if (!snapshot) return;
  const stage = stageInfo();
  const timer = snapshot.state.timer;
  const active = timer?.stage === current && Date.now()/1000 - timer.heartbeat <= 45;
  const mine = active && timer.owner === owner;
  const seconds = stageSeconds(current);
  $('#timer-clock').textContent = clockText(seconds);
  $('#timer-stage').textContent = `${stage.name}计时`;
  $('#timer-state').textContent = active ? (mine ? '计时中' : '另一窗口计时中') : '已暂停';
  $('#timer-budget').textContent = seconds >= stage.minutes*60 ? '已到建议用时。检查任务后再确认完成。' : `建议 ${stage.minutes} 分钟 · 还剩 ${clockText(stage.minutes*60-seconds)}`;
  $('#timer-toggle').textContent = mine ? '暂停计时' : (active ? '在本窗口接着计时' : '开始计时');
  const total = lesson.stages.reduce((sum, s) => sum+stageSeconds(s.id), 0);
  $('#total-time').textContent = `本课累计 ${Math.floor(total/60)} 分钟 / 计划 120 分钟`;
}
function updateChrome() {
  if (!snapshot || !lesson) return;
  const {summary, state} = snapshot;
  $('#percent').innerHTML = `${summary.percent}<small>%</small>`;
  $('#progress-fill').style.width = `${summary.percent}%`;
  $('#task-count').textContent = `${summary.completedTasks} / 20 个任务`;
  $('#stage-nav').innerHTML = lesson.stages.map((stage, i) => {
    const done = state.stages[stage.id].done.length === stage.tasks.length;
    return `<button class="nav-stage ${current===stage.id?'active':''} ${done?'done':''}" data-stage="${stage.id}" ${current===stage.id?'aria-current="step"':''}><span class="nav-number">${done?'✓':String(i+1).padStart(2,'0')}</span><span><strong>${stage.name}</strong><small>${stage.en}</small></span><span class="nav-mins">${stage.minutes}′</span></button>`;
  }).join('');
  $('#next-stage').textContent = summary.next.stage ? `${summary.next.name} · 建议 ${summary.next.minutes} 分钟` : '五个阶段已完成';
  $('#next-title').textContent = summary.next.title;
  $('#next-instruction').textContent = summary.next.instruction;
  $('#next-meta').textContent = `${summary.completedStages} / 5 个阶段完成 · 可以随时暂停，下次接着学`;
  $('#go-next').textContent = summary.next.stage ? '前往这一步 →' : '查看本课复盘 →';
  document.querySelectorAll('.task-check').forEach(input => {
    input.checked = state.stages[current].done.includes(input.dataset.task);
    input.closest('.task-row').classList.toggle('finished', input.checked);
  });
  $('#history-list').innerHTML = state.reports.length ? state.reports.slice(-8).reverse().map(r => `<div class="history-item"><time>${escapeHTML(new Date(r.at).toLocaleString('zh-CN'))}</time><p><strong>${lesson.stages.find(s=>s.id===r.stage).name} · ${{progress:'进行中',blocked:'卡住了',done:'已完成'}[r.status]}</strong></p><p>${escapeHTML(r.note || '已记录阶段状态。')}</p></div>`).join('') : '<p class="empty">还没有汇报。从第一遍盲听开始。</p>';
  tick();
}
function details(title, content, open=false) { return `<details class="learning-details" ${open?'open':''}><summary>${title}</summary><div class="details-body">${content}</div></details>`; }
function pairs(items) { return items.map(pair => `<div class="question-pair"><p>${escapeHTML(pair[0])}</p><p>${escapeHTML(pair[1])}</p></div>`).join(''); }
function transcript(showNotes=false, open=false) {
  const rows = lesson.sentences.filter(s=>!s.question).map(s => `<div class="sentence-row" id="sentence-${s.id}" data-start="${s.start}" data-end="${s.end}"><div class="sentence-en"><button class="sentence-play" data-sentence="${s.id}" aria-label="播放句子：${escapeHTML(s.text)}">▶</button><p lang="en">${escapeHTML(s.text)}</p></div><p class="sentence-zh" hidden>${escapeHTML(s.zh)}</p>${showNotes?`<p class="sentence-note">${escapeHTML(s.note)}</p>`:''}</div>`).join('');
  return details('课文与逐句播放 · 点击展开 / 隐藏', `<div class="transcript-tools"><span class="subtle">播放一句后自动暂停，留给你模仿。</span><label><input id="show-translation" type="checkbox">显示中文</label></div>${rows}`, open);
}
function materials(stage) {
  if (stage === 'listening') return `<div class="story-map"><strong>带着这个问题听</strong><br><span lang="en">Why did the writer complain to the people behind him?</span></div>${details('听完后，再核对三个问题', pairs(lesson.listeningAnswers))}${transcript(false)}`;
  if (stage === 'reading') return `${transcript(true,true)}${details('六个核心词组', lesson.vocabulary.map(v=>`<div class="vocab-row"><strong lang="en">${escapeHTML(v[0])}</strong><span>${escapeHTML(v[1])}</span><p lang="en">${escapeHTML(v[2])}</p></div>`).join(''),true)}${details('语法观察：动作、背景与说话方式','<p><strong>I went to the theatre.</strong> 用一般过去时推进故事。<strong>They were talking loudly.</strong> 用过去进行时交代当时持续的动作。</p><p><strong>I got angry.</strong> 用形容词描述状态；<strong>I looked at them angrily.</strong> 用副词说明动作方式。</p><p>先找主语、动词与宾语，再补方式、地点和时间。时间也可置于句首。</p>')}${details('精读练习 · 先在学习记录里作答', '<ol>'+lesson.readingExercises.map(x=>`<li>${escapeHTML(x[0])}</li>`).join('')+'</ol>',true)}${details('精读参考答案',pairs(lesson.readingExercises))}`;
  if (stage === 'shadowing') return `<div class="story-map"><strong>先听 → 暂停 → 模仿两次</strong><br>重读有信息的词：<span lang="en">TALKING LOUDLY · VERY ANGRY · PRIVATE CONVERSATION</span>。跟随原声的自然停顿。</div>${transcript(false,true)}${details('这三处，值得多练一次','<p><strong>were sitting behind me</strong>：把 were 读轻，把 sitting、behind 的重音读清。</p><p><strong>I could not bear it.</strong>：按意思分组，模仿 bear it 的自然衔接。</p><p><strong>This is a private conversation!</strong>：听出年轻人反驳时的语气，不只读准单词。</p>',true)}`;
  if (stage === 'retelling') return `${details('五个关键词 · 脱稿时收起', '<p class="english-example" lang="en">theatre → good seat → noisy couple → complaint → misunderstanding</p>',true)}${details('复述自查 · 讲完再看', '<ol>'+lesson.retellChecklist.map(t=>`<li>${escapeHTML(t)}</li>`).join('')+'</ol>')}${details('简化复述示例 · 先讲自己的版本', `<p class="english-example" lang="en">${escapeHTML(lesson.retellSample)}</p><p class="subtle">这是改写示例，不要求逐字背诵。</p>`)}${transcript(false)}`;
  return `${details('出口小测 · 先闭卷作答', '<ol>'+lesson.exitQuiz.map(x=>`<li>${escapeHTML(x[0])}</li>`).join('')+'</ol>',true)}${details('出口小测参考答案', pairs(lesson.exitQuiz))}<div class="story-map"><strong>给明天留 10 分钟</strong><br>盲听 2 分钟 → 脱稿复述 5 分钟 → 修正卡点 3 分钟。<br>在学习记录里写一个具体时间，回来告诉我“复习 Lesson 1”。</div>`;
}
function renderStage() {
  const stage = stageInfo();
  $('#stage-content').innerHTML = `<section class="stage-header"><p class="eyebrow">${String(lesson.stages.indexOf(stage)+1).padStart(2,'0')} / ${stage.en.toUpperCase()} · ${stage.minutes} MIN</p><h2>${stage.headline}</h2><p class="stage-intro">${stage.intro}</p></section><div class="task-list">${stage.tasks.map(task=>`<div class="task-row"><input type="checkbox" class="task-check" id="${task.id}" data-task="${task.id}"><div><label for="${task.id}">${task.title}</label><p>${task.instruction}</p></div><span class="task-minutes">${task.minutes} 分钟</span></div>`).join('')}</div><p class="criterion"><strong>这一阶段的完成标准</strong>${stage.criterion}</p>${materials(current)}`;
  $('#note-label').textContent = stage.noteLabel;
  $('#notes').placeholder = stage.placeholder;
  $('#notes').value = snapshot.state.stages[current].notes;
  noteBase = snapshot.state.stages[current].notes;
  $('#confidence').value = snapshot.state.stages[current].confidence || '';
  dirty = false;
  try {
    const draft = JSON.parse(localStorage.getItem(draftKey(current)) || 'null');
    if (draft && (draft.note !== noteBase || draft.confidence !== ($('#confidence').value || ''))) {
      $('#notes').value = draft.note;
      $('#confidence').value = draft.confidence || '';
      dirty = true;
    }
  } catch (_) {}
  saveLabel(dirty ? '已恢复草稿，请保存' : '已保存到本机');
  $('#report-status').value = 'progress';
  $('#report-feedback').textContent = '';
  $('#reported-minutes').value = '';
  updateChrome();
}
function saveLabel(text) { $('#save-status').textContent = text; $('#save-status').classList.toggle('dirty',dirty); }
function rememberDraft() {
  dirty = true;
  try { localStorage.setItem(draftKey(current),JSON.stringify({note:$('#notes').value,confidence:$('#confidence').value})); } catch (_) {}
  saveLabel('正在编辑…');
  clearTimeout(saveTimeout);
  saveTimeout = setTimeout(()=>saveNotes().catch(e=>error(e.message)),650);
}
async function saveNotes() {
  clearTimeout(saveTimeout);
  if (!dirty) return;
  const stage = current;
  const note = $('#notes').value;
  const confidence = $('#confidence').value ? Number($('#confidence').value) : null;
  saveLabel('正在保存…');
  try {
    await send({action:'notes',stage,note,confidence,baseNote:noteBase});
    if (current === stage) {
      noteBase = note;
      if ($('#notes').value === note && (Number($('#confidence').value)||null) === confidence) {
        dirty = false;
        try { localStorage.removeItem(draftKey(stage)); } catch (_) {}
      }
      saveLabel(dirty?'有新编辑待保存':'已保存到本机');
    }
  } catch (e) { saveLabel('保存失败 · 草稿已保留'); throw e; }
}
async function changeStage(key, scroll=false) {
  if (!lesson.stages.some(s=>s.id===key)) return;
  await saveNotes();
  audio.pause(); clipEnd = null;
  if (snapshot.state.timer?.owner === owner && snapshot.state.timer.stage !== key) await send({action:'timer',stage:current,command:'pause',owner});
  current = key;
  $('#report-note').value = '';
  renderStage();
  if (scroll) $('#stage-content').scrollIntoView({behavior:'auto',block:'start'});
}
async function playAt(start, end=null) {
  clipEnd = end;
  audio.currentTime = start;
  try { await audio.play(); clearError(); }
  catch (_) { error('音频暂时无法播放。请确认本地服务仍在运行，再点击播放。'); }
}
$('#stage-nav').addEventListener('click', e=> { const button=e.target.closest('[data-stage]'); if(button) changeStage(button.dataset.stage).catch(e=>error(e.message)); });
$('#go-next').addEventListener('click', ()=>changeStage(snapshot.summary.next.stage || 'review',true).catch(e=>error(e.message)));
$('#stage-content').addEventListener('change', async e=> {
  if (e.target.id === 'show-translation') document.querySelectorAll('.sentence-zh').forEach(p=>p.hidden=!e.target.checked);
  if (!e.target.matches('.task-check')) return;
  e.target.disabled = true;
  try { await send({action:'task',stage:current,task:e.target.dataset.task,checked:e.target.checked}); clearError(); }
  catch (err) { error(err.message); updateChrome(); }
  finally { e.target.disabled=false; }
});
$('#stage-content').addEventListener('click',e=> { const button=e.target.closest('[data-sentence]'); if(button) {const s=lesson.sentences.find(s=>s.id===button.dataset.sentence); playAt(s.start,s.end);} });
$('#play-story').addEventListener('click',()=>playAt(lesson.sentences[1].start));
$('#replay').addEventListener('click',()=> { clipEnd=null; audio.currentTime=Math.max(0,audio.currentTime-5); });
$('#speed').addEventListener('change',e=>audio.playbackRate=Number(e.target.value));
audio.addEventListener('timeupdate',()=> {
  if(clipEnd !== null && audio.currentTime>=clipEnd) { audio.pause(); clipEnd=null; $('#audio-status').textContent='这一句播完了。轮到你模仿两次。'; }
  document.querySelectorAll('.sentence-row').forEach(row=>row.classList.toggle('playing',audio.currentTime>=Number(row.dataset.start)&&audio.currentTime<Number(row.dataset.end)));
});
audio.addEventListener('play',()=>$('#audio-status').textContent=clipEnd!==null?'逐句播放 · 句末自动暂停':'正在播放本地课文原声');
audio.addEventListener('error',()=>error('原声音频加载失败。请重新运行“启动学习助手.command”，并确认 assets/audio/lesson-01.mp3 存在。'));
audio.addEventListener('ended',()=>{clipEnd=null;$('#audio-status').textContent='播放结束。完成练习后勾选对应任务。';});
$('#notes').addEventListener('input',rememberDraft);
$('#confidence').addEventListener('change',rememberDraft);
$('#save-notes').addEventListener('click',()=>saveNotes().then(clearError).catch(e=>error(e.message)));
$('#timer-toggle').addEventListener('click', async ()=> {
  const button=$('#timer-toggle');button.disabled=true;
  try { const timer=snapshot.state.timer; await send({action:'timer',stage:current,command:timer?.stage===current&&timer.owner===owner?'pause':'start',owner}); clearError(); }
  catch(e){error(e.message);} finally{button.disabled=false;}
});
$('#report-form').addEventListener('submit',async e=> {
  e.preventDefault();
  const button=e.target.querySelector('button[type=submit]');button.disabled=true;
  const stage=current;
  try {
    await saveNotes();
    const minutes=$('#reported-minutes').value;
    await send({action:'report',stage,status:$('#report-status').value,note:$('#report-note').value,minutes:minutes===''?null:Number(minutes),confidence:$('#confidence').value?Number($('#confidence').value):null});
    if(current===stage) {
      $('#notes').value=snapshot.state.stages[stage].notes;noteBase=$('#notes').value;
      $('#report-note').value='';$('#reported-minutes').value='';$('#report-status').value='progress';
      const next=snapshot.summary.next;
      $('#report-feedback').textContent=`已保存。下一步：${next.name} · ${next.title}。${next.instruction}`;
    }
    clearError();
  }catch(err){error(err.message);$('#report-feedback').textContent='汇报尚未保存，请保留内容后重试。';}
  finally{button.disabled=false;}
});
window.addEventListener('pagehide',()=> {
  if(snapshot?.state.timer?.owner===owner) navigator.sendBeacon('/api/event',new Blob([JSON.stringify({id:crypto.randomUUID(),action:'timer',stage:snapshot.state.timer.stage,command:'pause',owner})],{type:'application/json'}));
});
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
async function sync() {
  if(!snapshot) return;
  try {
    const timer=snapshot.state.timer;
    if(timer?.owner===owner) await send({action:'timer',stage:timer.stage,command:'pulse',owner});
    else { await queue.catch(()=>{}); snapshot=await request('/api/state'); updateChrome(); }
    if(!dirty && document.activeElement!==$('#notes') && document.activeElement!==$('#confidence')) {
      $('#notes').value=snapshot.state.stages[current].notes;noteBase=$('#notes').value;
      $('#confidence').value=snapshot.state.stages[current].confidence||'';
    }
  }catch(e){error(e.message);}
}
(async()=> {
  try {
    [lesson,snapshot]=await Promise.all([request('/api/lesson'),request('/api/state')]);
    current=snapshot.summary.next.stage||'review';
    $('#loading').hidden=true;$('#workspace').hidden=false;
    renderStage();
    setInterval(tick,1000);
    setInterval(sync,10000);
  } catch(e) { $('#loading').textContent='学习助手暂时未能打开。请重新启动本地服务并刷新页面。'; error(e.message); }
})();
