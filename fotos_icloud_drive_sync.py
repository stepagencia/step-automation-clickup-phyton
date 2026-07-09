"""
STEP - Automação diária de fotos (iCloud -> Google Drive)
=========================================================

Todo dia (cron), este script:

1. Lê as tarefas ATIVAS da lista ClickUp "Gestão de Especialista"
   (LIST_GESTAO). De cada tarefa pega dois custom fields:
     - "Álbum Compartilhado"        (CF_ALBUM)  -> link público do iCloud
     - "Link drive pasta fotos/vídeos" (CF_DRIVE) -> pasta do cliente no Drive
   Clientes sem álbum são ignorados.

2. Acessa o álbum público do iCloud pela API webstream
   (POST .../sharedstreams/webstream e .../webasseturls). O host correto
   (pXX-sharedstreams) vem do redirect HTTP 330. Só considera FOTOS
   (ignora vídeos).

3. Compara os GUIDs das fotos com o estado salvo em
   state/fotos-processadas.json.
     - Cliente ainda não visto (ou BASELINE=1)  -> BASELINE: registra todos
       os GUIDs como processados SEM baixar nada.
     - Cliente já conhecido -> processa só as fotos novas, no máximo
       MAX_POR_CLIENTE por execução.

4. Baixa cada foto nova na maior resolução disponível.

5. Classifica a foto com a API da Anthropic (Claude Haiku), enviando uma
   cópia reduzida a 768px só para análise. Lógica em dois passos + exceções
   (ver CLASSIFY_PROMPT). Resultado é uma das categorias em CATEGORIAS.

6. Sobe o arquivo ORIGINAL (resolução cheia) na pasta do cliente no Drive,
   dentro da subpasta da categoria (cria se não existir). Nome do arquivo:
   AAAA-MM-DD_categoria_NN.jpg (data da foto).

7. Atualiza a planilha "Registro de Fotos" (uma linha no topo por cliente
   processado: Data | Cliente | Qtd por categoria | Link da pasta). A
   planilha é criada no primeiro run, dentro da pasta "Automação Fotos".

8. Sem foto nova -> encerra em silêncio. Um erro em um cliente NÃO derruba
   os outros: loga e segue.

Variáveis de ambiente:
    CLICKUP_API_TOKEN            (obrigatório)  token pk_... do ClickUp
    ANTHROPIC_API_KEY           (necessário fora do baseline) chave sk-ant-...
    GOOGLE_SERVICE_ACCOUNT_JSON (necessário fora do baseline) JSON da service account
    DRIVE_AUTOMACAO_FOLDER_ID   (opcional) id de uma pasta já compartilhada
                                com a service account, onde a planilha e a
                                pasta "Automação Fotos" são criadas.
    BASELINE                    (opcional) "1"/"true" força baseline em TODOS
                                os clientes (não baixa nem classifica nada).
    DRY_RUN                     (opcional) "1"/"true" não escreve em lugar
                                nenhum (nem Drive, nem Sheets, nem estado).

Rodar localmente:
    export CLICKUP_API_TOKEN="pk_..."
    export BASELINE=1
    python fotos_icloud_drive_sync.py
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

# ---------------------------------------------------------------------------
# Configuração — IDs do workspace da STEP
# ---------------------------------------------------------------------------

WORKSPACE_ID = "9013038195"

# Lista "Gestão de Especialista"
LIST_GESTAO = "901301376959"

# Custom fields da lista
CF_ALBUM = "bd001c8c-c733-4e5e-9eab-af990002bf8f"   # Álbum Compartilhado
CF_DRIVE = "5d543419-70f9-44da-8dd9-36bdc9aa4b0b"   # Link drive pasta fotos/vídeos

# Estado persistido no próprio repositório
STATE_PATH = os.path.join("state", "fotos-processadas.json")

# Limite de fotos baixadas por cliente por execução (fora do baseline)
MAX_POR_CLIENTE = 40

# Modelo de visão da Anthropic (Haiku) e versão da API
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_VERSION = "2023-06-01"

# Tamanho (maior lado) da cópia enviada para classificação
ANALISE_MAX_PX = 768

# Nome da planilha e da pasta-raiz da automação
SHEET_TITULO = "Registro de Fotos"
AUTOMACAO_FOLDER_NAME = "Automação Fotos"
SHEET_HEADER = ["Data", "Cliente", "Qtd por categoria", "Link da pasta"]

# Escopos do Google
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Categorias -> (slug usado no nome do arquivo, caminho de subpastas no Drive)
CATEGORIAS: dict[str, dict[str, Any]] = {
    "sozinha":           {"slug": "sozinha",      "path": ["sozinha"]},
    "sozinha/lifestyle": {"slug": "lifestyle",    "path": ["sozinha", "lifestyle"]},
    "familia":           {"slug": "familia",      "path": ["familia"]},
    "detalhes":          {"slug": "detalhes",     "path": ["detalhes"]},
    "eventos":           {"slug": "eventos",      "path": ["eventos"]},
    "trabalho":          {"slug": "trabalho",     "path": ["trabalho"]},
    "a-classificar":     {"slug": "a-classificar", "path": ["a-classificar"]},
}
CATEGORIA_FALLBACK = "a-classificar"

CLASSIFY_PROMPT = (
    "Você classifica UMA foto do álbum pessoal de uma cliente (mulher). "
    "Responda APENAS com um JSON no formato {\"categoria\": \"...\"}, sem texto extra.\n\n"
    "Escolha exatamente UMA categoria seguindo esta lógica:\n\n"
    "EXCEÇÕES (têm prioridade sobre tudo):\n"
    "- Palco, palestra, congresso, evento com plateia -> \"eventos\".\n"
    "- Consultório, bastidor de gravação, setup de trabalho, cenário "
    "profissional -> \"trabalho\".\n\n"
    "PASSO A — quem aparece:\n"
    "- Apenas UMA pessoa (a cliente sozinha) -> vá para o PASSO B.\n"
    "- DUAS ou mais pessoas -> \"familia\".\n"
    "- NENHUMA pessoa (objeto, paisagem, comida, detalhe) -> \"detalhes\".\n\n"
    "PASSO B — só quando é uma pessoa sozinha:\n"
    "- Retrato / pose / foto posada -> \"sozinha\".\n"
    "- Cotidiano/lifestyle (café, viagem, academia, hobby, rua) -> "
    "\"sozinha/lifestyle\".\n\n"
    "Se ficar em dúvida entre categorias, responda \"a-classificar\".\n"
    "Categorias válidas: sozinha, sozinha/lifestyle, familia, detalhes, "
    "eventos, trabalho, a-classificar."
)

log = logging.getLogger("fotos_icloud_drive_sync")


# ---------------------------------------------------------------------------
# ClickUp
# ---------------------------------------------------------------------------

CLICKUP_API_BASE = "https://api.clickup.com/api/v2"


class ClickUp:
    """Wrapper mínimo sobre a API do ClickUp, com retry em 429."""

    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": token,
            "Content-Type": "application/json",
        })

    def _req(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(3):
            resp = self.session.request(
                method, f"{CLICKUP_API_BASE}{path}", timeout=30, **kwargs)
            if resp.status_code == 429:
                wait = 2 ** attempt
                log.warning("ClickUp rate limit, aguardando %ss", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                log.error("ClickUp %s %s -> %s: %s",
                          method, path, resp.status_code, resp.text[:400])
                resp.raise_for_status()
            return resp.json() if resp.text else None
        resp.raise_for_status()

    def list_active_tasks(self, list_id: str) -> list[dict]:
        """Tarefas ATIVAS da lista: não arquivadas e não fechadas."""
        out: list[dict] = []
        page = 0
        while True:
            data = self._req(
                "GET", f"/list/{list_id}/task",
                params={
                    "archived": "false",
                    "include_closed": "false",
                    "subtasks": "false",
                    "page": page,
                },
            )
            tasks = data.get("tasks", []) if data else []
            out.extend(tasks)
            if data and data.get("last_page"):
                break
            if len(tasks) < 100:
                break
            page += 1
        return out


def cf_value(task: dict, field_id: str) -> Any:
    for cf in task.get("custom_fields", []):
        if cf.get("id") == field_id:
            return cf.get("value")
    return None


# ---------------------------------------------------------------------------
# iCloud — álbum público compartilhado (webstream)
# ---------------------------------------------------------------------------

ICLOUD_START_HOSTS = [
    "p42-sharedstreams.icloud.com",
    "p23-sharedstreams.icloud.com",
    "p52-sharedstreams.icloud.com",
]


def extract_icloud_token(raw: str) -> Optional[str]:
    """Extrai o token do álbum a partir do link colado no ClickUp.

    Aceita links como https://www.icloud.com/sharedalbum/#B0Xyz... ou o
    token cru. O token é a parte depois do '#'.
    """
    if not raw:
        return None
    raw = raw.strip()
    if "#" in raw:
        raw = raw.split("#")[-1]
    raw = raw.strip().strip("/")
    # tokens do iCloud são alfanuméricos (letras/números), começam com letra
    m = re.search(r"([A-Za-z0-9]{10,})", raw)
    return m.group(1) if m else None


class ICloudAlbum:
    """Lê a lista de fotos de um álbum público do iCloud."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "text/plain",
            "Origin": "https://www.icloud.com",
            "Referer": "https://www.icloud.com/",
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                           "Version/16.0 Safari/605.1.15"),
        })
        self.host: Optional[str] = None
        self._photos: Optional[list[dict]] = None

    def _url(self, host: str, endpoint: str) -> str:
        return (f"https://{host}/{self.token}/sharedstreams/{endpoint}")

    def _fetch(self) -> list[dict]:
        """Busca o webstream UMA vez: segue o redirect 330 para achar o host
        correto e já devolve a lista de fotos dessa mesma resposta. Em caso
        de timeout, tenta de novo no MESMO host com backoff (algumas
        partições do iCloud, como p118, respondem devagar)."""
        body = json.dumps({"streamCtag": None})
        host = ICLOUD_START_HOSTS[0]
        tried_starts = 0
        last_exc: Optional[Exception] = None

        for attempt in range(8):
            try:
                resp = self.session.post(
                    self._url(host, "webstream"),
                    data=body, timeout=90, allow_redirects=False)
            except requests.exceptions.ReadTimeout as exc:
                # host certo, só lento: espera e tenta de novo no mesmo host
                last_exc = exc
                time.sleep(3 * (attempt + 1))
                continue
            except requests.RequestException as exc:
                # host inicial não respondeu: tenta o próximo da lista
                last_exc = exc
                tried_starts += 1
                if tried_starts < len(ICLOUD_START_HOSTS):
                    host = ICLOUD_START_HOSTS[tried_starts]
                    continue
                break

            if resp.status_code == 330:
                new_host = resp.json().get("X-Apple-MMe-Host")
                if new_host and new_host != host:
                    host = new_host
                    continue
                # sem host novo: usa o atual e tenta listar
            if resp.status_code == 200:
                self.host = host
                return resp.json().get("photos", [])
            last_exc = RuntimeError(
                f"iCloud webstream {resp.status_code}: {resp.text[:200]}")
            break

        raise RuntimeError(f"Não consegui ler o álbum do iCloud: {last_exc}")

    def list_photos(self) -> list[dict]:
        """Lista os itens FOTO (ignora vídeos). Faz cache do resultado."""
        if self._photos is None:
            raw = self._fetch()
            self._photos = [
                p for p in raw
                if str(p.get("mediaAssetType", "")).lower() != "video"
            ]
        return self._photos

    def asset_urls(self, guids: list[str]) -> dict[str, str]:
        """Mapa checksum -> URL de download, para os GUIDs pedidos."""
        urls: dict[str, str] = {}
        # a API aceita lotes; mantemos lotes de 25 por segurança
        for i in range(0, len(guids), 25):
            lote = guids[i:i + 25]
            body = json.dumps({"photoGuids": lote})
            resp = self.session.post(self._url(self.host, "webasseturls"),
                                     data=body, timeout=60)
            resp.raise_for_status()
            items = resp.json().get("items", {})
            for checksum, info in items.items():
                loc = info.get("url_location")
                path = info.get("url_path")
                if loc and path:
                    urls[checksum] = f"https://{loc}{path}"
        return urls

    @staticmethod
    def best_derivative(photo: dict) -> Optional[dict]:
        """Escolhe a maior derivada (mais pixels) de uma foto."""
        best = None
        best_area = -1
        for _, d in (photo.get("derivatives") or {}).items():
            try:
                w = int(d.get("width") or 0)
                h = int(d.get("height") or 0)
            except (TypeError, ValueError):
                w = h = 0
            area = w * h
            if area > best_area and d.get("checksum"):
                best = d
                best_area = area
        return best

    def download(self, url: str) -> bytes:
        resp = self.session.get(url, timeout=120)
        resp.raise_for_status()
        return resp.content


