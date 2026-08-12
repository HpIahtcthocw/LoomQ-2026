/* LoomQ 网页入口 · 交互逻辑
 *
 * 组织方式：一个 stage 状态机（intro → pick → result → ask → quiz），
 * 加一个跑任务的轮询器。没有框架，因为整页只有五屏、一个请求循环和一张画布，
 * 引入构建工具反而会让"解压即用、离线可跑"这件事变难。
 *
 * 计时器是刻意放在最显眼位置的：专项奖的判据原话是"五分钟内完成人生第一个
 * 实验"，那就把这个标准摆到台面上，让它可被当场证伪。
 */

const $ = (id) => document.getElementById(id);
const api = {
  state: () => fetch('/api/state').then((r) => r.json()),
  run: (body) => fetch('/api/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then((r) => r.json()),
  job: (id) => fetch('/api/job?id=' + encodeURIComponent(id)).then((r) => r.json()),
};

const state = {
  stage: 'intro',
  started: 0,
  elapsed: 0,
  ticker: null,
  ran: 0,
  askedOwn: false,
  ranHardware: false,
  hardwareDown: false,
  last: null,
  quiz: { picked: {}, right: 0 },
  backends: [],
};

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

function toast(text, ms = 2800) {
  const el = $('toast');
  el.textContent = text;
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, ms);
}

/* ── 舞台切换与计时 ─────────────────────────────────── */

const STAGES = ['intro', 'pick', 'result', 'ask', 'quiz'];

function go(name) {
  if (!STAGES.includes(name)) return;
  state.stage = name;
  for (const id of STAGES) $('stage-' + id).classList.toggle('is-active', id === name);
  window.scrollTo({ top: 0, behavior: 'smooth' });
  paintRail();
}

/* 步骤条同时是导航和进度。没走到的步骤禁用——给零基础用户一条唯一的路，
 * 比给他五个平等的入口更容易走完。 */
function paintRail() {
  const reached = {
    pick: state.started > 0,
    result: state.ran > 0,
    ask: state.ran > 0,
    quiz: state.ran > 0 && state.askedOwn,
  };
  for (const btn of document.querySelectorAll('.rail__step')) {
    const target = btn.dataset.goto;
    btn.disabled = !reached[target];
    const done = { pick: state.ran > 0, result: state.askedOwn, ask: state.askedOwn, quiz: state.quiz.right > 0 }[target];
    btn.dataset.state = state.stage === target ? 'here' : (done ? 'done' : '');
  }
}

function startClock() {
  if (state.started) return;
  state.started = Date.now();
  $('clock').hidden = false;
  state.ticker = setInterval(() => {
    state.elapsed = Math.floor((Date.now() - state.started) / 1000);
    const m = Math.floor(state.elapsed / 60);
    const s = String(state.elapsed % 60).padStart(2, '0');
    $('clock-time').textContent = `${m}:${s}`;
    $('clock').classList.toggle('is-over', state.elapsed > 300);
  }, 1000);
}

/* 实时算，不读 ticker 缓存的那个值。第一次跑完往往在计时器 tick 之前，
 * 读缓存会让「你用了多久」显示成 0 秒——恰好是最该显示对的那一刻。 */
function elapsedText() {
  const t = state.started ? Math.floor((Date.now() - state.started) / 1000) : 0;
  return t < 60 ? `${t} 秒` : `${Math.floor(t / 60)} 分 ${t % 60} 秒`;
}

/* ── 启动 ───────────────────────────────────────────── */

const ASK_PRESETS = [
  '让三个比特全都纠缠起来',
  '做一个量子随机数，结果均匀分布在 0 到 7',
  '在八个抽屉里找出第五个',
  '让两个比特有 25% 的概率同时是 1',
];

async function boot() {
  let info;
  try {
    info = await api.state();
  } catch {
    toast('连不上本地服务，刷新一下页面试试');
    return;
  }
  state.backends = info.backends;

  $('cards').innerHTML = info.examples.map((ex, i) => `
    <button class="card" data-example="${ex.key}">
      <span class="card__n">EXAMPLE ${String(i + 1).padStart(2, '0')}</span>
      <span class="card__title">${ex.title}</span>
      <span class="card__term">${ex.term || ''}</span>
      <span class="card__wire"></span>
      <span class="card__desc">${ex.explanation}</span>
      <span class="card__go">点这里运行 →</span>
    </button>`).join('');
  // 「电路」对没接触过的人是个空词。先在卡片上画出来，点进去才不是从零开始。
  $('cards').querySelectorAll('.card__wire').forEach((slot, i) => {
    const layout = info.examples[i] && info.examples[i].circuit;
    if (layout) slot.appendChild(drawCircuit(layout, MINI));
  });

  $('sel-backend').innerHTML = info.backends
    .map((b) => `<option value="${b.id}" ${b.ready ? '' : 'disabled'}>${b.name}${b.ready ? '' : '（不可用）'}</option>`)
    .join('');

  $('ask-chips').innerHTML = ASK_PRESETS
    .map((q) => `<button class="chip" data-preset="${q}">${q}</button>`).join('');

  $('ask-hint').textContent = info.llm
    ? '交给智能体理解并生成电路，通常几秒钟'
    : '还没配置模型服务，这一步会用不了。可以先回去跑现成的例子，流程完全一样。';
  $('btn-ask').disabled = !info.llm;

  $('dock').hidden = false;
  updateDockNote();
  paintRail();
  await replay();
}

