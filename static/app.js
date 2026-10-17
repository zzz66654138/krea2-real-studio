/* 界面 B · ComfyUI 工具集 —— 前端逻辑（无构建步骤） */
'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let OPTIONS = {};
const UPLOADED = { pose: null, upscale: null, tag: null };

/* ---------------- 通用 ---------------- */
function toast(msg, ms = 2200) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove('show'), ms);
}

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const txt = await r.text();
  let data = null;
  try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = { raw: txt }; }
  if (!r.ok) throw new Error((data && (data.error || data.text || data.raw)) || ('HTTP ' + r.status));
  return data;
}

function fillSelect(el, items, { keepPlaceholder = false } = {}) {
  if (!el) return;
  el.innerHTML = '';
  if (keepPlaceholder) {
    const o = document.createElement('option');
    o.value = ''; o.textContent = '（不用）';
    el.appendChild(o);
  }
  (items || []).forEach((v) => {
    const o = document.createElement('option');
    o.value = v; o.textContent = v;
    el.appendChild(o);
  });
}

function pickDefault(sel, list, preferred) {
  if (!sel) return;
  const want = (preferred || []).find((p) => (list || []).includes(p));
  if (want) sel.value = want;
}

/* ---------------- 上传 ---------------- */
function setupDrop(dropId, inputId, key, thumbsId) {
  const drop = $(dropId), input = $(inputId), thumbs = $(thumbsId);
  drop.addEventListener('click', () => input.click());
  input.addEventListener('change', () => { if (input.files[0]) uploadFile(input.files[0], key, thumbs); });
  ['dragenter', 'dragover'].forEach((e) =>
    drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((e) =>
    drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (ev) => {
    const f = ev.dataTransfer.files[0];
    if (f) uploadFile(f, key, thumbs);
  });
  // 支持直接粘贴截图
  document.addEventListener('paste', (ev) => {
    if (!$('#panel-' + (key === 'upscale' ? 'upscale' : key)).classList.contains('active')) return;
    const item = (ev.clipboardData || {}).items || [];
    for (const it of item) {
      if (it.type && it.type.startsWith('image/')) {
        uploadFile(it.getAsFile(), key, thumbs);
        ev.preventDefault();
        return;
      }
    }
  });
}

async function uploadFile(file, key, thumbs) {
  const fd = new FormData();
  fd.append('file', file, file.name || 'paste.png');
  thumbs.innerHTML = '<span class="hint">上传中…</span>';
  try {
    const d = await api('/api/upload', { method: 'POST', body: fd });
    UPLOADED[key] = d.image;
    thumbs.innerHTML = '';
    const img = document.createElement('img');
    img.src = '/api/view?filename=' + encodeURIComponent(d.image.split('/').pop())
            + '&subfolder=' + encodeURIComponent(d.image.includes('/') ? d.image.split('/').slice(0, -1).join('/') : '')
            + '&type=input';
    thumbs.appendChild(img);
    toast('已上传：' + d.image);
  } catch (e) {
    thumbs.innerHTML = '<div class="err-box">上传失败：' + e.message + '</div>';
  }
}

/* ---------------- 任务轮询 ---------------- */
function watchJob(jobId, { stateEl, barEl, resultEl, onDone, onTick, onSettled }) {
  const t0 = Date.now();
  const timer = setInterval(async () => {
    let job;
    try { job = await api('/api/job/' + jobId); }
    catch (e) { clearInterval(timer); stateEl.textContent = '查询失败'; if (onSettled) onSettled(null); return; }
    const el = ((Date.now() - t0) / 1000).toFixed(0);
    if (job.status === 'running' || job.status === 'queued') {
      // 多图任务会陆续产出，用 done_count/count 显示真实进度
      const r = job.result || {};
      const prog = (r.count > 1) ? ' ' + (r.done_count || 0) + '/' + r.count : '';
      stateEl.innerHTML = '<span class="spinner"></span>' + (job.status === 'queued' ? '排队中' : '执行中') + prog + ' ' + el + 's';
      barEl.style.width = (r.count > 1
        ? Math.min(95, 4 + 92 * ((r.done_count || 0) / r.count))
        : Math.min(92, 8 + (Date.now() - t0) / 900)) + '%';
      if (onTick) onTick(job);
      return;
    }
    clearInterval(timer);
    barEl.style.width = '100%';
    if (job.status === 'done') {
      stateEl.textContent = '完成 · ' + job.elapsed + 's';
      barEl.style.width = '100%';
      onDone(job);
    } else {
      // 停止/失败时把已经出好的图留下来再追加说明 —— 不能因为点了停止就把成果丢掉
      const imgs = ((job.result || {}).images) || [];
      stateEl.textContent = job.status === 'cancelled' ? '已停止' : '失败';
      barEl.style.width = '0';
      if (imgs.length && onDone) {
        onDone(job);
        const note = document.createElement('div');
        note.className = 'err-box';
        note.textContent = job.error || (job.status === 'cancelled' ? '已停止' : '未知错误');
        resultEl.appendChild(note);
      } else {
        resultEl.innerHTML = '<div class="err-box">' + (job.error || '未知错误') + '</div>';
      }
    }
    if (onSettled) onSettled(job);
  }, 1400);
}

function renderImages(resultEl, images, noteMap) {
  resultEl.innerHTML = '';
  if (!images || !images.length) {
    resultEl.innerHTML = '<p class="hint">没有产出图片</p>';
    return;
  }
  images.forEach((im) => {
    const url = '/api/view?filename=' + encodeURIComponent(im.filename)
              + '&subfolder=' + encodeURIComponent(im.subfolder || '')
              + '&type=' + encodeURIComponent(im.type || 'output');
    const fig = document.createElement('figure');
    const a = document.createElement('a');
    a.href = url; a.target = '_blank';
    const img = document.createElement('img');
    img.src = url; img.loading = 'lazy';
    img.onerror = () => { img.replaceWith(Object.assign(document.createElement('div'),
      { className: 'err-box', textContent: '图片加载失败：' + im.filename })); };
    a.appendChild(img);
    fig.appendChild(a);
    const cap = document.createElement('figcaption');
    cap.textContent = (noteMap && noteMap[im.node]) || im.filename;
    fig.appendChild(cap);
    // 把完整落盘路径也标出来 —— 用户反馈「不知道下载的文件在哪」
    const pathLine = document.createElement('div');
    pathLine.className = 'imgpath';
    const rel = (im.subfolder ? im.subfolder + '\\' : '') + im.filename;
    pathLine.textContent = rel;
    pathLine.title = OUT_ROOT ? (OUT_ROOT + '\\' + rel) : rel;
    fig.appendChild(pathLine);
    const acts = document.createElement('div');
    acts.className = 'acts';
    const dl = document.createElement('a');
    dl.className = 'mini'; dl.textContent = '下载';
    dl.href = '/api/download?filename=' + encodeURIComponent(im.filename)
            + '&subfolder=' + encodeURIComponent(im.subfolder || '')
            + '&type=' + encodeURIComponent(im.type || 'output');
    acts.appendChild(dl);
    fig.appendChild(acts);
    resultEl.appendChild(fig);
  });
}

/* ---------------- Tab 切换 ---------------- */
$$('.tab').forEach((t) => t.addEventListener('click', () => {
  $$('.tab').forEach((x) => x.classList.remove('active'));
  $$('.panel').forEach((x) => x.classList.remove('active'));
  t.classList.add('active');
  $('#panel-' + t.dataset.tab).classList.add('active');
}));

/* ---------------- 骨架生成 ---------------- */
const POSE_PRESETS = {
  beach: ['highres, absurdres, 1girl, solo, long hair, white dress, barefoot, ocean, sunset, backlighting, depth of field',
          'lowres, blurry, jpeg artifacts, bad anatomy, bad hands, extra limbs, watermark, artist name'],
  studio: ['highres, absurdres, 1girl, solo, studio lighting, softbox, plain background, upper body, looking at viewer, detailed face',
           'lowres, blurry, jpeg artifacts, bad anatomy, bad hands, watermark, artist name, cluttered background'],
  city: ['highres, absurdres, 1girl, solo, city street at night, neon signs, wet pavement, rain, cinematic lighting, depth of field',
         'lowres, blurry, jpeg artifacts, bad anatomy, bad hands, extra limbs, watermark, artist name, daytime'],
  room: ['highres, absurdres, 1girl, solo, bedroom, white curtains, morning light, sitting on bed, casual clothes, soft lighting',
         'lowres, blurry, jpeg artifacts, bad anatomy, bad hands, watermark, artist name, outdoors'],
};
$$('[data-pose-preset]').forEach((b) => b.addEventListener('click', () => {
  const p = POSE_PRESETS[b.dataset.posePreset];
  if (!p) return;
  $('#posePrompt').value = p[0];
  $('#poseNegative').value = p[1];
  toast('已套用预设：' + b.textContent);
}));

$('#poseStrength').addEventListener('input', (e) => {
  $('#poseStrengthV').textContent = Number(e.target.value).toFixed(2);
});

$('#poseInputType').addEventListener('change', (e) => {
  const skel = e.target.value === 'skeleton';
  $('#poseInputHint').textContent = skel
    ? '直接上传黑底纯骨架图（OpenPose 风格），不做提取，控制最精准。'
    : '上传一张有姿势的图片，会自动用 DWPose 提取骨架再生成。提取质量受原图影响，服装遮挡多时四肢可能不准。';
  $('#poseExtractMode').disabled = skel;
});

$('#poseRun').addEventListener('click', async () => {
  if (!UPLOADED.pose) return toast('请先上传图片');
  const btn = $('#poseRun'), st = $('#poseState'), bar = $('#poseBar'), res = $('#poseResult');
  btn.disabled = true; res.innerHTML = ''; bar.style.width = '4%';
  st.innerHTML = '<span class="spinner"></span>提交中…';
  try {
    const body = {
      image: UPLOADED.pose,
      prompt: $('#posePrompt').value.trim(),
      negative: $('#poseNegative').value.trim(),
      patch: $('#posePatch').value,
      strength: Number($('#poseStrength').value),
      extract: $('#poseInputType').value === 'extract',
      extract_mode: $('#poseExtractMode').value,
      width: Number($('#poseWidth').value),
      height: Number($('#poseHeight').value),
      steps: Number($('#poseSteps').value),
      cfg: Number($('#poseCfg').value),
      seed: Number($('#poseSeed').value),
      unet: $('#poseUnet').value,
      lora: $('#poseLora').value,
    };
    const job = await api('/api/pose', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    watchJob(job.id, {
      stateEl: st, barEl: bar, resultEl: res,
      onDone: (j) => renderImages(res, j.result.images, { '8': '提取的骨架', '15': '生成结果' }),
    });
  } catch (e) {
    st.textContent = '失败'; res.innerHTML = '<div class="err-box">' + e.message + '</div>';
  } finally {
    setTimeout(() => { btn.disabled = false; }, 400);
  }
});

/* ---------------- 图变高清 ---------------- */
const UP_HINTS = {
  '4x_foolhardy_Remacri.safetensors': '通用/照片向，皮肤光滑。真人图首选。',
  '4x-AnimeSharp.safetensors': '动漫向，锐化线条。二次元图首选。',
  '2x-AnimeSharpV4_RCAN.safetensors': '动漫向 2x 版，更省显存。',
};
function updateUpHint() {
  $('#upModelHint').textContent = UP_HINTS[$('#upModel').value] || '';
}
$('#upModel').addEventListener('change', updateUpHint);
$('#upMode').addEventListener('change', (e) => {
  $('#upTargetRow').style.display = e.target.value === 'target' ? '' : 'none';
});

$('#upRun').addEventListener('click', async () => {
  if (!UPLOADED.upscale) return toast('请先上传图片');
  const btn = $('#upRun'), st = $('#upState'), bar = $('#upBar'), res = $('#upResult');
  btn.disabled = true; res.innerHTML = ''; bar.style.width = '4%';
  st.innerHTML = '<span class="spinner"></span>提交中…';
  try {
    const mode = $('#upMode').value;
    const body = {
      image: UPLOADED.upscale,
      model: $('#upModel').value,
      mode,
      width: Number($('#upWidth').value),
      height: Number($('#upHeight').value),
    };
    const job = await api('/api/upscale', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    watchJob(job.id, {
      stateEl: st, barEl: bar, resultEl: res,
      onDone: (j) => renderImages(res, j.result.images, { '5': '放大结果' }),
    });
  } catch (e) {
    st.textContent = '失败'; res.innerHTML = '<div class="err-box">' + e.message + '</div>';
  } finally { setTimeout(() => { btn.disabled = false; }, 400); }
});

/* ---------------- 图解析 Tag ---------------- */
$('#tagMode').addEventListener('change', (e) => {
  const real = e.target.value === 'real';
  $('#tagAnimeOpts').style.display = real ? 'none' : '';
  $('#tagRealOpts').style.display = real ? '' : 'none';
});
$('#tagGeneral').addEventListener('input', (e) => { $('#tagGeneralV').textContent = Number(e.target.value).toFixed(2); });
$('#tagChar').addEventListener('input', (e) => { $('#tagCharV').textContent = Number(e.target.value).toFixed(2); });

$('#tagRun').addEventListener('click', async () => {
  if (!UPLOADED.tag) return toast('请先上传图片');
  const btn = $('#tagRun'), st = $('#tagState'), bar = $('#tagBar'), out = $('#tagText');
  btn.disabled = true; out.value = ''; bar.style.width = '4%';
  st.innerHTML = '<span class="spinner"></span>提交中…';
  try {
    const mode = $('#tagMode').value;
    const body = {
      image: UPLOADED.tag, mode,
      general_threshold: Number($('#tagGeneral').value),
      character_threshold: Number($('#tagChar').value),
      max_length: Number($('#tagMaxLen').value),
    };
    const job = await api('/api/tag', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    watchJob(job.id, {
      stateEl: st, barEl: bar, resultEl: document.createElement('div'),
      onDone: (j) => {
        const txt = (j.result.texts || []).join('\n').trim();
        out.value = txt || '（没有拿到文本，检查 ComfyUI 日志）';
        out.dataset.mode = mode;
      },
    });
  } catch (e) {
    st.textContent = '失败'; out.value = '错误：' + e.message;
  } finally { setTimeout(() => { btn.disabled = false; }, 400); }
});

$('#tagCopy').addEventListener('click', async () => {
  const t = $('#tagText').value.trim();
  if (!t) return toast('没有内容');
  try { await navigator.clipboard.writeText(t); toast('已复制'); }
  catch (e) { $('#tagText').select(); document.execCommand('copy'); toast('已复制'); }
});

$('#tagToPose').addEventListener('click', () => {
  const t = $('#tagText').value.trim();
  if (!t) return toast('没有内容');
  $('#posePrompt').value = t;
  $$('.tab').forEach((x) => x.classList.remove('active'));
  $$('.panel').forEach((x) => x.classList.remove('active'));
  document.querySelector('.tab[data-tab="pose"]').classList.add('active');
  $('#panel-pose').classList.add('active');
  toast('已送到「骨架生成」的正向提示词');
});

/* ---------------- 初始化 ---------------- */
async function refreshStatus() {
  try {
    const s = await api('/api/status');
    const dot = $('#status .dot');
    if (s.online) {
      dot.className = 'dot ok';
      $('#statusText').textContent = (s.device || 'ComfyUI') + ' · v' + (s.version || '?');
    } else {
      dot.className = 'dot err';
      $('#statusText').textContent = 'ComfyUI 未连接';
    }
  } catch (e) {
    $('#status .dot').className = 'dot err';
    $('#statusText').textContent = 'ComfyUI 未连接';
  }
}

async function loadOptions() {
  try {
    OPTIONS = await api('/api/options');
  } catch (e) {
    $('#status .dot').className = 'dot err';
    $('#statusText').textContent = '读不到节点信息';
    return;
  }
  fillSelect($('#upModel'), OPTIONS.upscalers);
  pickDefault($('#upModel'), OPTIONS.upscalers, ['4x-AnimeSharp.safetensors', '4x_foolhardy_Remacri.safetensors']);
  updateUpHint();

  fillSelect($('#posePatch'), OPTIONS.patches);
  pickDefault($('#posePatch'), OPTIONS.patches, ['anima-lllite-pose-1.safetensors']);
  fillSelect($('#poseUnet'), OPTIONS.unets);
  pickDefault($('#poseUnet'), OPTIONS.unets, ['anima-base-v1.0.safetensors', 'anima-turbo-v1.1.safetensors']);
  fillSelect($('#poseLora'), OPTIONS.loras, { keepPlaceholder: true });
}

setupDrop('#poseDrop', '#poseFile', 'pose', '#poseThumbs');
setupDrop('#upDrop', '#upFile', 'upscale', '#upThumbs');
setupDrop('#tagDrop', '#tagFile', 'tag', '#tagThumbs');

refreshStatus();
loadOptions();
setInterval(refreshStatus, 15000);

/* ============================================================
   真人随机生图（Krea2）
   ============================================================ */
let REAL_OPTS = null;
let REAL_SLOT_CTRL = {};
let OUT_ROOT = '';        // ComfyUI 输出根目录，由 /api/output-info 填充

// 尺寸改为「宽高比 + 百万像素」，由后端按同样公式算（见 run.py 的 resolve_size）。
// 这里只用于界面预览，必须与后端算法一致，否则显示和实际不符。
function computeSize(aspect, megapixels) {
  const p = String(aspect || '2:3').split(':').map(Number);
  const wr = p[0] || 2;
  const hr = p[1] || 3;
  const mp = Math.max(0.1, Math.min(16, Number(megapixels) || 1.5));
  const total = mp * 1e6;
  const h0 = Math.sqrt((total * hr) / wr);
  const w0 = total / h0;
  const m = 8;
  return { w: Math.max(m, Math.round(w0 / m) * m), h: Math.max(m, Math.round(h0 / m) * m) };
}

function updateSizePreview() {
  const a = $('#realAspect'); const mp = $('#realMegapixels'); const out = $('#realSizePreview');
  if (!a || !mp || !out) return;
  const s = computeSize(a.value, mp.value);
  out.value = s.w + ' × ' + s.h;
}

// ── 产物目录提示条 ────────────────────────────────────────────────
// 用户反馈「下载的文件不知道去哪找」。图是 ComfyUI 写的，界面B 原本不显示路径。
async function initOutputBar() {
  const bar = document.getElementById('outBar');
  if (!bar) return;
  let info;
  try { info = await api('/api/output-info'); }
  catch (e) { console.warn('读取产物目录失败', e); return; }
  OUT_ROOT = info.root || '';
  const pathEl = document.getElementById('outPath');
  if (pathEl) {
    pathEl.textContent = info.exists
      ? info.root
      : info.root + '  （当前读不到这个目录，请确认 ComfyUI 输出路径）';
  }
  const subs = document.getElementById('outSubs');
  if (subs) {
    subs.innerHTML = '';
    Object.keys(info.subdirs || {}).forEach((k) => {
      const d = info.subdirs[k];
      const span = document.createElement('span');
      span.className = 'outitem';
      span.textContent = d.label + ' → ' + d.rel;
      span.title = d['abs'];
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'mini'; b.textContent = '打开';
      b.addEventListener('click', async () => {
        try {
          await api('/api/open-folder', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ which: k }),
          });
        } catch (e) { toast('打开失败：' + e.message); }
      });
      span.appendChild(b);
      subs.appendChild(span);
    });
  }
  const rootBtn = document.getElementById('outOpenRoot');
  if (rootBtn) {
    rootBtn.addEventListener('click', async () => {
      try {
        await api('/api/open-folder', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ which: 'root' }),
        });
      } catch (e) { toast('打开失败：' + e.message); }
    });
  }
}

