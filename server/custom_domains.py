"""Кастомные домены для сайтов (CNAME + Let's Encrypt automation).

Архитектура:
  1. Юзер привязывает свой `example.com` → создаётся SiteCustomDomain с
     verification_token (24 hex символа).
  2. Прописывает у регистратора:
       CNAME @            → aiche.ru
       TXT _aiche-verify  → <verification_token>
  3. /verify endpoint → проверяет TXT-запись через dnspython.
  4. После verified: SSL issue через certbot + nginx-config + reload.

Ограничения по умолчанию:
  - Macc 5 доменов на юзера (custom_domains.max_per_user в pricing_config)
  - Whitelist символов: a-z, 0-9, точка, дефис (RFC 1123)
  - Запрещены sub-домены aiche.ru / *.aiche.ru
  - Cron-задача обновления сертификатов (раз в неделю) — см. cron.maintenance

certbot и nginx-reload требуют sudo. На проде сервис ai-che работает от root
(systemd Unit с User=root). Если бы был не-root — потребовался бы sudoers
NOPASSWD: /usr/bin/certbot, /bin/systemctl reload nginx.
"""
from __future__ import annotations

import logging
import re
import secrets
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# ── Validation ───────────────────────────────────────────────────────────────

# Полное доменное имя: каждая label 1-63 chars, латиница/цифры/дефис, не начинается/
# заканчивается дефисом. Минимум 2 label (a.b), TLD ≥ 2 chars.
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}$"
)

# Запрещённые домены: наш собственный или его поддомены — иначе юзер мог бы
# затереть/перехватить trafic основного приложения.
_BLOCKED_DOMAINS = {"aiche.ru", "www.aiche.ru", "localhost"}
_BLOCKED_SUFFIXES = (".aiche.ru",)

# Запрещённые префиксы (sub-домены) которые могут конфликтовать с инфрой
_BLOCKED_PREFIXES = ("www.aiche.", "_acme-challenge.")


def normalize_domain(domain: str) -> str:
    """Нормализация: lowercase, strip, без trailing dot, без http://."""
    if not domain:
        return ""
    d = domain.strip().lower()
    # Снять схему если юзер вставил URL
    if d.startswith(("http://", "https://")):
        d = d.split("://", 1)[1]
    # Снять path/query
    d = d.split("/", 1)[0].split("?", 1)[0]
    # Снять trailing dot (FQDN form)
    d = d.rstrip(".")
    return d


def validate_domain(domain: str) -> str | None:
    """Возвращает ошибку или None если домен корректен."""
    if not domain:
        return "Домен не указан"
    if not _DOMAIN_RE.match(domain):
        return ("Некорректный формат домена. Используйте только латиницу, "
                "цифры, точки и дефисы (например, example.com).")
    if domain in _BLOCKED_DOMAINS:
        return "Этот домен использовать нельзя."
    for sfx in _BLOCKED_SUFFIXES:
        if domain.endswith(sfx):
            return "Поддомены aiche.ru недоступны для custom-привязки."
    for pfx in _BLOCKED_PREFIXES:
        if domain.startswith(pfx):
            return "Этот префикс зарезервирован."
    return None


def generate_verification_token() -> str:
    """24 hex-символа — достаточно энтропии (96 bit), коротко для TXT-записи."""
    return secrets.token_hex(12)


# ── DNS verification ─────────────────────────────────────────────────────────

def check_verification_txt(domain: str, expected_token: str) -> tuple[bool, str]:
    """Проверяет TXT-запись _aiche-verify.<domain> = expected_token.

    Returns: (ok, reason). reason пуст если ok.
    """
    if not expected_token:
        return False, "Нет verification_token"
    target_host = f"_aiche-verify.{domain}"
    try:
        import dns.resolver  # type: ignore
    except ImportError:
        # Fallback: subprocess `dig`. Если и того нет — fail.
        log.warning("[custom-domains] dnspython не установлен, fallback на `dig`")
        return _check_txt_via_dig(target_host, expected_token)
    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = 5.0
        # Используем 8.8.8.8 и 1.1.1.1 — гарантированно публичные, не наш локальный
        resolver.nameservers = ["8.8.8.8", "1.1.1.1"]
        answers = resolver.resolve(target_host, "TXT")
    except Exception as e:
        return False, f"TXT-запись не найдена ({type(e).__name__}). Подождите распространения DNS (5-30 мин)."
    found_values: list[str] = []
    for rdata in answers:
        # rdata.strings → list[bytes]. Объединяем (некоторые провайдеры дробят
        # на 255-char chunks при длинных значениях).
        try:
            value = b"".join(rdata.strings).decode("utf-8", errors="ignore").strip()
        except Exception:
            value = str(rdata).strip('"').strip()
        found_values.append(value)
        if value == expected_token:
            return True, ""
    return False, (
        f"TXT-запись {target_host} найдена, но значение не совпадает. "
        f"Ожидалось: {expected_token}, получено: {', '.join(found_values[:3])}"
    )