/* 直达参数。录演示和回归截图要能确定性地复现任意一屏，
 * 靠人手点四步再截图，每次的状态都不一样。
 *   ?example=1        跑第一个内置示例并停在结果页
 *   ?ask=让三个比特…   走自然语言路径
 *   ?backend=spinq_cloud  指定后端
 *   ?stage=quiz       直接跳到某一屏
 *   ?answers=1,0,2    按序作答（用于截验收页）
 *   ?cert=1           直接出凭证
 */
async function replay() {
  const p = new URLSearchParams(location.search);
  if (![...p.keys()].length) return;

  if (p.get('backend')) $('sel-backend').value = p.get('backend');
  if (p.get('shots')) $('sel-shots').value = p.get('shots');
  updateDockNote();
  if (p.has('clock')) { startClock(); state.started -= (parseInt(p.get('clock'), 10) || 0) * 1000; }

  // ?job=<编号> 直接回放一个已经跑完的任务。真机一次要排两分钟队，
  // 录演示或截图时不该为了看同一份结果再排一次。
  if (p.get('job')) {
    const j = await api.job(p.get('job')).catch(() => null);
    if (j && j.status === 'done') { go('result'); show(j.result); }
    else toast('这个任务编号取不到了，服务重启后内存里的任务会清空');
  } else if (p.get('example')) {
    startClock();
    await run({ example: p.get('example') }, '正在运行');
  } else if (p.get('ask')) {
    startClock();
    state.askedOwn = true;
    await run({ question: p.get('ask') }, '正在理解你的话，并把它变成一个量子电路');
  }

  if (p.get('answers')) {
    // 凭证要写"跑了哪个实验"，所以答题前必须真的有一次运行垫底
    if (!state.last) await run({ example: '1' }, '正在运行');
    state.askedOwn = true;
    buildQuiz();
    go('quiz');
    p.get('answers').split(',').forEach((k, i) => answer(i, parseInt(k, 10)));
  }
  if (p.get('stage')) { if (p.get('stage') === 'quiz') buildQuiz(); go(p.get('stage')); }
  if (p.get('cert')) { drawCert($('cert-canvas')); $('veil-cert').hidden = false; }
}

function updateDockNote() {
  const id = $('sel-backend').value;
  const b = state.backends.find((x) => x.id === id);
  const hw = id === 'spinq_cloud';
  $('dock-note').dataset.hw = hw ? '1' : '0';
  $('dock-note').textContent = hw
    ? '真机在深圳，要排队，一次约两分钟。它只有 2 到 3 个量子比特，而且会算错——这恰恰是最值得看的部分。'
    : (b && b.note) || '模拟器算的是精确的理想值，立刻出结果。';
}

/* ── 跑一次实验 ─────────────────────────────────────── */

async function run(payload, titleHint) {
  go('result');
  $('result').hidden = true;
  $('running').hidden = false;
  $('running-title').textContent = titleHint;
  $('running-log').innerHTML = '';

  state.lastPayload = payload;
  const body = {
    ...payload,
    backend: $('sel-backend').value || 'refsim',
    shots: parseInt($('sel-shots').value, 10) || 1024,
  };

  let started;
  try {
    started = await api.run(body);
  } catch {
    return fail('请求没能发出去，本地服务可能已经停了。');
  }
  if (started.error) return fail(started.error);

  // 真机排队要一两分钟，这期间后端一句进度也不会再吐。没有任何动静的
  // 一百秒足以让人认为页面死了然后关掉——被试就是这么丢掉真机那一步的。
  // 秒表本身不提供信息，但它证明这个页面还活着。
  const t0 = Date.now();
  const waitBox = $('running-wait');
  waitBox.hidden = body.backend !== 'spinq_cloud';
  const tick = setInterval(() => {
    const s = Math.floor((Date.now() - t0) / 1000);
    waitBox.textContent = `已等待 ${s} 秒　排队通常一到两分钟，页面会一直等，不用关`;
  }, 1000);

  try {
    return await pollUntilDone(started.job);
  } finally {
    clearInterval(tick);
    waitBox.hidden = true;
  }
}

