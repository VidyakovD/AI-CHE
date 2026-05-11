# Frontend build (Tailwind)

## Что и зачем

До: каждый HTML подключал `<script src="https://cdn.tailwindcss.com/3.4.0">` —
CDN-script ~100 КБ запускает JIT в браузере, парсит все классы из inline-HTML,
генерирует CSS «на лету». Это удобно для прототипа, но в проде:
- блокирует first paint (~400ms на 4G);
- зависимость от cdn.tailwindcss.com;
- нет древо-shaking — даже неиспользуемые классы загружаются.

После: компилим `views/styles.css` через tailwindcss CLI, отдаём как
обычную статику. Размер: ~100-120 КБ minified gzipped, кэш 10 мин.

## Когда запускать build

При изменении:
- любых классов в `views/*.html` или `views/*.js`;
- `tailwind.config.js` (палитра, шрифты, safelist);
- `views/input.css` (custom @layer components).

**НЕ** перед каждым деплоем. `styles.css` коммитим в репо, прод просто
делает `git pull` и подхватывает свежий CSS без npm.

## Команды

```bash
# Один прогон, минифицировать
npm run build:css

# Watch-mode для разработки
npm run watch:css

# Чистый билд без скриптов
npx tailwindcss -i ./views/input.css -o ./views/styles.css --minify
```

## Постепенный переход с CDN

`<script src="cdn.tailwindcss.com">` пока ОСТАЁТСЯ в HTML параллельно с
`<link rel="stylesheet" href="/styles.css"/>`. Это даёт нам два слоя:
- styles.css загружается первым (быстро);
- CDN-script JIT добавляет недостающие классы которые не попали в build
  (динамические template strings, etc).

После визуального тестирования (≥ 1 неделя на проде без проблем) — убираем
CDN-script и оставляем только `/styles.css`.

## Что в `safelist`

Tailwind не видит классы которые формируются динамически в JS:
```js
el.className = `bg-${color}-500`;  // ← не попадёт в build без safelist
```

В `tailwind.config.js` есть `safelist` с паттернами для:
- цветовых классов (`bg-primary-dim`, `text-on-surface-dim`, `border-error`);
- сетки/отступов с responsive-variants (sm/md/lg).

Если после переключения с CDN на styles.css что-то пропало стилистически —
скорее всего недостающий класс. Решение: расширить `safelist` в `tailwind.config.js`,
перебилдить.

## Файлы

- `tailwind.config.js` — палитра + content paths + safelist
- `views/input.css` — `@tailwind base/components/utilities` + custom layers
- `views/styles.css` — сгенерированный (коммитим в репо)
- `package.json` — only devDependency: `tailwindcss@^3.4.0`
- `package-lock.json` — коммитим для reproducible builds
