/**
 * shared.js — общие хелперы для всех HTML-страниц.
 *
 * Раньше у каждого файла были свои `function esc()`, `fmtRub()`, `aiToast()`,
 * fetch-обёртки — приходилось править в 11 местах. Теперь центральный источник.
 *
 * Подключается ПЕРЕД icons.js, чтобы icons.js (и страничные скрипты) могли
 * полагаться на глобальный window.* API.
 *
 * Идемпотентен: если на странице уже определён esc/escHtml/fmtRub —
 * не перетираем (легаси-страничные функции продолжают работать).
 */
(function () {
  "use strict";

  // ── HTML escape ────────────────────────────────────────────────────────────
  const _escMap = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const _esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => _escMap[c]);
  if (!window.esc) window.esc = _esc;
  if (!window.escHtml) window.escHtml = _esc;

  // ── Format kopecks → rubles ────────────────────────────────────────────────
  // Внутренний баланс хранится в копейках (1 ₽ = 100 коп). UI всегда показывает рубли.
  const _fmtRub = (kop) => {
    const n = Number(kop) || 0;
    return (n / 100).toLocaleString("ru-RU", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }) + " ₽";
  };
  if (!window.fmtRub) window.fmtRub = _fmtRub;
  if (!window.chToRub) window.chToRub = _fmtRub;   // legacy alias
  if (!window.formatCH) window.formatCH = _fmtRub; // legacy alias

  // ── Humanize backend errors ────────────────────────────────────────────────
  // Бэкенд возвращает {detail: "..."} или {error: "..."} или просто текст.
  // Здесь приводим к одной понятной строке для пользователя.
  if (!window.humanizeError) {
    window.humanizeError = function (err) {
      if (!err) return "Неизвестная ошибка";
      if (typeof err === "string") return err;
      if (err.detail) return String(err.detail);
      if (err.error) return String(err.error);
      if (err.message) return String(err.message);
      if (err.status === 401) return "Требуется вход в систему";
      if (err.status === 403) return "Доступ запрещён";
      if (err.status === 404) return "Не найдено";
      if (err.status === 429) return "Слишком много запросов — подождите минуту";
      if (err.status >= 500) return "Ошибка сервера, попробуйте позже";
      try { return JSON.stringify(err); } catch (_) { return String(err); }
    };
  }

  // ── Fetch wrapper с timeout/JSON/CSRF ──────────────────────────────────────
  // Применяет:
  //   - timeout (default 30s)
  //   - credentials: 'include' (cookies)
  //   - JSON-сериализация если body — объект
  //   - X-CSRF-Token если есть cookie csrf_token (double-submit)
  //   - бросает {status, detail} на не-2xx ответе
  if (!window.aiFetch) {
    window.aiFetch = async function (url, opts) {
      opts = opts || {};
      const timeout = opts.timeout != null ? opts.timeout : 30000;

      const headers = Object.assign({}, opts.headers || {});

      // JSON body
      let body = opts.body;
      if (body && typeof body === "object" && !(body instanceof FormData) && !(body instanceof Blob)) {
        body = JSON.stringify(body);
        if (!headers["Content-Type"]) headers["Content-Type"] = "application/json";
      }

      // CSRF double-submit: если есть cookie csrf_token — подложим в заголовок
      const csrfMatch = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
      if (csrfMatch && !headers["X-CSRF-Token"]) {
        headers["X-CSRF-Token"] = decodeURIComponent(csrfMatch[1]);
      }

      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeout);

      let resp;
      try {
        resp = await fetch(url, {
          method: opts.method || (body ? "POST" : "GET"),
          headers,
          body,
          credentials: opts.credentials || "include",
          signal: opts.signal || controller.signal,
        });
      } catch (e) {
        clearTimeout(timer);
        if (e && e.name === "AbortError") {
          const err = new Error("Запрос превысил время ожидания");
          err.code = "timeout";
          throw err;
        }
        throw e;
      }
      clearTimeout(timer);

      const ct = resp.headers.get("content-type") || "";
      const parse = async () => {
        if (ct.indexOf("application/json") >= 0) {
          try { return await resp.json(); } catch (_) { return null; }
        }
        try { return await resp.text(); } catch (_) { return null; }
      };

      if (!resp.ok) {
        const data = await parse();
        const err = new Error(window.humanizeError(data || resp.statusText));
        err.status = resp.status;
        err.data = data;
        throw err;
      }

      if (opts.raw) return resp;
      return await parse();
    };
  }

  // ── Toast (если ещё не определён в icons.js) ───────────────────────────────
  // icons.js определяет полноценный aiToast — здесь только fallback.
  if (!window.aiToast) {
    window.aiToast = function (msg, type) {
      try { console.log("[toast " + (type || "info") + "]", msg); } catch (_) {}
    };
  }

  // ── aiAlertError: показать ошибку из catch-блока через aiAlert/aiToast ────
  if (!window.aiAlertError) {
    window.aiAlertError = function (err, prefix) {
      const msg = window.humanizeError(err);
      const full = prefix ? prefix + ": " + msg : msg;
      if (window.aiAlert) {
        window.aiAlert(full);
      } else if (window.aiToast) {
        window.aiToast(full, "error");
      } else {
        alert(full);
      }
    };
  }

  // ── PWA: регистрация Service Worker на любой странице ─────────────────────
  // Делаем здесь чтобы не дублировать в каждой view-страничке. SW даёт
  // offline-fallback + stale-while-revalidate для статики. Install-banner
  // живёт только на index.html (там есть кнопка), на под-страницах не нужен.
  if ("serviceWorker" in navigator && !window.__aichePwaRegistered) {
    window.__aichePwaRegistered = true;
    window.addEventListener("load", function () {
      navigator.serviceWorker
        .register("/sw.js", { scope: "/" })
        .catch(function (err) {
          console.warn("[PWA] SW registration failed:", err);
        });
    });
  }
})();