async function pollUntilDone(jobId) {
  let seen = 0;
  for (;;) {
    await wait(700);
    let job;
    try {
      job = await api.job(jobId);
    } catch {
      return fail('取不到运行状态，本地服务可能已经停了。');
    }
    if (job.progress && job.progress.length > seen) {
      for (const line of job.progress.slice(seen)) {
        const li = document.createElement('li');
        li.textContent = line;
        $('running-log').appendChild(li);
      }
      seen = job.progress.length;
    }
    if (job.status === 'error') return fail(job.error);
    if (job.status === 'done') return show(job.result);
  }
}

function fail(message) {
  $('running').hidden = true;
  $('result').hidden = false;
  $('r-backend').textContent = '没能跑成';
  $('r-backend').dataset.hw = '0';
  $('r-title').textContent = '这一次没成功';
  $('r-intent').textContent = message;
  $('r-fallback').hidden = true;
  $('r-circuit').innerHTML = '';
  $('r-bars').innerHTML = '';
  $('r-meta').textContent = '';
  $('r-shots').textContent = '';
  $('r-legend').textContent = '';
  $('r-explain').textContent = '这不是你的问题。换个说法再试一次，或者回去跑一个现成的例子。';
  $('r-noise').innerHTML = '';
  $('next-title').textContent = '回到第一步';
  $('next-note').textContent = '现成的例子一定能跑通';
  $('btn-next').textContent = '看现成的例子';
  $('btn-next').onclick = () => go('pick');
}

function show(r) {
  state.ran += 1;
  state.last = r;
  if (r.on_hardware) state.ranHardware = true;

  $('running').hidden = true;
  $('result').hidden = false;

  $('r-backend').textContent = r.on_hardware ? '真实量子计算机 · ' + r.backend_note : r.backend_note;
  $('r-backend').dataset.hw = r.on_hardware ? '1' : '0';
  $('r-title').textContent = r.title;
  $('r-term').textContent = r.term || '';
  $('r-term').hidden = !r.term;
  $('r-intent').textContent = r.explanation || '';

  // 他点了真机却拿到模拟器结果时，得当面把原因讲清楚，
  // 否则这一屏和普通模拟器结果长得一模一样，只会让人以为是自己按错了。
  const fb = r.fallback;
  if (fb) state.hardwareDown = true;
  $('r-fallback').hidden = !fb;
  if (fb) {
    $('r-fallback-title').textContent = fb.title;
    $('r-fallback-hint').textContent = fb.hint || '';
    $('r-fallback-detail').textContent = fb.detail || '';
    $('r-fallback-detail').hidden = !fb.detail;
  }

  $('r-circuit').innerHTML = '';
  $('r-circuit').appendChild(drawCircuit(r.circuit));
  $('r-meta').textContent =
    `用了 ${r.circuit.n_qubits} 个比特 · 图上是 ${r.circuit.n_gates} 步操作`
    + `加最后 ${r.circuit.n_measures} 个读数方块（写着 M 的那种） · `
    + `最忙的那个比特要走 ${r.circuit.depth} 步（行内把这个叫「线路深度」，步数越多越容易出错）`;

  $('r-shots').textContent =
    `同一个电路从头到尾跑了 ${r.shots} 遍，每遍最后都读出一串 0 和 1。`
    + `下面一条就是一种读数，条越长说明这种读数出现得越多。`
    + `跑这么多遍是因为量子的结果本来就是随机的，只看一遍什么也说明不了。`;
  drawBars(r);
  $('r-legend').textContent = r.legend;

  // 真机模式下这段讲的是理想分布，而上面的条是实测的。不点破这一层，
  // 用户会看到「主要就两种结果」配着四条柱子，以为哪边写错了。
  $('r-explain-lead').hidden = !r.on_hardware;
  $('r-explain-lead').textContent = r.on_hardware
    ? '先说这个电路本该给出什么。下面这段讲的是理想情况，也就是上面那条虚线的位置；真机实际量到的偏差留到最后一段说。'
    : '';
  $('r-explain').textContent = r.explain;
  $('r-noise').innerHTML = r.noise
    ? `<div class="noise"><div class="noise__title">真机差在哪</div><p class="prose">${escapeHtml(r.noise)}</p></div>`
    : '';

  paintNext();
  paintRail();
}

