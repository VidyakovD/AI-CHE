/*!
 * Knowledge UI: общая модалка управления RAG-базой знаний.
 *
 * Подключается на /agents.html и /chatbots.html. Открывается через
 *   window.aiOpenKnowledge({ ownerType: 'agent'|'bot', ownerId: 42, ownerName: 'SMM-агент' });
 *
 * Внутри: загрузка файлов (drag-n-drop), список с прогрессом индексации,
 * удаление, тестовый поиск. Безопасно: вся вставка через textContent.
 */
(function () {
  'use strict';

  if (window.aiOpenKnowledge) return;

  const ALLOWED_EXT = ['.pdf', '.docx', '.xlsx', '.xlsm', '.csv', '.tsv',
                       '.txt', '.md', '.json', '.html', '.htm'];

  function _injectStyles() {
    if (document.getElementById('ai-kb-styles')) return;
    const css = `
#ai-kb-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);display:flex;align-items:center;justify-content:center;z-index:99996;padding:16px;font:14px/1.5 system-ui,-apple-system,sans-serif;color:#eee}
#ai-kb-modal{background:#1c1c1c;border:1px solid rgba(255,255,255,.08);border-radius:18px;width:100%;max-width:640px;max-height:90vh;display:flex;flex-direction:column;overflow:hidden}
#ai-kb-hdr{padding:16px 20px;background:#0d0d0d;border-bottom:1px solid rgba(255,255,255,.05);display:flex;align-items:center;justify-content:space-between}
#ai-kb-hdr h3{margin:0;font-size:17px;font-weight:700}
#ai-kb-hdr .sub{font-size:12px;color:#888;margin-top:2px}
#ai-kb-close{background:transparent;border:none;color:#bbb;cursor:pointer;font-size:22px;line-height:1;padding:4px 8px}
#ai-kb-body{padding:18px 20px;overflow-y:auto;flex:1}
.ai-kb-tabs{display:flex;gap:6px;border-bottom:1px solid rgba(255,255,255,.06);margin-bottom:14px}
.ai-kb-tab{padding:8px 14px;border:none;background:transparent;color:#aaa;cursor:pointer;border-bottom:2px solid transparent;font-weight:500}
.ai-kb-tab.active{color:#ff8c42;border-bottom-color:#ff8c42}

.ai-kb-drop{border:2px dashed rgba(255,140,66,.3);border-radius:12px;padding:22px;text-align:center;color:#aaa;cursor:pointer;transition:background .15s,border-color .15s}
.ai-kb-drop:hover,.ai-kb-drop.dragover{background:rgba(255,140,66,.06);border-color:#ff8c42;color:#ff8c42}
.ai-kb-drop b{display:block;margin-bottom:4px;color:#fff}
.ai-kb-tags{margin-top:10px;width:100%;background:#252525;border:1px solid rgba(255,255,255,.08);color:#eee;padding:9px 11px;border-radius:8px;font:inherit}

.ai-kb-list{margin-top:16px;display:flex;flex-direction:column;gap:8px}
.ai-kb-item{background:#252525;border:1px solid rgba(255,255,255,.05);border-radius:12px;padding:12px 14px;display:flex;align-items:center;gap:12px}
.ai-kb-item .nm{flex:1;min-width:0}
.ai-kb-item .nm .t{font-weight:600;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ai-kb-item .nm .s{font-size:11.5px;color:#888;margin-top:2px}
.ai-kb-item .badge{padding:3px 8px;border-radius:8px;font-size:11px;font-weight:600;flex-shrink:0}
.ai-kb-item .badge.ok{background:rgba(123,217,104,.15);color:#7bd968}
.ai-kb-item .badge.idx{background:rgba(255,140,66,.15);color:#ff8c42}
.ai-kb-item .badge.err{background:rgba(255,107,107,.15);color:#ff6b6b}
.ai-kb-item .badge.off{background:rgba(120,120,120,.15);color:#888}
.ai-kb-item.disabled{opacity:.5}
.ai-kb-item.disabled .nm{text-decoration:line-through}
.ai-kb-item .del{background:transparent;border:none;color:#777;cursor:pointer;padding:6px;font-size:16px}
.ai-kb-item .del:hover{color:#ff6b6b}
.ai-kb-toggle{position:relative;width:36px;height:20px;background:#3a3a3a;border-radius:11px;cursor:pointer;flex-shrink:0;transition:background .2s;border:none;padding:0}
.ai-kb-toggle::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;background:#fff;border-radius:50%;transition:transform .2s}
.ai-kb-toggle.on{background:#ff8c42}
.ai-kb-toggle.on::after{transform:translateX(16px)}
.ai-kb-empty{padding:24px;text-align:center;color:#777;font-size:13px;font-style:italic}

.ai-kb-search-row{display:flex;gap:8px;margin-bottom:14px}
.ai-kb-search-row input{flex:1;background:#252525;border:1px solid rgba(255,255,255,.08);color:#eee;padding:10px 12px;border-radius:10px;font:inherit;outline:none}
.ai-kb-search-row input:focus{border-color:#ff8c42}
.ai-kb-search-row button{background:linear-gradient(135deg,#FFB300,#FF6F00);color:#fff;border:none;padding:10px 16px;border-radius:10px;font-weight:700;cursor:pointer}
.ai-kb-result{background:#252525;border:1px solid rgba(255,255,255,.05);border-radius:10px;padding:12px;margin-bottom:8px;font-size:13px}
.ai-kb-result .meta{font-size:11px;color:#888;margin-bottom:6px}
.ai-kb-result .meta .score{color:#7bd968}

.ai-kb-stats{font-size:11.5px;color:#888;margin-top:12px;text-align:right}
.ai-kb-progress{margin-top:8px;font-size:12px;color:#aaa}
.ai-kb-err-msg{color:#ff6b6b;font-size:12px;margin-top:6px}
`;
    const st = document.createElement('style');
    st.id = 'ai-kb-styles';
    st.textContent = css;
    document.head.appendChild(st);
  }

  function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  function _fmtSize(b) {
    if (!b) return '0 КБ';
    if (b < 1024) return b + ' Б';
    if (b < 1024 * 1024) return Math.round(b / 1024) + ' КБ';
    return (b / 1024 / 1024).toFixed(1) + ' МБ';
  }

  function _fmtDate(iso) {
    if (!iso) return '';
    try {
      return new Date(iso).toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' });
    } catch (_) { return ''; }
  }

  window.aiOpenKnowledge = function (opts) {
    opts = opts || {};
    const ownerType = opts.ownerType || 'agent';
    const ownerId = opts.ownerId;
    const ownerName = opts.ownerName || (ownerType === 'agent' ? 'агента' : 'бота');
    if (!ownerId) {
      console.error('aiOpenKnowledge: ownerId required');
      return;
    }

    _injectStyles();
    if (document.getElementById('ai-kb-overlay')) return;

    const overlay = document.createElement('div');
    overlay.id = 'ai-kb-overlay';
    overlay.innerHTML = `
<div id="ai-kb-modal" role="dialog" aria-modal="true">
  <div id="ai-kb-hdr">
    <div>
      <h3>📚 База знаний</h3>
      <div class="sub" id="ai-kb-owner-label"></div>
    </div>
    <button id="ai-kb-close" aria-label="Закрыть">&times;</button>
  </div>
  <div id="ai-kb-body">
    <div class="ai-kb-tabs">
      <button class="ai-kb-tab active" data-tab="files">Файлы</button>
      <button class="ai-kb-tab" data-tab="search">Тест поиска</button>
    </div>
    <div id="ai-kb-pane-files">
      <label class="ai-kb-drop" id="ai-kb-drop">
        <b>Загрузить файл</b>
        Перетащите PDF / DOCX / XLSX / CSV / TXT / MD сюда или нажмите
        <input type="file" id="ai-kb-input" style="display:none" accept=".pdf,.docx,.xlsx,.xlsm,.csv,.tsv,.txt,.md,.json,.html,.htm"/>
      </label>
      <input type="text" class="ai-kb-tags" id="ai-kb-tags-input" placeholder="Теги через запятую (опционально, например: прайс, услуги)" maxlength="200"/>
      <div class="ai-kb-progress" id="ai-kb-upload-progress"></div>
      <div class="ai-kb-list" id="ai-kb-list"></div>
      <div class="ai-kb-stats" id="ai-kb-stats"></div>
    </div>
    <div id="ai-kb-pane-search" style="display:none">
      <div class="ai-kb-search-row">
        <input type="text" id="ai-kb-q" placeholder="Например: какие услуги мы оказываем?"/>
        <button id="ai-kb-search-btn">Найти</button>
      </div>
      <div id="ai-kb-results"></div>
    </div>
  </div>
</div>
`;
    document.body.appendChild(overlay);

    const $ = sel => overlay.querySelector(sel);
    $('#ai-kb-owner-label').textContent = `Для ${ownerType === 'agent' ? 'агента' : 'бота'}: ${ownerName}`;

    function close() {
      overlay.remove();
    }
    $('#ai-kb-close').addEventListener('click', close);
    overlay.addEventListener('click', e => { if (e.target === overlay) close(); });

    // Tabs
    overlay.querySelectorAll('.ai-kb-tab').forEach(t => {
      t.addEventListener('click', () => {
        overlay.querySelectorAll('.ai-kb-tab').forEach(x => x.classList.remove('active'));
        t.classList.add('active');
        const tab = t.dataset.tab;
        $('#ai-kb-pane-files').style.display = (tab === 'files') ? '' : 'none';
        $('#ai-kb-pane-search').style.display = (tab === 'search') ? '' : 'none';
      });
    });

    // ── Загрузка файлов ──
    const drop = $('#ai-kb-drop');
    const input = $('#ai-kb-input');
    const tagsInput = $('#ai-kb-tags-input');
    const progress = $('#ai-kb-upload-progress');

    drop.addEventListener('click', () => input.click());
    drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('dragover'); });
    drop.addEventListener('dragleave', () => drop.classList.remove('dragover'));
    drop.addEventListener('drop', e => {
      e.preventDefault();
      drop.classList.remove('dragover');
      if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
    });
    input.addEventListener('change', () => {
      if (input.files.length) handleFile(input.files[0]);
    });

    async function handleFile(f) {
      const ext = '.' + (f.name.split('.').pop() || '').toLowerCase();
      if (!ALLOWED_EXT.includes(ext)) {
        progress.innerHTML = `<span class="ai-kb-err-msg">Неподдерживаемый формат: ${_esc(ext)}</span>`;
        return;
      }
      progress.textContent = `⏳ Загружаю «${f.name}» (${_fmtSize(f.size)})...`;
      const fd = new FormData();
      fd.append('file', f);
      const tags = tagsInput.value.trim();
      const url = `/knowledge/upload?owner_type=${encodeURIComponent(ownerType)}&owner_id=${ownerId}&tags=${encodeURIComponent(tags)}`;
      try {
        const r = await fetch(url, { method: 'POST', body: fd, credentials: 'same-origin' });
        if (!r.ok) {
          const d = await r.json().catch(() => ({}));
          progress.innerHTML = `<span class="ai-kb-err-msg">Ошибка: ${_esc(d.detail || r.status)}</span>`;
          return;
        }
        progress.textContent = `✓ Загружено. Индексация в фоне (1-30 секунд для PDF/XLSX).`;
        input.value = '';
        tagsInput.value = '';
        loadList();
        // Через 4 сек обновим список — может уже проиндексировано
        setTimeout(loadList, 4000);
        setTimeout(loadList, 12000);
      } catch (e) {
        progress.innerHTML = `<span class="ai-kb-err-msg">Сеть недоступна</span>`;
      }
    }

    // ── Список файлов ──
    async function loadList() {
      const list = $('#ai-kb-list');
      const stats = $('#ai-kb-stats');
      try {
        const r = await fetch(`/knowledge?owner_type=${encodeURIComponent(ownerType)}&owner_id=${ownerId}`, {
          credentials: 'same-origin',
        });
        if (!r.ok) {
          list.innerHTML = `<div class="ai-kb-empty">Ошибка: ${r.status}</div>`;
          return;
        }
        const data = await r.json();
        const files = data.files || [];
        if (!files.length) {
          list.innerHTML = `<div class="ai-kb-empty">База пока пуста. Загрузите первый файл выше.</div>`;
        } else {
          list.innerHTML = '';
          files.forEach(f => {
            const enabled = f.enabled !== false;  // default true
            const item = document.createElement('div');
            item.className = 'ai-kb-item' + (enabled ? '' : ' disabled');
            let badge;
            if (!enabled) {
              badge = `<span class="badge off">выключен</span>`;
            } else if (f.status === 'ready') {
              badge = `<span class="badge ok">✓ ${f.chunk_count} ${_pluralRu(f.chunk_count, 'чанк', 'чанка', 'чанков')}</span>`;
            } else if (f.status === 'failed') {
              badge = `<span class="badge err" title="${_esc(f.error || '')}">⚠ Ошибка</span>`;
            } else {
              badge = `<span class="badge idx">⏳ Индексация...</span>`;
            }
            item.innerHTML = `
              <div class="nm">
                <div class="t"></div>
                <div class="s"></div>
              </div>
              ${badge}
              <button class="ai-kb-toggle ${enabled ? 'on' : ''}" title="${enabled ? 'Выключить (агент перестанет использовать файл)' : 'Включить (агент будет искать в этом файле)'}" data-id="${f.id}"></button>
              <button class="del" title="Удалить" data-id="${f.id}">🗑</button>
            `;
            // Безопасно через textContent
            item.querySelector('.t').textContent = f.name;
            item.querySelector('.s').textContent = `${_fmtSize(f.size)} · ${_fmtDate(f.created_at)}` + (f.tags ? ` · 🏷 ${f.tags}` : '');
            // Toggle: переключаем enabled
            item.querySelector('.ai-kb-toggle').addEventListener('click', async (e) => {
              e.stopPropagation();
              const tgl = e.currentTarget;
              const newEnabled = !tgl.classList.contains('on');
              tgl.classList.toggle('on', newEnabled);  // optimistic
              try {
                const r = await fetch(`/knowledge/${f.id}/toggle?owner_type=${encodeURIComponent(ownerType)}&owner_id=${ownerId}&enabled=${newEnabled}`, {
                  method: 'PATCH', credentials: 'same-origin',
                });
                if (!r.ok) throw new Error('toggle failed');
                loadList();
              } catch (_) {
                tgl.classList.toggle('on', !newEnabled);  // rollback
              }
            });
            item.querySelector('.del').addEventListener('click', async () => {
              if (!confirm(`Удалить «${f.name}» из базы знаний?`)) return;
              const dr = await fetch(`/knowledge/${f.id}?owner_type=${encodeURIComponent(ownerType)}&owner_id=${ownerId}`, {
                method: 'DELETE', credentials: 'same-origin',
              });
              if (dr.ok) loadList();
            });
            list.appendChild(item);
          });
        }
        const sum = data.summary || {};
        stats.textContent = `${sum.count || 0} файлов · ${_fmtSize(sum.total_bytes || 0)} · ${sum.total_chunks || 0} чанков · лимит ${sum.max_files} файлов / ${sum.max_file_mb} МБ на файл`;
      } catch (e) {
        list.innerHTML = `<div class="ai-kb-empty">Сеть недоступна</div>`;
      }
    }

    function _pluralRu(n, one, few, many) {
      n = Math.abs(n) % 100; const n1 = n % 10;
      if (n > 10 && n < 20) return many;
      if (n1 > 1 && n1 < 5) return few;
      if (n1 === 1) return one;
      return many;
    }

    // ── Тест поиска ──
    $('#ai-kb-search-btn').addEventListener('click', doSearch);
    $('#ai-kb-q').addEventListener('keydown', e => {
      if (e.key === 'Enter') doSearch();
    });
    async function doSearch() {
      const q = $('#ai-kb-q').value.trim();
      const out = $('#ai-kb-results');
      if (!q) { out.innerHTML = ''; return; }
      out.innerHTML = '<div class="ai-kb-empty">⏳ Ищу...</div>';
      try {
        const r = await fetch(`/knowledge/search?owner_type=${encodeURIComponent(ownerType)}&owner_id=${ownerId}&q=${encodeURIComponent(q)}&top=5`, {
          credentials: 'same-origin',
        });
        if (!r.ok) {
          out.innerHTML = `<div class="ai-kb-empty">Ошибка: ${r.status}</div>`;
          return;
        }
        const d = await r.json();
        const res = d.results || [];
        if (!res.length) {
          out.innerHTML = '<div class="ai-kb-empty">Ничего не найдено в базе. Загрузите релевантные файлы или измените запрос.</div>';
          return;
        }
        out.innerHTML = '';
        res.forEach((r, i) => {
          const div = document.createElement('div');
          div.className = 'ai-kb-result';
          const meta = document.createElement('div');
          meta.className = 'meta';
          meta.innerHTML = `<b></b> · фрагмент ${r.chunk_index + 1} · <span class="score">score ${r.score}</span>`;
          meta.querySelector('b').textContent = r.file_name;
          const txt = document.createElement('div');
          txt.textContent = (r.text || '').slice(0, 600) + ((r.text || '').length > 600 ? '...' : '');
          div.appendChild(meta);
          div.appendChild(txt);
          out.appendChild(div);
        });
      } catch (e) {
        out.innerHTML = '<div class="ai-kb-empty">Сеть недоступна</div>';
      }
    }

    loadList();
  };
})();