def photo_date(photo: dict) -> str:
    """Data da foto em AAAA-MM-DD a partir do dateCreated do iCloud."""
    raw = photo.get("dateCreated") or photo.get("batchDateCreated")
    if raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Classificação (Anthropic)
# ---------------------------------------------------------------------------

class Classifier:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        })

    @staticmethod
    def _thumb_b64(image_bytes: bytes) -> str:
        """Reduz a imagem para no máx ANALISE_MAX_PX no maior lado (JPEG)."""
        from PIL import Image  # import tardio: só precisa fora do baseline
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("RGB")
        w, h = img.size
        escala = min(1.0, ANALISE_MAX_PX / float(max(w, h)))
        if escala < 1.0:
            img = img.resize((max(1, int(w * escala)), max(1, int(h * escala))))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def classify(self, image_bytes: bytes) -> str:
        try:
            b64 = self._thumb_b64(image_bytes)
        except Exception as exc:  # noqa: BLE001
            log.warning("Falha ao gerar thumbnail p/ análise: %s", exc)
            return CATEGORIA_FALLBACK

        payload = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 60,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64,
                    }},
                    {"type": "text", "text": CLASSIFY_PROMPT},
                ],
            }],
        }
        try:
            resp = self.session.post(
                "https://api.anthropic.com/v1/messages",
                json=payload, timeout=90)
            if resp.status_code >= 400:
                log.error("Anthropic %s: %s", resp.status_code, resp.text[:300])
                return CATEGORIA_FALLBACK
            data = resp.json()
            text = "".join(
                blk.get("text", "") for blk in data.get("content", [])
                if blk.get("type") == "text")
        except requests.RequestException as exc:
            log.warning("Erro na chamada Anthropic: %s", exc)
            return CATEGORIA_FALLBACK

        return self._parse(text)

    @staticmethod
    def _parse(text: str) -> str:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                cat = json.loads(m.group(0)).get("categoria", "")
                cat = str(cat).strip().lower()
                if cat in CATEGORIAS:
                    return cat
            except json.JSONDecodeError:
                pass
        # fallback: procura o nome de uma categoria no texto cru
        low = text.lower()
        if "sozinha/lifestyle" in low or "lifestyle" in low:
            return "sozinha/lifestyle"
        for cat in CATEGORIAS:
            if cat in low:
                return cat
        return CATEGORIA_FALLBACK


