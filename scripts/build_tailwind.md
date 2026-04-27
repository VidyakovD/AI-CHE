# Локальная сборка Tailwind (полный self-host)

Сейчас все views используют **pinned CDN** `https://cdn.tailwindcss.com/3.4.0`.
Это защищает от breaking-changes (latest может выйти и сломать UI), но
**supply-chain risk остаётся** — если кто-то скомпрометирует tailwindlabs CDN,
получит инъекцию JS на нашу страницу.

## Когда мигрировать

- Когда появится node на dev-машине / CI
- Если CDN tailwindcss.com станет нестабильным
- Если нужно офлайн-использование (локальный dev без интернета)

## Шаги миграции

### 1. Установить tailwindcli
```bash
# На dev-машине (или CI)
npm install -D tailwindcss@3.4.0
```

### 2. Создать `tailwind.config.js` с актуальным конфигом

В каждом view сейчас inline-config:
```js
tailwind.config = {
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        primary: "#ff8c42", "primary-dim": "#ff6b00", "primary-soft": "#ff8c42",
        secondary: "#ffb347",
        "surface-lo": "#181510", surface: "#1e1a14",
        surface2: "#272018", surface3: "#322a1e", surface4: "#3d3325",
        "on-surface": "#f0e6d8", "on-surface-dim": "#a89880",
        outline: "#4a3f2f", error: "#ff6b6b", background: "#141210",
      },
      fontFamily: { headline: ["Manrope"], body: ["Inter"] },
    },
  },
};
```

Перенести его в `tailwind.config.js` в корне проекта:
```js
module.exports = {
  content: ["./views/**/*.html", "./views/**/*.js"],
  darkMode: "class",
  theme: {
    extend: {
      // ... тот же конфиг что выше
    },
  },
};
```

### 3. Создать `src/tailwind.css`
```css
@tailwind base;
@tailwind components;
@tailwind utilities;
```

### 4. Собрать
```bash
npx tailwindcss \
  -c tailwind.config.js \
  -i src/tailwind.css \
  -o views/static/tailwind.min.css \
  --minify
```

Размер минифицированного bundle с tree-shaking: **~30-50 КБ**
(вместо 280 КБ полного CDN или 50 КБ runtime + JIT).

### 5. Заменить в каждом view

**Было** (с inline-config):
```html
<script src="https://cdn.tailwindcss.com/3.4.0"></script>
<script>
tailwind.config = {...};
</script>
```

**Станет**:
```html
<link rel="stylesheet" href="/static/tailwind.min.css">
```

И в `main.py` смонтировать static dir:
```python
app.mount("/static", StaticFiles(directory="views/static"), name="static")
```

### 6. Удалить из CSP
В `main.py` middleware csp убрать `https://cdn.tailwindcss.com`:
```python
# БЫЛО:
"script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com ..."
# СТАЛО:
"script-src 'self' 'unsafe-inline' ..."
```

### 7. Автоматизировать в CI

Добавить в `.github/workflows/ci.yml` шаг:
```yaml
- name: Build Tailwind
  run: |
    npm install -D tailwindcss@3.4.0
    npx tailwindcss -c tailwind.config.js -i src/tailwind.css \
      -o views/static/tailwind.min.css --minify
- name: Commit if changed
  run: |
    git diff --exit-code views/static/tailwind.min.css || \
    (git config user.email "ci@aiche.ru" && \
     git config user.name "CI" && \
     git add views/static/tailwind.min.css && \
     git commit -m "chore: rebuild tailwind" && \
     git push)
```

## Проверка после миграции

- Открыть все views в браузере, убедиться что цвета `bg-primary`, шрифты не сломались
- Проверить что нет 404 на `/static/tailwind.min.css`
- Проверить bundle size через DevTools → Network → размер CSS
- Удалить `cdn.tailwindcss.com` из `main.py` CSP — убедиться что не появляются warning'и

## Почему сейчас не делаем

- На dev-машине Windows у юзера не настроен node (приоритет — фичи продукта)
- CDN с pinned-version даёт 90% защиты от breaking changes
- Оставшийся 10% supply-chain risk — известный trade-off