async function initRealTab() {
  if (!$('#panel-real')) return;
  try {
    REAL_OPTS = await api('/api/real/options');
  } catch (e) {
    console.warn('真人池读取失败', e);
    return;
  }

  // 宽高比下拉（值就是后端的 key，如 "2:3"）
  const aspectSel = $('#realAspect');
  if (aspectSel) {
    (REAL_OPTS.aspect_ratios || ['2:3']).forEach((a) => {
      const o = document.createElement('option');
      o.value = a; o.textContent = a;
      aspectSel.appendChild(o);
    });
    aspectSel.value = '2:3';
    aspectSel.addEventListener('change', updateSizePreview);
  }
  const mpInput = $('#realMegapixels');
  if (mpInput) {
    mpInput.value = String(REAL_OPTS.default_megapixels || 1.5);
    mpInput.addEventListener('input', updateSizePreview);
  }
  updateSizePreview();

  // 放大模型下拉（两段式精修用）
  const upSel = $('#realUpscale');
  if (upSel) {
    (REAL_OPTS.upscale_models || []).forEach((m) => {
      const o = document.createElement('option');
      o.value = m; o.textContent = m;
      upSel.appendChild(o);
    });
    upSel.value = REAL_OPTS.default_upscale || (REAL_OPTS.upscale_models || [])[0] || '';
  }

  // ── 多选白名单控件（勾几条 → 从那几条里抽）──────────────────────
  // 语义：
  //   · 一条不勾        → 该槽全部随机
  //   · 勾 N 条         → 生成时只从这 N 条里随机抽（勾 1 条就是"钉死"）
  //   · 「不写」        → 这一项不进提示词
  // 「勾 1 条」只是「勾 N 条」的特例，所以不设单独的「指定」模式 ——
  // 上一版做成了「三态 + 只能钉一条」，用户反馈"还是没得选"，就是这里错了。
  REAL_SLOT_CTRL = {};

  function makePicker(key, label, groups, total, hint, onChange, expanded) {
    const state = { off: false, sel: new Set() };
    const itemVal = (it) => (typeof it === 'string' ? it : it.v);
    const itemTxt = (it) => (typeof it === 'string' ? it : (it.t || it.v));

    const wrap = document.createElement('div');
    wrap.className = 'pick';
    if (hint) wrap.title = hint;

    const head = document.createElement('div');
    head.className = 'pickhead';
    const lab = document.createElement('label');
    lab.textContent = label + (total ? '（' + total + '）' : '');
    const sum = document.createElement('span');
    sum.className = 'picksum';
    const bOff = document.createElement('button');
    bOff.type = 'button'; bOff.className = 'mini'; bOff.textContent = '不写';
    const bExp = document.createElement('button');
    bExp.type = 'button'; bExp.className = 'mini';
    bExp.textContent = expanded ? '收起 ▴' : '展开条目 ▾';
    const bClr = document.createElement('button');
    bClr.type = 'button'; bClr.className = 'mini'; bClr.textContent = '清空';
    head.appendChild(lab); head.appendChild(sum);
    head.appendChild(bOff); head.appendChild(bExp); head.appendChild(bClr);

    const body = document.createElement('div');
    body.className = 'pickbody';
    body.style.display = expanded ? '' : 'none';

    const boxes = [];
    groups.forEach((g) => {
      const gw = document.createElement('div');
      gw.className = 'pickgrp';
      if (g.name) {
        const gh = document.createElement('div');
        gh.className = 'pickgrph';
        const gn = document.createElement('span');
        gn.textContent = g.name + '（' + g.items.length + '）';
        const gAll = document.createElement('button');
        gAll.type = 'button'; gAll.className = 'mini'; gAll.textContent = '全选';
        const gNon = document.createElement('button');
        gNon.type = 'button'; gNon.className = 'mini'; gNon.textContent = '清空';
        gAll.addEventListener('click', () => {
          g.items.forEach((it) => state.sel.add(itemVal(it)));
          sync();
        });
        gNon.addEventListener('click', () => {
          g.items.forEach((it) => state.sel.delete(itemVal(it)));
          sync();
        });
        gh.appendChild(gn); gh.appendChild(gAll); gh.appendChild(gNon);
        gw.appendChild(gh);
      }
      g.items.forEach((it) => {
        const val = itemVal(it);
        const txt = itemTxt(it);
        const l = document.createElement('label');
        l.className = 'chk';
        const cb = document.createElement('input');
        cb.type = 'checkbox'; cb.value = val;
        cb.addEventListener('change', () => {
          if (cb.checked) state.sel.add(val); else state.sel.delete(val);
          sync();
        });
        l.appendChild(cb);
        l.appendChild(document.createTextNode(txt.length > 52 ? txt.slice(0, 52) + '…' : txt));
        l.title = txt;
        gw.appendChild(l);
        boxes.push(cb);
      });
      body.appendChild(gw);
    });

    function sync() {
      boxes.forEach((cb) => { cb.checked = state.sel.has(cb.value); });
      if (state.off) {
        sum.textContent = '不写';
        sum.className = 'picksum off';
      } else if (state.sel.size) {
        sum.textContent = '已勾 ' + state.sel.size + ' 条 → 从里面抽';
        sum.className = 'picksum on';
      } else {
        sum.textContent = '全部随机';
        sum.className = 'picksum';
      }
      bOff.textContent = state.off ? '取消不写' : '不写';
      if (onChange) onChange(state);
    }
    bOff.addEventListener('click', () => { state.off = !state.off; sync(); });
    // 折叠态只显示「全部随机」，用户找不到具体条目（实测反馈「你说的这些服装在哪」）。
    // 所以：按钮文案写明「展开条目」，而且整行都能点开。
    const toggle = () => {
      const open = body.style.display !== 'none';
      body.style.display = open ? 'none' : '';
      bExp.textContent = open ? '展开条目 ▾' : '收起 ▴';
    };
    bExp.addEventListener('click', toggle);
    lab.style.cursor = 'pointer';
    lab.addEventListener('click', toggle);
    head.addEventListener('click', (ev) => {
      if (ev.target && ev.target.tagName === 'BUTTON') return;
      toggle();
    });
    bClr.addEventListener('click', () => {
      state.sel.clear(); state.off = false; sync();
    });

    wrap.appendChild(head); wrap.appendChild(body);
    sync();
    // 一条不勾 → 空串（后端按"全部随机"处理）
    REAL_SLOT_CTRL[key] = {
      state,
      get: () => (state.off ? 'off' : (state.sel.size ? Array.from(state.sel) : '')),
    };
    return wrap;
  }

  function poolGroups(p) {
    if (p && p.categories) {
      return Object.keys(p.categories).map((c) => ({ name: c, items: p.categories[c] }));
    }
    return [{ name: '', items: (p && p.items) || [] }];
  }

  function addPicker(container, key, labelOverride, groupsOverride, totalOverride, hint,
                     onChange, expanded) {
    const p = (REAL_OPTS.pools || {})[key];
    const groups = groupsOverride || poolGroups(p);
    const total = totalOverride != null ? totalOverride
      : groups.reduce((a, g) => a + g.items.length, 0);
    const label = labelOverride || (p && p.label) || key;
    const el = makePicker(key, label, groups, total, hint || (p && p.hint),
                          onChange, expanded);
    if (container) container.appendChild(el);
    return el;
  }

  const splitOrder = REAL_OPTS.slot_order || [];
  const splitBox = $('#realSlots');
  const baseBox = $('#realBaseSlots');
  if (!splitBox || !baseBox) {
    // 只有「新 HTML + 旧 app.js」才会走到这里（缓存导致版本错配）。
    console.error('槽位容器不存在：HTML 与 app.js 版本不一致');
    const hint = $('#realClothingHint');
    if (hint) {
      hint.textContent = '⚠️ 检测到新旧版本混用（浏览器缓存了旧的 app.js）—— 请按 Ctrl+Shift+R 强制刷新。';
    }
    return;
  }
  // 槽默认折叠（用户要求）：只显示「全部随机」，点行内「展开条目 ▾」或点槽名才展开
  splitOrder.forEach((k) => addPicker(splitBox, k));
  (REAL_OPTS.base_slots || []).forEach((k) => addPicker(baseBox, k));

  if (!splitBox.children.length && !baseBox.children.length) {
    $('#realSplitHint').textContent =
      '⚠️ 控件没生成（可能是缓存了旧 app.js）—— 请按 Ctrl+Shift+R 强制刷新一次。';
  }
  $('#realSplitHint').textContent =
    '共 ' + splitOrder.length + ' 个槽。勾几条就只从那几条里抽；一条不勾 = 全部随机；' +
    '「不写」= 不进提示词。勾了上衣或下衣就自动走「上衣+下衣」轮廓，不会被随机连衣裙吃掉。';

  const cats = (REAL_OPTS.pools.clothing || {}).categories || {};
  const catNames = Object.keys(cats);
  $('#realClothingHint').textContent =
    '服装池共 ' + (REAL_OPTS.pools.clothing || {}).total + ' 套真实服装，分 ' +
    catNames.length + ' 个类别 —— 这是真人版的特色。';

  // 服装模式：成套 vs 拆分；cosplay「角色原装」优先级最高，两者都不生效
  const syncClothingUI = () => {
    const mode = $('#realClothingMode').value;
    const cc = REAL_SLOT_CTRL.clothing_category;
    const charOrig = $('#realMode').value === 'cosplay' &&
      cc && cc.state.sel.has('__character__');
    const useSet = (mode === 'set') || charOrig;
    $('#realSetBox').style.display = useSet ? '' : 'none';
    $('#realSplitBox').style.display = (!useSet && mode === 'split') ? '' : 'none';
    $('#realClothingModeHint').textContent = charOrig
      ? '⚠️ 已勾「角色原装服装」：上面两种服装模式都不生效，交给模型画角色原版。'
      : (mode === 'split'
        ? '拆分搭配：11 个槽逐槽多选，抽词时拼成一句中文散文。'
        : '成套服装：可以勾几套（优先），也可以勾几个类别。');
  };

  // 成套服装的类别（含 cosplay 的「角色原装」）
  addPicker($('#realClothingCatBox'), 'clothing_category', '按类别抽',
    [{ name: '', items: [{ v: '__character__', t: '角色原装服装（仅 cosplay 有意义）' }]
        .concat(catNames.map((c) => ({ v: c, t: c + '（' + cats[c].length + ' 套）' }))) }],
    catNames.length + 1,
    '勾几个类别就从这几个类别里抽。', () => syncClothingUI());

  // 成套服装：勾选的整套（54 套，按类别分组）
  addPicker($('#realClothingFixedBox'), 'clothing_fixed', '固定某几套（优先于类别）',
    catNames.map((c) => ({ name: c, items: cats[c] })),
    (REAL_OPTS.pools.clothing || {}).total,
    '勾几套就只从那几套里抽；一条不勾 = 按上面的类别随机。');

  $('#realClothingMode').addEventListener('change', syncClothingUI);
  syncClothingUI();

  // 镜头取景包：同样多选
  addPicker($('#realCameraBox'), 'camera_bundle', '取景包',
    [{ name: '', items: (REAL_OPTS.pools.camera || {}).bundles || [] }],
    ((REAL_OPTS.pools.camera || {}).bundles || []).length,
    '勾几个取景包就从这几个里抽；动作池会按最终抽中的景别自动收窄。');

  // 参数预设（步数/CFG）—— 实测：靠加步数，不要加 CFG
  const presets = REAL_OPTS.param_presets || [];
  // 采样器 / 调度器：列表来自 /api/options（后端转发的 ComfyUI KSampler 取值）
  const spSel = $('#realSampler');
  const scSel = $('#realScheduler');
  if (spSel && OPTIONS) (OPTIONS.samplers || ['er_sde']).forEach((x) => {
    const o = document.createElement('option'); o.value = x; o.textContent = x; spSel.appendChild(o);
  });
  if (scSel && OPTIONS) (OPTIONS.schedulers || ['simple']).forEach((x) => {
    const o = document.createElement('option'); o.value = x; o.textContent = x; scSel.appendChild(o);
  });
  // 预设原来只设 steps/cfg，采样器改不了 —— 现在一并套用
  function applyParamPreset(p) {
    if (!p) return;
    if (p.sampler && spSel) spSel.value = p.sampler;
    if (p.scheduler && scSel) scSel.value = p.scheduler;
  }
  const ppSel = $('#realParamPreset');
  presets.forEach((p, i) => {
    const o = document.createElement('option');
    o.value = String(i);
    o.textContent = p.name;
    ppSel.appendChild(o);
  });
  if (presets.length) {
    $('#realSteps').value = String(presets[0].steps);
    $('#realCfg').value = String(presets[0].cfg);
    applyParamPreset(presets[0]);
  }
  ppSel.addEventListener('change', () => {
    const p = presets[Number(ppSel.value)];
    if (!p) return;
    applyParamPreset(p);
    $('#realSteps').value = String(p.steps);
    $('#realCfg').value = String(p.cfg);
  });

  // 角色：多选白名单。按系列分组，勾几个角色就从这几个里抽（每张图各抽一个）。
  const charGroups = Object.keys(REAL_OPTS.characters || {}).map((s) => ({
    name: s,
    items: (REAL_OPTS.characters[s] || []).map((c) => ({
      v: c.tag,
      t: (c.cn ? c.cn + '  ·  ' : '') + c.tag + '  ·  ' + c.post_count + ' 作品',
    })),
  }));
  addPicker($('#realCharacterBox'), 'character', '角色',
    charGroups, charGroups.reduce((a, g) => a + g.items.length, 0),
    '勾几个角色，每张图就从这几个里抽一个。用 Danbooru 格式的角色名。');

  if (OPTIONS && OPTIONS.unets) {
    const u = $('#realUnet');
    OPTIONS.unets.filter((x) => x.startsWith('krea2')).forEach((x) => {
      const o = document.createElement('option'); o.value = x; o.textContent = x;
      u.appendChild(o);
    });
  }
  if (OPTIONS && OPTIONS.loras) {
    // ── 多 LoRA 编辑器 ─────────────────────────────────────────────
    // 官方配对是「RAW 训练 → Turbo 使用」；「只能挂一个 LoRA」是我们界面的限制，
    // 不是底模的限制（社区的区域多 LoRA 节点跑的就是 Turbo）。
    // 串联对风格类 LoRA 安全；身份类 LoRA 会互相串味（要空间隔离得用区域掩码节点）。
    const rows = $('#realLoraRows');
    const lhint = $('#realLoraHint');

    function loraPresetOf(name) { return (REAL_OPTS.lora_presets || {})[name] || {}; }

    function describe(name) {
      if (!lhint) return;
      if (!name) {
        lhint.textContent = '选择 LoRA 后会显示它是什么、推荐强度和触发词。'
          + ' 这批 LoRA 分属 4 个模型族，跨族使用大概率无效。';
        return;
      }
      const p = loraPresetOf(name);
      lhint.textContent = (p.family ? '[' + p.family + ' 族] ' : '') + (p.note || '')
        + (p.trigger ? '  触发词：' + p.trigger : '')
        + (p.strength != null ? '  推荐强度 ' + p.strength : '');
    }

    function addLoraRow(preset) {
      if (!rows) return null;
      const row = document.createElement('div');
      row.className = 'lorarow';
      const sel = document.createElement('select');
      const none = document.createElement('option');
      none.value = ''; none.textContent = '（不用）';
      sel.appendChild(none);
      (OPTIONS.loras || []).forEach((x) => {
        const o = document.createElement('option'); o.value = x; o.textContent = x;
        sel.appendChild(o);
      });
      const st = document.createElement('input');
      st.type = 'number'; st.min = '0'; st.max = '2'; st.step = '0.05'; st.value = '1';
      st.title = 'LoRA 强度';
      const del = document.createElement('button');
      del.type = 'button'; del.className = 'mini'; del.textContent = '删除';
      del.addEventListener('click', () => {
        row.remove();
        const first = rows.querySelector('select');
        describe(first ? first.value : '');
      });
      sel.addEventListener('change', () => {
        const p = loraPresetOf(sel.value);
        if (p.strength != null) st.value = String(p.strength);
        describe(sel.value);
      });
      row.appendChild(sel); row.appendChild(st); row.appendChild(del);
      rows.appendChild(row);
      if (preset) { sel.value = preset; const p = loraPresetOf(preset); if (p.strength != null) st.value = String(p.strength); describe(preset); }
      return row;
    }

    addLoraRow();   // 默认给一行
    const addBtn = $('#realLoraAdd');
    if (addBtn) {
      addBtn.addEventListener('click', () => {
        if (!rows || rows.children.length >= 8) { toast('最多 8 个 LoRA'); return; }
        addLoraRow();
      });
    }
    const trigBtn = $('#realLoraTrigger');
    if (trigBtn) {
      trigBtn.addEventListener('click', () => {
        // 取第一个非空行
        let name = '';
        if (rows) {
          for (const s of rows.querySelectorAll('select')) {
            if (s.value) { name = s.value; break; }
          }
        }
        const p = loraPresetOf(name);
        if (!p.trigger) { toast(name ? '这个 LoRA 没有登记触发词' : '先选一个 LoRA'); return; }
        const ta = $('#realPrompt');
        const cur = ta.value || '';
        if (cur.indexOf(p.trigger) >= 0) { toast('提示词里已经有这个触发词了'); return; }
        ta.value = (cur ? cur.replace(/[。\s]*$/, '，') : '') + p.trigger + '。';
        toast('已加入触发词：' + p.trigger);
      });
    }
  }

  $('#realMode').addEventListener('change', (e) => {
    const cos = e.target.value === 'cosplay';
    $('#realCosplayOpts').style.display = cos ? '' : 'none';
    // 切到 cosplay 且没勾任何角色时，默认勾上「角色原装服装」。
    // 切回普通模式必须取消勾选，否则 __character__ 残留会把普通真人图误判成角色原装。
    const cc = REAL_SLOT_CTRL.clothing_category;
    if (cc) {
      if (cos) cc.state.sel.add('__character__');
      else cc.state.sel.delete('__character__');
    }
    syncClothingUI();
  });
  $('#realDraw').addEventListener('click', () => realDraw(true));
  $('#realRoll').addEventListener('click', () => {
    $('#realSeed').value = String(Math.floor(Math.random() * 2147483000));
    realDraw(false);
  });
  $('#realRun').addEventListener('click', runReal);
  // 停止生成：打断当前这张并清掉排队中的
  const stopBtn = $('#realStop');
  if (stopBtn) {
    stopBtn.addEventListener('click', async () => {
      if (!CUR_JOB) return;
      stopBtn.disabled = true;
      try {
        await api('/api/job/' + CUR_JOB + '/cancel', { method: 'POST' });
        toast('已发送停止');
      } catch (e) {
        toast('停止失败：' + e.message);
      } finally {
        stopBtn.disabled = false;
      }
    });
  }
}