# ---------------------------------------------------------------------------
# Google Drive + Sheets (service account)
# ---------------------------------------------------------------------------

DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


class Google:
    def __init__(self, sa_json: str) -> None:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=GOOGLE_SCOPES)
        # Opção 2 (domain-wide delegation): se GOOGLE_IMPERSONATE_SUBJECT
        # estiver definido, a service account age COMO esse usuário
        # (ex.: nathalia@stepagenciamkt.com), subindo arquivos na conta e na
        # cota dele — assim as pastas podem continuar no "Meu Drive".
        subject = os.environ.get("GOOGLE_IMPERSONATE_SUBJECT", "").strip()
        if subject:
            creds = creds.with_subject(subject)
        self.session = AuthorizedSession(creds)
        self.sa_email = info.get("client_email", "?")

    # ----- Drive -----

    def _drive_get(self, params: dict) -> dict:
        params = {**params,
                  "supportsAllDrives": "true",
                  "includeItemsFromAllDrives": "true"}
        resp = self.session.get(DRIVE_FILES, params=params, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def find_child(self, parent_id: str, name: str,
                   mime: Optional[str] = None) -> Optional[str]:
        safe = name.replace("'", "\\'")
        q = (f"name = '{safe}' and '{parent_id}' in parents "
             f"and trashed = false")
        if mime:
            q += f" and mimeType = '{mime}'"
        data = self._drive_get({
            "q": q, "fields": "files(id,name)", "corpora": "allDrives",
            "pageSize": 10,
        })
        files = data.get("files", [])
        return files[0]["id"] if files else None

    def create_folder(self, parent_id: str, name: str) -> str:
        meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        resp = self.session.post(
            DRIVE_FILES, params={"supportsAllDrives": "true",
                                 "fields": "id"},
            json=meta, timeout=60)
        resp.raise_for_status()
        return resp.json()["id"]

    def ensure_folder(self, parent_id: str, name: str) -> str:
        return (self.find_child(parent_id, name, FOLDER_MIME)
                or self.create_folder(parent_id, name))

    def ensure_path(self, root_id: str, segments: list[str]) -> str:
        cur = root_id
        for seg in segments:
            cur = self.ensure_folder(cur, seg)
        return cur

    def count_prefix(self, folder_id: str, prefix: str) -> int:
        safe = prefix.replace("'", "\\'")
        data = self._drive_get({
            "q": f"'{folder_id}' in parents and trashed = false "
                 f"and name contains '{safe}'",
            "fields": "files(id,name)", "corpora": "allDrives", "pageSize": 1000,
        })
        return sum(1 for f in data.get("files", [])
                   if f.get("name", "").startswith(prefix))

    def upload_jpeg(self, folder_id: str, name: str, data: bytes) -> str:
        meta = {"name": name, "parents": [folder_id]}
        boundary = "===============stepfotos=="
        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(meta)}\r\n"
            f"--{boundary}\r\n"
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
        resp = self.session.post(
            DRIVE_UPLOAD,
            params={"uploadType": "multipart", "supportsAllDrives": "true",
                    "fields": "id"},
            data=body,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            timeout=180)
        resp.raise_for_status()
        return resp.json()["id"]

    @staticmethod
    def folder_link(folder_id: str) -> str:
        return f"https://drive.google.com/drive/folders/{folder_id}"

    def create_spreadsheet_in(self, parent_id: str, title: str) -> str:
        meta = {"name": title, "mimeType": SHEET_MIME, "parents": [parent_id]}
        resp = self.session.post(
            DRIVE_FILES, params={"supportsAllDrives": "true", "fields": "id"},
            json=meta, timeout=60)
        resp.raise_for_status()
        return resp.json()["id"]

    # ----- Sheets -----

    def sheet_set_header(self, spreadsheet_id: str) -> None:
        self.session.put(
            f"{SHEETS_BASE}/{spreadsheet_id}/values/A1",
            params={"valueInputOption": "RAW"},
            json={"values": [SHEET_HEADER]}, timeout=60).raise_for_status()

    def sheet_insert_top(self, spreadsheet_id: str, row: list[str]) -> None:
        """Insere uma linha logo abaixo do cabeçalho (topo dos dados)."""
        self.session.post(
            f"{SHEETS_BASE}/{spreadsheet_id}:batchUpdate",
            json={"requests": [{"insertDimension": {
                "range": {"sheetId": 0, "dimension": "ROWS",
                          "startIndex": 1, "endIndex": 2},
                "inheritFromBefore": False,
            }}]}, timeout=60).raise_for_status()
        self.session.put(
            f"{SHEETS_BASE}/{spreadsheet_id}/values/A2",
            params={"valueInputOption": "USER_ENTERED"},
            json={"values": [row]}, timeout=60).raise_for_status()


