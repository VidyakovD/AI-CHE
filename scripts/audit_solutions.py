"""Auto-audit всех 40 пилотов бизнес-решений.

Что делает:
  1. Логинится как админ (vidyakovd@gmail.com)
  2. Опционально пополняет баланс если < 1000 ₽
  3. Для каждого Solution с input_schema_json — генерит синтетику и запускает
  4. Polling до status in (done/failed)
  5. Сохраняет результат в audit_results/sol_{id}.json + .md
  6. В конце пишет audit_results/SUMMARY.md с оценками

Запуск:
  cd /root/AI-CHE && venv/bin/python scripts/audit_solutions.py

Пропускаем:
  - Solutions без orchestra_json И без steps (битые)
  - Solutions с file-полями в input_schema (нужны реальные uploads)
  - Solutions без input_schema И без user_prompt (legacy с непонятным input)
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Запускаем из корня проекта
ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import httpx

from server.db import SessionLocal
from server.models import Solution, User, SolutionRun
from server.billing import credit_atomic

BASE = "http://127.0.0.1:8000"
ADMIN_EMAIL = "vidyakovd@gmail.com"
ADMIN_PASSWORD = "28371988"

OUTDIR = ROOT / "audit_results"
OUTDIR.mkdir(exist_ok=True)

MAX_WAIT_SEC = 240    # 4 минуты на один пилот
POLL_INTERVAL = 5
MIN_BALANCE_KOP = 100_000   # 1000 ₽ — порог
TOPUP_KOP = 5_000_000        # +50,000 ₽


def fake_value_for_field(field: dict) -> str | dict:
    """Сгенерировать фейк-значение для одного input_schema-поля."""
    ftype = field.get("type", "text")
    name = field.get("name", "")
    label = (field.get("label") or "").lower()

    if ftype == "select":
        opts = field.get("options") or []
        if opts:
            return opts[0].get("value", "") if isinstance(opts[0], dict) else str(opts[0])
        return ""

    if ftype == "number":
        # Часто бывают metric-поля; даём правдоподобное число
        if "год" in label or "опыт" in label or "лет" in label:
            return "5"
        if "процент" in label or "%" in label or "маржа" in label:
            return "20"
        if "цен" in label or "руб" in label or "выручк" in label or "доход" in label:
            return "1000000"
        if "час" in label:
            return "8"
        if "команд" in label or "сотрудн" in label:
            return "12"
        return "10"

    # textarea / text по умолчанию: правдоподобный текст по семантике имени
    n = name.lower()
    if "product" in n or "продукт" in label or "услуг" in label or "ниш" in label:
        return ("SaaS-платформа для автоматизации продаж в B2B. "
                "Помогаем малому и среднему бизнесу управлять воронкой "
                "лидов, контактов и сделок. Подписка 5 990 ₽/мес.")
    if "audience" in n or "ца" in label or "клиент" in label or "аудитор" in label:
        return ("Малый и средний бизнес в РФ, 5-50 сотрудников. "
                "Руководители отделов продаж и операционные директора, "
                "30-45 лет. Используют Excel и Bitrix24, ищут что-то "
                "более удобное и дешёвое.")
    if "goal" in n or "цел" in label:
        return "Назначить demo-встречу"
    if "company" in n or "компан" in label or "бренд" in label:
        return "ООО «Тестовая компания»"
    if "competit" in n or "конкурент" in label:
        return "amoCRM, Bitrix24, Pipedrive"
    if "город" in label or "city" in n or "регион" in label:
        return "Москва"
    if "проблем" in label or "боль" in label or "issue" in n:
        return ("Менеджеры теряют лиды, нет единой воронки, "
                "отчёты собираются вручную в Excel.")
    if "url" in n or "сайт" in label or "ссылк" in label:
        return "https://example.com"
    if "email" in n or "почт" in label:
        return "client@example.com"
    if "ИНН" in label or "inn" in n:
        return "7707083893"   # реальный валидный (Сбер)
    if "договор" in label or "contract" in n:
        return "Договор поставки оборудования между ООО Альфа и ООО Бета на сумму 1 000 000 ₽."
    if "приоритет" in label or "топ" in label:
        return "1) Увеличить выручку на 30%. 2) Запустить новый продукт. 3) Найти CMO."
    if "должност" in label or "роль" in label or "position" in n:
        return "Founder / CEO"
    if ftype == "textarea":
        return ("Тестовое описание для аудита. Контекст: B2B SaaS-стартап в "
                "Москве, 8 сотрудников, выручка 3 млн ₽/мес, средний чек "
                "60 тыс ₽, цикл сделки 21 день. Главная боль — много холодных "
                "лидов, низкая конверсия в demo (около 8%). Хотим докрутить "
                "outbound-процесс до конверсии 15-20%.")
    # text
    return label.split("(")[0].strip().capitalize() or "Тестовое значение"


def build_input(sol: Solution) -> tuple[str, dict]:
    """Возвращает (input_str_for_api, meta_dict).
    meta_dict содержит human-readable дамп что сгенерили — для отчёта.
    """
    schema_raw = sol.input_schema_json
    if not schema_raw:
        # Legacy без input_schema — даём один длинный текст
        text = ("Контекст для аудита: B2B SaaS для малого бизнеса в РФ. "
                "Подписка 5 990 ₽/мес, текущая выручка 3 млн/мес, команда 8 человек, "
                "цикл сделки 21 день, конверсия в demo 8%. Цель — поднять конверсию "
                "до 15%, выйти на 5 млн выручки за 6 месяцев. ЦА — руководители "
                "отделов продаж в малом бизнесе, 30-45 лет.")
        return text, {"_legacy_text": text}

    try:
        schema = json.loads(schema_raw)
    except Exception:
        return "", {"_error": "schema_parse_failed"}

    if not isinstance(schema, list):
        return "", {"_error": "schema_not_list"}

    # Файловые поля — мы не можем синтетически загрузить файл; пилот пропускается
    for f in schema:
        if isinstance(f, dict) and f.get("type") == "file":
            return "", {"_skip": "has_file_field"}

    result_dict = {}
    for f in schema:
        if not isinstance(f, dict): continue
        name = f.get("name")
        if not name: continue
        result_dict[name] = fake_value_for_field(f)

    return json.dumps(result_dict, ensure_ascii=False), result_dict


def login(client: httpx.Client) -> bool:
    """Логин админа через /auth/login. Cookie сохраняется в client.cookies."""
    r = client.post("/auth/login", json={
        "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD,
    })
    if r.status_code != 200:
        print(f"[FATAL] login failed: {r.status_code} {r.text[:200]}")
        return False
    return True


def get_csrf(client: httpx.Client) -> str:
    return client.cookies.get("csrf_token") or ""


def run_solution(client: httpx.Client, sol: Solution, input_str: str) -> dict:
    """Запустить пилот и дождаться результата.

    Возвращает: {"status": "done"/"failed"/"timeout",
                 "run_id": int|None, "final_output": str,
                 "total_cost_kop": int, "duration_sec": int,
                 "error": str|None, "stages_state": dict|None}
    """
    csrf = get_csrf(client)
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    started = time.time()

    if sol.orchestra_json:
        # Orchestra: /solutions/{id}/orchestra/start
        r = client.post(f"/solutions/{sol.id}/orchestra/start",
                         json={"input": input_str}, headers=headers, timeout=30)
    else:
        # Legacy plain: /solutions/{id}/run
        r = client.post(f"/solutions/{sol.id}/run", json={}, headers=headers, timeout=30)

    if r.status_code != 200:
        return {"status": "failed", "run_id": None, "final_output": "",
                "total_cost_kop": 0, "duration_sec": 0,
                "error": f"start http {r.status_code}: {r.text[:300]}",
                "stages_state": None}

    data = r.json()
    run_id = data.get("run_id")
    status = data.get("status", "running")

    # Для legacy steps — /continue с input
    if not sol.orchestra_json and status == "waiting_input":
        r2 = client.post(f"/solutions/runs/{run_id}/continue",
                         json={"message_text": input_str}, headers=headers, timeout=120)
        if r2.status_code != 200:
            return {"status": "failed", "run_id": run_id, "final_output": "",
                    "total_cost_kop": 0, "duration_sec": int(time.time() - started),
                    "error": f"continue http {r2.status_code}: {r2.text[:300]}",
                    "stages_state": None}
        # Может вернуть сразу done
        cd = r2.json()
        if cd.get("status") == "done":
            with SessionLocal() as db:
                run = db.query(SolutionRun).filter_by(id=run_id).first()
                return {
                    "status": "done", "run_id": run_id,
                    "final_output": (run.final_output or "")[:8000],
                    "total_cost_kop": int(run.total_cost_kop or 0),
                    "duration_sec": int(time.time() - started),
                    "error": None,
                    "stages_state": None,
                }

    # Polling
    while True:
        if time.time() - started > MAX_WAIT_SEC:
            return {"status": "timeout", "run_id": run_id, "final_output": "",
                    "total_cost_kop": 0, "duration_sec": int(time.time() - started),
                    "error": "timeout > 240s", "stages_state": None}
        time.sleep(POLL_INTERVAL)
        with SessionLocal() as db:
            run = db.query(SolutionRun).filter_by(id=run_id).first()
            if not run:
                return {"status": "failed", "run_id": run_id, "final_output": "",
                        "total_cost_kop": 0, "duration_sec": int(time.time() - started),
                        "error": "run row disappeared", "stages_state": None}
            if run.status in ("done", "failed"):
                stages = None
                try:
                    stages = json.loads(run.stages_state) if run.stages_state else None
                except Exception:
                    pass
                return {
                    "status": run.status, "run_id": run_id,
                    "final_output": (run.final_output or "")[:8000],
                    "total_cost_kop": int(run.total_cost_kop or 0),
                    "duration_sec": int(time.time() - started),
                    "error": run.error_msg if hasattr(run, "error_msg") else None,
                    "stages_state": stages,
                }


def quality_score(out: str) -> tuple[int, list[str]]:
    """Базовая оценка output. Возвращает (score 0-100, flags)."""
    flags = []
    score = 100
    n = len(out or "")
    if n < 200:
        flags.append(f"очень короткий ответ ({n} симв)")
        score -= 50
    elif n < 600:
        flags.append(f"короткий ответ ({n} симв)")
        score -= 20

    lower = (out or "").lower()
    refusal_markers = [
        "не могу", "не имею возможности", "я не имею доступа",
        "извините, но я", "к сожалению, у меня нет",
        "к сожалению, я не", "i cannot", "i can't",
    ]
    for m in refusal_markers:
        if m in lower:
            flags.append(f"refusal-маркер: «{m}»")
            score -= 30
            break

    if out.strip().endswith(("...", "…")):
        flags.append("обрыв на многоточии")
        score -= 10

    # Нет ни одного заголовка/списка/таблицы — плоский текст
    if "##" not in out and "**" not in out and "—" not in out and "1." not in out:
        flags.append("нет markdown-структуры (плоский текст)")
        score -= 10

    return max(0, score), flags


def main():
    print("=" * 60)
    print("AUDIT SOLUTIONS — синтетический прогон всех пилотов")
    print("=" * 60)

    # Топ-ап баланса админа если нужно
    with SessionLocal() as db:
        admin = db.query(User).filter_by(email=ADMIN_EMAIL).first()
        if not admin:
            print(f"[FATAL] Админ {ADMIN_EMAIL} не найден")
            return 1
        bal = int(admin.tokens_balance or 0)
        print(f"Админ баланс: {bal/100:.2f} ₽")
        if bal < MIN_BALANCE_KOP:
            credit_atomic(db, admin.id, TOPUP_KOP)
            db.commit()
            admin = db.query(User).filter_by(id=admin.id).first()
            print(f"Пополнено: +{TOPUP_KOP/100:.0f} ₽ → новый баланс {admin.tokens_balance/100:.2f} ₽")

        sols = (db.query(Solution)
                  .filter(Solution.is_active == True)
                  .order_by(Solution.id).all())
        sols_meta = [(s.id, s.title, bool(s.orchestra_json), s.input_schema_json,
                       int(s.price_tokens or 0))
                      for s in sols]

    print(f"Найдено активных пилотов: {len(sols_meta)}")

    client = httpx.Client(base_url=BASE, timeout=httpx.Timeout(120),
                          headers={"User-Agent": "audit-bot/1.0"})
    if not login(client):
        return 1

    all_results = []
    skipped = []
    start_total = time.time()

    for idx, (sid, title, has_orch, schema, price) in enumerate(sols_meta, 1):
        print(f"\n[{idx}/{len(sols_meta)}] Solution #{sid}: {title!r:.60}  "
              f"(orch={has_orch}, schema={bool(schema)}, price={price/100:.0f}₽)")

        # Достаём фактический solution из БД (для свежих полей)
        with SessionLocal() as db:
            sol = db.query(Solution).filter_by(id=sid).first()
            if not sol:
                continue

        input_str, meta = build_input(sol)
        if meta.get("_skip") == "has_file_field":
            print("  → пропуск (есть file-поле)")
            skipped.append({"id": sid, "title": title, "reason": "has_file_field"})
            continue
        if not input_str:
            print(f"  → пропуск (input не сгенерён: {meta.get('_error')})")
            skipped.append({"id": sid, "title": title,
                            "reason": meta.get("_error", "no_input")})
            continue

        try:
            res = run_solution(client, sol, input_str)
        except Exception as e:
            print(f"  ✗ exception: {e}")
            res = {"status": "failed", "run_id": None, "final_output": "",
                   "total_cost_kop": 0, "duration_sec": 0,
                   "error": str(e), "stages_state": None}

        out = res.get("final_output") or ""
        score, flags = quality_score(out)
        print(f"  → {res['status']} · {res['duration_sec']}s · "
              f"{res['total_cost_kop']/100:.2f} ₽ · "
              f"{len(out)} симв · score={score}")
        if flags:
            print(f"    flags: {', '.join(flags)}")

        entry = {
            "id": sid, "title": title,
            "is_orchestra": has_orch,
            "has_schema": bool(schema),
            "price_tokens": price,
            "input_meta": meta,
            "status": res["status"],
            "duration_sec": res["duration_sec"],
            "total_cost_kop": res["total_cost_kop"],
            "final_output_len": len(out),
            "score": score,
            "flags": flags,
            "error": res.get("error"),
        }
        all_results.append(entry)

        # JSON-дамп
        (OUTDIR / f"sol_{sid:03d}.json").write_text(
            json.dumps({**entry, "final_output": out,
                        "stages_state": res.get("stages_state")},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # MD-дамп (human-readable)
        md = [f"# Solution #{sid}: {title}",
              "",
              f"- **status**: {res['status']}",
              f"- **duration**: {res['duration_sec']}s",
              f"- **cost**: {res['total_cost_kop']/100:.2f} ₽",
              f"- **output length**: {len(out)} симв",
              f"- **score**: {score}/100",
              f"- **flags**: {', '.join(flags) if flags else 'OK'}",
              "",
              "## Input (synthetic)",
              "```json",
              json.dumps(meta, ensure_ascii=False, indent=2),
              "```",
              "",
              "## Final output",
              "",
              out if out else "_(пусто)_",
              ]
        (OUTDIR / f"sol_{sid:03d}.md").write_text("\n".join(md), encoding="utf-8")

    elapsed = int(time.time() - start_total)
    total_cost = sum(r["total_cost_kop"] for r in all_results)
    n_done = sum(1 for r in all_results if r["status"] == "done")
    n_failed = sum(1 for r in all_results if r["status"] in ("failed", "timeout"))
    avg_score = (sum(r["score"] for r in all_results) / len(all_results)) if all_results else 0

    # SUMMARY.md
    summary = [
        "# Solutions audit — SUMMARY",
        "",
        f"_Generated: {datetime.utcnow().isoformat()}Z_",
        "",
        f"- Всего активных пилотов: {len(sols_meta)}",
        f"- Запущено: {len(all_results)}",
        f"- Пропущено: {len(skipped)}",
        f"- ✅ done: {n_done}",
        f"- ❌ failed/timeout: {n_failed}",
        f"- Средний score: {avg_score:.1f}/100",
        f"- Суммарная стоимость: {total_cost/100:.2f} ₽",
        f"- Время прогона: {elapsed}s ({elapsed//60}m {elapsed%60}s)",
        "",
        "## 🚨 Топ проблемных (score < 60 или failed)",
        "",
    ]
    problems = [r for r in all_results
                 if r["score"] < 60 or r["status"] in ("failed", "timeout")]
    problems.sort(key=lambda r: r["score"])
    for r in problems:
        flags_s = ", ".join(r["flags"]) if r["flags"] else "—"
        summary.append(f"- **#{r['id']} {r['title']}** · score={r['score']} · "
                        f"{r['status']} · {r['final_output_len']} симв · {flags_s}")
        if r.get("error"):
            summary.append(f"  - error: `{r['error'][:200]}`")
    if not problems:
        summary.append("_(нет проблемных)_")

    summary += [
        "",
        "## ✅ Все done (score ≥ 60)",
        "",
    ]
    okays = [r for r in all_results if r["score"] >= 60 and r["status"] == "done"]
    okays.sort(key=lambda r: -r["score"])
    for r in okays:
        summary.append(f"- #{r['id']} {r['title']} · score={r['score']} · "
                        f"{r['final_output_len']} симв · {r['total_cost_kop']/100:.2f} ₽")

    if skipped:
        summary += ["", "## ⏸ Пропущенные", ""]
        for s in skipped:
            summary.append(f"- #{s['id']} {s['title']} — {s['reason']}")

    (OUTDIR / "SUMMARY.md").write_text("\n".join(summary), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"AUDIT DONE: {n_done}✅ {n_failed}❌ avg={avg_score:.0f}/100 "
          f"cost={total_cost/100:.0f}₽ time={elapsed}s")
    print(f"Results: {OUTDIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