/* 每次跑完都要有明确的下一步，否则用户就停在这里了。
 *
 * 这里曾经是一条死板的直线：必须先「自己说一个」，真机按钮才肯出现。
 * 结果被试跑完示例想去真机，界面根本不给入口——他只能自己翻右下角那个
 * 折叠着的「运行环境」去换后端。实测反馈就是一句「真机的跑不了」。
 * 判据里「在真实量子机上完成」是硬指标，通往它的路不能藏在二级菜单里。
 */
function paintNext() {
  const hwReady = state.backends.some((b) => b.id === 'spinq_cloud' && b.ready);
  const btn = $('btn-next');
  const alt = $('btn-alt');
  alt.hidden = true;

  const toHardware = () => {
    $('sel-backend').value = 'spinq_cloud';
    updateDockNote();
    // 重放上一次的 payload，而不是拿标题回头再问一遍模型——
    // 真机对照要成立，两边必须是同一个电路。
    run(state.lastPayload || { example: '1' }, '正在送往真实的量子计算机');
  };
  const toAsk = () => go('ask');
  const toQuiz = () => { buildQuiz(); go('quiz'); };

  const setAlt = (label, fn) => {
    alt.hidden = false;
    alt.textContent = label;
    alt.onclick = fn;
  };

  // 真机试过一次却没跑成（云上一台在线的都没有），就别再把它当主按钮劝下去了。
  // 反复点同一个按钮撞同一堵墙，比一开始就没有这个按钮更让人泄气。
  if (hwReady && !state.ranHardware && !state.hardwareDown) {
    $('next-title').textContent = state.ran === 1
      ? '你已经跑完了人生第一个量子实验'
      : '到这里为止，跑的都是模拟器';
    $('next-note').textContent =
      `用时 ${elapsedText()}。刚才那是模拟器算出来的理想结果。` +
      '把同一个电路送到深圳那台真的量子计算机上，看看真实世界里它长什么样——要排队约两分钟。';
    btn.textContent = '送到真机上跑';
    btn.onclick = toHardware;
    setAlt(state.askedOwn ? '去验收' : '先自己说一个', state.askedOwn ? toQuiz : toAsk);
    return;
  }

  if (state.hardwareDown && !state.ranHardware && !state.askedOwn) {
    $('next-title').textContent = '真机这会儿没在线，流程照走';
    $('next-note').textContent =
      `用时 ${elapsedText()}。上面那段已经说了原因。真机什么时候能排上不由我们决定，` +
      '但接下来这步不用等它——用你自己的话说一个电路。';
    btn.textContent = '换我自己说一个';
    btn.onclick = toAsk;
    setAlt('再试一次真机', toHardware);
    return;
  }

  if (!state.askedOwn) {
    $('next-title').textContent = state.ranHardware
      ? '真机你也跑过了'
      : '你已经跑完了人生第一个量子实验';
    $('next-note').textContent = `用时 ${elapsedText()}。接下来试试不用现成的例子，用你自己的话说一个。`;
    btn.textContent = '换我自己说一个';
    btn.onclick = toAsk;
    setAlt('直接去验收', toQuiz);
    return;
  }

  $('next-title').textContent = '最后一步';
  $('next-note').textContent = '三个问题，检验一下刚才发生的事你是不是真的看懂了。';
  btn.textContent = '去验收';
  btn.onclick = toQuiz;
}

/* ── 电路图 ─────────────────────────────────────────── */

const FULL = { colW: 80, rowH: 70, padL: 68, padT: 30, box: 44, wire: 14, label: 14, mini: false };
const MINI = { colW: 34, rowH: 30, padL: 26, padT: 12, box: 20, wire: 6, label: 9, mini: true };
const NS = 'http://www.w3.org/2000/svg';

function el(name, attrs = {}, text) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

function drawCircuit(layout, s = FULL) {
  const cols = layout.columns.length || 1;
  const w = s.padL + cols * s.colW + (s.mini ? 10 : 30);
  const h = s.padT * 2 + layout.n_qubits * s.rowH;
  const svg = el('svg', {
    class: 'circuit', width: w, height: h, viewBox: `0 0 ${w} ${h}`,
    'aria-hidden': s.mini ? 'true' : 'false',
  });

  const rowY = (q) => s.padT + q * s.rowH + s.rowH / 2;
  const colX = (c) => s.padL + c * s.colW + s.colW / 2;

  for (let q = 0; q < layout.n_qubits; q++) {
    svg.appendChild(el('line', {
      x1: s.padL - s.wire - 4, y1: rowY(q), x2: w - s.wire, y2: rowY(q),
      stroke: 'rgba(122,170,214,0.3)', 'stroke-width': 1,
    }));
    svg.appendChild(el('text', {
      x: s.padL - s.wire - 12, y: rowY(q) + s.label / 3, 'text-anchor': 'end',
      fill: '#5d6d84', 'font-size': s.label,
    }, `q${q}`));
  }

  layout.columns.forEach((items, c) => {
    for (const item of items) {
      const x = colX(c);
      const qs = item.qubits;
      if (qs.length > 1) {
        svg.appendChild(el('line', {
          x1: x, y1: rowY(Math.min(...qs)), x2: x, y2: rowY(Math.max(...qs)),
          stroke: 'rgba(111,215,232,0.55)', 'stroke-width': s.mini ? 1 : 1.2,
        }));
      }
      svg.appendChild(glyph(item, x, rowY, s));
    }
  });

  return svg;
}