# ---------------------------------------------------------------------------
# Estado
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {"clients": {}, "spreadsheet_id": None, "automacao_folder_id": None}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------------------
# Google lazy init + planilha
# ---------------------------------------------------------------------------

def ensure_spreadsheet(google: "Google", state: dict, dry_run: bool) -> Optional[str]:
    """Garante a pasta 'Automação Fotos' e a planilha. Devolve o id da
    planilha (ou None em dry_run/erro)."""
    if state.get("spreadsheet_id"):
        return state["spreadsheet_id"]
    if dry_run:
        return None
    parent = os.environ.get("DRIVE_AUTOMACAO_FOLDER_ID", "").strip()
    if state.get("automacao_folder_id"):
        base = state["automacao_folder_id"]
    elif parent:
        base = google.ensure_folder(parent, AUTOMACAO_FOLDER_NAME)
        state["automacao_folder_id"] = base
    else:
        # Sem pasta pai informada: cria "Automação Fotos" no Drive da SA.
        base = google.ensure_folder("root", AUTOMACAO_FOLDER_NAME)
        state["automacao_folder_id"] = base
    sid = google.create_spreadsheet_in(base, SHEET_TITULO)
    google.sheet_set_header(sid)
    state["spreadsheet_id"] = sid
    log.info("Planilha '%s' criada: %s", SHEET_TITULO, sid)
    return sid


