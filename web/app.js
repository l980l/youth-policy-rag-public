(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const state = {
    category: '', page: 1, query: '', bootstrap: null, selected: null,
    chats: new Map(), pendingChats: new Set(), lastFocus: null,
    policyRequest: 0, catalogueRequest: 0, discoverRequest: 0, discoveryPending: false, toastTimer: null,
  };
  const drawer = $('#policy-drawer');
  const drawerContent = $('#drawer-content');
  const grid = $('#policy-grid');

  function node(tag, options = {}, children = []) {
    const element = document.createElement(tag);
    Object.entries(options).forEach(([key, value]) => {
      if (value === undefined || value === null) return;
      if (key === 'text') element.textContent = String(value);
      else if (key === 'class') element.className = value;
      else if (key === 'dataset') Object.entries(value).forEach(([name, data]) => { element.dataset[name] = data; });
      else if (key === 'attrs') Object.entries(value).forEach(([name, attribute]) => element.setAttribute(name, String(attribute)));
      else element[key] = value;
    });
    const values = Array.isArray(children) ? children : [children];
    values.filter(Boolean).forEach((child) => element.append(child));
    return element;
  }

  function clear(element) { element.replaceChildren(); }
  function number(value) { return Number(value || 0).toLocaleString('ko-KR'); }
  function clean(value, fallback = '정보 확인 필요') { return String(value || '').trim() || fallback; }
  function safeHttps(value) { try { const url = new URL(value); return url.protocol === 'https:' ? url.href : ''; } catch { return ''; } }
  function errorMessage(error) { return error?.message || '요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.'; }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      method: options.method || 'GET', headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    let data = null;
    try { data = await response.json(); } catch { /* malformed response falls through */ }
    if (!response.ok) throw new Error(data?.error?.message || '서버 요청에 실패했습니다.');
    return data;
  }

  function status(target, message, kind = '') {
    clear(target);
    if (!message) return;
    const text = node('p', { class: `status-message ${kind}`, text: message });
    if (kind === 'busy') text.prepend(node('span', { class: 'busy-dot', attrs: { 'aria-hidden': 'true' } }));
    target.append(text);
  }

  function showToast(message) {
    const toast = $('#toast');
    toast.textContent = message;
    toast.classList.add('show');
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => toast.classList.remove('show'), 2500);
  }

  function renderBootstrap(data) {
    state.bootstrap = data;
    const notice = $('#demo-notice');
    notice.hidden = !data?.demo;
    notice.textContent = data?.demo?.message || '';
    const stats = data?.stats || {};
    document.querySelectorAll('[data-stat]').forEach((item) => { item.textContent = number(stats[item.dataset.stat]); });
    const date = stats.collected_at ? `자료 수집 기준: ${clean(stats.collected_at)}` : '';
    $('#collection-date').textContent = date;
    renderCategories(data?.categories || []);
  }

  function renderCategories(categories) {
    const container = $('#category-pills');
    clear(container);
    const allCount = state.bootstrap?.stats?.policy_count;
    const all = makeCategoryButton('전체', allCount, '');
    container.append(all);
    categories.forEach((item) => container.append(makeCategoryButton(clean(item.name, '기타'), item.count, item.name)));
  }

  function makeCategoryButton(label, count, value) {
    const button = node('button', { class: 'category-pill', type: 'button', text: label, attrs: { 'aria-pressed': String(state.category === value) } });
    if (Number.isFinite(Number(count))) button.append(node('small', { text: number(count) }));
    button.addEventListener('click', () => { if (state.category !== value) { state.category = value; state.page = 1; renderCategories(state.bootstrap?.categories || []); loadPolicies(); } });
    return button;
  }

  function policyCard(policy) {
    const card = node('button', { class: 'policy-card', type: 'button', attrs: { 'aria-label': `${clean(policy.name)} 상세 보기` } });
    card.append(node('span', { class: 'policy-category', text: clean(policy.category) }));
    card.append(node('span', { class: 'policy-institution', text: clean(policy.institution) }));
    card.append(node('h3', { text: clean(policy.name) }));
    card.append(node('p', { class: 'policy-summary', text: clean(policy.summary, '정책 세부 내용을 확인해 보세요.') }));
    const bottom = node('div', { class: 'policy-bottom' });
    bottom.append(node('span', { class: 'age-label', text: clean(policy.age_label, '연령 조건 확인') }));
    bottom.append(node('span', { class: 'detail-link', text: '자세히 보기 →' }));
    card.append(bottom);
    card.addEventListener('click', () => openPolicy(policy.policy_id, card));
    return card;
  }

  function policySkeletons() {
    clear(grid); grid.setAttribute('aria-busy', 'true');
    for (let i = 0; i < 6; i += 1) {
      const item = node('div', { class: 'policy-card skeleton', attrs: { 'aria-hidden': 'true' } });
      item.append(node('div', { class: 'sk' }), node('div', { class: 'sk sk-title' }), node('div', { class: 'sk' }), node('div', { class: 'sk sk-last' })); grid.append(item);
    }
  }

  async function loadPolicies() {
    const statusTarget = $('#catalogue-status');
    policySkeletons(); status(statusTarget, '정책 목록을 불러오는 중입니다.', 'busy'); clear($('#pagination'));
    const params = new URLSearchParams({ page: String(state.page), limit: '12' });
    if (state.query) params.set('q', state.query);
    if (state.category) params.set('category', state.category);
    const request = ++state.catalogueRequest;
    try {
      const data = await api(`/api/policies?${params}`);
      if (request !== state.catalogueRequest) return;
      renderPolicies(data);
      status(statusTarget, '');
    } catch (error) {
      if (request !== state.catalogueRequest) return;
      clear(grid); grid.setAttribute('aria-busy', 'false');
      grid.append(errorBox(errorMessage(error), loadPolicies));
      status(statusTarget, '목록을 불러오지 못했습니다.', 'error');
      $('#catalogue-count').textContent = '';
    }
  }

  function errorBox(message, retry) {
    const box = node('div', { class: 'error-state' });
    box.append(node('p', { text: message }));
    const button = node('button', { type: 'button', text: '다시 시도' }); button.addEventListener('click', retry); box.append(button); return box;
  }

  function renderPolicies(data) {
    clear(grid); grid.setAttribute('aria-busy', 'false');
    const items = Array.isArray(data.items) ? data.items : [];
    $('#catalogue-count').textContent = `총 ${number(data.total)}개 정책`;
    if (!items.length) grid.append(node('div', { class: 'empty-state', text: '조건에 맞는 정책을 찾지 못했습니다. 다른 키워드나 분야로 다시 찾아보세요.' }));
    else items.forEach((policy) => grid.append(policyCard(policy)));
    renderPagination(data);
  }

  function renderPagination(data) {
    const nav = $('#pagination'); clear(nav);
    const current = Number(data.page) || 1; const total = Number(data.total_pages) || 1;
    if (total <= 1) return;
    const add = (label, page, disabled, currentPage) => {
      const button = node('button', { type: 'button', text: label, disabled: Boolean(disabled) });
      if (currentPage) button.setAttribute('aria-current', 'page');
      button.addEventListener('click', () => { state.page = page; loadPolicies(); document.querySelector('#catalogue').scrollIntoView({ behavior: 'smooth', block: 'start' }); }); nav.append(button);
    };
    add('‹', Math.max(1, current - 1), current === 1);
    const start = Math.max(1, Math.min(current - 2, total - 4)); const end = Math.min(total, start + 4);
    for (let page = start; page <= end; page += 1) add(String(page), page, false, page === current);
    add('›', Math.min(total, current + 1), current === total);
  }

  async function runDiscovery(query) {
    const results = $('#discovery-results'); const statusTarget = $('#discovery-status');
    if (state.bootstrap?.demo?.ai_enabled === false) { status(statusTarget, state.bootstrap.demo.message, 'error'); return; }
    const trimmed = query.trim();
    if (!trimmed) { status(statusTarget, '찾고 싶은 상황을 한 문장으로 적어주세요.', 'error'); $('#discovery-query').focus(); return; }
    if (state.discoveryPending) return;
    const request = ++state.discoverRequest; state.discoveryPending = true; setDiscoveryBusy(true); clear(results); status(statusTarget, '정책과 근거 문서를 살펴보고 있어요. 잠시만 기다려 주세요.', 'busy');
    try {
      const data = await api('/api/discover', { method: 'POST', body: { query: trimmed } });
      if (request !== state.discoverRequest) return;
      status(statusTarget, ''); renderDiscovery(data);
    } catch (error) { if (request === state.discoverRequest) { status(statusTarget, errorMessage(error), 'error'); clear(results); } }
    finally { if (request === state.discoverRequest) { state.discoveryPending = false; setDiscoveryBusy(false); } }
  }

  function setDiscoveryBusy(busy) {
    const form = $('#discovery-form'); form.setAttribute('aria-busy', String(busy));
    form.querySelectorAll('button').forEach((button) => { button.disabled = busy; });
  }

  function renderDiscovery(data) {
    const root = $('#discovery-results'); clear(root);
    const matches = Array.isArray(data.matches) ? data.matches : [];
    const unsure = Array.isArray(data.needs_confirmation) ? data.needs_confirmation : [];
    if (!matches.length && !unsure.length) { root.append(node('div', { class: 'empty-state', text: '지금 입력한 내용과 직접 연결되는 정책을 찾지 못했습니다. 지역, 관심 분야 또는 현재 상황을 조금 더 알려주세요.' })); return; }
    if (matches.length) root.append(discoveryGroup('관련 정책', '관련 근거를 확인한 후보', matches, false));
    if (unsure.length) root.append(discoveryGroup('자격 확인 필요', '추가 확인이 필요한 후보', unsure, true));
    const caveat = node('p', { class: 'status-message', text: '추천 결과는 정책과의 관련성을 보여드립니다. 실제 신청 자격·일정은 공식 안내에서 최종 확인해 주세요.' }); root.append(caveat);
  }

  function discoveryGroup(title, subtitle, matches, needsConfirm) {
    const section = node('section', { class: 'discovery-group' }); const heading = node('h3', { text: title }); heading.append(node('small', { text: subtitle })); section.append(heading);
    const list = node('div', { class: 'match-list' });
    matches.forEach((match) => {
      const policy = match.policy || {}; const card = node('button', { class: 'match-card', type: 'button' });
      card.append(node('span', { class: 'match-badge', text: needsConfirm ? '자격 확인 필요' : '관련 근거 확인' }));
      card.append(node('h4', { text: clean(policy.name) }));
      const explanation = needsConfirm ? (match.unverified_conditions || match.evidence) : (match.evidence || match.verified_conditions);
      card.append(node('p', { text: clean(Array.isArray(explanation) ? explanation.join(' · ') : explanation, '정책 상세에서 근거를 확인할 수 있어요.') }));
      if (needsConfirm && Array.isArray(match.additional_requirements)) {
        match.additional_requirements.forEach((requirement) => {
          card.append(node('p', { class: 'requirement-evidence', text: `${clean(requirement.condition)} 근거: ${clean(requirement.evidence)}` }));
        });
      }
      card.append(node('span', { class: 'match-more', text: '정책 내용 보기 →' }));
      card.addEventListener('click', () => openPolicy(policy.policy_id, card)); list.append(card);
    }); section.append(list); return section;
  }

  function lockDialog(open) { document.body.classList.toggle('drawer-open', open); }
  function focusables() { return Array.from(drawer.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')).filter((item) => item.offsetParent !== null); }
  function openDrawer(trigger) { state.lastFocus = trigger || document.activeElement; if (!drawer.open) drawer.showModal(); lockDialog(true); $('#drawer-close').focus(); }
  function closeDrawer() { if (drawer.open) drawer.close(); }

  async function openPolicy(policyId, trigger) {
    if (!policyId) { showToast('정책 정보를 불러올 수 없습니다.'); return; }
    state.selected = { id: String(policyId), policy: null, sections: [], tab: 'overview' };
    openDrawer(trigger); renderDrawerLoading(); drawerContent.scrollTop = 0; const request = ++state.policyRequest;
    try {
      const data = await api(`/api/policies/${encodeURIComponent(policyId)}`);
      if (request !== state.policyRequest || state.selected?.id !== String(policyId)) return;
      state.selected.policy = data.policy || {}; state.selected.sections = Array.isArray(data.sections) ? data.sections : []; renderDrawer();
    } catch (error) {
      if (request !== state.policyRequest || state.selected?.id !== String(policyId)) return;
      clear(drawerContent); drawerContent.append(errorBox(errorMessage(error), () => openPolicy(policyId, trigger)));
    }
  }

  function renderDrawerLoading() { drawerContent.classList.remove('chat-mode'); clear(drawerContent); drawerContent.append(node('p', { class: 'drawer-loading', text: '정책 원문과 안내 정보를 불러오는 중입니다.' })); }
  function renderDrawer() {
    const selected = state.selected; if (!selected?.policy) return;
    const restoreTabFocus = drawerContent.contains(document.activeElement) && document.activeElement.getAttribute('role') === 'tab';
    clear(drawerContent);
    drawerContent.classList.toggle('chat-mode', selected.tab === 'chat');
    const policy = selected.policy;
    drawerContent.append(node('p', { class: 'drawer-category', text: clean(policy.category) }));
    drawerContent.append(node('h2', { id: 'drawer-title', class: 'drawer-title', text: clean(policy.name) }));
    drawerContent.append(node('p', { class: 'drawer-institution', text: clean(policy.institution) }));
    const tabs = node('div', { class: 'drawer-tabs', attrs: { role: 'tablist', 'aria-label': '정책 상세 탭' } });
    [['overview', '정책 정보'], ['chat', 'AI에게 묻기']].forEach(([id, label]) => { const button = node('button', { type: 'button', text: label, attrs: { role: 'tab', 'aria-selected': String(selected.tab === id) } }); button.addEventListener('click', () => { selected.tab = id; renderDrawer(); }); tabs.append(button); }); drawerContent.append(tabs);
    if (selected.tab === 'chat') renderChat(drawerContent, selected); else renderOverview(drawerContent, policy, selected.sections);
    if (selected.tab === 'overview' && restoreTabFocus) tabs.querySelector('[aria-selected="true"]').focus({ preventScroll: true });
  }

  function renderOverview(root, policy, sections) {
    const facts = node('div', { class: 'facts' });
    [['분야', policy.category], ['세부 분야', policy.subcategory], ['연령 안내', policy.age_label], ['자료 안내', evidenceLabel(policy.evidence_quality)]].forEach(([label, value]) => { const fact = node('div', { class: 'fact' }); fact.append(node('strong', { text: label }), node('span', { text: clean(value) })); facts.append(fact); }); root.append(facts);
    if (policy.summary) root.append(node('p', { class: 'overview-summary', text: policy.summary }));
    const content = node('div', { class: 'content-sections' }); sections.forEach((section) => { content.append(node('h3', { text: clean(section.label) }), node('p', { text: clean(section.text) })); }); root.append(content);
    const href = safeHttps(policy.source_url); if (href) { const link = node('a', { class: 'source-link', href, target: '_blank', rel: 'noopener noreferrer', text: '공식 원문 열기 ↗' }); root.append(link); }
  }

  function evidenceLabel(value) {
    const labels = { substantive: '설명 자료 있음', limited: '추가 확인 필요', unavailable: '추가 확인 필요' };
    return labels[String(value || '').toLowerCase()] || '공식 원문 확인 권장';
  }

  function messagesFor(id) { if (!state.chats.has(id)) state.chats.set(id, []); return state.chats.get(id); }
  function renderChat(container, selected) {
    const root = node('section', { class: 'chat-workspace', attrs: { 'aria-label': '정책별 AI 상담' } });
    container.append(root);
    const messages = messagesFor(selected.id); root.append(node('p', { class: 'chat-intro', text: '이 정책의 내용과 조건을 물어보세요. 답변의 출처를 함께 확인할 수 있어요.' }));
    const log = node('div', { class: 'chat-log', attrs: { 'aria-live': 'polite', 'aria-label': '정책 대화' } });
    if (!messages.length) log.append(node('div', { class: 'chat-message assistant', text: '무엇이 궁금한가요? 정책 원문을 바탕으로 확인해 볼게요.' }));
    messages.forEach((message) => appendMessage(log, message));
    if (state.pendingChats.has(selected.id)) log.append(node('p', { class: 'chat-pending', text: '답변을 확인하고 있어요.', attrs: { role: 'status' } }));
    root.append(log);
    const quick = node('div', { class: 'quick-questions', attrs: { 'aria-label': '빠른 질문' } });
    ['누가 대상인가요?', '준비할 서류가 있나요?', '신청 전에 확인할 점은?'].forEach((question) => { const button = node('button', { type: 'button', text: question, disabled: state.pendingChats.has(selected.id), attrs: { 'aria-busy': String(state.pendingChats.has(selected.id)) } }); button.addEventListener('click', () => submitChat(question)); quick.append(button); }); root.append(quick);
    const form = node('form', { class: 'chat-form' }); const area = node('textarea', { id: 'chat-question', placeholder: '이 정책에 관해 물어보세요', disabled: state.pendingChats.has(selected.id), attrs: { 'aria-label': '정책 질문', rows: '1', maxlength: '800' } }); const send = node('button', { type: 'submit', text: state.pendingChats.has(selected.id) ? '답변 중' : '보내기', disabled: state.pendingChats.has(selected.id) });
    form.append(area, send); form.addEventListener('submit', (event) => { event.preventDefault(); submitChat(area.value); }); area.addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); form.requestSubmit(); } }); root.append(form);
    const checklist = node('button', { class: 'checklist-button', type: 'button', text: '신청 전 확인 목록 만들기', disabled: state.pendingChats.has(selected.id), attrs: { 'aria-busy': String(state.pendingChats.has(selected.id)) } }); checklist.addEventListener('click', () => submitChat('이 정책을 신청하기 전에 확인할 사항을 체크리스트로 정리해 줘. 근거가 불확실한 내용은 명확히 밝혀 줘.')); root.append(checklist);
    if (messages.length) { const clearChat = node('button', { class: 'clear-chat-button', type: 'button', text: '이 정책의 대화 지우기', disabled: state.pendingChats.has(selected.id) }); clearChat.addEventListener('click', () => { state.chats.set(selected.id, []); renderDrawer(); showToast('이 정책의 대화를 지웠습니다.'); }); root.append(clearChat); }
    requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; if (!area.disabled) area.focus({ preventScroll: true }); });
  }

  function appendMessage(log, message) {
    const type = message.role === 'user' ? 'user' : (message.role === 'error' ? 'error' : 'assistant'); const bubble = node('div', { class: `chat-message ${type}` }); renderSafeMarkdown(bubble, clean(message.content, '')); log.append(bubble);
    if (Array.isArray(message.sources) && message.sources.length) { const sources = node('div', { class: 'chat-sources' }); message.sources.forEach((source, index) => { const detail = node('details'); const meta = [source.source_type, source.collected_at].filter(Boolean).join(' · '); detail.append(node('summary', { text: `[근거 ${source.number || index + 1}] ${clean(source.title, '출처')}${meta ? ` — ${meta}` : ''}` })); const body = node('div', { class: 'citation-body', text: clean(source.text, '인용문이 제공되지 않았습니다.') }); const href = safeHttps(source.url); if (href) body.append(node('a', { href, target: '_blank', rel: 'noopener noreferrer', text: '공식 링크 열기 ↗' })); detail.append(body); sources.append(detail); }); if (message.partialSources) sources.append(node('p', { class: 'citation-body', text: '일부 근거만 확인되었습니다. 공식 원문을 함께 확인해 주세요.' })); log.append(sources); }
  }

  function renderSafeMarkdown(target, content) {
    const lines = String(content).split(/\r?\n/); let list = null;
    const flushList = () => { if (list) { target.append(list); list = null; } };
    lines.forEach((line) => {
      const heading = line.match(/^#{1,3}\s+(.+)/); const bullet = line.match(/^[-*]\s+(.+)/); const ordered = line.match(/^\d+[.)]\s+(.+)/);
      if (heading) { flushList(); const title = node('strong', { class: 'md-heading' }); appendInline(title, heading[1]); target.append(title); return; }
      if (bullet || ordered) { if (!list || list.tagName !== (ordered ? 'OL' : 'UL')) { flushList(); list = node(ordered ? 'ol' : 'ul', { class: 'md-list' }); } const item = node('li'); appendInline(item, (bullet || ordered)[1]); list.append(item); return; }
      flushList(); const paragraph = node('span', { class: 'md-line' }); appendInline(paragraph, line || ' '); target.append(paragraph);
    }); flushList();
  }

  function appendInline(target, text) {
    String(text).split(/(\*\*[^*]+\*\*)/g).filter((part) => part !== '').forEach((part) => { if (part.startsWith('**') && part.endsWith('**')) target.append(node('strong', { text: part.slice(2, -2) })); else target.append(document.createTextNode(part)); });
  }

  async function submitChat(question) {
    if (state.bootstrap?.demo?.ai_enabled === false) { showToast(state.bootstrap.demo.message); return; }
    const selected = state.selected; const trimmed = String(question || '').trim();
    if (!selected?.policy || !trimmed || state.pendingChats.has(selected.id)) return;
    const id = selected.id; const messages = messagesFor(id); const history = messages.filter((message) => message.role === 'user' || message.role === 'assistant').slice(-6).map(({ role, content }) => ({ role, content: String(content).slice(0, 1500) }));
    messages.push({ role: 'user', content: trimmed }); state.pendingChats.add(id); renderDrawer();
    try {
      const data = await api('/api/chat', { method: 'POST', body: { policy_id: id, question: trimmed, history } });
      messages.push({ role: 'assistant', content: clean(data.answer, '답변을 받지 못했습니다.'), sources: Array.isArray(data.sources) ? data.sources : [], partialSources: Boolean(data.partial_sources) });
    } catch (error) { messages.push({ role: 'error', content: errorMessage(error) }); }
    finally { state.pendingChats.delete(id); if (state.selected?.id === id && state.selected.tab === 'chat') renderDrawer(); }
  }

  function bind() {
    $('#discovery-form').addEventListener('submit', (event) => { event.preventDefault(); runDiscovery($('#discovery-query').value); });
    document.querySelectorAll('[data-suggestion]').forEach((button) => button.addEventListener('click', () => { $('#discovery-query').value = button.dataset.suggestion || ''; runDiscovery($('#discovery-query').value); }));
    $('[data-focus-search]').addEventListener('click', () => { $('#explore').scrollIntoView({ behavior: 'smooth', block: 'start' }); setTimeout(() => $('#discovery-query').focus(), 360); });
    $('#catalogue-search').addEventListener('submit', (event) => { event.preventDefault(); state.query = $('#catalogue-query').value.trim(); state.page = 1; loadPolicies(); });
    $('#drawer-close').addEventListener('click', closeDrawer);
    drawer.addEventListener('close', () => { lockDialog(false); state.selected = null; if (state.lastFocus?.focus) state.lastFocus.focus(); state.lastFocus = null; });
    drawer.addEventListener('cancel', (event) => { event.preventDefault(); closeDrawer(); });
    drawer.addEventListener('keydown', (event) => { if (event.key !== 'Tab') return; const items = focusables(); if (!items.length) return; const first = items[0]; const last = items[items.length - 1]; if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } });
  }

  async function boot() {
    bind();
    policySkeletons();
    try { renderBootstrap(await api('/api/bootstrap')); } catch (error) { status($('#catalogue-status'), `기본 정보를 불러오지 못했습니다: ${errorMessage(error)}`, 'error'); }
    loadPolicies();
  }
  boot();
})();