function glyph(item, x, rowY, s) {
  const g = el('g', { class: s.mini ? '' : 'gatebox' });
  if (!s.mini) {
    const hint = item.hint + (item.params && item.params.length ? `（角度 ${item.params.join(', ')}）` : '');
    g.appendChild(el('title', {}, hint));
  }

  const name = item.name || '';
  const qs = item.qubits;

  if (item.kind === 'measure') {
    box(g, x, rowY(qs[0]), 'M', '#f0a463', 'rgba(240,164,99,0.1)', s);
    if (!s.mini) {
      g.appendChild(el('text', {
        x, y: rowY(qs[0]) + s.box / 2 + 16, 'text-anchor': 'middle',
        fill: '#5d6d84', 'font-size': 11,
      }, `c[${item.clbit}]`));
    }
    return g;
  }

  if (name === 'cx' || name === 'ccx') {
    const target = qs[qs.length - 1];
    for (const q of qs.slice(0, -1)) dot(g, x, rowY(q), s);
    oplus(g, x, rowY(target), s);
    return g;
  }
  if (name === 'swap') {
    for (const q of qs) cross(g, x, rowY(q), s);
    return g;
  }
  if (name === 'cu1') {
    dot(g, x, rowY(qs[0]), s);
    box(g, x, rowY(qs[1]), 'U1', '#6fd7e8', 'rgba(11,18,32,0.98)', s);
    return g;
  }

  box(g, x, rowY(qs[0]), item.label, '#6fd7e8', 'rgba(11,18,32,0.98)', s);
  if (item.params && item.params.length && !s.mini) {
    g.appendChild(el('text', {
      x, y: rowY(qs[0]) + s.box / 2 + 15, 'text-anchor': 'middle',
      fill: '#5d6d84', 'font-size': 11,
    }, item.params.join(',')));
  }
  return g;
}

function box(g, x, y, label, stroke, fill, s) {
  g.appendChild(el('rect', {
    class: 'gatebox__rect',
    x: x - s.box / 2, y: y - s.box / 2 + 2, width: s.box, height: s.box - 4, rx: 3,
    fill, stroke, 'stroke-width': s.mini ? 1 : 1.1,
  }));
  g.appendChild(el('text', {
    x, y: y + (s.mini ? 3.5 : 6), 'text-anchor': 'middle', fill: '#e4ebf4',
    'font-size': (label.length > 2 ? 0.3 : 0.39) * s.box,
  }, label));
}

function dot(g, x, y, s) {
  g.appendChild(el('circle', { cx: x, cy: y, r: s.box * 0.125, fill: '#6fd7e8' }));
}

function oplus(g, x, y, s) {
  const r = s.box * 0.32;
  const wide = s.mini ? 1 : 1.3;
  g.appendChild(el('circle', {
    cx: x, cy: y, r, fill: 'rgba(5,8,15,0.95)', stroke: '#6fd7e8', 'stroke-width': wide,
  }));
  g.appendChild(el('line', { x1: x - r, y1: y, x2: x + r, y2: y, stroke: '#6fd7e8', 'stroke-width': wide }));
  g.appendChild(el('line', { x1: x, y1: y - r, x2: x, y2: y + r, stroke: '#6fd7e8', 'stroke-width': wide }));
}

function cross(g, x, y, s) {
  const r = s.box * 0.2;
  for (const d of [1, -1]) {
    g.appendChild(el('line', {
      x1: x - r, y1: y - r * d, x2: x + r, y2: y + r * d,
      stroke: '#6fd7e8', 'stroke-width': s.mini ? 1.2 : 1.5,
    }));
  }
}

/* ── 结果条 ─────────────────────────────────────────── */

