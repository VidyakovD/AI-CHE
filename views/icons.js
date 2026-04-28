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

    // Telegram — официальный paper-plane на фирменном #26A5E4 (canonical
    // SVG path из simple-icons.org/telegram, CC0)
    brand_telegram: '<svg width="14" height="14" viewBox="0 0 24 24" fill="#26A5E4"><path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/></svg>',

    // VK — официальный лого на #0077FF (canonical SVG из simple-icons.org/vk)
    brand_vk: '<svg width="14" height="14" viewBox="0 0 24 24" fill="#0077FF"><path d="M15.07 2H8.93C3.33 2 2 3.33 2 8.93v6.14C2 20.67 3.33 22 8.93 22h6.14c5.6 0 6.93-1.33 6.93-6.93V8.93C22 3.33 20.67 2 15.07 2zm3.07 14.27h-1.45c-.55 0-.72-.44-1.71-1.43-.86-.83-1.24-.94-1.45-.94-.3 0-.39.08-.39.5v1.31c0 .36-.11.57-1.06.57-1.57 0-3.32-.95-4.55-2.73-1.85-2.59-2.36-4.54-2.36-4.94 0-.22.08-.42.5-.42h1.45c.38 0 .52.17.66.58.71 2.06 1.91 3.86 2.4 3.86.18 0 .27-.08.27-.55v-2.13c-.06-.98-.58-1.07-.58-1.42 0-.16.14-.33.36-.33h2.28c.32 0 .43.17.43.55v2.87c0 .32.14.43.23.43.18 0 .33-.11.66-.44 1.02-1.14 1.74-2.9 1.74-2.9.09-.21.26-.41.65-.41h1.45c.43 0 .53.22.43.52-.18.83-1.92 3.29-1.92 3.29-.15.24-.21.36 0 .63.15.21.65.64.99 1.04.62.7 1.09 1.29 1.22 1.69.13.4-.08.61-.49.61z"/></svg>',

    // MAX — мессенджер VK Group, фирменный градиент красно-розовый.
    // Простая стилизация: круг с буквой M (нет canonical SVG публично).
    brand_max: '<svg width="14" height="14" viewBox="0 0 24 24"><defs><linearGradient id="g_max" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#FF4858"/><stop offset="100%" stop-color="#FF1744"/></linearGradient></defs><circle cx="12" cy="12" r="11" fill="url(#g_max)"/><path d="M5.8 6.4h2.5l3.5 6 3.5-6h2.5v11.2h-2.4v-7l-3 5.1h-1.2l-3-5.1v7H5.8z" fill="#fff"/></svg>',

    // Авито — оригинальный зелёно-бирюзовый круг #04E061 с буквой A.
    // Канонической SVG нет в open libs; используем точные фирменные цвета.
    brand_avito: '<svg width="14" height="14" viewBox="0 0 24 24"><circle cx="12" cy="12" r="11" fill="#04E061"/><path d="M12 5.6 6.5 18.4h2.6l1-2.5h3.8l1 2.5h2.6L12 5.6zm-1.1 8.1 1.1-2.8 1.1 2.8h-2.2z" fill="#fff"/></svg>',

    // Виджет — chat-bubble в нашем фирменном #ff8c42
    brand_widget: '<svg width="14" height="14" viewBox="0 0 24 24"><circle cx="12" cy="12" r="11" fill="#ff8c42"/><path d="M5.5 8c0-1.1.9-2 2-2h9c1.1 0 2 .9 2 2v6c0 1.1-.9 2-2 2h-6.5l-3.5 3v-3c-.55 0-1-.45-1-1V8z" fill="#fff"/></svg>',

    // ── БРЕНДЫ AI-МОДЕЛЕЙ ──────────────────────────────────────────────
    // Узнаваемые лого каждого AI-провайдера в их фирменных цветах.
    // Рендерятся в селекторе модели на главной + аватаре сообщения от AI.

    // OpenAI — официальный «цветок-узел» в фирменном #10a37f
    // (canonical SVG path из simple-icons.org/openai, CC0)
    brand_openai: '<svg width="14" height="14" viewBox="0 0 24 24" fill="#10a37f"><path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z"/></svg>',

    // Anthropic Claude — официальный оранжевый «*» в #D97757
    // (canonical SVG path из simple-icons.org/anthropic, CC0)
    brand_claude: '<svg width="14" height="14" viewBox="0 0 24 24" fill="#D97757"><path d="M17.3041 3.541h-3.6718l6.696 16.918H24Zm-10.6082 0L0 20.459h3.7442l1.3693-3.5527h7.0052l1.3693 3.5527h3.7442L10.5363 3.541ZM6.0843 13.7338l2.4983-6.5063 2.4983 6.5063z"/></svg>',

    // Google Gemini — официальный 4-pointed sparkle с radial gradient
    // (фирменные цвета Google: фиолетовый → голубой → бирюзовый)
    brand_gemini: '<svg width="14" height="14" viewBox="0 0 16 16"><defs><radialGradient id="g_gem" cx="50%" cy="50%" r="80%" fx="20%" fy="20%"><stop offset="0%" stop-color="#9168C0"/><stop offset="40%" stop-color="#5684D1"/><stop offset="100%" stop-color="#1BA1E3"/></radialGradient></defs><path d="M16 8.016A8.522 8.522 0 0 0 8.016 16h-.032A8.521 8.521 0 0 0 0 8.016v-.032A8.521 8.521 0 0 0 7.984 0h.032A8.522 8.522 0 0 0 16 7.984v.032z" fill="url(#g_gem)"/></svg>',

    // xAI Grok — официальный X (логотип xAI / Twitter X)
    // (canonical SVG path из simple-icons.org/x, CC0)
    brand_grok: '<svg width="14" height="14" viewBox="0 0 24 24" fill="#000"><path d="M18.901 1.153h3.68l-8.04 9.19L24 22.846h-7.406l-5.8-7.584-6.638 7.584H.474l8.6-9.83L0 1.154h7.594l5.243 6.932Zm-1.61 19.494h2.039L6.486 3.24H4.298Z"/></svg>',

    // Perplexity — официальный logo (asterisk-стиль) в #1FB8CD
    // (canonical SVG path из simple-icons.org/perplexity, CC0)
    brand_perplexity: '<svg width="14" height="14" viewBox="0 0 24 24" fill="#1FB8CD"><path d="M22.3977 7.0896h-2.3106V.6224l-7.4477 6.4672V.7156h-1.4153v6.3739L4.224.6224v6.4672H1.6028C.7172 7.0896 0 7.8068 0 8.6924v6.6151c0 .8856.7172 1.6028 1.6028 1.6028h2.6212v6.467l7.0193-6.0285v6.0285h1.4153v-6.0285l7.0193 6.0285v-6.467h2.7396c.8857 0 1.6028-.7173 1.6028-1.6029V8.6924c.0024-.8856-.7148-1.6028-1.6004-1.6028zm-2.3106 8.7172h-2.6151c-.8857 0-1.6029.7172-1.6029 1.6028v3.358l-5.604-4.8138v-7.0708l9.8204-8.5385v15.4623zm-9.8228 0v4.6985l-5.6076-4.81-.012.0012V8.5042l5.6196 4.825v2.4776zM2.4197 13.4805 8.4385 8.504H2.4197v4.9765zm15.0488-4.9777h-1.6796 5.4885v4.971l-3.8089-3.252v-1.719z"/></svg>',

    // Google Imagen / Nano Banana — жёлто-фиолетовый кружок (Google brand)
    brand_imagen: '<svg width="14" height="14" viewBox="0 0 24 24"><defs><linearGradient id="g_img" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#FBBC04"/><stop offset="100%" stop-color="#9333EA"/></linearGradient></defs><circle cx="12" cy="12" r="10" fill="url(#g_img)"/><path d="M8 8h8v8H8V8zm2 2v4h4v-4h-4z" fill="#fff"/></svg>',

    // Google Veo (видео) — красно-фиолетовый круг с play
    brand_veo: '<svg width="14" height="14" viewBox="0 0 24 24"><defs><linearGradient id="g_veo" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#EA4335"/><stop offset="100%" stop-color="#9333EA"/></linearGradient></defs><circle cx="12" cy="12" r="10" fill="url(#g_veo)"/><path d="M9 7l8 5-8 5V7z" fill="#fff"/></svg>',

    // Kling (видео) — китайский AI, фиолетово-синий
    brand_kling: '<svg width="14" height="14" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" fill="#7C3AED"/><path d="M9 7v10l6-5-6-5z" fill="#fff"/></svg>',
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

  // ── HTML-escape: единственный источник истины для всех views.
  // Используем при подстановке user-/API-данных в .innerHTML / template literals.
  // На страницах могут быть локальные `escHtml/esc` — оставляем (idempotent).
  if (!window.escHtml) {
    window.escHtml = function (s) {
      return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[c]));
    };
  }
  if (!window.esc) window.esc = window.escHtml;
  if (!window.escAttr) window.escAttr = window.escHtml;

  // Маппинг model_id (или подстроки в id) → SVG бренда AI-провайдера.
  // Используется в селекторе модели на главной + аватаре сообщения.
  window.getModelBrandIcon = function(modelId) {
    if (!modelId) return ICONS.brand_openai;
    const id = String(modelId).toLowerCase();
    if (id.includes('claude'))     return ICONS.brand_claude;
    if (id.includes('gemini'))     return ICONS.brand_gemini;
    if (id.includes('grok'))       return ICONS.brand_grok;
    if (id.includes('perplex'))    return ICONS.brand_perplexity;
    if (id.includes('veo'))        return ICONS.brand_veo;
    if (id.includes('kling'))      return ICONS.brand_kling;
    if (id === 'nano' || id.includes('imagen') || id.includes('banana')) return ICONS.brand_imagen;
    // gpt / gpt-4o / gpt-image / dalle / openai
    return ICONS.brand_openai;
  };

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

  // ── PWA boot: регистрация SW + manifest + install-prompt ─────────────────
  // Делает страницу «устанавливаемой как приложение» на iOS/Android/Windows/Mac.
  // Вставляем <link rel="manifest"> и <meta theme-color> программно — чтобы
  // не плодить копипасту в каждом HTML.
  function _ensurePwaTags(){
    if (document.querySelector('link[rel="manifest"]')) return;
    var link = document.createElement('link');
    link.rel = 'manifest'; link.href = '/manifest.json';
    document.head.appendChild(link);
    var theme = document.createElement('meta');
    theme.name = 'theme-color'; theme.content = '#ff8c42';
    document.head.appendChild(theme);
    // iOS-специфичные теги для «На экран Домой» — PNG (Safari не принимает SVG)
    var apple = document.createElement('link');
    apple.rel = 'apple-touch-icon'; apple.href = '/logo-192.png';
    document.head.appendChild(apple);
    var apple512 = document.createElement('link');
    apple512.rel = 'apple-touch-icon'; apple512.sizes = '512x512';
    apple512.href = '/logo-512.png';
    document.head.appendChild(apple512);
    var capable = document.createElement('meta');
    capable.name = 'apple-mobile-web-app-capable'; capable.content = 'yes';
    document.head.appendChild(capable);
    var status = document.createElement('meta');
    status.name = 'apple-mobile-web-app-status-bar-style'; status.content = 'black-translucent';
    document.head.appendChild(status);
    var titleApp = document.createElement('meta');
    titleApp.name = 'apple-mobile-web-app-title'; titleApp.content = 'AI Че';
    document.head.appendChild(titleApp);
  }
  _ensurePwaTags();

  // SW регистрируем на /sw.js — контролирует всё приложение (scope=/)
  if ('serviceWorker' in navigator && location.protocol === 'https:') {
    window.addEventListener('load', function(){
      navigator.serviceWorker.register('/sw.js', {scope: '/'})
        .catch(function(){ /* offline — не критично, регистрируется при следующем заходе */ });
    });
  }

  // Перехват install-prompt (Chrome/Edge desktop+Android). Откладываем
  // показ — даём юзеру возможность поставить через нашу кнопку.
  var _deferredInstall = null;
  window.addEventListener('beforeinstallprompt', function(e){
    e.preventDefault();
    _deferredInstall = e;
    _markInstallable();
  });
  window.addEventListener('appinstalled', function(){
    _deferredInstall = null;
    _markInstallable(false);
  });

  function _isStandalone(){
    return (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) ||
           window.navigator.standalone === true;  // iOS
  }

  function _platform(){
    var ua = (navigator.userAgent || '').toLowerCase();
    if (/iphone|ipad|ipod/.test(ua)) return 'ios';
    if (/android/.test(ua)) return 'android';
    if (/macintosh|mac os/.test(ua)) return 'mac';
    if (/windows/.test(ua)) return 'windows';
    return 'other';
  }

  function _markInstallable(can){
    // Внешним кодом можно проверить через window.aiCanInstall()
    window.__aiInstallable = (can !== false);
  }
  window.aiCanInstall = function(){
    return !!_deferredInstall || !_isStandalone();
  };
  window.aiIsInstalled = _isStandalone;

  // Показывает модалку с инструкцией «как установить» — учитывает платформу.
  // Возвращает Promise (resolve когда юзер закрыл/установил).
  window.aiShowInstall = async function(){
    if (_isStandalone()){
      if (window.aiAlert) await window.aiAlert('Приложение уже установлено и запущено в этом режиме.', 'success');
      return;
    }
    // Native install-prompt доступен — используем его (лучший UX)
    if (_deferredInstall){
      try {
        _deferredInstall.prompt();
        var choice = await _deferredInstall.userChoice;
        _deferredInstall = null;
        if (choice && choice.outcome === 'accepted'){
          if (window.aiAlert) await window.aiAlert('Готово! Приложение появилось на рабочем столе.', 'success');
        }
        return;
      } catch(e){ /* fallthrough */ }
    }
    // Fallback: модалка с инструкцией под платформу
    var p = _platform();
    var inst = '';
    if (p === 'ios'){
      inst = 'На iPhone/iPad:\n1. Нажми кнопку «Поделиться» внизу экрана (квадрат со стрелкой ↑)\n2. Прокрути и выбери «На экран Домой»\n3. Нажми «Добавить» — иконка появится как у обычного приложения';
    } else if (p === 'android'){
      inst = 'На Android:\n1. Открой меню браузера (три точки сверху ⋮)\n2. Выбери «Установить приложение» или «Добавить на главный экран»\n3. Подтверди — иконка появится в списке приложений';
    } else if (p === 'mac'){
      inst = 'На Mac (Chrome / Edge):\n1. Открой меню браузера (⋮ или •••)\n2. Найди пункт «Установить AI Студия Че…»\n3. Подтверди — приложение появится в Applications\n\nВ Safari пока без установки — добавь в закладки.';
    } else if (p === 'windows'){
      inst = 'На Windows (Chrome / Edge):\n1. Нажми на иконку «+» в адресной строке справа\n2. Или открой меню (⋮) → «Установить AI Студия Че»\n3. Иконка появится на рабочем столе и в меню Пуск';
    } else {
      inst = 'Открой меню браузера и найди пункт «Установить» / «Добавить на главный экран». Если такого пункта нет — браузер не поддерживает PWA, попробуй Chrome или Edge.';
    }
    if (window.aiAlert) await window.aiAlert(inst, 'info');
  };

  // ── Custom modals: confirm/alert/prompt в стиле «Че» ──────────────────────
  // Заменяет браузерные diaогли (которые выглядят как window.alert) на
  // фирменные модалки с темным фоном, оранжевыми кнопками. Возвращают
  // Promise — вместо синхронного confirm() пиши: `if(!await aiConfirm(...))`.
  //
  // Использование:
  //   await aiAlert('Готово!', 'success');         // info | success | error | warn
  //   if(!await aiConfirm('Удалить?')) return;
  //   const v = await aiPrompt('Имя?', 'Default'); // null если отмена
  //
  // Стиль наследуется со страницы (Tailwind primary/surface/outline).
  // Если страница не использует Tailwind — есть инлайновые цвета.

  function _ensureModalRoot(){
    let r = document.getElementById('__aiModalRoot');
    if (r) return r;
    r = document.createElement('div');
    r.id = '__aiModalRoot';
    document.body.appendChild(r);
    // Базовые стили (если на странице не подгружен Tailwind)
    if (!document.getElementById('__aiModalCss')){
      const s = document.createElement('style');
      s.id = '__aiModalCss';
      s.textContent = `
        .__ai_mw{position:fixed;inset:0;background:rgba(0,0,0,0.72);backdrop-filter:blur(6px);
          z-index:99999;display:flex;align-items:center;justify-content:center;padding:20px;
          font-family:Inter,'Segoe UI',sans-serif;animation:__ai_fade .15s ease-out}
        @keyframes __ai_fade{from{opacity:0}to{opacity:1}}
        @keyframes __ai_slide{from{opacity:0;transform:translateY(-12px)}to{opacity:1;transform:translateY(0)}}
        .__ai_mb{background:#1e1a14;border:1.5px solid rgba(255,140,66,0.25);border-radius:14px;
          max-width:440px;width:100%;color:#f0e6d8;box-shadow:0 12px 48px rgba(0,0,0,0.5);
          animation:__ai_slide .18s ease-out}
        .__ai_mh{display:flex;align-items:center;gap:10px;padding:18px 22px 12px;
          font-family:Manrope,Inter,sans-serif;font-weight:700;font-size:15px}
        .__ai_mh .__ai_ic{width:28px;height:28px;border-radius:8px;display:flex;
          align-items:center;justify-content:center;flex-shrink:0;font-size:15px}
        .__ai_mh.info .__ai_ic{background:rgba(99,102,241,0.15);color:#a5b4fc}
        .__ai_mh.success .__ai_ic{background:rgba(34,197,94,0.15);color:#86efac}
        .__ai_mh.error .__ai_ic{background:rgba(255,107,107,0.15);color:#ff6b6b}
        .__ai_mh.warn .__ai_ic{background:rgba(255,140,66,0.18);color:#ff8c42}
        .__ai_mh.q .__ai_ic{background:linear-gradient(135deg,#ff8c42,#ffb347);color:#141210}
        .__ai_mt{padding:4px 22px 18px;font-size:13.5px;line-height:1.55;color:#d4c8b0;
          white-space:pre-wrap;word-break:break-word}
        .__ai_mi{margin:0 22px 16px;width:calc(100% - 44px);padding:9px 13px;border-radius:9px;
          background:#272018;color:#f0e6d8;border:1px solid rgba(74,63,47,0.5);outline:none;
          font-size:13px;font-family:inherit}
        .__ai_mi:focus{border-color:#ff8c42}
        .__ai_mf{display:flex;justify-content:flex-end;gap:8px;padding:0 18px 18px}
        .__ai_btn{padding:9px 18px;border-radius:9px;font-size:13px;font-weight:600;
          cursor:pointer;border:none;transition:opacity .15s,transform .1s;font-family:inherit}
        .__ai_btn:active{transform:translateY(1px)}
        .__ai_btn.cancel{background:#272018;color:#a89880;border:1px solid rgba(74,63,47,0.5)}
        .__ai_btn.cancel:hover{color:#f0e6d8;border-color:#ff8c42}
        .__ai_btn.ok{background:linear-gradient(135deg,#ff8c42,#ffb347);color:#141210}
        .__ai_btn.ok:hover{opacity:.9}
        .__ai_btn.danger{background:rgba(255,107,107,0.15);color:#ff6b6b;
          border:1px solid rgba(255,107,107,0.35)}
        .__ai_btn.danger:hover{background:rgba(255,107,107,0.28)}
      `;
      document.head.appendChild(s);
    }
    return r;
  }

  function _showModal({type, title, message, withInput, defaultInput, okLabel, cancelLabel, danger}){
    return new Promise(resolve => {
      const root = _ensureModalRoot();
      const wrap = document.createElement('div');
      wrap.className = '__ai_mw';
      const iconMap = {info:'ⓘ', success:'✓', error:'✕', warn:'!', q:'?'};
      const iconChar = iconMap[type] || 'ⓘ';
      const titleSafe = String(title || '').replace(/[<>&]/g, c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
      const msgSafe = String(message || '').replace(/[<>&]/g, c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
      const inputHtml = withInput
        ? `<input class="__ai_mi" type="text" value="${String(defaultInput||'').replace(/"/g,'&quot;')}"/>`
        : '';
      const cancelHtml = (okLabel === null) ? '' :
        `<button class="__ai_btn cancel">${cancelLabel || 'Отмена'}</button>`;
      const okClass = danger ? 'danger' : 'ok';
      wrap.innerHTML =
        `<div class="__ai_mb" role="dialog" aria-modal="true">
          <div class="__ai_mh ${type||'info'}"><span class="__ai_ic">${iconChar}</span><span>${titleSafe}</span></div>
          ${msgSafe ? `<div class="__ai_mt">${msgSafe}</div>` : ''}
          ${inputHtml}
          <div class="__ai_mf">
            ${cancelHtml}
            <button class="__ai_btn ${okClass}">${okLabel || 'OK'}</button>
          </div>
        </div>`;
      root.appendChild(wrap);
      const inputEl = wrap.querySelector('.__ai_mi');
      const okBtn = wrap.querySelector('.__ai_btn.' + okClass);
      const cancelBtn = wrap.querySelector('.__ai_btn.cancel');

      function close(val){
        wrap.remove();
        document.removeEventListener('keydown', onKey);
        resolve(val);
      }
      function onKey(e){
        if (e.key === 'Escape'){ close(withInput ? null : false); }
        else if (e.key === 'Enter' && (!inputEl || document.activeElement === inputEl)){
          close(withInput ? (inputEl.value) : true);
        }
      }
      okBtn.addEventListener('click', () => close(withInput ? (inputEl ? inputEl.value : true) : true));
      if (cancelBtn) cancelBtn.addEventListener('click', () => close(withInput ? null : false));
      // Клик вне окна = отмена
      wrap.addEventListener('click', e => { if (e.target === wrap) close(withInput ? null : false); });
      document.addEventListener('keydown', onKey);
      setTimeout(() => { (inputEl || okBtn).focus(); }, 30);
    });
  }

  window.aiConfirm = function(message, opts){
    opts = opts || {};
    return _showModal({
      type: opts.type || 'q',
      title: opts.title || 'Подтвердите действие',
      message: message,
      withInput: false,
      okLabel: opts.okLabel || 'Подтвердить',
      cancelLabel: opts.cancelLabel || 'Отмена',
      danger: !!opts.danger,
    });
  };

  window.aiAlert = function(message, type){
    return _showModal({
      type: type || 'info',
      title: ({success:'Готово', error:'Ошибка', warn:'Внимание', info:'Уведомление'})[type] || 'Уведомление',
      message: message,
      withInput: false,
      okLabel: 'OK',
      cancelLabel: null,
    }).then(() => undefined);
  };

  window.aiPrompt = function(message, defaultValue, opts){
    opts = opts || {};
    return _showModal({
      type: opts.type || 'q',
      title: opts.title || 'Введите значение',
      message: message,
      withInput: true,
      defaultInput: defaultValue || '',
      okLabel: opts.okLabel || 'OK',
      cancelLabel: opts.cancelLabel || 'Отмена',
    });
  };

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

  // ── Контекстный помощник по разделам ────────────────────────────────────
  // Плавающая кнопка-bubble в правом нижнем углу. Клик → открывает чат-панель.
  // Секция определяется атрибутом <body data-assistant-section="proposals.projects">.
  // Если атрибут не задан — помощник не подключается. Это позволяет точечно
  // включать его на нужных страницах (например, не в /terms.html).
  function _initAssistant() {
    if (!document.body) return;
    const section = document.body.getAttribute('data-assistant-section');
    if (!section) return;
    if (document.getElementById('ai-assistant-root')) return;

    // ── Стили ──
    const css = `
#ai-assistant-root{position:fixed;right:18px;bottom:18px;z-index:99998;font:13px/1.45 system-ui,-apple-system,sans-serif;color:#1a1a1a}
#ai-assistant-bubble{width:56px;height:56px;border-radius:50%;border:none;cursor:pointer;background:#1C1C1C;color:#fff;box-shadow:0 6px 22px rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;transition:transform .15s;padding:0;overflow:hidden;border:2px solid rgba(255,140,66,.55)}
#ai-assistant-bubble:hover{transform:scale(1.07);border-color:#ff8c42}
#ai-assistant-bubble img{width:42px;height:42px;object-fit:contain;display:block}
#ai-assistant-panel{position:absolute;right:0;bottom:64px;width:360px;max-width:calc(100vw - 24px);height:520px;max-height:calc(100vh - 96px);min-width:280px;min-height:340px;background:#fff;border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.25);display:none;flex-direction:column;overflow:hidden;border:1px solid rgba(0,0,0,.08)}
#ai-assistant-panel.open{display:flex}
#ai-assistant-panel.dragging,#ai-assistant-panel.resizing{transition:none;user-select:none;opacity:.95}
#ai-assistant-panel.detached{position:fixed;right:auto;bottom:auto}
#ai-assistant-resize{position:absolute;right:0;bottom:0;width:18px;height:18px;cursor:nwse-resize;z-index:5;background:linear-gradient(135deg,transparent 0%,transparent 50%,rgba(255,140,66,.4) 50%,rgba(255,140,66,.4) 60%,transparent 60%,transparent 70%,rgba(255,140,66,.4) 70%,rgba(255,140,66,.4) 80%,transparent 80%);border-bottom-right-radius:16px;touch-action:none}
#ai-assistant-resize:hover{background:linear-gradient(135deg,transparent 0%,transparent 50%,#ff8c42 50%,#ff8c42 60%,transparent 60%,transparent 70%,#ff8c42 70%,#ff8c42 80%,transparent 80%)}
#ai-assistant-hdr{padding:10px 14px;background:#1C1C1C;color:#fff;display:flex;align-items:center;justify-content:space-between;font-weight:600;cursor:grab;touch-action:none;user-select:none;border-bottom:1px solid rgba(255,140,66,.2)}
#ai-assistant-hdr-logo{width:28px;height:28px;border-radius:50%;background:#0f0f0f;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-right:8px}
#ai-assistant-hdr-logo img{width:22px;height:22px;object-fit:contain}
#ai-assistant-hdr:active{cursor:grabbing}
#ai-assistant-hdr small{display:block;font-weight:400;opacity:.85;font-size:11px;margin-top:2px}
#ai-assistant-hdr-grip{display:inline-flex;flex-direction:column;gap:2px;margin-right:8px;opacity:.7}
#ai-assistant-hdr-grip span{display:block;width:14px;height:2px;background:#fff;border-radius:1px}
#ai-assistant-close{background:transparent;border:none;color:#fff;font-size:18px;cursor:pointer;padding:4px 6px;line-height:1}
#ai-assistant-reset{background:transparent;border:none;color:#fff;cursor:pointer;padding:4px 6px;line-height:1;font-size:12px;opacity:.85;margin-right:2px}
#ai-assistant-reset:hover{opacity:1}
#ai-assistant-msgs{flex:1;overflow-y:auto;padding:12px 14px;background:#fafafa}
.ai-msg{margin-bottom:10px;max-width:88%;padding:8px 11px;border-radius:12px;word-wrap:break-word}
.ai-msg.user{background:#FFB300;color:#fff;margin-left:auto;border-bottom-right-radius:4px}
.ai-msg.bot{background:#fff;color:#1a1a1a;border:1px solid #eee;border-bottom-left-radius:4px}
.ai-msg.bot a{color:#FF6F00;text-decoration:underline}
.ai-msg .lnks{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px}
.ai-msg .lnks a{display:inline-flex;align-items:center;gap:5px;padding:7px 11px;background:linear-gradient(135deg,#FFB300,#FF6F00);border:none;border-radius:10px;font-size:12px;font-weight:600;text-decoration:none;color:#fff;box-shadow:0 2px 6px rgba(255,140,66,.25);transition:transform .12s,box-shadow .12s}
.ai-msg .lnks a:hover{transform:translateY(-1px);box-shadow:0 4px 10px rgba(255,140,66,.35)}
.ai-msg .lnks a:active{transform:translateY(0)}
.ai-msg .lnks a::after{content:"→";font-weight:700;opacity:.9;margin-left:1px}
.ai-msg.err{background:#fff1f1;color:#9b1a1a;border:1px solid #fecaca}
.ai-msg.thinking{font-style:italic;color:#888}
#ai-assistant-inp-wrap{padding:10px;background:#fff;border-top:1px solid #eee;display:flex;gap:6px}
#ai-assistant-inp{flex:1;border:1px solid #ddd;border-radius:10px;padding:9px 11px;font:inherit;outline:none;resize:none;max-height:90px;min-height:38px}
#ai-assistant-inp:focus{border-color:#FFB300}
#ai-assistant-send{border:none;background:#FFB300;color:#fff;padding:0 14px;border-radius:10px;cursor:pointer;font-weight:600}
#ai-assistant-send:disabled{opacity:.5;cursor:not-allowed}
@media (prefers-color-scheme:dark){
  #ai-assistant-panel{background:#1c1815;border-color:rgba(255,255,255,.1);color:#eee}
  #ai-assistant-msgs{background:#15110e}
  .ai-msg.bot{background:#231e1a;color:#eee;border-color:#332b25}
  #ai-assistant-inp-wrap{background:#1c1815;border-color:#332b25}
  #ai-assistant-inp{background:#231e1a;color:#eee;border-color:#332b25}
}
`;
    const styleEl = document.createElement('style');
    styleEl.textContent = css;
    document.head.appendChild(styleEl);

    // ── Markup ──
    const root = document.createElement('div');
    root.id = 'ai-assistant-root';
    root.innerHTML = `
<div id="ai-assistant-panel" role="dialog" aria-label="AI-помощник">
  <div id="ai-assistant-hdr" title="Перетащите за этот заголовок, чтобы переместить окно">
    <div style="display:flex;align-items:center;min-width:0;flex:1">
      <span id="ai-assistant-hdr-grip" aria-hidden="true"><span></span><span></span><span></span></span>
      <span id="ai-assistant-hdr-logo" aria-hidden="true"><img src="/logo-192.png" alt=""/></span>
      <div style="min-width:0">AI-помощник<small id="ai-assistant-section-label"></small></div>
    </div>
    <div style="display:flex;align-items:center;flex-shrink:0">
      <button id="ai-assistant-reset" type="button" title="Вернуть окно в исходное место" aria-label="Сбросить позицию">⤓</button>
      <button id="ai-assistant-close" type="button" aria-label="Закрыть">×</button>
    </div>
  </div>
  <div id="ai-assistant-msgs" role="log" aria-live="polite"></div>
  <div id="ai-assistant-inp-wrap">
    <textarea id="ai-assistant-inp" rows="1" placeholder="Спросите про этот раздел…" maxlength="600"></textarea>
    <button id="ai-assistant-send" disabled>↑</button>
  </div>
  <div id="ai-assistant-resize" title="Потяните, чтобы изменить размер" aria-label="Изменить размер"></div>
</div>
<button id="ai-assistant-bubble" type="button" aria-label="Открыть AI-помощника">
  <img src="/logo-192.png" alt="AI Студия Че" width="42" height="42"/>
</button>
`;
    document.body.appendChild(root);

    const panel = root.querySelector('#ai-assistant-panel');
    const bubble = root.querySelector('#ai-assistant-bubble');
    const closeBtn = root.querySelector('#ai-assistant-close');
    const resetBtn = root.querySelector('#ai-assistant-reset');
    const hdr = root.querySelector('#ai-assistant-hdr');
    const msgs = root.querySelector('#ai-assistant-msgs');
    const inp = root.querySelector('#ai-assistant-inp');
    const sendBtn = root.querySelector('#ai-assistant-send');
    const sectionLabel = root.querySelector('#ai-assistant-section-label');

    // Подпись секции — берём из data-assistant-label или из секции id.
    const niceLabel = document.body.getAttribute('data-assistant-label')
      || section.replace(/[._]/g, ' › ');
    sectionLabel.textContent = niceLabel;

    // История разговора в sessionStorage (на одну вкладку, не БД).
    const STORAGE_KEY = 'ai-assistant:' + section;
    let history = [];
    try {
      const saved = sessionStorage.getItem(STORAGE_KEY);
      if (saved) history = JSON.parse(saved) || [];
    } catch (_) { history = []; }

    function _saveHistory() {
      try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(history.slice(-20))); }
      catch (_) {}
    }

    function _renderMsg(m) {
      const div = document.createElement('div');
      div.className = 'ai-msg ' + (m.role || 'bot') + (m.kind === 'err' ? ' err' : '');
      // Текст: безопасно, через textContent (никакого innerHTML с user-данными)
      const txt = document.createElement('div');
      txt.textContent = m.text || '';
      div.appendChild(txt);
      // Ссылки от бота — рендерим кнопками
      if (m.role === 'bot' && Array.isArray(m.links) && m.links.length) {
        const lnks = document.createElement('div');
        lnks.className = 'lnks';
        m.links.forEach(l => {
          const a = document.createElement('a');
          a.textContent = l.label || l.href;
          a.href = l.href;
          // Если ссылка ведёт на хэш текущей страницы — не открываем в новой вкладке
          if (!l.href.startsWith('#')) a.target = '_self';
          lnks.appendChild(a);
        });
        div.appendChild(lnks);
      }
      msgs.appendChild(div);
      msgs.scrollTop = msgs.scrollHeight;
      return div;
    }

    function _initialGreeting() {
      if (history.length) {
        history.forEach(_renderMsg);
        return;
      }
      _renderMsg({
        role: 'bot',
        text: 'Привет! Я подскажу по этому разделу. Спросите что-нибудь — например, как создать или что значит та или иная кнопка.',
      });
    }

    // ── Drag + resize окна помощника ──────────────────────────────────────
    // Позиция и размер помнятся между сессиями через localStorage. Ключи —
    // на origin (не section'у), чтобы окно было «там же» на любой странице.
    const POS_KEY = 'ai-assistant:pos';
    const SIZE_KEY = 'ai-assistant:size';

    const MIN_W = 280, MIN_H = 340;
    const _maxW = () => window.innerWidth - 8;
    const _maxH = () => window.innerHeight - 8;

    function _applySize(w, h) {
      const cw = Math.min(_maxW(), Math.max(MIN_W, w));
      const ch = Math.min(_maxH(), Math.max(MIN_H, h));
      panel.style.width = cw + 'px';
      panel.style.height = ch + 'px';
    }

    function _loadSavedSize() {
      try {
        const raw = localStorage.getItem(SIZE_KEY);
        if (!raw) return null;
        const s = JSON.parse(raw);
        if (typeof s.w === 'number' && typeof s.h === 'number') return s;
      } catch (_) {}
      return null;
    }

    function _clampPos(left, top) {
      const w = panel.offsetWidth || 360;
      const h = panel.offsetHeight || 520;
      const maxLeft = Math.max(0, window.innerWidth - w - 4);
      const maxTop = Math.max(0, window.innerHeight - h - 4);
      return {
        left: Math.min(maxLeft, Math.max(4, left)),
        top: Math.min(maxTop, Math.max(4, top)),
      };
    }

    function _applyPos(left, top) {
      const c = _clampPos(left, top);
      panel.classList.add('detached');
      panel.style.left = c.left + 'px';
      panel.style.top = c.top + 'px';
      panel.style.right = 'auto';
      panel.style.bottom = 'auto';
    }

    function _resetPos() {
      panel.classList.remove('detached');
      panel.style.left = '';
      panel.style.top = '';
      panel.style.right = '';
      panel.style.bottom = '';
      panel.style.width = '';
      panel.style.height = '';
      try {
        localStorage.removeItem(POS_KEY);
        localStorage.removeItem(SIZE_KEY);
      } catch (_) {}
    }

    function _loadSavedPos() {
      try {
        const raw = localStorage.getItem(POS_KEY);
        if (!raw) return null;
        const p = JSON.parse(raw);
        if (typeof p.left === 'number' && typeof p.top === 'number') return p;
      } catch (_) {}
      return null;
    }

    function _toggle(open) {
      if (open == null) open = !panel.classList.contains('open');
      panel.classList.toggle('open', open);
      if (open) {
        // Применяем сохранённую позицию и размер при каждом открытии —
        // viewport мог измениться, заодно clamp по новым размерам.
        const savedSize = _loadSavedSize();
        if (savedSize) _applySize(savedSize.w, savedSize.h);
        const saved = _loadSavedPos();
        if (saved) _applyPos(saved.left, saved.top);
        if (!msgs.children.length) _initialGreeting();
        setTimeout(() => inp.focus(), 50);
      }
    }

    bubble.addEventListener('click', () => _toggle());
    closeBtn.addEventListener('click', () => _toggle(false));
    resetBtn.addEventListener('click', () => {
      _resetPos();
      // Чтобы сразу применить дефолтное (привязка к bubble'у) — toggle off+on
      const wasOpen = panel.classList.contains('open');
      if (wasOpen) { panel.classList.remove('open'); panel.classList.add('open'); }
    });

    // Drag: pointer events работают и с мышью, и с тач-экранами.
    // Игнорируем клики по close/reset кнопкам — у них своя логика.
    let _drag = null;  // {pointerId, startX, startY, baseLeft, baseTop}
    hdr.addEventListener('pointerdown', (e) => {
      if (e.button !== undefined && e.button !== 0) return;
      const t = e.target;
      if (t && t.closest && (t.closest('#ai-assistant-close') || t.closest('#ai-assistant-reset'))) return;
      // Текущая позиция — берём из getBoundingClientRect (правильно даже когда
      // panel ещё в режиме absolute right:0/bottom:64px относительно root).
      const rect = panel.getBoundingClientRect();
      _drag = {
        pointerId: e.pointerId,
        startX: e.clientX,
        startY: e.clientY,
        baseLeft: rect.left,
        baseTop: rect.top,
      };
      // Сразу переключаем в fixed-режим, чтобы дальше двигать по координатам viewport.
      _applyPos(rect.left, rect.top);
      panel.classList.add('dragging');
      try { hdr.setPointerCapture(e.pointerId); } catch (_) {}
      e.preventDefault();
    });

    hdr.addEventListener('pointermove', (e) => {
      if (!_drag || e.pointerId !== _drag.pointerId) return;
      const dx = e.clientX - _drag.startX;
      const dy = e.clientY - _drag.startY;
      _applyPos(_drag.baseLeft + dx, _drag.baseTop + dy);
    });

    function _endDrag(e) {
      if (!_drag) return;
      if (e && e.pointerId !== _drag.pointerId) return;
      _drag = null;
      panel.classList.remove('dragging');
      try { hdr.releasePointerCapture(e.pointerId); } catch (_) {}
      // Сохраняем итоговую позицию
      const rect = panel.getBoundingClientRect();
      try {
        localStorage.setItem(POS_KEY, JSON.stringify({
          left: Math.round(rect.left), top: Math.round(rect.top),
        }));
      } catch (_) {}
    }
    hdr.addEventListener('pointerup', _endDrag);
    hdr.addEventListener('pointercancel', _endDrag);

    // На ресайз окна — clamp текущей позиции, чтобы окно не уехало за экран.
    window.addEventListener('resize', () => {
      if (!panel.classList.contains('detached')) return;
      const rect = panel.getBoundingClientRect();
      _applyPos(rect.left, rect.top);
      try {
        localStorage.setItem(POS_KEY, JSON.stringify({
          left: Math.round(parseFloat(panel.style.left) || 0),
          top: Math.round(parseFloat(panel.style.top) || 0),
        }));
      } catch (_) {}
    });

    // ── Resize: ручка в правом нижнем углу ────────────────────────────────
    const resizeHandle = root.querySelector('#ai-assistant-resize');
    let _rs = null;  // {pointerId, startX, startY, baseW, baseH}
    if (resizeHandle) {
      resizeHandle.addEventListener('pointerdown', (e) => {
        if (e.button !== undefined && e.button !== 0) return;
        e.stopPropagation();  // не путаем с drag header
        e.preventDefault();
        const rect = panel.getBoundingClientRect();
        _rs = {
          pointerId: e.pointerId,
          startX: e.clientX, startY: e.clientY,
          baseW: rect.width, baseH: rect.height,
        };
        panel.classList.add('resizing');
        try { resizeHandle.setPointerCapture(e.pointerId); } catch (_) {}
      });
      resizeHandle.addEventListener('pointermove', (e) => {
        if (!_rs || e.pointerId !== _rs.pointerId) return;
        const dw = e.clientX - _rs.startX;
        const dh = e.clientY - _rs.startY;
        _applySize(_rs.baseW + dw, _rs.baseH + dh);
      });
      function _endResize(e) {
        if (!_rs) return;
        if (e && e.pointerId !== _rs.pointerId) return;
        _rs = null;
        panel.classList.remove('resizing');
        try { resizeHandle.releasePointerCapture(e.pointerId); } catch (_) {}
        const rect = panel.getBoundingClientRect();
        try {
          localStorage.setItem(SIZE_KEY, JSON.stringify({
            w: Math.round(rect.width), h: Math.round(rect.height),
          }));
        } catch (_) {}
      }
      resizeHandle.addEventListener('pointerup', _endResize);
      resizeHandle.addEventListener('pointercancel', _endResize);
    }

    inp.addEventListener('input', () => {
      sendBtn.disabled = !inp.value.trim();
      // auto-grow
      inp.style.height = 'auto';
      inp.style.height = Math.min(90, inp.scrollHeight) + 'px';
    });
    inp.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (!sendBtn.disabled) _send();
      }
    });
    sendBtn.addEventListener('click', _send);

    let _busy = false;
    async function _send() {
      if (_busy) return;
      const text = inp.value.trim();
      if (!text) return;
      _busy = true;
      sendBtn.disabled = true;
      inp.value = '';
      inp.style.height = 'auto';
      const userMsg = { role: 'user', text };
      history.push(userMsg);
      _renderMsg(userMsg);
      _saveHistory();

      const thinking = _renderMsg({ role: 'bot', text: 'Думаю…' });
      thinking.classList.add('thinking');

      try {
        const r = await fetch('/assistant/ask', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({ section, message: text }),
        });
        thinking.remove();
        if (!r.ok) {
          let detail = 'Помощник недоступен';
          try {
            const d = await r.json();
            if (d && d.detail) detail = String(d.detail);
          } catch (_) {}
          const errMsg = { role: 'bot', text: detail, kind: 'err' };
          _renderMsg(errMsg);
          history.push(errMsg);
          _saveHistory();
          return;
        }
        const data = await r.json();
        const botMsg = { role: 'bot', text: data.answer || '', links: data.links || [] };
        _renderMsg(botMsg);
        history.push(botMsg);
        _saveHistory();
      } catch (e) {
        thinking.remove();
        _renderMsg({ role: 'bot', text: 'Сеть недоступна. Попробуйте позже.', kind: 'err' });
      } finally {
        _busy = false;
        sendBtn.disabled = !inp.value.trim();
        inp.focus();
      }
    }
  }

  // Инициализация после загрузки DOM (включая body с data-assistant-section)
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _initAssistant);
  } else {
    _initAssistant();
  }

  // ── Mobile-banner: предложение открыть лайт-режим ────────────────────────
  // Показывается один раз на узких экранах. Юзер может закрыть → больше не
  // приходит. Не показывается на самой /mobile.html, /qr/* и /terms.html.
  function _showMobileBanner() {
    if (!document.body) return;
    var path = window.location.pathname;
    if (path === '/mobile.html' || path === '/m'
        || path.indexOf('/qr/') === 0
        || path === '/terms.html') return;
    if (window.innerWidth >= 768) return;
    try {
      if (localStorage.getItem('ai-mobile-banner-dismissed') === '1') return;
    } catch (_) {}
    if (document.getElementById('ai-mobile-banner')) return;

    var css = '#ai-mobile-banner{position:fixed;left:12px;right:12px;bottom:84px;z-index:99997;'
      + 'background:#1c1c1c;border:1px solid rgba(255,140,66,.4);border-radius:14px;padding:12px 14px;'
      + 'box-shadow:0 6px 24px rgba(0,0,0,.4);display:flex;align-items:center;gap:10px;'
      + 'font:13px/1.4 system-ui,-apple-system,sans-serif;color:#eee}'
      + '#ai-mobile-banner img{width:32px;height:32px;flex-shrink:0}'
      + '#ai-mobile-banner .b-text{flex:1;min-width:0}'
      + '#ai-mobile-banner .b-title{font-weight:700;color:#fff;font-size:13px}'
      + '#ai-mobile-banner .b-sub{color:#aaa;font-size:11px;margin-top:1px}'
      + '#ai-mobile-banner a.b-go{background:linear-gradient(135deg,#FFB300,#FF6F00);color:#fff;'
      + 'padding:7px 11px;border-radius:9px;text-decoration:none;font-weight:700;font-size:12px;flex-shrink:0}'
      + '#ai-mobile-banner button.b-x{background:transparent;border:none;color:#777;font-size:18px;cursor:pointer;padding:0 4px;line-height:1}';
    var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

    var b = document.createElement('div');
    b.id = 'ai-mobile-banner';
    b.innerHTML = '<img src="/logo-192.png" alt=""/>'
      + '<div class="b-text"><div class="b-title">Удобнее на телефоне?</div>'
      + '<div class="b-sub">Лайт-режим: лента + голос + быстрый доступ</div></div>'
      + '<a href="/mobile.html" class="b-go">Открыть</a>'
      + '<button class="b-x" type="button" aria-label="Скрыть">&times;</button>';
    document.body.appendChild(b);
    b.querySelector('.b-x').addEventListener('click', function(){
      try { localStorage.setItem('ai-mobile-banner-dismissed', '1'); } catch (_) {}
      b.remove();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _showMobileBanner);
  } else {
    _showMobileBanner();
  }
})();
