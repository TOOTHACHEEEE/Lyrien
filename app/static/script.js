"use strict";

// 芙宁娜主题轻交互：卡片鼠标光晕 + 按钮磁吸
// Phase 3 新增：深挖/出题的流式读取（真·打字机），笔记 Markdown 渲染

// 与后端 app/main.py 的 NOTE_SAVED_PREFIX / SOURCES_PREFIX 保持一致
const NOTE_SAVED_PREFIX = "__NOTE_SAVED__:";
const SOURCES_PREFIX = "__SOURCES__:";

document.addEventListener("DOMContentLoaded", () => {
  initCardGlow();
  initMagneticButtons();
  renderNoteContent();
  renderReportContent();
  initNoteEditor();
});

/**
 * 卡片悬停光晕跟随鼠标
 */
function initCardGlow() {
  const cards = document.querySelectorAll(".furina-card");

  cards.forEach((card) => {
    card.addEventListener("mousemove", (e) => {
      const rect = card.getBoundingClientRect();
      const x = ((e.clientX - rect.left) / rect.width) * 100;
      const y = ((e.clientY - rect.top) / rect.height) * 100;
      card.style.setProperty("--mouse-x", `${x}%`);
      card.style.setProperty("--mouse-y", `${y}%`);
    });
  });
}

/**
 * 按钮轻微磁吸效果（纯 CSS transform，不使用 setState）
 */
function initMagneticButtons() {
  const buttons = document.querySelectorAll(".btn-magnetic");

  buttons.forEach((btn) => {
    btn.addEventListener("mousemove", (e) => {
      const rect = btn.getBoundingClientRect();
      const x = e.clientX - rect.left - rect.width / 2;
      const y = e.clientY - rect.top - rect.height / 2;
      btn.style.transform = `translate(${x * 0.15}px, ${y * 0.15}px)`;
    });

    btn.addEventListener("mouseleave", () => {
      btn.style.transform = "translate(0, 0)";
    });
  });
}

/**
 * HTMX 在卡片内容 swap 后重新绑定光晕
 */
document.body.addEventListener("htmx:afterSwap", () => {
  initCardGlow();
  initMagneticButtons();
});

/**
 * 通用流式读取：fetch + ReadableStream，边收边显示。
 * 服务端流末尾可能附带哨兵行（默认 "\n__NOTE_SAVED__:<id>"），解析后回调 onMeta。
 */
async function streamInto(url, target, { markdown = false, metaPrefix = NOTE_SAVED_PREFIX, onMeta = null } = {}) {
  let resp;
  try {
    resp = await fetch(url);
  } catch {
    target.textContent = "网络请求失败，请稍后再试。";
    return;
  }
  if (!resp.ok || !resp.body) {
    target.textContent = "请求失败，请稍后再试。";
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  const sentinel = `\n${metaPrefix}`;
  let buf = "";
  target.classList.add("streaming");
  target.textContent = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    // 流式期间只显示哨兵之前的正文
    target.textContent = buf.split(sentinel)[0];
  }
  target.classList.remove("streaming");

  const [text, meta] = buf.split(sentinel);
  const body = text.trim();

  if (markdown && window.marked && window.DOMPurify) {
    target.innerHTML = DOMPurify.sanitize(marked.parse(body));
    target.classList.add("prose-furina");
  } else {
    target.textContent = body;
  }

  if (meta && onMeta) onMeta(meta.trim());
}

/**
 * 深挖成教程：流式生成 → Markdown 渲染 → 显示"已存入知识库"链接
 */
async function digCard(cardId, btn) {
  const target = document.getElementById(`dig-${cardId}`);
  if (!target) return;

  btn.disabled = true;
  btn.textContent = "深挖中…";

  await streamInto(`/dig/${cardId}`, target, {
    markdown: true,
    onMeta: (noteId) => {
      const link = document.createElement("a");
      link.href = `/notes/${noteId}`;
      link.className = "note-link";
      link.textContent = "已存入知识库 · 查看笔记 →";
      target.after(link);
      btn.textContent = "已存入知识库";
    },
  });

  if (!target.textContent.trim() && !target.innerHTML.trim()) {
    btn.disabled = false;
    btn.textContent = "深挖成教程";
  }
}