function drawBars(r) {
  const entries = Object.entries(r.distribution).sort((a, b) => b[1] - a[1]).slice(0, 8);
  const peak = entries[0] ? entries[0][1] : 1;
  const ideal = r.ideal || null;

  const wrap = document.createElement('div');
  wrap.className = 'bars';
  wrap.innerHTML = entries.map(([key, p]) => {
    const count = r.counts[key] || 0;
    const ghost = ideal && ideal[key] !== undefined
      ? `<i class="bar__ghost" style="--w:${((ideal[key] / peak) * 100).toFixed(2)}%"></i>` : '';
    return `<div class="bar" data-hw="${r.on_hardware ? 1 : 0}">
      <span class="bar__key">${key}</span>
      <span class="bar__track"><i class="bar__fill" style="--w:${((p / peak) * 100).toFixed(2)}%"></i>${ghost}</span>
      <span class="bar__val">${(p * 100).toFixed(1)}%　${count} 次</span>
    </div>`;
  }).join('');

  if (ideal) {
    const note = document.createElement('div');
    note.className = 'bars__key';
    note.innerHTML = '<span><i></i>虚线是这个电路本该给出的理想值，实心条是真机实际量到的</span>';
    wrap.appendChild(note);
  }

  $('r-bars').innerHTML = '';
  $('r-bars').appendChild(wrap);
}

/* ── 验收 ───────────────────────────────────────────── */

const QUESTIONS = [
  {
    ask: '刚才的结果里，为什么只出现了少数几种位串，而不是所有可能都各占一点？',
    opts: [
      '因为测量次数还不够多，剩下的还没轮到',
      '因为这些比特被电路绑在了一起，一个的结果决定了另一个',
      '因为量子计算机只能输出这几个数',
    ],
    right: 1,
    why: '这就是纠缠。电路里那个受控非门把两个比特连了起来，所以它们要么一起是 0，要么一起是 1，不会各行其是。中间那些结果的概率被干涉抵消掉了。',
  },
  {
    ask: '为什么要重复测量上千次，而不是测一次就好？',
    opts: [
      '因为每次测量只随机吐一个答案，只有重复很多次才看得出分布',
      '因为机器不准，多测几次求个平均值',
      '因为量子计算机跑一次太便宜了，不跑白不跑',
    ],
    right: 0,
    why: '量子计算的答案本身就是概率性的。一次测量只能给你一个塌缩后的结果，看不出任何规律；分布才是这个电路真正的输出。这跟机器准不准是两回事。',
  },
  {
    ask: '在真机上跑，会冒出一些本该概率为零的结果。这说明什么？',
    opts: [
      '说明电路写错了，需要改',
      '说明量子计算本来就是随机的，没有规律可言',
      '说明量子态很脆弱，环境干扰、门操作误差和读取误差都会让结果偏掉',
    ],
    right: 2,
    why: '这叫退相干和门误差。量子态维持不了多久，每个操作也都有误差，所以真机结果总是「脏」的。今天全世界的量子计算机都这样——这正是它还没法投入实用的原因，也是为什么模拟器仍然重要。',
  },
];

function buildQuiz() {
  if ($('quiz').dataset.built) return;
  $('quiz').dataset.built = '1';
  $('quiz').innerHTML = QUESTIONS.map((q, i) => `
    <div class="q" data-q="${i}">
      <p class="q__ask"><i>Q${i + 1}</i>${q.ask}</p>
      <div class="q__opts">
        ${q.opts.map((o, k) => `<button class="opt" data-q="${i}" data-k="${k}">${o}</button>`).join('')}
      </div>
    </div>`).join('');
}

function answer(qi, ki) {
  if (state.quiz.picked[qi] !== undefined) return;
  state.quiz.picked[qi] = ki;
  const q = QUESTIONS[qi];
  const correct = ki === q.right;
  if (correct) state.quiz.right += 1;

  const block = document.querySelector(`.q[data-q="${qi}"]`);
  for (const btn of block.querySelectorAll('.opt')) {
    btn.disabled = true;
    const k = Number(btn.dataset.k);
    if (k === q.right) btn.dataset.pick = 'right';
    else if (k === ki) btn.dataset.pick = 'wrong';
  }
  const why = document.createElement('p');
  why.className = 'q__why';
  why.textContent = (correct ? '对。' : '这一题选错了。') + q.why;
  block.appendChild(why);

  if (Object.keys(state.quiz.picked).length === QUESTIONS.length) {
    const n = state.quiz.right;
    $('quiz-verdict').textContent = n === 3
      ? `三题全对，用时 ${elapsedText()}。你刚才做的事——描述一个想法、让它变成量子电路、在机器上跑出来、看懂结果——完整地走了一遍，而且你现在能说清楚它为什么是那样。`
      : `答对 ${n} 题，用时 ${elapsedText()}。错的那几题上面写了原因，读一遍就行。重要的是你已经真的跑过量子程序了。`;
    paintCoda();
    $('quiz-foot').hidden = false;
    paintRail();
  }
}

