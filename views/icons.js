/**
 * Icon library — единый набор иконок в стиле «AI Студия Че».
 *
 * Заменяет эмодзи (🤖 💰 ✨ и т.д.) на векторные SVG в едином стиле.
 *
 * Использование:
 *   1. Подключить: <script src="/icons.js"></script>
 *   2. Поставить плейсхолдер: <span data-i="sparkle"></span>
 *      или вместо эмодзи в текстах кнопок:
 *      <button><span data-i="check"></span> Сохранить</button>
 *   3. Скрипт автоматически заменит на SVG при DOMContentLoaded.
 *      Также экспортирует window.ICONS — можно вставлять программно:
 *      el.innerHTML = ICONS.sparkle + ' Готово';
 *
 * Все иконки 24×24 viewBox, fill="currentColor" — наследуют цвет текста.
 * Размер по умолчанию 14×14 (под текст 13-14px); переопределяется CSS-ом
 * на родителе или через class="i-lg" / "i-xl".
 *
 * Стиль: Material Symbols Outlined-подобный, чистые линии, минималистичный.
 */
(function () {
  'use strict';

  const ICONS = {
    // ── Status & feedback ─────────────────────────────────────────────────
    check:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z"/></svg>',
    cross:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19 6.4 17.6 5 12 10.6 6.4 5 5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12z"/></svg>',
    warn:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></svg>',
    info:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10 10-4.5 10-10S17.5 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg>',
    question: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10 10-4.5 10-10S17.5 2 12 2zm1 17h-2v-2h2v2zm2.1-7.8-.9.9c-.7.7-1.2 1.4-1.2 2.9h-2v-.5c0-1.1.4-2.1 1.2-2.8l1.2-1.2c.4-.3.6-.8.6-1.4 0-1.1-.9-2-2-2s-2 .9-2 2H8c0-2.2 1.8-4 4-4s4 1.8 4 4c0 .9-.4 1.7-.9 2.2z"/></svg>',
    success:  '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10 10-4.5 10-10S17.5 2 12 2zm-2 15-5-5 1.4-1.4L10 14.2l7.6-7.6L19 8l-9 9z"/></svg>',
    error:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10 10-4.5 10-10S17.5 2 12 2zm5 13.6L15.6 17 12 13.4 8.4 17 7 15.6 10.6 12 7 8.4 8.4 7 12 10.6 15.6 7 17 8.4 13.4 12 17 15.6z"/></svg>',
    dot:      '<svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="6"/></svg>',

    // ── Actions ───────────────────────────────────────────────────────────
    sparkle:  '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 1.5 13.6 7 19 8.6 13.6 10.2 12 15.6 10.4 10.2 5 8.6 10.4 7zM18.5 14l.7 2.4 2.3.6-2.3.6-.7 2.4-.7-2.4-2.3-.6 2.3-.6zM5.5 14l.7 2.4L8.5 17l-2.3.6-.7 2.4-.7-2.4L2.5 17l2.3-.6z"/></svg>',
    edit:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34a.9959.9959 0 00-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>',
    trash:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>',
    download: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>',
    upload:   '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M9 16h6v-6h4l-7-7-7 7h4zm-4 2h14v2H5z"/></svg>',
    save:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M17 3H5c-1.11 0-2 .9-2 2v14c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2V7l-4-4zm-5 16c-1.66 0-3-1.34-3-3s1.34-3 3-3 3 1.34 3 3-1.34 3-3 3zm3-10H5V5h10v4z"/></svg>',
    plus:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>',
    refresh:  '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M17.65 6.35A7.958 7.958 0 0 0 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0 1 12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>',
    copy:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>',
    search:   '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M15.5 14h-.79l-.28-.27a6.5 6.5 0 1 0-.7.7l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0a4.5 4.5 0 1 1 0-9 4.5 4.5 0 0 1 0 9z"/></svg>',
    eye:      '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zM12 17a5 5 0 1 1 0-10 5 5 0 0 1 0 10zm0-8a3 3 0 1 0 0 6 3 3 0 0 0 0-6z"/></svg>',
    eyeOff:   '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 7c2.76 0 5 2.24 5 5 0 .65-.13 1.26-.36 1.83l2.92 2.92c1.51-1.26 2.7-2.89 3.43-4.75-1.73-4.39-6-7.5-11-7.5-1.4 0-2.74.25-3.98.7l2.16 2.16C10.74 7.13 11.35 7 12 7zM2 4.27l2.28 2.28.46.46A11.804 11.804 0 0 0 1 12c1.73 4.39 6 7.5 11 7.5 1.55 0 3.03-.3 4.38-.84l.42.42L19.73 22 21 20.73 3.27 3 2 4.27zM7.53 9.8l1.55 1.55c-.05.21-.08.43-.08.65 0 1.66 1.34 3 3 3 .22 0 .44-.03.65-.08l1.55 1.55c-.67.33-1.41.53-2.2.53-2.76 0-5-2.24-5-5 0-.79.2-1.53.53-2.2zm4.31-.78 3.15 3.15.02-.16c0-1.66-1.34-3-3-3l-.17.01z"/></svg>',
    link:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7a5 5 0 0 0 0 10h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8v2zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4a5 5 0 0 0 0-10z"/></svg>',
    close:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19 6.4 17.6 5 12 10.6 6.4 5 5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12z"/></svg>',
    arrowLeft: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>',
    arrowRight:'<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 4l-1.41 1.41L16.17 11H4v2h12.17l-5.58 5.59L12 20l8-8z"/></svg>',
    fingerDown:'<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M16.59 8.59 12 13.17 7.41 8.59 6 10l6 6 6-6z"/></svg>',
    wand:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M7.5 5.6 10 7 8.6 4.5 10 2 7.5 3.4 5 2l1.4 2.5L5 7zm12 9.8L17 14l1.4 2.5L17 19l2.5-1.4L22 19l-1.4-2.5L22 14zM22 2l-2.5 1.4L17 2l1.4 2.5L17 7l2.5-1.4L22 7l-1.4-2.5zm-7.63 5.29a.9959.9959 0 0 0-1.41 0L1.29 18.96a.9959.9959 0 0 0 0 1.41l2.34 2.34c.39.39 1.02.39 1.41 0L16.7 11.05c.39-.39.39-1.02 0-1.41l-2.33-2.35zm-1.03 5.49-2.12-2.12 2.44-2.44 2.12 2.12-2.44 2.44z"/></svg>',
    settings: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19.4 13c.04-.33.06-.66.06-1s-.02-.67-.06-1l2.11-1.65a.5.5 0 0 0 .12-.64l-2-3.46a.5.5 0 0 0-.61-.22l-2.49 1c-.52-.4-1.08-.73-1.69-.98l-.38-2.65A.488.488 0 0 0 14 2h-4a.488.488 0 0 0-.49.42l-.38 2.65c-.61.25-1.17.59-1.69.98l-2.49-1a.566.566 0 0 0-.18-.03c-.17 0-.34.09-.43.25l-2 3.46a.5.5 0 0 0 .12.64L4.57 11c-.04.33-.07.66-.07 1s.03.67.07 1l-2.11 1.65a.5.5 0 0 0-.12.64l2 3.46a.5.5 0 0 0 .61.22l2.49-1c.52.4 1.08.73 1.69.98l.38 2.65c.05.24.25.42.49.42h4c.24 0 .44-.18.49-.42l.38-2.65c.61-.25 1.17-.59 1.69-.98l2.49 1c.06.02.12.03.18.03.17 0 .34-.09.43-.25l2-3.46a.5.5 0 0 0-.12-.64L19.4 13zM12 15.5c-1.93 0-3.5-1.57-3.5-3.5s1.57-3.5 3.5-3.5 3.5 1.57 3.5 3.5-1.57 3.5-3.5 3.5z"/></svg>',
    bolt:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M7 2v11h3v9l7-12h-4l3-8z"/></svg>',
    play:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
    pause:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>',
    flag:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M14.4 6 14 4H5v17h2v-7h5.6l.4 2h7V6z"/></svg>',
    gift:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M20 6h-2.18c.11-.31.18-.65.18-1a2.996 2.996 0 0 0-5.5-1.65l-.5.67-.5-.68C10.96 2.54 10.05 2 9 2 7.34 2 6 3.34 6 5c0 .35.07.69.18 1H4c-1.11 0-1.99.89-1.99 2L2 19c0 1.11.89 2 2 2h16c1.11 0 2-.89 2-2V8c0-1.11-.89-2-2-2zm-5-2c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1zM9 4c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1zm11 15H4v-2h16v2zm0-5H4V8h5.08L7 10.83 8.62 12 11 8.76l1-1.36 1 1.36L15.38 12 17 10.83 14.92 8H20v6z"/></svg>',
    lightbulb:'<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M9 21c0 .55.45 1 1 1h4c.55 0 1-.45 1-1v-1H9v1zm3-19C8.14 2 5 5.14 5 9c0 2.38 1.19 4.47 3 5.74V17c0 .55.45 1 1 1h6c.55 0 1-.45 1-1v-2.26c1.81-1.27 3-3.36 3-5.74 0-3.86-3.14-7-7-7z"/></svg>',
    plane:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="m21 16-1.5-1.5-3 1V8L21 4l-1-1-5 3-5-3-1 1 4.5 4.5v7l-3-1L9 16v1l3-1 3 1v-1z"/></svg>',
    plug:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M16 7V3h-2v4h-4V3H8v4H7v5l3 2v3h4v-3l3-2V7h-1z"/></svg>',
    paperclip:'<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="m16.5 6-7.42 7.42c-.78.78-.78 2.05 0 2.83.78.78 2.05.78 2.83 0L17.83 9.5l1.41 1.41L13 17.16c-1.95 1.95-5.12 1.95-7.07 0-1.95-1.95-1.95-5.12 0-7.07l8.51-8.51c1.18-1.17 3.17-1.17 4.34 0 1.17 1.17 1.17 3.17 0 4.34L9.5 15.42l-1.41-1.42L16 6.09l-.54-.54L10 11l1.41 1.41L17 6.83 16.5 6z"/></svg>',

    // ── Money & business ──────────────────────────────────────────────────
    money:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M11.8 10.9c-2.27-.59-3-1.2-3-2.15 0-1.09 1.01-1.85 2.7-1.85 1.78 0 2.44.85 2.5 2.1h2.21c-.07-1.72-1.12-3.3-3.21-3.81V3h-3v2.16c-1.94.42-3.5 1.68-3.5 3.61 0 2.31 1.91 3.46 4.7 4.13 2.5.6 3 1.48 3 2.41 0 .69-.49 1.79-2.7 1.79-2.06 0-2.87-.92-2.98-2.1h-2.2c.12 2.19 1.76 3.42 3.68 3.83V21h3v-2.15c1.95-.37 3.5-1.5 3.5-3.55 0-2.84-2.43-3.81-4.7-4.4z"/></svg>',
    creditCard:'<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M20 4H4c-1.11 0-1.99.89-1.99 2L2 18c0 1.11.89 2 2 2h16c1.11 0 2-.89 2-2V6c0-1.11-.89-2-2-2zm0 14H4v-6h16v6zm0-10H4V6h16v2z"/></svg>',
    briefcase:'<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M20 6h-4V4c0-1.11-.89-2-2-2h-4c-1.11 0-2 .89-2 2v2H4c-1.11 0-1.99.89-1.99 2L2 19c0 1.11.89 2 2 2h16c1.11 0 2-.89 2-2V8c0-1.11-.89-2-2-2zm-6 0h-4V4h4v2z"/></svg>',
    chart:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M3.5 18.49 9.5 12.48l4 4L22 6.92l-1.41-1.41-7.09 7.97-4-4L2 16.99z"/></svg>',
    document: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg>',
    clipboard:'<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19 3h-4.18C14.4 1.84 13.3 1 12 1s-2.4.84-2.82 2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-7 0c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1zm2 14H7v-2h7v2zm3-4H7v-2h10v2zm0-4H7V7h10v2z"/></svg>',
    folder:   '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M10 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/></svg>',
    target:   '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zm0-14c-3.31 0-6 2.69-6 6s2.69 6 6 6 6-2.69 6-6-2.69-6-6-6zm0 10c-2.21 0-4-1.79-4-4s1.79-4 4-4 4 1.79 4 4-1.79 4-4 4z"/></svg>',
    fire:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M13.5.67s.74 2.65.74 4.8c0 2.06-1.35 3.73-3.41 3.73S7.3 7.53 7.3 5.47l.03-.36C5.39 7.42 4.2 10.06 4.2 12.91 4.2 17.4 7.81 21 12.3 21s8.1-3.6 8.1-8.09c0-5.5-2.65-10.45-6.9-12.24z"/></svg>',

    // ── Devices & UI ──────────────────────────────────────────────────────
    desktop:  '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M21 16H3V4h18m0-2H3c-1.11 0-2 .89-2 2v12a2 2 0 0 0 2 2h7l-2 3v1h8v-1l-2-3h7a2 2 0 0 0 2-2V4c0-1.11-.9-2-2-2z"/></svg>',
    tablet:   '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M21 4H3a2 2 0 0 0-2 2v12c0 1.1.9 2 2 2h18a2 2 0 0 0 2-2V6c0-1.1-.9-2-2-2zm-2 14H5V6h14v12z"/></svg>',
    mobile:   '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M17 1.01 7 1c-1.1 0-2 .9-2 2v18c0 1.1.9 2 2 2h10c1.1 0 2-.9 2-2V3c0-1.1-.9-2-1.99-1.99zM17 19H7V5h10v14z"/></svg>',
    image:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"/></svg>',
    camera:   '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M9.4 10.5 12 6l2.6 4.5 4.4 1L16 16l-3-3-3 3-3-4.5zM12 4 9 9 4 10l4 4-1 5 5-3 5 3-1-5 4-4-5-1z"/></svg>',
    globe:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zm6.93 6h-2.95c-.32-1.25-.78-2.45-1.38-3.56 1.84.63 3.37 1.91 4.33 3.56zM12 4.04c.83 1.2 1.48 2.53 1.91 3.96h-3.82c.43-1.43 1.08-2.76 1.91-3.96zM4.26 14C4.1 13.36 4 12.69 4 12s.1-1.36.26-2h3.38c-.08.66-.14 1.32-.14 2 0 .68.06 1.34.14 2H4.26zm.82 2h2.95c.32 1.25.78 2.45 1.38 3.56-1.84-.63-3.37-1.9-4.33-3.56zm2.95-8H5.08c.96-1.66 2.49-2.93 4.33-3.56C8.81 5.55 8.35 6.75 8.03 8zM12 19.96c-.83-1.2-1.48-2.53-1.91-3.96h3.82c-.43 1.43-1.08 2.76-1.91 3.96zM14.34 14H9.66c-.09-.66-.16-1.32-.16-2 0-.68.07-1.35.16-2h4.68c.09.65.16 1.32.16 2 0 .68-.07 1.34-.16 2zm.25 5.56c.6-1.11 1.06-2.31 1.38-3.56h2.95a8.03 8.03 0 0 1-4.33 3.56zM16.36 14c.08-.66.14-1.32.14-2 0-.68-.06-1.34-.14-2h3.38c.16.64.26 1.31.26 2s-.1 1.36-.26 2h-3.38z"/></svg>',
    chat:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M20 2H4c-1.1 0-1.99.9-1.99 2L2 22l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-2 12H6v-2h12v2zm0-3H6V9h12v2zm0-3H6V6h12v2z"/></svg>',

    // ── People & misc ─────────────────────────────────────────────────────
    person:   '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>',
    people:   '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/></svg>',
    bot:      '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M20 9V7c0-1.1-.9-2-2-2h-3a3 3 0 0 0-6 0H6c-1.1 0-2 .9-2 2v2H2v8h2v3h16v-3h2V9h-2zM7.5 13.5a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm9 0a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zM12 4a1 1 0 1 1 0 2 1 1 0 0 1 0-2z"/></svg>',
    brain:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M21.33 12.91c.09 1.55-.62 3.04-1.89 3.95l.77 1.49c.23.45.26.98.06 1.45-.19.47-.58.84-1.06 1l-.79.25a1.69 1.69 0 0 1-1.86-.55L14.44 18c-.89-.15-1.73-.53-2.44-1.1-.5.16-1 .23-1.51.23a4.99 4.99 0 0 1-3.41-1.34c-1.12 0-2.21-.34-3.13-.97-1.13-.94-1.74-2.28-1.69-3.66-.13-1.05.5-2.04 1.5-2.34-1.07-.28-1.69-1.34-1.41-2.36.36-.84.79-1.67.79-2.51-.06-1.83 1.4-3.36 3.23-3.41h.31c.5-.06 1.02-.04 1.5.05.66-.43 1.45-.66 2.25-.65 1.18-.05 2.32.38 3.16 1.21.93-.83 2.14-1.27 3.39-1.21h.05c1.59 0 2.99.95 3.59 2.4.43.93.51 1.99.21 2.97a4.05 4.05 0 0 1 1.95 3.42c.04.32.04.65.05.97 0 .29-.02.57-.05.85.96.43 1.59 1.4 1.55 2.45z"/></svg>',
    key:      '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12.65 10C11.83 7.67 9.61 6 7 6c-3.31 0-6 2.69-6 6s2.69 6 6 6c2.61 0 4.83-1.67 5.65-4H17v4h4v-4h2v-4H12.65zM7 14c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2z"/></svg>',
    lock:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1 1.71 0 3.1 1.39 3.1 3.1v2z"/></svg>',
    palette:  '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.49 2 2 6.49 2 12s4.49 10 10 10c1.38 0 2.5-1.12 2.5-2.5 0-.61-.23-1.21-.64-1.67a.7.7 0 0 1-.16-.46.5.5 0 0 1 .5-.5H16c3.31 0 6-2.69 6-6 0-4.96-4.49-9-10-9zm5.5 11c-.83 0-1.5-.67-1.5-1.5S16.67 10 17.5 10s1.5.67 1.5 1.5-.67 1.5-1.5 1.5zM7 9.5C7 8.67 7.67 8 8.5 8s1.5.67 1.5 1.5S9.33 11 8.5 11 7 10.33 7 9.5zm5-3C12 5.67 12.67 5 13.5 5s1.5.67 1.5 1.5S14.33 8 13.5 8 12 7.33 12 6.5zm-7 7C5 12.67 5.67 12 6.5 12s1.5.67 1.5 1.5S7.33 15 6.5 15 5 14.33 5 13.5z"/></svg>',
    construction:'<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M13.78 15.3 19.61 21l1.41-1.41-5.7-5.83-1.54 1.54zm4.85-8.55-3.27-3.28 1.42-1.41 3.27 3.28-1.42 1.41zm-9.97 5.49 1.41 1.41-1.41 1.41-1.41-1.41 1.41-1.41zm6.79-2.18 1.41-1.41-1.41-1.41-1.41 1.41 1.41 1.41zM2.39 13.91l3.83-3.83 5.5 5.5-3.83 3.83a2 2 0 0 1-2.83 0l-2.66-2.66a2 2 0 0 1-.01-2.84zM10 8.5l3.5-3.5L20 11.5 16.5 15 10 8.5z"/></svg>',
    spinner:  '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 4V2A10 10 0 0 0 2 12h2a8 8 0 0 1 8-8z"><animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="1s" repeatCount="indefinite"/></path></svg>',
    box:      '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2 4 6v6c0 5.55 3.84 10.74 8 12 4.16-1.26 8-6.45 8-12V6l-8-4z"/></svg>',

    // ── БРЕНДОВЫЕ ЛОГОТИПЫ КАНАЛОВ ───────────────────────────────────────
    // Используются вместо generic-иконок (plane/people/chat/bolt). Каждая
    // логотипа в фирменном цвете и узнаваемой форме. Цвет в SVG hardcoded
    // (не наследуется от currentColor), чтобы Telegram всегда был синим
    // и т.д. независимо от контекста.

    // Telegram (paper-plane, голубой #229ED9)
    brand_telegram: '<svg width="14" height="14" viewBox="0 0 24 24"><circle cx="12" cy="12" r="12" fill="#229ED9"/><path d="M5.5 11.7c3.7-1.6 6.2-2.7 7.4-3.2 3.5-1.5 4.3-1.7 4.8-1.8.1 0 .4 0 .5.1.1.1.1.2.1.3v.4c-.4 4.4-2.1 15.2-3 20.1 0 0 0 0 0 0-.4 1.4-.7 1.9-1.2 1.9-1 .1-1.7-.7-2.7-1.4-1.5-1-2.4-1.7-3.9-2.7-1.7-1.1-.6-1.8.4-2.8.3-.3 4.7-4.3 4.8-4.7 0-.1 0-.3-.1-.4-.1-.1-.3-.1-.5 0-.2 0-3.5 2.2-9.8 6.6-.9.6-1.8.9-2.5.9-.8 0-2.4-.5-3.5-.9-1.4-.5-2.5-.7-2.4-1.5 0-.4.6-.8 1.6-1.2z" fill="#fff" transform="translate(-1 -1.5) scale(0.95)"/></svg>',

    // VK (буква V, синий #0077FF)
    brand_vk: '<svg width="14" height="14" viewBox="0 0 24 24"><rect width="24" height="24" rx="5" fill="#0077FF"/><path d="M13.34 17.5c-5.05 0-7.93-3.46-8.05-9.22h2.53c.08 4.22 1.94 6.01 3.42 6.38V8.28h2.38v3.65c1.46-.16 3-1.81 3.51-3.65h2.38a6.99 6.99 0 0 1-3.21 4.59c1.59.78 2.97 2.21 3.59 4.63h-2.62c-.49-1.6-1.91-2.84-3.65-3.04v3.04h-.28z" fill="#fff"/></svg>',

    // MAX (буква M, красный #FF1744 — VK-Group brand для мессенджера)
    brand_max: '<svg width="14" height="14" viewBox="0 0 24 24"><rect width="24" height="24" rx="5" fill="#FF1744"/><path d="M5 7v10h2.4v-6.5L10 14.5h.6L13.2 10.5V17H15.6V7h-2.4l-2.7 4.4L7.8 7H5z" fill="#fff"/></svg>',

    // Авито (зелёный #00BB00, упрощённая буква A)
    brand_avito: '<svg width="14" height="14" viewBox="0 0 24 24"><rect width="24" height="24" rx="5" fill="#00B900"/><path d="M12 6 7 18h2.4l1-2.6h3.2l1 2.6H17L12 6zm-1 7.4 1-2.7 1 2.7h-2z" fill="#fff"/></svg>',

    // Виджет (chat-bubble в наших фирменных цветах)
    brand_widget: '<svg width="14" height="14" viewBox="0 0 24 24"><rect width="24" height="24" rx="5" fill="#ff8c42"/><path d="M5 7c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2v8c0 1.1-.9 2-2 2h-7l-3 3V7z" fill="#fff"/></svg>',
  };

  // Алиасы для семантических имён, чтобы не плодить дубли
  ICONS.success_full = ICONS.success;
  ICONS.tip = ICONS.lightbulb;
  ICONS.delete = ICONS.trash;
  ICONS.publish = ICONS.upload;

  function _replace(el) {
    const name = el.dataset.i;
    const svg = ICONS[name];
    if (!svg) return;
    // inline-flex чтобы SVG корректно центрировался с текстом
    el.style.display = 'inline-flex';
    el.style.alignItems = 'center';
    el.style.verticalAlign = '-2px';
    el.innerHTML = svg;
  }

  function _replaceAll(root) {
    (root || document).querySelectorAll('[data-i]').forEach(_replace);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => _replaceAll());
  } else {
    _replaceAll();
  }

  window.ICONS = ICONS;
  // Утилита для динамически вставляемого HTML — вызывать после insertAdjacentHTML
  window.renderIcons = _replaceAll;

  // ── Глобальный лимит на размер textarea ───────────────────────────────────
  // Защита от DoS: пользователь может вставить мегабайты текста и положить
  // event-loop на сериализации/обработке. Дефолт — 50000 символов (≈100KB).
  // Переопределить per-textarea: <textarea maxlength="200000"> или data-no-limit="1".
  const _DEFAULT_TEXTAREA_MAX = 50000;
  function _ensureTextareaLimits(root) {
    (root || document).querySelectorAll('textarea').forEach(t => {
      if (!t.hasAttribute('maxlength')) {
        // Не трогаем редакторы кода (там лимит ставится отдельно)
        if (t.id === 'codeEditor' || t.dataset.noLimit === '1') return;
        t.setAttribute('maxlength', String(_DEFAULT_TEXTAREA_MAX));
      }
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => _ensureTextareaLimits());
  } else {
    _ensureTextareaLimits();
  }
  // Применять и к динамически добавленным textarea
  try {
    const _mo = new MutationObserver(muts => {
      muts.forEach(m => m.addedNodes.forEach(n => {
        if (n.nodeType === 1) _ensureTextareaLimits(n);
      }));
    });
    _mo.observe(document.documentElement, {childList: true, subtree: true});
  } catch (e) { /* no-op */ }

  // ── Global fetch shim: автоматический CSRF-token на write-методах ─────────
  // После миграции JWT в httpOnly cookie каждый write-запрос должен нести
  // X-CSRF-Token равный cookie csrf_token (double-submit pattern).
  // Cookie csrf_token НЕ httpOnly — JS читает его через document.cookie.
  // Если cookie нет (старый клиент с токеном в Authorization-header) —
  // shim ничего не делает, запросы идут как раньше.
  function _readCookie(name){
    const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : null;
  }
  const _origFetch = window.fetch.bind(window);
  window.fetch = function(input, init){
    init = init || {};
    const method = (init.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
    if (!['GET','HEAD','OPTIONS'].includes(method)){
      const csrf = _readCookie('csrf_token');
      if (csrf){
        // Не перезаписываем если caller уже задал
        const headers = new Headers(init.headers || (typeof input === 'object' ? input.headers : undefined));
        if (!headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', csrf);
        init.headers = headers;
        // credentials: 'same-origin' нужен чтобы cookie уехало на сервер
        // (FastAPI default — same-origin, но явно безопаснее)
        if (!('credentials' in init)) init.credentials = 'same-origin';
      }
    } else {
      // На GET всё равно нужно передавать cookies
      if (!('credentials' in init)) init.credentials = 'same-origin';
    }
    return _origFetch(input, init);
  };
})();