// 抽词明细：把 used 摊开显示。
// 加它的直接原因：用户勾了「泳装沙滩 + 碎花比基尼」，但模式还停在「拆分搭配」，
// 后端把成套选择整段忽略、还随机加了一身衣服，而界面上完全看不出来。
// 有了这个面板，哪一项是「随机来的」一眼可见。
const USED_ORDER = ['clothing_mode_note', 'clothing_mode', 'silhouette', 'template',
  'clothing', 'character', 'hair', 'hair_acc', 'hat', 'top', 'bottom', 'dress',
  'outerwear', 'legwear', 'footwear', 'bag', 'jewelry', 'camera', 'person', 'body',
  'pose', 'scene', 'expression', 'light'];

function renderUsed(used) {
  const el = $('#realUsed');
  if (!el) return;
  if (!used) { el.textContent = '（无）'; return; }
  const keys = USED_ORDER.filter((k) => k in used)
    .concat(Object.keys(used).filter((k) => USED_ORDER.indexOf(k) < 0));
  el.textContent = keys.map((k) => k + '：' + used[k]).join('\n');
}

function realBody() {
  const num = (sel, dflt) => {
    const v = $(sel) ? $(sel).value : '';
    return (v === '' || v === null) ? dflt : Number(v);
  };
  const body = {
    mode: $('#realMode').value,
    clothing_mode: $('#realClothingMode').value,
    // 尺寸改成「宽高比 + 百万像素」，由后端算实际像素并对齐到 8 的倍数
    aspect: $('#realAspect') ? $('#realAspect').value : '2:3',
    megapixels: num('#realMegapixels', 1.5),
    steps: num('#realSteps', 8),
    cfg: num('#realCfg', 1.5),
    sampler: $('#realSampler') ? $('#realSampler').value : 'er_sde',
    scheduler: $('#realScheduler') ? $('#realScheduler').value : 'simple',
    count: num('#realCount', 1),
    seed: num('#realSeed', -1),
    // 这两个下拉以前只填充、从不发送 —— 所以选 RAW / LoRA 是无效的
    unet: $('#realUnet') ? $('#realUnet').value : '',
    // 多 LoRA：按行收集，空行跳过
    loras: (() => {
      const out = [];
      const box = $('#realLoraRows');
      if (!box) return out;
      box.querySelectorAll('.lorarow').forEach((r) => {
        const s = r.querySelector('select');
        const ns = r.querySelectorAll('input');
        if (s && s.value) {
          out.push({ name: s.value, strength: Number(ns[0].value || 1), clip: 0 });
        }
      });
      return out;
    })(),
    sharpen: num('#realSharpen', 0),
    // 两段式精修：0 = 关
    refine: num('#realRefine', 0),
    refine_steps: num('#realRefineSteps', 4),
    refine_scale: num('#realRefineScale', 1),
    upscale_model: $('#realUpscale') ? $('#realUpscale').value : '',
  };
  // 所有槽（含 character / camera_bundle / clothing_category / clothing_fixed）：
  //   ''      -> 全部随机
  //   'off'   -> 不写
  //   [条目…] -> 只从勾选的这些里抽
  //
  // ⚠️ 槽名绝不能与请求参数同名。踩过：新增的「身高」槽 key 叫 height，
  //    把图片像素高度 height 覆盖成 'off'，后端 int('off') 直接 500。
  //    这些保留字段一律跳过，改了名也不会再悄悄覆盖。
  const RESERVED = new Set(['mode', 'clothing_mode', 'width', 'height', 'steps', 'cfg',
    'count', 'seed', 'unet', 'lora', 'lora_strength', 'sharpen', 'prompt', 'negative']);
  Object.keys(REAL_SLOT_CTRL).forEach((k) => {
    if (RESERVED.has(k)) {
      console.warn('槽名与请求参数冲突，已跳过：' + k);
      return;
    }
    body[k] = REAL_SLOT_CTRL[k].get();
  });
  // 角色只在 cosplay 模式下有意义
  if (body.mode !== 'cosplay') body.character = '';
  // 勾了「用文本框里的提示词」就直接把原文发过去，后端不再抽词。
  // 这样用户能改完颜色反复出图（等于原来的生图功能）。
  const reuse = $('#realReuse');
  if (reuse && reuse.checked) {
    body.prompt = $('#realPrompt').value || '';
    body.negative = $('#realNegative').value || '';
  }
  return body;
}

