"""OAuth Mercado Livre — refresh de access_token."""

from __future__ import annotations

import time
from pathlib import Path

import requests

from wago.config import ROOT, env, require_env

BASE_URL = "https://api.mercadolibre.com"
SLEEP_BETWEEN_CALLS = 0.5

_access_token: str | None = None


def get_access_token() -> str:
    global _access_token
    if _access_token:
        return _access_token
    existing = env("ML_ACCESS_TOKEN")
    if existing:
        _access_token = existing
        return existing
    token = refresh_ml_token()
    if not token:
        raise SystemExit("Não foi possível obter access_token do Mercado Livre.")
    return token


def refresh_ml_token() -> str | None:
    global _access_token
    require_env("ML_CLIENT_ID", "ML_CLIENT_SECRET", "ML_REFRESH_TOKEN")

    data = {
        "grant_type": "refresh_token",
        "client_id": env("ML_CLIENT_ID"),
        "client_secret": env("ML_CLIENT_SECRET"),
        "refresh_token": env("ML_REFRESH_TOKEN"),
    }
    try:
        print("Renovando access_token ML...")
        response = requests.post(
            f"{BASE_URL}/oauth/token", data=data, timeout=30
        )
        if response.status_code != 200:
            print(f"Erro refresh: {response.status_code} {response.text[:300]}")
            return None

        token_data = response.json()
        _access_token = token_data.get("access_token")
        new_refresh = token_data.get("refresh_token") or env("ML_REFRESH_TOKEN")
        _atualizar_env(_access_token or "", new_refresh or "")
        print("Access token renovado.")
        return _access_token
    except Exception as exc:
        print(f"Erro ao renovar token: {exc}")
        return None


def _atualizar_env(access_token: str, refresh_token: str) -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    lines = env_file.read_text(encoding="utf-8").splitlines(keepends=True)
    new_lines: list[str] = []
    seen_access = seen_refresh = False
    for line in lines:
        if line.startswith("ML_ACCESS_TOKEN="):
            new_lines.append(f"ML_ACCESS_TOKEN={access_token}\n")
            seen_access = True
        elif line.startswith("ML_REFRESH_TOKEN="):
            new_lines.append(f"ML_REFRESH_TOKEN={refresh_token}\n")
            seen_refresh = True
        else:
            new_lines.append(line if line.endswith("\n") else line + "\n")
    if not seen_access:
        new_lines.append(f"ML_ACCESS_TOKEN={access_token}\n")
    if not seen_refresh:
        new_lines.append(f"ML_REFRESH_TOKEN={refresh_token}\n")
    env_file.write_text("".join(new_lines), encoding="utf-8")


def api_call(endpoint: str, max_retries: int = 5) -> dict | list | None:
    global _access_token
    url = f"{BASE_URL}{endpoint}"
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    for attempt in range(max_retries):
        try:
            time.sleep(SLEEP_BETWEEN_CALLS)
            response = requests.get(url, headers=headers, timeout=30)

            if response.status_code == 401:
                print("Token expirado, renovando...")
                new_token = refresh_ml_token()
                if new_token:
                    headers["Authorization"] = f"Bearer {new_token}"
                    continue
                return None

            if response.status_code in (429, 500, 502, 503, 504):
                wait = 30
                print(
                    f"Erro {response.status_code} em {endpoint}. "
                    f"Aguardando {wait}s... ({attempt + 1}/{max_retries})"
                )
                time.sleep(wait)
                continue

            if response.status_code == 200:
                return response.json()

            print(
                f"Erro {response.status_code} em {endpoint}: "
                f"{response.text[:200]}"
            )
            return None
        except Exception as exc:
            print(
                f"Erro na chamada {endpoint}: {exc}. "
                f"Aguardando 30s... ({attempt + 1}/{max_retries})"
            )
            time.sleep(30)
    return None