def _check_txt_via_dig(host: str, expected: str) -> tuple[bool, str]:
    """Fallback через subprocess dig."""
    try:
        out = subprocess.check_output(
            ["dig", "+short", "TXT", host, "@8.8.8.8"],
            timeout=10,
        ).decode("utf-8", errors="ignore")
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        return False, f"DNS-резолв не удался: {type(e).__name__}"
    values = [line.strip().strip('"') for line in out.splitlines() if line.strip()]
    for v in values:
        if v == expected:
            return True, ""
    return False, f"TXT {host} не содержит ожидаемое значение."


def check_cname_target(domain: str, expected_target: str = "aiche.ru") -> tuple[bool, str]:
    """Проверяет что A-резолв (или CNAME chain) ведёт на aiche.ru.

    Может быть прямой CNAME или A-record совпадающий с aiche.ru IP. Главное —
    юзер реально направил трафик к нам.
    """
    try:
        import dns.resolver  # type: ignore
        resolver = dns.resolver.Resolver()
        resolver.lifetime = 5.0
        resolver.nameservers = ["8.8.8.8", "1.1.1.1"]
        # Получим A-записи нашего домена и custom-домена; должны совпасть.
        target_ips = {str(r) for r in resolver.resolve(expected_target, "A")}
        custom_ips = {str(r) for r in resolver.resolve(domain, "A")}
        if target_ips & custom_ips:
            return True, ""
        return False, (
            f"A-записи {domain} ({', '.join(custom_ips) or 'нет'}) не совпадают "
            f"с aiche.ru ({', '.join(target_ips)}). Пропишите CNAME @ → aiche.ru."
        )
    except ImportError:
        return True, ""  # без dnspython не проверяем — допускаем
    except Exception as e:
        return False, f"DNS-резолв A-записи не удался ({type(e).__name__})"


# ── SSL via certbot ──────────────────────────────────────────────────────────

CERTBOT_BIN = "/usr/bin/certbot"
WEBROOT_PATH = "/var/www/aiche/.well-known"
CERTBOT_EMAIL = "aiche@aiche.ru"  # для уведомлений Let's Encrypt
NGINX_CUSTOM_DIR = "/etc/nginx/conf.d/custom"
NGINX_RELOAD_CMD = ["/bin/systemctl", "reload", "nginx"]


def issue_ssl_certificate(domain: str) -> tuple[bool, str]:
    """Запускает certbot certonly --webroot для домена.

    Должен вызываться ТОЛЬКО после DNS verification + actual трафик идёт на нас.
    Иначе certbot не сможет дёрнуть .well-known/acme-challenge на example.com.

    Returns: (ok, message).
    """
    if not Path(CERTBOT_BIN).exists():
        return False, f"certbot не установлен ({CERTBOT_BIN})"
    cmd = [
        CERTBOT_BIN, "certonly", "--webroot",
        "--webroot-path", WEBROOT_PATH,
        "--non-interactive", "--agree-tos",
        "--email", CERTBOT_EMAIL,
        "-d", domain,
        "--keep-until-expiring",
    ]
    log.info(f"[custom-domains] certbot start: {shlex.join(cmd)}")
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=180)
        return True, out.decode("utf-8", errors="ignore")[-2000:]
    except subprocess.CalledProcessError as e:
        return False, f"certbot exit={e.returncode}: {e.output.decode('utf-8', errors='ignore')[-2000:]}"
    except subprocess.TimeoutExpired:
        return False, "certbot timeout (>180 sec) — попробуйте позже"
    except Exception as e:
        return False, f"certbot failed: {type(e).__name__}: {e}"