async function realDraw(notify) {
  const btn = $('#realDraw');
  btn.disabled = true;
  try {
    const d = await api('/api/real/draw', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(realBody()),
    });
    $('#realPrompt').value = d.prompt || '';
    $('#realNegative').value = d.negative || '';
    renderUsed(d.used);
    if (notify) toast('已抽出一组提示词');
  } catch (e) {
    toast('抽词失败：' + e.message);
  } finally {
    btn.disabled = false;
  }
}

let CUR_JOB = null;

async function runReal() {
  const btn = $('#realRun'), st = $('#realState'), bar = $('#realBar'), res = $('#realResult');
  const stopBtn = $('#realStop');
  btn.disabled = true; res.innerHTML = ''; bar.style.width = '4%';
  st.innerHTML = '<span class="spinner"></span>提交中…';
  try {
    const job = await api('/api/real', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(realBody()),
    });
    CUR_JOB = job.id;
    if (stopBtn) stopBtn.style.display = '';
    if (job.note) $('#realPrompt').value = job.note;
    const plans = (job.result || {}).plans || [];
    if (plans[0]) {
      $('#realNegative').value = plans[0].negative || '';
      renderUsed(plans[0].used);
    }
    watchJob(job.id, {
      stateEl: st, barEl: bar, resultEl: res,
      // 多图任务每张各抽一套词，出来一张就先显示一张
      onTick: (j) => {
        const imgs = ((j.result || {}).images) || [];
        if (imgs.length) renderImages(res, imgs, { '9': '生成结果' });
      },
      onDone: (j) => renderImages(res, (j.result || {}).images, { '9': '生成结果' }),
      onSettled: () => { CUR_JOB = null; if (stopBtn) stopBtn.style.display = 'none'; },
    });
  } catch (e) {
    st.textContent = '失败';
    res.innerHTML = '<div class="err-box">' + e.message + '</div>';
    if (stopBtn) stopBtn.style.display = 'none';
  } finally {
    setTimeout(() => { btn.disabled = false; }, 400);
  }
}

window.addEventListener('load', () => setTimeout(() => {
  initOutputBar();
  initRealTab();
}, 800));