# ---------------------------------------------------------------------------
# Processamento de um cliente
# ---------------------------------------------------------------------------

def process_client(task: dict, state: dict, google_holder: dict,
                   classifier_holder: dict, force_baseline: bool,
                   dry_run: bool) -> None:
    nome = task.get("name", "(sem nome)")
    task_id = task.get("id")

    album_raw = cf_value(task, CF_ALBUM)
    token = extract_icloud_token(album_raw) if album_raw else None
    if not token:
        log.info("  [%s] sem álbum — ignorado.", nome)
        return

    clients = state.setdefault("clients", {})
    entry = clients.get(task_id)
    novo_cliente = entry is None
    baseline = force_baseline or novo_cliente

    if entry is None:
        entry = {"name": nome, "token": token, "processed_guids": []}
        clients[task_id] = entry
    entry["name"] = nome
    entry["token"] = token
    processados = set(entry.get("processed_guids", []))

    # --- iCloud: lista de fotos ---
    album = ICloudAlbum(token)
    photos = album.list_photos()
    guids_atuais = [p.get("photoGuid") for p in photos if p.get("photoGuid")]

    novas = [p for p in photos if p.get("photoGuid") not in processados]

    if baseline:
        entry["processed_guids"] = sorted(set(guids_atuais) | processados)
        log.info("  [%s] BASELINE: %d fotos registradas (nada baixado).",
                 nome, len(guids_atuais))
        return

    if not novas:
        log.info("  [%s] sem foto nova.", nome)
        return

    novas = novas[:MAX_POR_CLIENTE]
    log.info("  [%s] %d foto(s) nova(s) a processar.", nome, len(novas))

    # --- Pasta do cliente no Drive ---
    drive_raw = cf_value(task, CF_DRIVE)
    folder_id = extract_drive_folder_id(drive_raw) if drive_raw else None
    if not folder_id:
        log.error("  [%s] tem fotos novas mas SEM pasta do Drive válida — "
                  "pulando cliente.", nome)
        return

    # --- Clients pesados (lazy) ---
    if dry_run:
        google = None
    else:
        google = google_holder.get("obj")
        if google is None:
            google = Google(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
            google_holder["obj"] = google
    classifier = classifier_holder.get("obj")
    if classifier is None:
        classifier = Classifier(os.environ["ANTHROPIC_API_KEY"])
        classifier_holder["obj"] = classifier

    # mapa checksum -> url para as fotos novas
    guids_novos = [p["photoGuid"] for p in novas]
    url_por_checksum = album.asset_urls(guids_novos)

    contagem: dict[str, int] = {}
    subpasta_cache: dict[str, str] = {}
    seq_por_chave: dict[str, int] = {}

    for photo in novas:
        guid = photo["photoGuid"]
        try:
            deriv = ICloudAlbum.best_derivative(photo)
            if not deriv:
                log.warning("    foto %s sem derivada válida — pulando.", guid)
                continue
            url = url_por_checksum.get(deriv["checksum"])
            if not url:
                log.warning("    foto %s sem URL de download — pulando.", guid)
                continue

            original = album.download(url)
            categoria = classifier.classify(original)
            info = CATEGORIAS.get(categoria, CATEGORIAS[CATEGORIA_FALLBACK])
            data_str = photo_date(photo)
            slug = info["slug"]

            if dry_run:
                log.info("    [DRY_RUN] %s -> %s (%s)", guid, categoria, data_str)
                contagem[categoria] = contagem.get(categoria, 0) + 1
                processados.add(guid)
                continue

            # subpasta da categoria (cria se preciso)
            chave = "/".join(info["path"])
            sub_id = subpasta_cache.get(chave)
            if sub_id is None:
                sub_id = google.ensure_path(folder_id, info["path"])
                subpasta_cache[chave] = sub_id

            # numeração NN sequencial por data+categoria
            prefixo = f"{data_str}_{slug}_"
            if chave not in seq_por_chave:
                seq_por_chave[chave] = google.count_prefix(sub_id, prefixo)
            seq_por_chave[chave] += 1
            nome_arq = f"{prefixo}{seq_por_chave[chave]:02d}.jpg"

            google.upload_jpeg(sub_id, nome_arq, original)
            log.info("    %s -> %s/%s", guid, categoria, nome_arq)

            contagem[categoria] = contagem.get(categoria, 0) + 1
            processados.add(guid)
        except Exception as exc:  # noqa: BLE001
            log.error("    Erro na foto %s de %s: %s", guid, nome, exc)

    entry["processed_guids"] = sorted(processados)

    # --- Planilha ---
    if contagem and not dry_run:
        try:
            sid = ensure_spreadsheet(google, state, dry_run)
            if sid:
                qtd = ", ".join(f"{k}:{v}" for k, v in sorted(contagem.items()))
                link = Google.folder_link(folder_id)
                hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                google.sheet_insert_top(sid, [hoje, nome, qtd, link])
        except Exception as exc:  # noqa: BLE001
            log.error("  [%s] falha ao atualizar planilha: %s", nome, exc)


def extract_drive_folder_id(raw: str) -> Optional[str]:
    if not raw:
        return None
    raw = raw.strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", raw)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", raw)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", raw):
        return raw
    return None


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    token = os.environ.get("CLICKUP_API_TOKEN")
    if not token:
        log.error("CLICKUP_API_TOKEN ausente.")
        return 1

    force_baseline = os.environ.get("BASELINE", "").lower() in {"1", "true", "yes"}
    dry_run = os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}
    if force_baseline:
        log.info("*** BASELINE: registra GUIDs sem baixar/classificar nada ***")
    if dry_run:
        log.info("*** DRY_RUN: nenhuma escrita em Drive/Sheets/estado ***")

    cu = ClickUp(token)
    state = load_state()
    google_holder: dict = {}
    classifier_holder: dict = {}

    try:
        tasks = cu.list_active_tasks(LIST_GESTAO)
    except Exception as exc:  # noqa: BLE001
        log.exception("Erro ao ler a lista do ClickUp: %s", exc)
        return 1

    log.info("Tarefas ativas em 'Gestão de Especialista': %d", len(tasks))

    for task in tasks:
        try:
            process_client(task, state, google_holder, classifier_holder,
                           force_baseline, dry_run)
        except Exception as exc:  # noqa: BLE001
            log.error("Erro no cliente '%s': %s", task.get("name"), exc)

    if not dry_run:
        save_state(state)
        log.info("Estado salvo em %s", STATE_PATH)

    log.info("Concluído.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