/**
 * AI 出题：复习页流式生成 3 道自测题（纯文本）
 */
async function quizCard(cardId, btn) {
  const target = document.getElementById(`quiz-${cardId}`);
  if (!target) return;

  btn.disabled = true;
  btn.textContent = "出题中…";

  await streamInto(`/review/${cardId}/quiz`, target);

  btn.disabled = false;
  btn.textContent = "换一批题";
}

/**
 * 笔记详情页：把 script 标签里的 Markdown 原文渲染成 HTML
 */
function renderNoteContent() {
  renderMarkdownFromScript("note-raw", "note-content");
}

/**
 * 周报页：渲染 Markdown 正文
 */
function renderReportContent() {
  renderMarkdownFromScript("report-raw", "report-body");
}

function renderMarkdownFromScript(rawId, targetId) {
  const raw = document.getElementById(rawId);
  const target = document.getElementById(targetId);
  if (!raw || !target) return;
  const content = JSON.parse(raw.textContent);
  if (!window.marked || !window.DOMPurify) {
    target.textContent = content;
    return;
  }
  target.innerHTML = DOMPurify.sanitize(marked.parse(content));
}

/**
 * 通用轮询：每 intervalMs 打一次 url，直到 cb 返回 true 或超时
 */
function pollUntil(url, cb, { intervalMs = 2000, timeoutMs = 90000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  const tick = async () => {
    try {
      const resp = await fetch(url);
      const data = await resp.json();
      if (cb(data)) return;
    } catch { /* 单次失败继续等 */ }
    if (Date.now() < deadline) setTimeout(tick, intervalMs);
  };
  tick();
}

/**
 * 手动投喂：POST URL → 后台生成 → 轮询状态 → 完成后刷新卡片列表
 */
async function submitFeed(event) {
  event.preventDefault();
  const form = event.target;
  const input = form.querySelector("input[name=url]");
  const status = document.getElementById("feed-status");
  const btn = document.getElementById("feed-btn");
  const url = input.value.trim();
  if (!url) return;

  btn.disabled = true;
  status.textContent = "投喂中…";

  const resp = await fetch("/feed", { method: "POST", body: new FormData(form) });
  const data = await resp.json();
  if (data.error) {
    status.textContent = data.error;
    btn.disabled = false;
    return;
  }

  pollUntil(`/feed/status?h=${encodeURIComponent(data.h)}`, (s) => {
    status.textContent = s.message;
    if (s.state === "done") {
      input.value = "";
      btn.disabled = false;
      // 刷新卡片列表，让新卡片登场
      htmx.ajax("GET", "/cards-partial", "#card-container");
      return true;
    }
    if (s.state === "error") {
      btn.disabled = false;
      return true;
    }
    return false;
  });
}

/**
 * 知识库问答：流式回答 + 渲染引用笔记链接
 */
async function askQuestion(event) {
  event.preventDefault();
  const input = event.target.querySelector("input[name=q]");
  const answer = document.getElementById("ask-answer");
  const sourcesBox = document.getElementById("ask-sources");
  const btn = document.getElementById("ask-btn");
  const q = input.value.trim();
  if (!q) return;

  btn.disabled = true;
  btn.textContent = "思考中…";
  sourcesBox.innerHTML = "";
  answer.textContent = "";

  await streamInto(`/ask/stream?q=${encodeURIComponent(q)}`, answer, {
    markdown: true,
    metaPrefix: SOURCES_PREFIX,
    onMeta: (meta) => {
      try {
        const sources = JSON.parse(meta);
        if (!sources.length) return;
        const title = document.createElement("span");
        title.className = "ask-sources-title";
        title.textContent = "参考了这些笔记：";
        sourcesBox.appendChild(title);
        sources.forEach((s) => {
          const a = document.createElement("a");
          a.href = `/notes/${s.id}`;
          a.className = "source-chip";
          a.textContent = s.title;
          sourcesBox.appendChild(a);
        });
      } catch { /* 引用解析失败不影响正文 */ }
    },
  });

  btn.disabled = false;
  btn.textContent = "提问";
}

/**
 * 手动生成周报：后台任务 + 轮询状态，完成后刷新页面
 */
async function generateReport(btn) {
  const status = document.getElementById("report-status");
  btn.disabled = true;
  status.textContent = "生成中…";

  await fetch("/report/generate", { method: "POST" });
  pollUntil("/report/status", (s) => {
    status.textContent = s.message;
    if (s.state === "done") {
      window.location.reload();
      return true;
    }
    if (s.state === "error") {
      btn.disabled = false;
      return true;
    }
    return false;
  });
}

/**
 * 笔记编辑器：实时预览 + localStorage 草稿兜底。
 *
 * 后端频繁重启丢不了已提交的数据（SQLite 落盘），会丢的是"输入框里
 * 还没保存的内容" —— 草稿每次击键落 localStorage，与服务器死活无关，
 * 页面重开时检测到草稿就提供恢复。
 */
function initNoteEditor() {
  const form = document.getElementById("note-editor-form");
  if (!form) return;

  const titleInput = form.querySelector("input[name=title]");
  const tagsInput = form.querySelector("input[name=tags]");
  const contentInput = form.querySelector("textarea[name=content]");
  const preview = document.getElementById("note-preview");
  const draftKey = form.dataset.draftKey;

  // 初始内容（编辑已有笔记时来自服务器，用于和草稿比对）
  const initial = {
    title: titleInput.value,
    tags: tagsInput.value,
    content: contentInput.value,
  };

  const renderPreview = () => {
    const text = contentInput.value;
    if (window.marked && window.DOMPurify) {
      preview.innerHTML = DOMPurify.sanitize(marked.parse(text || ""));
    } else {
      preview.textContent = text;
    }
  };

  const saveDraft = () => {
    const draft = {
      title: titleInput.value,
      tags: tagsInput.value,
      content: contentInput.value,
      savedAt: new Date().toISOString(),
    };
    // 完全空白时不留草稿
    if (!draft.title && !draft.tags && !draft.content) {
      localStorage.removeItem(draftKey);
      return;
    }
    localStorage.setItem(draftKey, JSON.stringify(draft));
  };

  // 检测草稿：与服务器内容不一致才提示（一致说明上次已保存成功）
  const banner = document.getElementById("draft-banner");
  let draft = null;
  try {
    draft = JSON.parse(localStorage.getItem(draftKey) || "null");
  } catch { /* 坏草稿当不存在 */ }
  const differs = draft && (
    draft.title !== initial.title ||
    draft.tags !== initial.tags ||
    draft.content !== initial.content
  );
  if (differs) {
    const when = draft.savedAt ? new Date(draft.savedAt).toLocaleString() : "未知时间";
    document.getElementById("draft-banner-text").textContent =
      `检测到 ${when} 的未保存草稿`;
    banner.classList.remove("hidden");
    document.getElementById("draft-restore").addEventListener("click", () => {
      titleInput.value = draft.title;
      tagsInput.value = draft.tags;
      contentInput.value = draft.content;
      renderPreview();
      banner.classList.add("hidden");
    });
    document.getElementById("draft-discard").addEventListener("click", () => {
      localStorage.removeItem(draftKey);
      banner.classList.add("hidden");
    });
  }

  // 输入联动：预览 300ms 防抖，草稿 500ms 防抖
  let previewTimer, draftTimer;
  form.addEventListener("input", () => {
    clearTimeout(previewTimer);
    clearTimeout(draftTimer);
    previewTimer = setTimeout(renderPreview, 300);
    draftTimer = setTimeout(saveDraft, 500);
  });

  // Ctrl/Cmd + S 直接保存
  form.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "s") {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  // Tab 缩进两格而不是切焦点
  contentInput.addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;
    e.preventDefault();
    const { selectionStart: s, selectionEnd: t, value } = contentInput;
    contentInput.value = `${value.slice(0, s)}  ${value.slice(t)}`;
    contentInput.selectionStart = contentInput.selectionEnd = s + 2;
    contentInput.dispatchEvent(new Event("input", { bubbles: true }));
  });

  // 保存成功后清掉草稿
  form.addEventListener("submit", () => localStorage.removeItem(draftKey));

  renderPreview();
}
