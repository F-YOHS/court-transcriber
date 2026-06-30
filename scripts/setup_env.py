"""
Настройка секретов в .env.

Режимы:
  --no-password   — только генерирует JWT_SECRET, пароль не трогает (для install.bat)
  --interactive   — дополнительно спрашивает имя пользователя и пароль (для set-password.bat)

Можно и руками:
    .venv\\Scripts\\python.exe scripts\\setup_env.py --interactive
"""
from __future__ import annotations

import argparse
import re
import secrets
import sys
from getpass import getpass
from pathlib import Path

# Make Russian output safe in cmd consoles regardless of code page.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
except Exception:
    pass

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        print(f"[setup_env] {ENV_PATH} не найден, нечего настраивать.")
        sys.exit(1)
    out: dict[str, str] = {}
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def write_env_value(key: str, value: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8")
    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=).*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(rf"\1{value}", text)
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += f"{key}={value}\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def ensure_jwt_secret(env: dict[str, str]) -> None:
    if env.get("JWT_SECRET"):
        print(" * JWT_SECRET — уже задан, оставляю.")
        return
    secret = secrets.token_urlsafe(48)
    write_env_value("JWT_SECRET", secret)
    print(" * JWT_SECRET — сгенерирован.")


def ask_password() -> None:
    try:
        import bcrypt
    except ImportError:
        print(" * bcrypt не установлен, пропускаю установку пароля.")
        return

    print()
    default_user = read_env().get("AUTH_USERNAME", "mom")
    try:
        username = input(f"   Имя пользователя [{default_user}]: ").strip() or default_user
    except (EOFError, KeyboardInterrupt):
        print(); return

    while True:
        try:
            pw1 = getpass("   Пароль: ")
            if len(pw1) < 8:
                print("   Минимум 8 символов.")
                continue
            pw2 = getpass("   Повтори пароль: ")
        except (EOFError, KeyboardInterrupt):
            print(); return
        if pw1 != pw2:
            print("   Не совпадают, попробуй снова.")
            continue
        break

    # bcrypt напрямую (passlib несовместим с bcrypt 4.x+). Первые 72 байта —
    # bcrypt всё равно использует только их; режем, чтобы не словить ValueError.
    hash_ = bcrypt.hashpw(pw1.encode("utf-8")[:72], bcrypt.gensalt()).decode("ascii")
    write_env_value("AUTH_USERNAME", username)
    write_env_value("AUTH_PASSWORD_HASH", hash_)
    print()
    print(f"   Пароль установлен. Логин: {username}")


def print_hf_hint(env: dict[str, str]) -> None:
    print()
    print(" * HF_TOKEN — нужен для WhisperX (диаризация).")
    if env.get("HF_TOKEN"):
        print("   Уже задан.")
        return
    print("   Не задан. Чтобы получить:")
    print("     1) https://huggingface.co/settings/tokens — создай read-токен")
    print("     2) https://huggingface.co/pyannote/speaker-diarization-3.1 — Accept terms")
    print("     3) Вставь HF_TOKEN=hf_... в .env")
    print("   Для теста UI без GPU поставь ASR_BACKEND=mock — HF_TOKEN не нужен.")


def ask_hf_token() -> None:
    print()
    print("   Открой в браузере (если ещё не сделал):")
    print("     1) https://huggingface.co/settings/tokens  → New token, тип Read")
    print("     2) https://huggingface.co/pyannote/speaker-diarization-3.1  → Accept terms")
    print("     3) https://huggingface.co/pyannote/segmentation-3.0  → Accept terms")
    print()
    print("   Затем вставь токен ниже (начинается с hf_...).")
    print("   Чтобы пропустить — нажми Enter без ввода.")
    print()
    try:
        token = input("   HF_TOKEN: ").strip()
    except (EOFError, KeyboardInterrupt):
        print(); return
    if not token:
        print("   Пропущено.")
        return
    if not token.startswith("hf_"):
        print("   Предупреждение: токен обычно начинается с 'hf_'. Сохраняю как есть.")
    write_env_value("HF_TOKEN", token)
    print("   HF_TOKEN сохранён в .env")


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--interactive", action="store_true",
                       help="Спросить пароль для мамы и сохранить bcrypt-хэш")
    group.add_argument("--no-password", action="store_true",
                       help="Не трогать пароль, только сгенерировать JWT_SECRET")
    group.add_argument("--hf-token", action="store_true",
                       help="Запросить и сохранить HF_TOKEN")
    args = parser.parse_args()

    env = read_env()
    print("--- Настройка .env ---")
    ensure_jwt_secret(env)

    env = read_env()
    if args.hf_token:
        ask_hf_token()
    elif args.interactive:
        ask_password()
    else:
        if not env.get("AUTH_PASSWORD_HASH"):
            print(" * AUTH_PASSWORD_HASH — пока пуст (auth выключен).")
            print("   Когда захочешь публиковать наружу через public.bat — запусти set-password.bat.")
        else:
            print(" * AUTH_PASSWORD_HASH — уже задан.")
        print_hf_hint(read_env())

    print("--- Готово ---")


if __name__ == "__main__":
    main()