def write_nginx_config(domain: str, public_token: str) -> tuple[bool, str]:
    """Создаёт server-block в /etc/nginx/conf.d/custom/<domain>.conf.

    Конфиг проксирует на наше hosted приложение через локальный путь
    /sites/hosted/<public_token>/. SSL-сертификаты ожидаются в
    /etc/letsencrypt/live/<domain>/.

    Returns: (ok, config_path_or_error).
    """
    try:
        Path(NGINX_CUSTOM_DIR).mkdir(parents=True, exist_ok=True)
    except PermissionError:
        return False, f"Нет прав на запись в {NGINX_CUSTOM_DIR}"
    config_path = Path(NGINX_CUSTOM_DIR) / f"{domain}.conf"
    cert_dir = f"/etc/letsencrypt/live/{domain}"
    if not Path(cert_dir).exists():
        return False, f"Сертификаты {cert_dir} не найдены — запустите issue_ssl_certificate"
    content = f"""# Auto-generated by AI Студия Че custom-domains
# Site: hosted/{public_token}/  Domain: {domain}

server {{
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name {domain};

    ssl_certificate     {cert_dir}/fullchain.pem;
    ssl_certificate_key {cert_dir}/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # Проксируем на наше FastAPI приложение, переписав путь на hosted-сайт.
    location / {{
        proxy_pass http://127.0.0.1:8000/sites/hosted/{public_token}$request_uri;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host {domain};
    }}

    # ACME challenge — для renewal certbot. Должен быть accessible на 80 тоже.
    location /.well-known/acme-challenge/ {{
        root {WEBROOT_PATH.rsplit('/', 1)[0]};
    }}
}}

server {{
    listen 80;
    listen [::]:80;
    server_name {domain};

    location /.well-known/acme-challenge/ {{
        root {WEBROOT_PATH.rsplit('/', 1)[0]};
    }}

    location / {{
        return 301 https://$host$request_uri;
    }}
}}
"""
    try:
        config_path.write_text(content, encoding="utf-8")
    except Exception as e:
        return False, f"Не удалось записать config: {e}"
    return True, str(config_path)


def reload_nginx() -> tuple[bool, str]:
    """systemctl reload nginx с предварительным nginx -t."""
    try:
        # Проверка синтаксиса прежде reload — иначе можем уронить весь nginx.
        subprocess.check_output(["/usr/sbin/nginx", "-t"],
                                stderr=subprocess.STDOUT, timeout=10)
    except subprocess.CalledProcessError as e:
        return False, f"nginx -t failed: {e.output.decode('utf-8', errors='ignore')[-500:]}"
    except Exception as e:
        return False, f"nginx -t error: {type(e).__name__}"
    try:
        subprocess.check_output(NGINX_RELOAD_CMD, stderr=subprocess.STDOUT, timeout=15)
        return True, "reloaded"
    except subprocess.CalledProcessError as e:
        return False, f"reload failed: {e.output.decode('utf-8', errors='ignore')[-500:]}"
    except Exception as e:
        return False, f"reload error: {type(e).__name__}"


def remove_custom_domain(domain: str) -> tuple[bool, str]:
    """Удаляет nginx-config + certbot delete + reload.

    Безопасно для одного домена — не трогает другие.
    """
    errors: list[str] = []
    config_path = Path(NGINX_CUSTOM_DIR) / f"{domain}.conf"
    if config_path.exists():
        try:
            config_path.unlink()
        except Exception as e:
            errors.append(f"unlink config: {e}")
    # certbot delete (best-effort; нет домена = nothing to do)
    if Path(CERTBOT_BIN).exists() and Path(f"/etc/letsencrypt/live/{domain}").exists():
        try:
            subprocess.check_output(
                [CERTBOT_BIN, "delete", "--cert-name", domain, "--non-interactive"],
                stderr=subprocess.STDOUT, timeout=60,
            )
        except Exception as e:
            errors.append(f"certbot delete: {type(e).__name__}")
    ok, msg = reload_nginx()
    if not ok:
        errors.append(f"nginx reload: {msg}")
    if errors:
        return False, "; ".join(errors)
    return True, "removed"


# ── Full activation pipeline ─────────────────────────────────────────────────

def activate_domain(domain: str, public_token: str) -> tuple[bool, str]:
    """Полная активация после успешной DNS verification:
    issue cert → write nginx config → reload nginx.

    Атомарность: при любой ошибке возвращаем failure без частичной записи
    (cert может остаться issued, но nginx config удаляем).
    """
    # 1. Сертификат
    ok, msg = issue_ssl_certificate(domain)
    if not ok:
        return False, f"SSL issue: {msg}"
    # 2. Nginx config
    ok, config_path_or_err = write_nginx_config(domain, public_token)
    if not ok:
        return False, f"nginx config: {config_path_or_err}"
    # 3. Reload
    ok, reload_msg = reload_nginx()
    if not ok:
        # Откатываем config чтобы следующий reload не сломал nginx
        try:
            Path(config_path_or_err).unlink()
        except Exception:
            pass
        return False, f"nginx reload: {reload_msg}"
    return True, "activated"