/* 收尾这段话必须照着他实际经历的事写。
 *
 * 原先它写死了「送进一台真的量子计算机，两分钟后带回一串数据」，可真机会整片
 * 下线——那天所有人走到最后，读到的都是一段自己没做过的事，还配一句「你刚才连的
 * 那台只有两个量子比特」。全篇的收尾陈词上撒一个能被当场戳穿的谎，前面攒的信任
 * 一次赔光。没跑成真机不丢人，假装跑成了才丢人。 */
function paintCoda() {
  const hw = state.ranHardware;
  // 电路从哪来、最后落在哪台机器上，两件事都按他实际做过的写
  const got = state.askedOwn
    ? '你没写一行代码。你说了一句中文，换来一个真实的量子电路'
    : '你没写一行代码。你点了一个现成的例子，看着它摊开成一张真实的量子电路';
  $('coda-did').textContent = hw
    ? got + '；它被送进一台真的量子计算机，排队之后带回一串数据；'
      + '页面告诉你哪些是电路本该给出的，哪些是机器自己抖出来的噪声。'
    : got + '，它在模拟器上跑出了结果，页面把这个电路本该给出什么讲给你听。'
      + '真机这一步这次没走成——云上的机器会下线，那是常事——'
      + '但送上去的通道和刚才用的是同一条，换的只是最后落在哪台机器上。';
  $('coda-blunt').textContent = hw
    ? '说句实话：今天的量子计算机还派不上用场。你刚才连的那台只有几个量子比特，'
      + '跑最简单的电路都要错掉两成。'
    : '说句实话：今天的量子计算机还派不上用场。你本来要连的那几台只有 2 到 8 个'
      + '量子比特，还常常一台都不在线；就算连上了，跑最简单的电路也要错掉两成。';
  $('coda-blunt').textContent +=
    '它现在的处境像 1950 年代占满一整间屋子的电子管计算机——原理成立，'
    + '工程上还差得远。真正指望它的方向是模拟分子（算新药、新材料）、密码学'
    + '和某些优化问题，一个都还没到能用的地步。';
}

/* ── 完成凭证 ───────────────────────────────────────── */

const CERT_W = 900, CERT_H = 1200;

function drawCert(canvas) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = CERT_W * dpr;
  canvas.height = CERT_H * dpr;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const cx = CERT_W / 2;
  const mono = getComputedStyle(document.body).getPropertyValue('--mono') || 'monospace';
  const font = getComputedStyle(document.body).getPropertyValue('--font') || 'sans-serif';

  const bg = ctx.createLinearGradient(0, 0, CERT_W, CERT_H);
  bg.addColorStop(0, '#0a1622');
  bg.addColorStop(0.55, '#060c15');
  bg.addColorStop(1, '#03060b');
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, CERT_W, CERT_H);

  let seed = 20260810;
  const rand = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296);
  for (let i = 0; i < 130; i++) {
    ctx.globalAlpha = 0.08 + rand() * 0.34;
    ctx.fillStyle = '#cfe6f5';
    ctx.beginPath();
    ctx.arc(rand() * CERT_W, rand() * CERT_H, rand() * 1.1 + 0.2, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.globalAlpha = 1;

  ctx.strokeStyle = 'rgba(111,215,232,0.26)';
  ctx.lineWidth = 1;
  ctx.strokeRect(44.5, 44.5, CERT_W - 89, CERT_H - 89);

  const halo = ctx.createRadialGradient(cx, 214, 0, cx, 214, 96);
  halo.addColorStop(0, 'rgba(111,215,232,0.42)');
  halo.addColorStop(1, 'rgba(111,215,232,0)');
  ctx.fillStyle = halo;
  ctx.fillRect(cx - 96, 118, 192, 192);
  ctx.strokeStyle = '#6fd7e8';
  ctx.lineWidth = 1.2;
  ctx.beginPath(); ctx.arc(cx, 214, 34, 0, Math.PI * 2); ctx.stroke();
  ctx.fillStyle = '#bdf1f8';
  ctx.beginPath(); ctx.arc(cx, 214, 5, 0, Math.PI * 2); ctx.fill();

  ctx.textAlign = 'center';
  ctx.font = `17px ${mono}`;
  ctx.fillStyle = 'rgba(111,215,232,0.95)';
  ctx.fillText('L O O M Q', cx, 324);
  ctx.font = `12px ${mono}`;
  ctx.fillStyle = 'rgba(93,109,132,0.95)';
  ctx.fillText('FIRST QUANTUM EXPERIMENT', cx, 352);

  ctx.strokeStyle = 'rgba(111,215,232,0.3)';
  ctx.beginPath(); ctx.moveTo(cx - 40, 388.5); ctx.lineTo(cx + 40, 388.5); ctx.stroke();

  ctx.font = `600 40px ${font}`;
  ctx.fillStyle = '#e4ebf4';
  ctx.fillText('我跑完了人生第一个', cx, 470);
  ctx.fillText('量子实验', cx, 526);

  ctx.font = `15px ${mono}`;
  ctx.fillStyle = 'rgba(145,163,186,0.95)';
  ctx.fillText('用 时', cx, 620);
  ctx.font = `600 76px ${font}`;
  ctx.fillStyle = '#bdf1f8';
  ctx.fillText(elapsedText(), cx, 700);

  const rows = [
    ['实　　验', state.last ? state.last.title : '—'],
    ['运 行 于', state.last ? state.last.backend_note.split('（')[0] : '—'],
    ['原理验收', `${state.quiz.right} / ${QUESTIONS.length} 题答对`],
  ];
  let y = 838;
  ctx.font = `15px ${mono}`;
  for (const [label, value] of rows) {
    ctx.textAlign = 'right';
    ctx.fillStyle = 'rgba(93,109,132,0.95)';
    ctx.fillText(label, cx - 24, y);
    ctx.textAlign = 'left';
    ctx.fillStyle = 'rgba(214,228,240,0.95)';
    ctx.fillText(clip(ctx, value, 320), cx + 24, y);
    y += 46;
  }

  ctx.textAlign = 'center';
  ctx.strokeStyle = 'rgba(111,215,232,0.3)';
  ctx.beginPath(); ctx.moveTo(150, 1006.5); ctx.lineTo(CERT_W - 150, 1006.5); ctx.stroke();
  ctx.font = `14px ${mono}`;
  ctx.fillStyle = 'rgba(111,215,232,0.8)';
  ctx.fillText(new Date().toLocaleDateString('zh-CN'), cx, 1050);
  ctx.font = `12.5px ${mono}`;
  ctx.fillStyle = 'rgba(93,109,132,0.95)';
  ctx.fillText('全程没有写一行代码，也没有用到任何量子物理知识', cx, 1086);
}

function clip(ctx, text, max) {
  if (ctx.measureText(text).width <= max) return text;
  let out = text;
  while (out.length > 1 && ctx.measureText(out + '…').width > max) out = out.slice(0, -1);
  return out + '…';
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* ── 事件 ───────────────────────────────────────────── */

$('btn-start').addEventListener('click', () => { startClock(); go('pick'); });

$('cards').addEventListener('click', (e) => {
  const card = e.target.closest('[data-example]');
  if (!card) return;
  startClock();
  run({ example: card.dataset.example }, '正在运行');
});

$('btn-again').addEventListener('click', () => go('pick'));

$('ask-chips').addEventListener('click', (e) => {
  const chip = e.target.closest('[data-preset]');
  if (chip) $('ask-input').value = chip.dataset.preset;
});

$('btn-ask').addEventListener('click', () => {
  const q = $('ask-input').value.trim();
  if (!q) return toast('先说说你想做什么');
  startClock();
  state.askedOwn = true;
  run({ question: q }, '正在理解你的话，并把它变成一个量子电路');
});

$('ask-input').addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') $('btn-ask').click();
});

$('quiz').addEventListener('click', (e) => {
  const opt = e.target.closest('.opt');
  if (opt) answer(Number(opt.dataset.q), Number(opt.dataset.k));
});

$('btn-cert').addEventListener('click', () => {
  drawCert($('cert-canvas'));
  $('veil-cert').hidden = false;
});
$('cert-close').addEventListener('click', () => { $('veil-cert').hidden = true; });
$('cert-save').addEventListener('click', () => {
  $('cert-canvas').toBlob((blob) => {
    if (!blob) return toast('导出失败，直接截图也一样');
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'LoomQ-第一个量子实验.png';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }, 'image/png');
});

$('dock-toggle').addEventListener('click', () => $('dock').classList.toggle('is-open'));
$('sel-backend').addEventListener('change', updateDockNote);

for (const btn of document.querySelectorAll('[data-goto]')) {
  btn.addEventListener('click', () => {
    if (btn.dataset.goto === 'quiz') buildQuiz();
    go(btn.dataset.goto);
  });
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('veil-cert').hidden) $('veil-cert').hidden = true;
});

boot();
